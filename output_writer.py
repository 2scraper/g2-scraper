#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT — diverging from it needs a
written reason, not a per-site tweak. Ported near-verbatim from
shein-scraper's output_writer.py (CLAUDE.md §7: porting starts by copying
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

import csv
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional, Sequence

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
    """Family-common fields first, in the exact order CLAUDE.md §9 fixes
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

    # --- family-common (CLAUDE.md §9 — exact order, never reordered) ---
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
def write_json(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            writer.writerow(asdict(p))


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> None:
    if fmt == "json":
        write_json(products, out_path)
    elif fmt == "csv":
        write_csv(products, out_path)
    else:
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
    Path(meta_path_for(out_path)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty."""
    failed_pages = failed_pages or []
    partial = bool(failed_pages) and pages_completed > 0
    zero_products = len(products) == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty, and
    # identical across every 2scraper family member (CLAUDE.md §9):
    # remote_api_error > blocked > zero_products > partial > complete.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or
    # remote-API-error run into a "complete" status just because the
    # caller also passed --allow-empty, and it must never do so just
    # because SOME batches did return results while the run was, in fact,
    # blocked partway through.
    #
    # zero_products is checked before partial deliberately: a run that
    # collected no products at all is reported as "empty", even if the
    # reason was a parser exception on its only page rather than a
    # genuinely empty category — `failed_pages` still records that page,
    # and the engine's own log line for it is the diagnostic detail, same
    # as this function already treats zero_products for blocked/
    # remote_api_error runs. (A prior draft of this function swapped this
    # order and cited a sibling repo, stockx-scraper commit 00b5570, as
    # precedent for doing so; that repo's actual finish_run() checks
    # zero_products before partial, same as here — the citation did not
    # hold up, so this stays in line with CLAUDE.md §9 and every sibling.)
    if remote_api_error:
        status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
    elif blocked:
        status, exit_code = "blocked", EXIT_BLOCKED
    elif zero_products:
        status, exit_code = "empty", EXIT_ZERO_PRODUCTS
    elif partial:
        status, exit_code = "partial", EXIT_PARTIAL
    else:
        status, exit_code = "complete", EXIT_OK

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above — blocked/remote_api_error/empty
    # all zero out `products`) writes NEITHER file NOR sidecar unless the
    # caller explicitly opted in with --allow-empty. NEVER write a sidecar
    # in the not-written case — a PREVIOUS good <out>.json is left in
    # place untouched, and a stale failure sidecar sitting right beside it
    # would contradict that good data rather than describe it. The
    # engine's own logs carry the diagnostic detail — that's what a
    # failed run's output is FOR, not this file.
    if zero_products and not allow_empty:
        return exit_code

    write_output(products, out_path, fmt)
    write_meta(
        out_path, status=status, stop_reason=status, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=extra_meta,
    )
    return exit_code
