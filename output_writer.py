#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT — diverging from it needs a
written reason, not a per-site tweak. Ported near-verbatim from
shein-scraper's output_writer.py (porting starts by copying
the family-shared modules from the newest sibling and diffing what
changed on purpose) — in turn ported from lidl-scraper's, in turn from
skyscanner-scraper's, in turn from stockx-scraper's original (commit
00b5570). The exit codes,
STATUS_BY_EXIT map, and finish_run() outcome-precedence logic are
IDENTICAL to every prior family member on purpose, including the fix
from a 2026-09-15 audit that found `blocked`/`remote_api_error` were
being silently ignored whenever products were present or --allow-empty
was passed. Only the `Product` dataclass's site-specific tail (below the
family-common fields) differs per repo.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Exit codes — identical across playwright_scraper / selenium_scraper /
# puppeteer_scraper, and identical to every other 2scraper family repo. A
# caller (CI, a cron job, another program) must be able to tell these apart
# without parsing stdout.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6

# A first-page navigation failure through a proxy pool is returned as this
# (truthy) value of `blocked`, so the --block-retries loop rotates to the next
# exit exactly as it does for a block. If the LAST attempt still ended this
# way, the site never answered at all: finish_run() reports that as
# remote_api_error (exit 5), not as a site refusal (exit 3).
LOAD_FAILED = "load_failed"

STATUS_BY_EXIT = {
    EXIT_OK: "complete",
    EXIT_CRASH: "crashed",
    EXIT_BAD_USAGE: "bad_usage",
    EXIT_BLOCKED: "blocked",
    EXIT_ZERO_PRODUCTS: "empty",
    EXIT_REMOTE_API_ERROR: "remote_api_error",
    EXIT_PARTIAL: "partial",
}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """Family-common fields first, in the exact order the family output contract fixes
    (same order in JSON and CSV across every 2scraper repo — stockx-scraper,
    skyscanner-scraper, lidl-scraper, perplexity-scraper, shein-scraper, and
    this one); g2.com-specific (B2B software review marketplace) fields at
    the end.

    `sku` is the product's own URL SLUG (`hubspot-sales-hub` from
    `https://www.g2.com/products/hubspot-sales-hub/reviews`), not the
    numeric `product_id`, falling back to a deterministic fingerprint of
    `product_url` (`g2_parser.make_sku()`) only when no slug can be read —
    never a random or run-scoped id, so `diff_runs.py` sees the same `sku`
    for the same product across two different runs. The slug was chosen
    over `product_id` for one concrete reason: `product_id` is only
    available from a CATEGORY-LISTING card's `data-event-options` JSON,
    while the slug is present in the canonical URL of BOTH a listing card
    and that same product's own detail page — so a listing row and a
    detail row for the same product join on `sku` instead of silently
    landing in `added`/`removed` on every diff. `product_id` is kept as its
    own field below, unchanged, for anyone who wants G2's numeric id.

    `title` is the product name, `category` is G2's own category name for
    the listing the row came from (e.g. `"CRM"`), both confirmed real from
    the listing card's `data-event-options` JSON.

    `brand` is the VENDOR (the company behind the product), and is
    currently always `None`: the listing card's `data-event-options`
    carries a numeric `vendor_id` but no vendor NAME, and no vendor-name
    field has been confirmed on the product page either. Left empty rather
    than filled with the product name, which would be a different thing
    wearing the same column. `vendor_id` below carries what IS confirmed.

    `price` / `currency` / `price_source` are the columns `diff_runs.py`
    and every cross-site tool key off. They are ALSO empty for a plain
    listing or detail row: unlike every prior family member, g2.com is a
    review/ratings marketplace and neither its category cards nor its
    product pages carry a price at all. Pricing lives on a separate
    `/products/{slug}/pricing` page as unstructured rendered text (no
    JSON-LD), parsed best-effort by `g2_parser.parse_pricing_page()` — a
    caller that fetched one can fill these three in via
    `g2_parser.apply_pricing_tiers()`, which sets `price` to the LOWEST
    tier price, `currency` from the tier's own symbol, and `price_source`
    to `"pricing_page_text"` so a consumer can tell a best-effort
    text-scraped price apart from a structured one at a glance.

    **The two rating scales are deliberately two separate fields and must
    never be averaged, converted, or folded into one `rating` column.**
    G2 publishes both, on different pages, on different scales:

      - `rating_5` — the 0-to-5 star rating shown on a CATEGORY-LISTING
        card (`.elv-star-wrapper`, e.g. `"4.4/5"`). Listing pages only.
      - `rating_10` — G2's own internal 0-to-10 composite score, from the
        PRODUCT page's `SoftwareApplication` JSON-LD `aggregateRating`
        (`bestRating: 10, worstRating: 0`, e.g. `8.9`). Product pages
        only. A listing row leaves this `None` and vice versa; a row with
        both came from a merge the caller did on purpose.

    Note that each individual embedded review's own `reviewRating.
    ratingValue` on the product page is back on the 0-5 scale — a third
    place the two scales can be confused, which is why neither field is
    just called `rating`.

    `review_count` comes from whichever page produced the row, and
    `review_count_source` says which: `"listing_card"` (the card's
    `.elv-star-wrapper__desc__count`) or `"json_ld"` (the product page's
    `aggregateRating.reviewCount`). These two genuinely disagree by small
    amounts for the same product at the same moment — 14,319 vs 14,333 for
    HubSpot Sales Hub, captured minutes apart in the same session — which
    is a cache/render-time difference on G2's side, NOT a parser bug. The
    source column is here so nobody has to relitigate that.
    """

    # --- family-common (exact order, never reordered) ---
    sku: Optional[str]
    source: str
    category: Optional[str]
    title: Optional[str]
    brand: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    price_source: Optional[str]
    product_url: Optional[str]
    image_url: Optional[str]
    scraped_at: str

    # --- g2.com-specific (B2B software review marketplace) ---
    # 0-5 star rating, CATEGORY-LISTING pages only. See class docstring.
    rating_5: Optional[float] = None
    # 0-10 G2 composite score, PRODUCT pages only. See class docstring.
    rating_10: Optional[float] = None
    review_count: Optional[int] = None
    # "listing_card" | "json_ld" — which page's count this is.
    review_count_source: Optional[str] = None
    # G2's own numeric product id, from a listing card's data-event-options.
    product_id: Optional[int] = None
    product_uuid: Optional[str] = None
    # The URL slug — also the basis of `sku`; kept as its own column so a
    # consumer doesn't have to know that.
    product_slug: Optional[str] = None
    vendor_id: Optional[int] = None
    category_id: Optional[int] = None
    # G2's own listing-card label, e.g. "Software". Listing pages only.
    product_type: Optional[str] = None


PRODUCT_FIELD_NAMES: List[str] = [f.name for f in fields(Product)]


# --------------------------------------------------------------------------- #
# Dedupe / merge — "merge in page order, not arrival order": the caller
# passes pages already sorted by page number (or, for a concurrent run,
# sorted before calling this), never in whichever-finished-first order.
# --------------------------------------------------------------------------- #
def sku_key(p: Product) -> str:
    """The identity `merge_pages` dedupes on, and the same one an engine's
    scroll/pagination loop should track to decide "did this page add
    anything NEW" — never just "is this page non-empty" (a page repeating
    an already-seen item, e.g. past the real last batch of results, isn't
    empty but isn't new either)."""
    return p.sku or f"__no_sku__:{p.product_url}"


def merge_pages(pages: Sequence[Sequence[Product]]) -> List[Product]:
    seen: dict = {}
    order: List[str] = []
    for page in pages:
        for p in page:
            key = sku_key(p)
            if key not in seen:
                order.append(key)
            seen[key] = p  # last write for a given sku wins, in page order
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write to a temporary file beside `path`, then rename over it.

    `open(path, "w")` truncates before a single byte is written, so a
    crash, a kill, a full disk or a Ctrl-C halfway through `json.dump`
    leaves a SHORTER file where a complete one was -- the previous good run
    destroyed by the attempt to replace it. Measured here before this
    change: a 496-byte good output came back as a 10-byte fragment after a
    write failure injected part-way through `json.dump`.

    * The temp file goes in the target's OWN directory: `os.replace` is
      atomic only within one filesystem.
    * `fsync` before the rename makes the bytes durable, not just visible.
    * `newline` is passed through because the csv module needs
      `newline=""`.
    * Mode: `NamedTemporaryFile` creates at 0600 and `os.replace` keeps
      that, so the final mode is set explicitly -- an existing target keeps
      its own mode (someone may have tightened it on purpose), and a new
      one gets exactly what `open()` would have given it, `0o666 & ~umask`.
      Never a flat 0644, which narrows 0664 under umask 002.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        # Including KeyboardInterrupt: an interrupted run must not leave
        # `out.json.ab12cd.tmp` behind beside the good file.
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# A spreadsheet treats a cell beginning with one of these as a FORMULA
# rather than as text, and every string in these rows was written by
# whoever listed the product on g2.com (a product name, a category name).
# The tab and the two newlines are here because some readers strip a
# leading one and then execute whatever followed it.
CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")
# Excel, Google Sheets and LibreOffice read a leading apostrophe as "the
# rest of this cell is text". A consumer parsing the file with the `csv`
# module DOES see it, which is why the count goes into the sidecar.
CSV_FORMULA_ESCAPE = "'"
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v


def _csv_escape(v: Any) -> Tuple[Any, bool]:
    """Neutralise a formula-shaped cell. Returns (value, was_escaped).

    Only STRINGS are touched: escaping a number would turn `-5` into text
    and break every sum over the column. CSV only -- the JSON output keeps
    the site's bytes exactly as served, and `csv_cells_escaped` in the
    sidecar declares the difference between the two files.
    """
    if isinstance(v, str) and v.startswith(CSV_FORMULA_LEADS):
        return CSV_FORMULA_ESCAPE + v, True
    return v, False


def write_json(products: Sequence[Product], out_path: str) -> None:
    with _atomic(out_path) as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> int:
    """Returns how many cells were formula-escaped."""
    escaped = 0
    with _atomic(out_path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows -- a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            row = {}
            for k, v in asdict(p).items():
                # AFTER the list/dict serialiser: the joined cell is text
                # too, and can start with a formula character.
                value, was_escaped = _csv_escape(_csv_value(v))
                escaped += was_escaped
                row[k] = value
            writer.writerow(row)
    return escaped


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> int:
    """Writes the rows; returns the number of formula-escaped CSV cells
    (always 0 for JSON)."""
    if fmt == "json":
        write_json(products, out_path)
        return 0
    if fmt == "csv":
        return write_csv(products, out_path)
    raise ValueError(f"Unsupported format: {fmt!r} (expected 'json' or 'csv')")


# --------------------------------------------------------------------------- #
# Run metadata sidecar
# --------------------------------------------------------------------------- #
def meta_path_for(out_path: str) -> str:
    return f"{out_path}.meta.json"


def write_meta(
    out_path: str,
    *,
    status: str,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    product_count: int,
    price_confirmed_pct: Optional[float] = None,
    started_at: float,
    finished_at: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    meta = {
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages or [],
        "product_count": product_count,
        "price_confirmed_pct": price_confirmed_pct,
        "started_at": started_at,
        "finished_at": finished_at or time.time(),
    }
    if extra:
        meta.update(extra)
    with _atomic(meta_path_for(out_path)) as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))


ATTEMPT_META_SUFFIX = ".latest_attempt.meta.json"


def write_failed_attempt(
    out_path: str,
    *,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    started_at: float,
    transport_error: str,
) -> str:
    """Record a run that ended before finish_run() could, e.g. because the
    proxy gateway refused the pre-check. Writes ONLY
    `<out>.latest_attempt.meta.json`: `<out>` and `<out>.meta.json` from an
    earlier good run stay as they were, so the two never contradict."""
    meta = {
        "status": "failed",
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": 0,
        "failed_pages": [],
        "product_count": 0,
        "started_at": started_at,
        "finished_at": time.time(),
        "transport_error": transport_error,
        "data_updated": False,
    }
    path = f"{out_path}{ATTEMPT_META_SUFFIX}"
    with _atomic(path) as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))
    return path


def precheck_or_stop(
    proxy_url: Optional[str],
    *,
    args: Any,
    start_url: str,
    engine: str,
    pages_requested: int,
    started_at: float,
    env_var: str,
    site: str,
    note: Optional[str] = None,
) -> Optional[int]:
    """The proxy pre-check every browser engine runs before launching:
    one HEAD for the start URL's robots.txt through `proxy_url`. Returns
    EXIT_REMOTE_API_ERROR after a gateway 407/402 (message printed, attempt
    sidecar written), or None to carry on — any other outcome, a transport
    error included, is not a verdict on the proxy."""
    import proxy_pool
    try:
        proxy_pool.precheck_proxy(proxy_url, proxy_pool.robots_url(start_url), timeout=20)
    except proxy_pool.ProxyPrecheckError as exc:
        print(proxy_pool.precheck_message(exc, env_var, site), file=sys.stderr)
        if note:
            print(f"    {note}", file=sys.stderr)
        write_failed_attempt(
            args.out, stop_reason=proxy_pool.PROXY_PRECHECK_STOP_REASON, engine=engine,
            url=start_url, pages_requested=pages_requested, started_at=started_at,
            transport_error=f"proxy pre-check: {exc}",
        )
        return EXIT_REMOTE_API_ERROR
    return None


# --------------------------------------------------------------------------- #
# finish_run — the single place every engine calls to decide exit code,
# whether to write output at all, and whether to write a sidecar. Keeping
# this in one shared function is what stops the three engines' exit-code
# mapping from drifting apart.
# --------------------------------------------------------------------------- #
def finish_run(
    *,
    products: List[Product],
    out_path: str,
    fmt: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]],
    blocked: bool,
    remote_api_error: bool,
    allow_empty: bool,
    started_at: float,
    price_confirmed_pct: Optional[float] = None,
    extra_meta: Optional[dict] = None,
    stop_reason: Optional[str] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty.

    The mapping, decided once here so the three engines cannot drift:

      no rows   remote_api_error         -> remote_api_error, exit 5
                blocked                  -> blocked,          exit 3
                failed_pages, no block   -> parser_found_nothing, exit 5
                                            (a page WAS served or attempted
                                            and nothing usable came out of it:
                                            a parser exception, an unreadable
                                            document, a product page with no
                                            product data. Reporting that as
                                            "empty" would send the reader to
                                            check the URL instead of the
                                            parser.)
                otherwise                -> empty,            exit 4
      rows      any of the above failed  -> partial,          exit 6
                (blocked or a remote error AFTER rows were gathered is
                partial data, not a block: the rows are written and the
                sidecar says which pages are missing)
                otherwise                -> complete,         exit 0

    `--allow-empty` controls only whether a zero-row result gets WRITTEN;
    it never changes the status.
    """
    if blocked == LOAD_FAILED:
        blocked, remote_api_error = False, True
    failed_pages = failed_pages or []
    zero_products = len(products) == 0

    if zero_products:
        if remote_api_error:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        elif blocked:
            status, exit_code = "blocked", EXIT_BLOCKED
        elif failed_pages:
            status, exit_code = "parser_found_nothing", EXIT_REMOTE_API_ERROR
        else:
            status, exit_code = "empty", EXIT_ZERO_PRODUCTS
        derived_reason = status
    elif remote_api_error or blocked or failed_pages:
        status, exit_code = "partial", EXIT_PARTIAL
        derived_reason = (
            "remote_api_error" if remote_api_error
            else "blocked" if blocked
            else "pages_failed"
        )
    else:
        status, exit_code = "complete", EXIT_OK
        derived_reason = "complete"
    stop_reason = stop_reason or derived_reason

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome writes NEITHER file NOR sidecar unless the caller explicitly
    # opted in with --allow-empty. A previous good <out>.json is left in
    # place untouched, and a stale failure sidecar beside it would
    # contradict that good data.
    if zero_products and not allow_empty:
        return exit_code

    escaped = write_output(products, out_path, fmt)
    # Housekeeping goes UNDER the caller's extra: a key collision must never
    # silently drop site data the caller chose to record.
    meta_extra = {"csv_cells_escaped": escaped} if fmt == "csv" else {}
    meta_extra.update(extra_meta or {})
    write_meta(
        out_path, status=status, stop_reason=stop_reason, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=meta_extra,
    )
    if escaped:
        print(f"note: {escaped} CSV cell(s) began with a formula character and were "
              f"prefixed with {CSV_FORMULA_ESCAPE!r}; the JSON output is unchanged "
              f"(csv_cells_escaped in the sidecar).", file=sys.stderr)
    return exit_code
