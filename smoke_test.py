#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline synthetic-fixture
checks. No pytest, no conftest, no engine driver required: this runs, and
must pass, in an environment with none of playwright/selenium/pyppeteer
installed (CLAUDE.md §6), which is exactly what CI's offline job is.

**Honesty note, read before trusting a green run** (the same caveat every
family member's smoke_test.py carries). Every HTML fixture below is
SYNTHETIC — hand-written to exercise the parsing code paths — but built
from the CONFIRMED real field names, class names and JSON-LD shapes
captured live from www.g2.com on 2026-09-22 and transcribed in
`g2_parser.py`'s module docstring, not from invented ones. A green run
here proves the architecture (exit codes, precedence, dedupe, credential
redaction, CLI validation, robots.txt compliance, engines importing with
no driver present) AND that the confirmed data shapes parse correctly. It
does NOT prove that this repo's own engine scripts get the same treatment
against the live site: **no engine in this repo has ever run against
g2.com** — the build environment's egress policy blocks the host, and both
the live captures behind g2_parser.py came from a browser-rendering tool,
not from `playwright_scraper.py`. That last step is still open.

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import captcha_solver
import diff_runs
import env_config
import g2_parser as gp
import output_writer
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent
ENGINE_FILES = ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py")
ENGINE_MODULES = (playwright_scraper, selenium_scraper, puppeteer_scraper)

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _parse(mod, argv):
    return mod.build_arg_parser().parse_args(argv)


# --------------------------------------------------------------------------- #
# Synthetic fixtures, built from g2_parser.py's CONFIRMED captured shapes
# --------------------------------------------------------------------------- #
def _card(slug: str, name: str, product_id: int, rating: str = "4.4", count: str = "27,555") -> str:
    """One `.category-product-card`, with the confirmed `data-event-options`
    JSON blob, `.elv-star-wrapper` rating text and image."""
    opts = json.dumps({
        "product_id": product_id,
        "product_uuid": f"16e299ae-0000-0000-0000-{product_id:012d}",
        "product": name,
        "vendor_id": 469,
        "product_type": "Software",
        "category": "CRM",
        "category_id": 179,
        "resource_type": "Category",
        "resource_id": 179,
        "list_type": None,
        "is_onboarding": False,
        "name": "Event::Products::ListItemClicked",
    })
    return (
        '<div class="content-card category-product-card x-category-product-card">'
        f"<a data-event-options='{opts}' href=\"/products/{slug}/reviews\">{name}</a>"
        f'<div class="elv-star-wrapper">{rating}/5 '
        f'<span class="elv-star-wrapper__desc__count">({count})</span></div>'
        f'<img src="https://images.g2crowd.com/uploads/product/image/{slug}.png">'
        "</div>"
    )


def _pagination(next_disabled: bool = False, current: int = 1) -> str:
    next_classes = "pagination__component" + (" pagination__component--disabled" if next_disabled else "")
    return (
        '<ul class="pagination" aria-label="Pagination">'
        f'<li class="pagination__component pagination__page-number pagination__page-number--current">{current}</li>'
        '<li class="pagination__component pagination__page-number">2</li>'
        f'<li class="{next_classes}">Next</li>'
        "</ul>"
    )


def listing_html(*, cards: int = 3, next_disabled: bool = False, datadome: bool = True) -> str:
    """A healthy category-listing page. DataDome's own tag is included by
    default because it is on EVERY g2.com page, healthy ones very much
    included — that fact is load-bearing for several checks below."""
    body = "".join(
        _card(f"product-{i}", f"Product {i}", 500 + i) for i in range(1, cards + 1)
    )
    dd = '<script>window.DataDomeJsTag = {};window.dataDomeOptions={endpoint:"https://dd.g2.com/js/"};</script>' if datadome else ""
    return (
        "<html><head><title>Best CRM Software in 2026 | G2</title>"
        f"{dd}</head><body>{body}{_pagination(next_disabled)}</body></html>"
    )


def product_html(*, best_rating: int = 10, rating_value: float = 8.9, datadome: bool = True) -> str:
    """A product `/reviews` page: the one useful `SoftwareApplication`
    JSON-LD block, plus the `BreadcrumbList` block that sits beside it and
    carries no product data."""
    node = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "HubSpot Sales Hub",
        "url": "https://www.g2.com/products/hubspot-sales-hub/reviews",
        "image": "https://images.g2crowd.com/uploads/product/image/hubspot.png",
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": rating_value,
            "bestRating": best_rating,
            "worstRating": 0,
            "reviewCount": 14333,
        },
        "review": [{
            "@type": "Review",
            "author": {"@type": "Person", "name": "Excel M."},
            "name": "Great for pipeline visibility",
            "reviewBody": "Works well for our team.",
            "datePublished": "2026-08-01",
            "reviewRating": {"@type": "Rating", "ratingValue": 4.5, "bestRating": 5},
        }],
    }
    breadcrumb = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []}
    dd = '<script>window.DataDomeJsTag = {};</script>' if datadome else ""
    return (
        "<html><head><title>HubSpot Sales Hub Reviews | G2</title>"
        f'<script type="application/ld+json">{json.dumps(breadcrumb)}</script>'
        f'<script type="application/ld+json">{json.dumps(node)}</script>'
        f"{dd}</head><body>reviews</body></html>"
    )


PRICING_HTML = (
    "<html><head><title>HubSpot Sales Hub Pricing 2026 | G2</title></head><body>"
    "<p>HubSpot Sales Hub offers 4 pricing editions, starting from $0 to $150.</p>"
    "<div>Free HubSpot CRM — $0</div>"
    "<div>Sales Hub Starter — $20 / 1 Core Seat Per Month</div>"
    "<div>Sales Hub Professional — $100 / 1 Sales Seat Per Month</div>"
    "<div>Sales Hub Enterprise — $150 / 1 Sales Seat Per Month</div>"
    "<p>*Pricing information is supplied by the software provider or retrieved from publicly "
    "accessible pricing materials.</p>"
    "</body></html>"
)

# A DataDome wall: the vendor's own tag, and none of the page's real
# content. This is what `identify_unsupported_vendor()` exists for — there
# is no widget and no sitekey to extract, because DataDome has neither.
DATADOME_WALL_HTML = (
    "<html><head><title>g2.com</title></head><body>"
    '<script src="https://dd.g2.com/js/" type="text/javascript"></script>'
    "<script>window.DataDomeJsTag = {};</script>"
    "<p>Please enable JS and disable any ad blocker</p>"
    "</body></html>"
)

# DataDome's own interstitial SLIDER challenge — the vendor-documented
# shape (https://2captcha.com/api-docs/datadome-slider-captcha), added
# 2026-09-22. Unlike DATADOME_WALL_HTML above, this one has the iframe a
# real challenge presents; entity-encoded `&amp;` in the query string
# matches how a browser's `page.content()`/`driver.page_source` would
# actually render the attribute.
DATADOME_SLIDER_WALL_HTML = (
    "<html><head><title>g2.com</title></head><body>"
    '<script src="https://dd.g2.com/js/" type="text/javascript"></script>'
    "<script>window.DataDomeJsTag = {};</script>"
    '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=abc123'
    '&amp;hash=deadbeef&amp;cid=xyz789&amp;t=fe&amp;referer=https%3A%2F%2Fwww.g2.com%2Fcategories%2Fcrm"'
    ' height="600" width="100%"></iframe>'
    "</body></html>"
)

# The OTHER confirmed DataDome shape — `/interstitial/`, not `/captcha/` —
# added 2026-09-22 after Roman's first real `--cdp-endpoint` + real-key run
# against live g2.com actually hit it (see captcha_solver.py's module
# docstring for the full story). Synthetic, mirroring
# DATADOME_SLIDER_WALL_HTML's own style; the real scrubbed capture this is
# modeled on lives at tests/fixtures/g2_datadome_interstitial.html and gets
# its own check below.
DATADOME_INTERSTITIAL_WALL_HTML = (
    "<html><head><title>g2.com</title></head><body>"
    '<script src="https://dd.g2.com/js/" type="text/javascript"></script>'
    "<script>window.DataDomeJsTag = {};</script>"
    '<iframe src="https://geo.captcha-delivery.com/interstitial/?initialCid=abc123'
    '&amp;hash=deadbeef&amp;cid=xyz789&amp;referer=https%3A%2F%2Fwww.g2.com%2Fcategories%2Fcrm'
    '&amp;s=48726&amp;e=deadbeef&amp;b=1648239&amp;dm=cd"'
    ' title="DataDome Device Check" height="600" width="100%"></iframe>'
    "</body></html>"
)


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in ENGINE_MODULES:
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError")
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "except ImportError as _IMPORT_ERROR" in src, f"{path}: missing guarded driver import"


@check(
    "BEHAVIORAL proof of CLAUDE.md §6, not just the structural grep above: with "
    "playwright/selenium/pyppeteer made unimportable (a meta_path blocker in a subprocess), all "
    "three engine modules still import, and each reports its own driver as absent rather than "
    "raising. This is the check the structural one can only approximate — and it is the one that "
    "matters in an environment where the drivers ARE installed, where a missing guard would "
    "otherwise go unnoticed until a user without them ran the suite."
)
def _():
    script = """
import importlib.abc, sys
BLOCKED = {"playwright", "selenium", "pyppeteer"}

class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError("blocked by smoke_test: " + fullname)
        return None

for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]
sys.meta_path.insert(0, _Blocker())

import playwright_scraper, selenium_scraper, puppeteer_scraper
assert playwright_scraper.async_playwright is None, "playwright guard did not fire"
assert selenium_scraper.webdriver is None, "selenium guard did not fire"
assert puppeteer_scraper.pyppeteer_launch is None, "pyppeteer guard did not fire"
assert playwright_scraper.build_arg_parser() is not None
assert selenium_scraper.build_arg_parser() is not None
assert puppeteer_scraper.build_arg_parser() is not None
print("NO_DRIVER_IMPORT_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT),
        capture_output=True, text=True, timeout=120,
    )
    assert "NO_DRIVER_IMPORT_OK" in proc.stdout, (
        f"engines did not import with their drivers blocked:\n{proc.stdout}\n{proc.stderr}"
    )


@check("no forbidden overclaiming wording in any shipped .py/.md/.html/.toml/.cfg/.yml file (CLAUDE.md §12)")
def _():
    banned = (
        "cloud browser", "antidetect browser", "2scraper antidetect browser",
        "gate.2prx.com", "--antidetect", "antidetect_local_api",
    )
    # smoke_test.py and CLAUDE.md are allowed to NAME the banned phrases —
    # that is what makes this check readable. Nothing else is.
    exempt_names = {"smoke_test.py", "CLAUDE.md"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = (flag_set(m) for m in ENGINE_MODULES)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem")
def _():
    for mod in ENGINE_MODULES:
        assert mod._default_out("json") == "g2_results.json"
        assert mod._default_out("csv") == "g2_results.csv"


@check("all three engines carry the CLAUDE.md §5 per-site constants block, and READINESS is SHORTER than an SPA sibling's 3s (g2.com is server-rendered Rails/PJAX, confirmed — nothing has to hydrate before the content is readable)")
def _():
    assert playwright_scraper.NAV_TIMEOUT_MS == 30_000
    assert playwright_scraper.READINESS_WAIT_MS < 3_000, playwright_scraper.READINESS_WAIT_MS
    assert selenium_scraper.NAV_TIMEOUT_S == 30
    assert selenium_scraper.READINESS_WAIT_S < 3.0, selenium_scraper.READINESS_WAIT_S
    assert puppeteer_scraper.NAV_TIMEOUT_MS == 30_000
    assert puppeteer_scraper.READINESS_WAIT_S < 3.0, puppeteer_scraper.READINESS_WAIT_S
    for mod in ENGINE_MODULES:
        assert mod.MIN_CARD_MATCHES == gp.MIN_CARD_MATCHES == 2, (
            f"{mod.__name__}: MIN_CARD_MATCHES must come from g2_parser, not be re-tuned per engine"
        )


@check("engines never request a robots.txt-disallowed path — _resolve_start_url refuses /products/*/reviews/* (the ONE extra rule g2.com's robots.txt gives ClaudeBot and its peers) before any fetch")
def _():
    for mod in ENGINE_MODULES:
        url, reason = mod._resolve_start_url(
            _parse(mod, ["--url", "https://www.g2.com/products/hubspot-sales-hub/reviews/2"])
        )
        assert url is None, f"{mod.__name__}: a disallowed path must never be attempted"
        assert reason == "disallowed", f"{mod.__name__}: expected a 'disallowed' reason, got {reason!r}"
        # ...while the bare product page it sits under IS this scraper's target.
        url, mode = mod._resolve_start_url(
            _parse(mod, ["--url", "https://www.g2.com/products/hubspot-sales-hub/reviews"])
        )
        assert url and mode == "product", f"{mod.__name__}: the bare /reviews page must still be allowed"


@check("engines refuse a URL shape this repo has no parser for, instead of fetching a page they will then fail to read")
def _():
    for mod in ENGINE_MODULES:
        url, reason = mod._resolve_start_url(_parse(mod, ["--url", "https://www.g2.com/sponsored/whatever"]))
        assert url is None and reason == "unknown_shape", f"{mod.__name__}: got {(url, reason)!r}"


@check("engines refuse a /categories/{slug}/grids.json --url with a message naming WHY, rather than fetching G2's own Grid(R) ranking JSON and reporting a confusing zero-card listing: the endpoint is real and sanctioned, but this repo ships no parser for its response (g2_parser.py, 'Left for the next stage')")
def _():
    for mod in ENGINE_MODULES:
        url, reason = mod._resolve_start_url(
            _parse(mod, ["--url", "https://www.g2.com/categories/crm/grids.json"])
        )
        assert url is None and reason == "grids_unsupported", f"{mod.__name__}: got {(url, reason)!r}"
        code = asyncio_run_maybe(mod, _parse(mod, ["--url", "https://www.g2.com/categories/crm/grids.json"]))
        assert code == output_writer.EXIT_BAD_USAGE, f"{mod.__name__}: got {code}"
    # The URL builder still exists — the endpoint is confirmed real, it just
    # has no parser behind it yet.
    assert gp.grids_json_url("crm").endswith("/categories/crm/grids.json")


@check("_resolve_start_url routes each of g2.com's three real page shapes to its own mode, in all three engines")
def _():
    cases = [
        (["--url", "https://www.g2.com/categories/crm?page=4"], "category"),
        (["--url", "https://www.g2.com/products/hubspot-sales-hub/reviews"], "product"),
        (["--url", "https://www.g2.com/products/hubspot-sales-hub/pricing"], "pricing"),
        (["--category", "crm"], "category"),
        (["--product", "hubspot-sales-hub"], "product"),
    ]
    for mod in ENGINE_MODULES:
        for argv, expected in cases:
            url, mode = mod._resolve_start_url(_parse(mod, argv))
            assert url and mode == expected, f"{mod.__name__} {argv}: got {(url, mode)!r}, expected {expected!r}"


@check("all three engines only classify a DataDome/captcha marker as blocked when the page's own content is ABSENT — DataDome's tag ships on every healthy g2.com page, so a bare marker match must never turn a good listing into EXIT_BLOCKED")
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "captcha_detected and not cards_present" in src, (
            f"{path}: a marker alone can still turn a healthy listing page into EXIT_BLOCKED"
        )
        assert "gp.count_result_cards(html) >= MIN_CARD_MATCHES" in src, (
            f"{path}: 'did this page render results' must use the site's own MIN_CARD_MATCHES threshold"
        )


@check("all three engines treat 'unsupported_vendor' (the EXPECTED outcome of a real DataDome wall) as still-blocked, exactly like the other non-solved outcomes — an honest vendor name in the log, the same EXIT_BLOCKED underneath (the skyscanner-scraper/PerimeterX precedent)")
def _():
    for mod in ENGINE_MODULES:
        assert "unsupported_vendor" in mod.STILL_BLOCKED_ACTIONS, (
            f"{mod.__name__}: a DataDome wall would not be counted as blocked"
        )
        for expected in (
            "warning_no_key", "warning_no_proxy", "warning_proxy_banned",
            "warning_solver_error", "detected_unidentified_widget",
        ):
            assert expected in mod.STILL_BLOCKED_ACTIONS, f"{mod.__name__}: missing {expected!r}"
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert 'action == "unsupported_vendor"' in src, (
            f"{path}: no dedicated log branch naming the vendor — a DataDome wall would report the "
            f"misleading 'no known widget/sitekey could be extracted' instead"
        )


@check("all three engines have a dedicated log branch for 'warning_no_proxy' and 'warning_proxy_banned' — added 2026-09-22 after both fell through their if/elif chain with NO log line at all (warning_no_proxy: a pre-existing gap since DataDomeSliderTask's proxy-required guard was added; warning_proxy_banned: the new action itself, added the same day after a real 2Captcha rejection — see captcha_solver.py's module docstring)")
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert 'action == "warning_no_proxy"' in src, (
            f"{path}: DataDomeSliderTask with no proxy would silently report EXIT_BLOCKED with "
            f"no explanation of why a solve was never attempted"
        )
        assert 'action == "warning_proxy_banned"' in src, (
            f"{path}: a 2Captcha 'IP address is banned' rejection would silently report EXIT_BLOCKED "
            f"with no hint that rotating the proxy (not retrying the same one) is the actual fix"
        )


@check(
    "BUG found live-testing 2026-09-23, now fixed: all three engines' scrape_category() used to set "
    "remote_api_error=True unconditionally the moment the FIRST page's navigation failed, which "
    "run()'s block_attempt loop treats as fatal (breaks immediately, same as a crash) — that defeated "
    "--proxy-file rotation entirely the instant any single proxy timed out on page 1. Confirmed live: "
    "a real run with 10 fresh proxies and --block-retries 9 stopped after ONE navigation timeout on "
    "attempt 6, never trying proxies 7-10. Fix: when a proxy_pool is active, treat this as `blocked "
    "= True` instead so the caller rotates to proxy_pool.next(); only fall back to the fatal "
    "remote_api_error when there is no alternative identity to rotate to (no proxy pool, or a "
    "--cdp-endpoint session providing its own fixed exit)."
)
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "if proxy_pool is not None and proxy is not None:" in src and "blocked = True" in src, (
            f"{path}: scrape_category()'s first-page-navigation-failure branch must check for a live "
            f"proxy_pool and set blocked=True (letting --block-retries rotate) rather than always "
            f"declaring remote_api_error"
        )
        # The two branches must both still exist, in this order: rotate-if-
        # possible, THEN fall back to the fatal remote_api_error only when
        # there's truly no alternative exit to try.
        rotate_idx = src.find("if proxy_pool is not None and proxy is not None:\n")
        fatal_idx = src.find("remote_api_error = True", rotate_idx)
        assert rotate_idx != -1 and fatal_idx != -1 and fatal_idx > rotate_idx, (
            f"{path}: expected the proxy-pool rotation check to come BEFORE the fatal "
            f"remote_api_error fallback in scrape_category()"
        )


@check(
    "parity fix 2026-09-23 (CLAUDE.md §4/§6): playwright_scraper.py's _goto_with_retries() now "
    "accepts proxy_pool=/proxy= and detects a dead-proxy marker via is_proxy_dead_error(), matching "
    "the capability selenium_scraper.py and puppeteer_scraper.py already had (a real, previously "
    "undiscovered engine-parity gap — playwright is the PRIMARY engine per CLAUDE.md §14 and had the "
    "weakest proxy-failure detection of the three)"
)
def _():
    src = (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    assert "is_proxy_dead_error" in src, (
        "playwright_scraper.py: _goto_with_retries() still can't recognise a dead-proxy marker"
    )
    assert "proxy_pool: Optional[ProxyPool] = None, proxy: Optional[Proxy] = None," in src, (
        "playwright_scraper.py: _goto_with_retries() is missing the proxy_pool=/proxy= parameters "
        "selenium/puppeteer's versions already have"
    )
    for mod in ENGINE_MODULES:
        sig = _inspect.signature(mod._goto_with_retries)
        assert "proxy_pool" in sig.parameters and "proxy" in sig.parameters, (
            f"{mod.__name__}: _goto_with_retries() must accept proxy_pool=/proxy= — engine parity "
            f"(CLAUDE.md §4)"
        )


@check("all three engines log a diagnostic (not silence) when a page yields zero products but was not flagged as blocked — a parity gap a sibling repo's first live run exposed")
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "diagnose_unexpected_page" in src, f"{path}: no zero-products diagnostic"


@check("all three engines check the HTTP status for a block (DataDome answers 403, it does not redirect) and record a failed page rather than crashing the run (CLAUDE.md §6 -> EXIT_PARTIAL)")
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "status >= 400" in src, f"{path}: no HTTP-status block check"
        assert "failed_pages.append(page_num)" in src, f"{path}: a bad page does not degrade to a failed page"


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected
    assert (output_writer.EXIT_OK, output_writer.EXIT_CRASH, output_writer.EXIT_BAD_USAGE,
            output_writer.EXIT_BLOCKED, output_writer.EXIT_ZERO_PRODUCTS,
            output_writer.EXIT_REMOTE_API_ERROR, output_writer.EXIT_PARTIAL) == (0, 1, 2, 3, 4, 5, 6)


def _mk_product(sku, price=None, **kw):
    defaults = dict(
        sku=sku, source="g2.com", category="CRM", title=f"Product {sku}",
        brand=None, price=price, currency="USD" if price is not None else None,
        price_source="pricing_page_text" if price is not None else None,
        product_url=f"https://www.g2.com/products/{sku}/reviews",
        image_url=None, scraped_at="2026-09-22T00:00:00Z",
        rating_5=4.4, review_count=27555, review_count_source="listing_card",
        product_slug=sku,
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run precedence: remote_api_error status is never laundered into 'complete' just because products were present")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=True, remote_api_error=True, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR
        assert Path(out).exists(), "already-collected products must still be written out"
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "remote_api_error", meta["status"]


@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"
        assert not Path(f"{out_b}.meta.json").exists(), "a failed run never gets a sidecar"


@check("finish_run precedence: a blocked run that DID collect products is still 'blocked', never 'complete' (the specific regression CLAUDE.md §9 names — some batches returning results while the run was in fact gated partway through)")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=3, pages_completed=2, failed_pages=None,
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "blocked", meta["status"]
        assert meta["product_count"] == 2, "the products it DID collect must still be written"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar (a stale failure sidecar beside a previous good output would contradict data that is still good)")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=3, pages_completed=2, failed_pages=[3],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial" and meta["failed_pages"] == [3]


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        assert len(json.loads(Path(out).read_text())) == 2


@check("EXIT_BAD_USAGE (2) is reachable from every engine's own run(): no target, a robots.txt-disallowed --url, an unparseable URL shape, and --scraper-api without a key")
def _():
    for mod in ENGINE_MODULES:
        for argv in (
            [],
            ["--url", "https://www.g2.com/products/hubspot-sales-hub/reviews/2"],
            ["--url", "https://www.g2.com/sponsored/whatever"],
            ["--category", "crm", "--scraper-api"],
        ):
            code = asyncio_run_maybe(mod, _parse(mod, argv))
            assert code == output_writer.EXIT_BAD_USAGE, f"{mod.__name__} {argv}: got {code}"


@check("EXIT_CRASH (1) is reachable: an engine whose driver is absent reports a crash (not a silent zero-product 'complete'), and says which requirements file installs it")
def _():
    cases = [
        (playwright_scraper, "async_playwright"),
        (selenium_scraper, "webdriver"),
        (puppeteer_scraper, "pyppeteer_launch"),
    ]
    for mod, attr in cases:
        original = getattr(mod, attr)
        setattr(mod, attr, None)
        try:
            code = asyncio_run_maybe(mod, _parse(mod, ["--category", "crm"]))
            assert code == output_writer.EXIT_CRASH, f"{mod.__name__}: got {code}"
        finally:
            setattr(mod, attr, original)


@check("EXIT_REMOTE_API_ERROR (5) is reachable without a crash: a Scraper API failure is a broken remote dependency, not a bug in this repo — a caller (CI/cron) must be able to tell those apart")
def _():
    class _FailingClient:
        def scrape_url(self, url, timeout=60):
            raise scraper_api_client.TwoCaptchaError("upstream 502")

    for mod in ENGINE_MODULES:
        args = _parse(mod, ["--category", "crm", "--scraper-api", "--twocaptcha-key", "k"])
        args.out = "unused.json"
        products, blocked, remote_api_error, pages_completed, failed = mod._scrape_via_scraper_api(
            args=args, start_url="https://www.g2.com/categories/crm", mode="category", client=_FailingClient(),
        )
        assert remote_api_error is True and products == [] and blocked is False, (
            f"{mod.__name__}: got {(products, blocked, remote_api_error)!r}"
        )


@check("Product field order: CLAUDE.md §9's family-common fields first, in that exact order, g2-specific fields after")
def _():
    expected_head = [
        "sku", "source", "category", "title", "brand", "price", "currency",
        "price_source", "product_url", "image_url", "scraped_at",
    ]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head, output_writer.PRODUCT_FIELD_NAMES
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("rating_5", "rating_10", "review_count", "review_count_source",
                 "product_id", "product_uuid", "product_slug", "vendor_id",
                 "category_id", "product_type"):
        assert name in tail, f"missing g2-specific field {name!r}"
    # The two rating scales must stay two columns. A plain `rating` would be
    # a 0-5 star rating and a 0-10 composite score sharing one name.
    assert "rating" not in output_writer.PRODUCT_FIELD_NAMES, (
        "a single `rating` column would merge g2.com's two DIFFERENT scales — see Product's docstring"
    )


@check("merge_pages dedupes by sku, last-write-wins, in PAGE order not arrival order")
def _():
    page1 = [_mk_product("a", price=10.0), _mk_product("b", price=20.0)]
    page2 = [_mk_product("a", price=15.0), _mk_product("c", price=30.0)]
    merged = output_writer.merge_pages([page1, page2])
    assert [p.sku for p in merged] == ["a", "b", "c"], [p.sku for p in merged]
    assert next(p for p in merged if p.sku == "a").price == 15.0


@check("write_csv writes a header even for zero rows, in the Product field order")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        header = Path(out).read_text().splitlines()[0]
        assert header.split(",") == output_writer.PRODUCT_FIELD_NAMES


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction (family-shared, unmodified)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked and "host.example" in masked
    assert "secretpass" not in p.server_only()


@check("redact_credentials scrubs URL userinfo AND key/token params, globally (CLAUDE.md §8: an exception message is a log)")
def _():
    message = (
        "connect_over_cdp failed: ws://g2user-zone-scraping_browser:secretpass@cb.2captcha.com:9222 "
        "Call log:\n - ws://g2user-zone-scraping_browser:secretpass@cb.2captcha.com:9222\n"
        "  clientKey=abcdEF123 also leaked here"
    )
    redacted = proxy_pool.redact_credentials(message)
    assert "secretpass" not in redacted
    assert "abcdEF123" not in redacted
    assert redacted.count("***:***@") == 2, "must scrub EVERY occurrence, not just the first"


@check("a failed --cdp-endpoint connection never leaks its credential into the raised error, under a REAL driver-shaped failure (playwright and puppeteer; selenium refuses a credentialed endpoint outright)")
def _():
    import unittest.mock as mock

    bad_endpoint = "ws://g2user-zone-scraping_browser:secretpass@cb.2captcha.com:9222"

    class _FakePW:
        class chromium:
            @staticmethod
            async def connect_over_cdp(_endpoint):
                # Exactly the shape CLAUDE.md §8 documents: the driver
                # repeats the endpoint, credentials and all, several times.
                raise RuntimeError(
                    f"could not connect: {_endpoint}\nCall log:\n - {_endpoint}\n - {_endpoint}"
                )

    async def _run_pw():
        try:
            await playwright_scraper._connect_over_cdp(_FakePW(), bad_endpoint)
            raise AssertionError("expected a RuntimeError")
        except RuntimeError as exc:
            assert "secretpass" not in str(exc), str(exc)
            assert exc.__cause__ is None, "the unredacted original must be dropped from the chain"

    asyncio.run(_run_pw())

    async def _fake_connect(browserWSEndpoint, **_kw):
        raise RuntimeError(f"could not connect: {browserWSEndpoint}")

    async def _run_pptr():
        with mock.patch.object(puppeteer_scraper, "pyppeteer_connect", _fake_connect):
            try:
                await puppeteer_scraper._launch(headless=True, proxy=None, cdp_endpoint=bad_endpoint)
                raise AssertionError("expected a RuntimeError")
            except RuntimeError as exc:
                assert "secretpass" not in str(exc), str(exc)
                assert exc.__cause__ is None

    asyncio.run(_run_pptr())


@check("a failed fingerprint/random request never leaks the 2Captcha key into the raised error or the log (requests puts the full URL, query string included, into every HTTPError)")
def _():
    import unittest.mock as mock

    import fingerprint_client
    import requests

    key = "supersecretkey123456"
    client = scraper_api_client.TwoCaptchaClient(key)

    def _boom(*_a, **_kw):
        # Exactly what `requests` raises: the FULL URL, query string
        # included, inside the exception text (CLAUDE.md §8 names this
        # endpoint by name).
        raise requests.exceptions.HTTPError(
            f"500 Server Error for url: https://api.2captcha.com/fingerprint/random?key={key}&format=chromium"
        )

    with mock.patch.object(scraper_api_client.requests, "get", _boom):
        try:
            client.random_fingerprint()
            raise AssertionError("expected a TwoCaptchaError")
        except scraper_api_client.TwoCaptchaError as exc:
            assert key not in str(exc), str(exc)
            assert exc.__cause__ is None, "the unredacted original must be dropped from the chain"
        # ...and the caller degrades to no profile rather than crashing.
        profile = fingerprint_client.fetch_fingerprint(client)
    assert profile is None, "a failed fingerprint fetch degrades to no profile, never a crash"


@check("selenium refuses a CREDENTIALED --cdp-endpoint outright with EXIT_BAD_USAGE (CLAUDE.md §6's standing named exception), rather than silently dropping the credential")
def _():
    code = selenium_scraper.run(_parse(selenium_scraper, [
        "--category", "crm",
        "--cdp-endpoint", "ws://g2user-zone-scraping_browser:secretpass@cb.2captcha.com:9222",
    ]))
    assert code == output_writer.EXIT_BAD_USAGE, code
    assert selenium_scraper._cdp_endpoint_has_credentials("ws://u:p@host:9222") is True
    assert selenium_scraper._cdp_endpoint_has_credentials("ws://host:9222") is False


# --------------------------------------------------------------------------- #
# captcha_solver — detection, DataDome's unsolvable-vendor path, injection
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("DataDome is detected on a real-shaped g2.com wall by the GENERIC marker list alone (no extra wiring needed), and g2_parser's own BOT_CHALLENGE_MARKERS add the two g2.com-specific spellings so a log line can name WHICH deployment was seen")
def _():
    assert captcha_solver.detect_from_html(DATADOME_WALL_HTML), "generic list must catch DataDome unaided"
    assert "datadome" in captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS
    assert gp.BOT_CHALLENGE_MARKERS == ("datadomejstag", "dd.g2.com")
    for marker in gp.BOT_CHALLENGE_MARKERS:
        assert marker in DATADOME_WALL_HTML.lower(), marker


@check("a DataDome wall with NO visible slider challenge reports action='unsupported_vendor' + vendor='datadome' — NOT the vague 'detected_unidentified_widget' — because DataDome's own device-check tag is present but there is currently nothing to solve on this page")
def _():
    # Corrected 2026-09-22 (Roman caught this): DataDome itself DOES have a
    # real 2Captcha task type — CaptchaType.DATADOME_SLIDER, see the
    # dedicated check below — so this fixture (DataDome's tag with no
    # slider iframe, the ordinary case on most page loads) is no longer
    # "DataDome can never be solved," it's "nothing to solve on THIS page."
    class _FakeClient:
        api_key = "fake"

    assert captcha_solver.identify_widget(DATADOME_WALL_HTML) is None, "no iframe in this fixture -> no extractable widget"
    assert captcha_solver.identify_unsupported_vendor(DATADOME_WALL_HTML) == "datadome"
    result = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://www.g2.com/categories/crm",
        html=DATADOME_WALL_HTML, count_product_links=gp.count_result_cards,
        extra_markers=gp.BOT_CHALLENGE_MARKERS,
    )
    assert result["action"] == "unsupported_vendor", result
    assert result["vendor"] == "datadome", result
    # DATADOME_SLIDER DOES exist now (see the dedicated checks below) — the
    # old assumption that no CaptchaType could ever name it was the bug.
    assert any(t == captcha_solver.CaptchaType.DATADOME_SLIDER for t in captcha_solver.CaptchaType)


@check("a DataDome page WITH a real slider iframe is identified as CaptchaType.DATADOME_SLIDER, with the iframe src carried through as captcha_url (HTML-entity-decoded)")
def _():
    signal = captcha_solver.identify_widget(DATADOME_SLIDER_WALL_HTML)
    assert signal is not None, "must find the slider iframe"
    assert signal.captcha_type == captcha_solver.CaptchaType.DATADOME_SLIDER
    assert signal.captcha_url.startswith("https://geo.captcha-delivery.com/captcha/?"), signal.captcha_url
    assert "&amp;" not in signal.captcha_url, "must be HTML-entity-decoded, not raw markup"
    assert "&" in signal.captcha_url


@check("a DataDome page with the OTHER confirmed shape — /interstitial/, not /captcha/ — is ALSO identified as CaptchaType.DATADOME_SLIDER (added 2026-09-22 after this exact shape was missed on a real g2.com run and silently reported unsupported_vendor instead of attempted)")
def _():
    signal = captcha_solver.identify_widget(DATADOME_INTERSTITIAL_WALL_HTML)
    assert signal is not None, "must find the /interstitial/ iframe too, not just /captcha/"
    assert signal.captcha_type == captcha_solver.CaptchaType.DATADOME_SLIDER
    assert signal.captcha_url.startswith("https://geo.captcha-delivery.com/interstitial/?"), signal.captcha_url
    assert "&amp;" not in signal.captcha_url
    # The real regression this guards against: solve_when_blocked() checks
    # identify_widget() FIRST and only falls back to
    # identify_unsupported_vendor() when that returns None (see this
    # module's own comment above _UNSUPPORTED_VENDOR_MARKERS) — so what
    # actually matters is solve_when_blocked()'s own routing, not whether
    # identify_unsupported_vendor() (a standalone marker search that still
    # matches "datadome" regardless) would also match this page.
    result = captcha_solver.solve_when_blocked(
        client=None, page_url="https://www.g2.com/categories/crm",
        html=DATADOME_INTERSTITIAL_WALL_HTML, count_product_links=gp.count_result_cards,
        extra_markers=gp.BOT_CHALLENGE_MARKERS,
        # proxy=None, client=None — same as the existing "NO proxy" check
        # above: DATADOME_SLIDER's proxy-required guard in _task_payload()
        # raises before `client` is ever touched, so this still proves the
        # routing without needing a real/fake client.
    )
    assert result["action"] == "warning_no_proxy", (
        "must route through the DATADOME_SLIDER solve path (which then "
        f"complains about the missing proxy), not 'unsupported_vendor': {result}"
    )


@check("the REAL scrubbed g2.com capture (tests/fixtures/g2_datadome_interstitial.html — Roman's first live --cdp-endpoint run, 2026-09-22) is identified as CaptchaType.DATADOME_SLIDER, not reported as unsupported_vendor")
def _():
    fixture_path = Path(__file__).parent / "tests" / "fixtures" / "g2_datadome_interstitial.html"
    real_html = fixture_path.read_text(encoding="utf-8")
    assert "geo.captcha-delivery.com/interstitial/" in real_html, "fixture must still contain the real iframe shape"
    assert 'title="DataDome Device Check"' in real_html
    signal = captcha_solver.identify_widget(real_html)
    assert signal is not None, "the real capture must be recognized as a solvable DataDome challenge"
    assert signal.captcha_type == captcha_solver.CaptchaType.DATADOME_SLIDER
    assert signal.captcha_url.startswith("https://geo.captcha-delivery.com/interstitial/?"), signal.captcha_url
    # Confirms the scrub didn't accidentally leave a real single-use token behind.
    for leaked_prefix in ("AHrlqAAAAAMAzHjDxiNG", "229542D5C186C7F5A5BB", "pg9Z1U1y5BqoS3mpc38t"):
        assert leaked_prefix not in real_html, f"scrubbed fixture still contains a real token: {leaked_prefix}"


@check("a SECOND real scrubbed g2.com capture (tests/fixtures/g2_datadome_captcha_banned_ip.html — Roman's first real PROXY-mode run, 2026-09-22) confirms the /captcha/ path is ALSO real, not just 2Captcha's own documented shape — and carries a real t=bv (banned-IP) marker this repo had never seen before")
def _():
    fixture_path = Path(__file__).parent / "tests" / "fixtures" / "g2_datadome_captcha_banned_ip.html"
    real_html = fixture_path.read_text(encoding="utf-8")
    assert "geo.captcha-delivery.com/captcha/" in real_html, "fixture must still contain the real /captcha/ iframe shape"
    assert 'title="DataDome CAPTCHA"' in real_html
    assert "t=bv" in real_html, "fixture must still carry the real banned-IP marker that triggered the 2Captcha rejection"
    signal = captcha_solver.identify_widget(real_html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.DATADOME_SLIDER
    assert signal.captcha_url.startswith("https://geo.captcha-delivery.com/captcha/?"), signal.captcha_url
    for leaked_prefix in ("AHrlqAAAAAMAIb9c2R7M", "229542D5C186C7F5A5BB", "0JtQjAIQwoWdsfsQT057", "e4f173767bec32fc76ef"):
        assert leaked_prefix not in real_html, f"scrubbed fixture still contains a real token: {leaked_prefix}"


@check("solve_when_blocked() reports action='warning_proxy_banned' — not the generic 'warning_solver_error' — when 2Captcha rejects a DataDomeSliderTask because the challenge's own t= marker says the proxy's IP is already banned (a REAL 2Captcha response, confirmed live 2026-09-22: 'ERROR_BAD_PARAMETERS ... your IP address is banned')")
def _():
    class _BannedIPClient:
        api_key = "fake"
        def solve_and_wait(self, task):
            raise scraper_api_client.TwoCaptchaError(
                'createTask failed: ERROR_BAD_PARAMETERS Your captcha_url value contains "t=bv", '
                "that means your IP address is banned."
            )

    result = captcha_solver.solve_when_blocked(
        client=_BannedIPClient(), page_url="https://www.g2.com/categories/crm",
        html=DATADOME_SLIDER_WALL_HTML, count_product_links=gp.count_result_cards,
        extra_markers=gp.BOT_CHALLENGE_MARKERS,
        proxy={"type": "http", "address": "eu.proxy.2captcha.com", "port": 2334, "login": "l", "password": "p"},
        user_agent="Mozilla/5.0",
    )
    assert result["action"] == "warning_proxy_banned", result
    assert "banned" in result["detail"].lower()


@check("solve_when_blocked() for a DataDome slider challenge with NO proxy supplied returns 'warning_no_proxy' — never a crash, never a silently-skipped solve — because DataDomeSliderTask has no proxyless variant")
def _():
    class _FakeClient:
        api_key = "fake"
        def solve_and_wait(self, task):  # pragma: no cover - must not be reached
            raise AssertionError("must not call 2Captcha without a proxy for DataDomeSliderTask")

    result = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://www.g2.com/categories/crm",
        html=DATADOME_SLIDER_WALL_HTML, count_product_links=gp.count_result_cards,
        extra_markers=gp.BOT_CHALLENGE_MARKERS,
        # proxy=None, user_agent=None — the point of this check
    )
    assert result["action"] == "warning_no_proxy", result
    assert "proxy" in result["detail"].lower()


@check("solve_when_blocked() for a DataDome slider challenge WITH proxy+user_agent builds a real DataDomeSliderTask (proxyType/proxyAddress/proxyPort/captchaUrl/userAgent) and reports action='solved'; parse_datadome_cookie() turns the cookie-shaped solution into a driver-ready dict")
def _():
    seen_tasks = []

    class _FakeClient:
        api_key = "fake"
        def solve_and_wait(self, task):
            seen_tasks.append(task)
            return json.dumps({"cookie": "datadome=SOLVEDVALUE123; Path=/; Secure; SameSite=Lax"})

    result = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://www.g2.com/categories/crm",
        html=DATADOME_SLIDER_WALL_HTML, count_product_links=gp.count_result_cards,
        extra_markers=gp.BOT_CHALLENGE_MARKERS,
        proxy={"type": "http", "address": "203.0.113.9", "port": 8000, "login": "u", "password": "p"},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) fake-smoke-test-ua",
    )
    assert result["action"] == "solved", result
    assert result["captcha_type"] == "datadome_slider"
    assert len(seen_tasks) == 1
    task = seen_tasks[0]
    assert task["type"] == "DataDomeSliderTask"
    assert task["proxyType"] == "http" and task["proxyAddress"] == "203.0.113.9" and task["proxyPort"] == 8000
    assert task["proxyLogin"] == "u" and task["proxyPassword"] == "p"
    assert task["captchaUrl"].startswith("https://geo.captcha-delivery.com/captcha/?")
    assert task["userAgent"] == "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) fake-smoke-test-ua"

    cookie = captcha_solver.parse_datadome_cookie(result["token"])
    assert cookie == {
        "name": "datadome", "value": "SOLVEDVALUE123",
        "path": "/", "secure": True, "same_site": "Lax",
    }, cookie


@check("_task_payload() refuses a DataDome slider task with no user_agent even when a proxy IS supplied — the second required-input guard, not just the proxy one")
def _():
    signal = captcha_solver.identify_widget(DATADOME_SLIDER_WALL_HTML)
    try:
        captcha_solver._task_payload(
            signal, "https://www.g2.com/categories/crm", proxyless=False,
            proxy={"type": "http", "address": "203.0.113.9", "port": 8000},
            user_agent=None,
        )
        raise AssertionError("must raise without user_agent")
    except captcha_solver.TwoCaptchaError as exc:
        assert "user_agent" in str(exc)


@check("parse_datadome_cookie() raises TwoCaptchaError (never a bare exception) on a malformed solution — the same 'solver error is a warning' contract every other branch of this module honors")
def _():
    for bad in ("not json at all", json.dumps({"no_cookie_key": "x"}), json.dumps("a string, not a dict")):
        try:
            captcha_solver.parse_datadome_cookie(bad)
            raise AssertionError(f"must raise for {bad!r}")
        except captcha_solver.TwoCaptchaError:
            pass


@check("a HEALTHY g2.com listing page — which also carries DataDome's tag, because every g2.com page does — is NOT treated as blocked: solve_when_blocked sees the rendered cards and skips solving entirely")
def _():
    class _FakeClient:
        api_key = "fake"

    html = listing_html(cards=3)
    assert captcha_solver.detect_from_html(html, gp.BOT_CHALLENGE_MARKERS), "fixture must trip the marker check"
    result = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://www.g2.com/categories/crm", html=html,
        count_product_links=gp.count_result_cards, extra_markers=gp.BOT_CHALLENGE_MARKERS,
    )
    assert result["action"] == "skipped_products_present", result


@check("captcha_solver.identify_widget still extracts a Turnstile sitekey (the family-shared solvable path is intact, even though g2.com's own confirmed defense isn't solvable)")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAAAAAABkMYin"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None and signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAAAAAABkMYin"


@check("captcha_solver.build_injection_script builds real JS per widget type, and returns None for reCAPTCHA v3 (no generic injection point exists) rather than JS that would silently do nothing")
def _():
    script = captcha_solver.build_injection_script(captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE, "tok123")
    assert script and "cf-turnstile-response" in script and "tok123" in script
    script = captcha_solver.build_injection_script(captcha_solver.CaptchaType.RECAPTCHA_V2, "tok123")
    assert script and "g-recaptcha-response" in script
    assert captcha_solver.build_injection_script(captcha_solver.CaptchaType.RECAPTCHA_V3, "tok123") is None


@check("all three engines actually call build_injection_script from their 'solved' branch — a solved (paid-for) token that is never written back into the page would be money spent for nothing")
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "build_injection_script" in src, f"{path} no longer imports/calls build_injection_script"
        assert 'CaptchaType(result["captcha_type"])' in src, f"{path} doesn't build a CaptchaType from the solved result"


# --------------------------------------------------------------------------- #
# The two AST-level regression checks this family added on 2026-09-22 —
# both for bugs that really shipped, in a sibling repo, and both written
# here so they cannot quietly reappear in g2-scraper's own three engines.
# --------------------------------------------------------------------------- #
@check(
    "BUG CLASS 1 (g2_parser.py's module docstring): EVERY function that takes an 'autosolve' "
    "parameter actually calls the Captcha.setAutoSolve helper somewhere in its body — "
    "playwright/puppeteer only, selenium is exempt because it cannot authenticate a "
    "--cdp-endpoint at all (CLAUDE.md §6) and therefore has no such helper. shein-scraper shipped "
    "a puppeteer scrape_product_page() that ACCEPTED `autosolve` and never armed anything, so a "
    "direct --url <product page> --cdp-endpoint run silently got no auto-solve and no warning. "
    "This repo has THREE page-loading functions per engine (category, product, pricing) and this "
    "check walks the AST of every one of them."
)
def _():
    import ast as _ast

    HELPER_NAME = "_enable_scraping_browser_auto_solve"
    for path in ("playwright_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=path)
        checked = []
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            if "autosolve" not in arg_names:
                continue
            checked.append(node.name)
            calls_helper = any(
                isinstance(n, _ast.Call)
                and (
                    (isinstance(n.func, _ast.Name) and n.func.id == HELPER_NAME)
                    or (isinstance(n.func, _ast.Attribute) and n.func.attr == HELPER_NAME)
                )
                for n in _ast.walk(node)
            )
            # A function that only FORWARDS autosolve to another function
            # which does arm it is equally fine — that is still full
            # coverage of the page it loads, just one hop away.
            forwards_autosolve = any(
                isinstance(n, _ast.Call)
                and any(kw.arg == "autosolve" for kw in n.keywords)
                for n in _ast.walk(node)
            )
            assert calls_helper or forwards_autosolve, (
                f"{path}: {node.name}() takes an 'autosolve' parameter but neither calls "
                f"{HELPER_NAME}() nor forwards autosolve to something that does — captcha "
                f"auto-solve would silently never be armed on this page/navigation path when "
                f"--cdp-endpoint + --solve-captcha are set."
            )
        # All three of this repo's real page shapes must be represented.
        for required in ("scrape_category", "scrape_product_page", "scrape_pricing_page"):
            assert required in checked, (
                f"{path}: {required}() does not take an 'autosolve' parameter — every function "
                f"that loads a real page must be able to arm auto-solve (there are three page "
                f"shapes on g2.com, and all three get it)."
            )


@check(
    "BUG CLASS 2 (g2_parser.py's module docstring): EVERY call to an engine's _maybe_solve_captcha() "
    "passes count_product_links as an explicit keyword argument, shaped for the page THAT call site "
    "is looking at. captcha_solver.solve_when_blocked() and each engine's wrapper both make it a "
    "required keyword-only argument with no default, so a missing one is already a plain TypeError "
    "at runtime — this AST check catches it at review/CI time, before any engine has to run, and "
    "additionally asserts the counter matches the page shape rather than merely being present."
)
def _():
    import ast as _ast

    HELPER_NAME = "_maybe_solve_captcha"
    EXPECTED_COUNTER = {
        "scrape_category": "count_result_cards",
        "scrape_product_page": "count_product_page_data",
        "scrape_pricing_page": "count_pricing_tiers",
    }
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=path)
        seen_functions = set()
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if node.name not in EXPECTED_COUNTER:
                continue
            seen_functions.add(node.name)
            calls = [
                n for n in _ast.walk(node)
                if isinstance(n, _ast.Call)
                and (
                    (isinstance(n.func, _ast.Name) and n.func.id == HELPER_NAME)
                    or (isinstance(n.func, _ast.Attribute) and n.func.attr == HELPER_NAME)
                )
            ]
            assert calls, (
                f"{path}: {node.name}() never calls {HELPER_NAME}() — a captcha on this page shape "
                f"would never even be looked at"
            )
            for call in calls:
                kwargs = {kw.arg: kw for kw in call.keywords}
                assert "count_product_links" in kwargs, (
                    f"{path}: {node.name}()'s call to {HELPER_NAME}() does not pass "
                    f"count_product_links explicitly"
                )
                value = kwargs["count_product_links"].value
                rendered = _ast.dump(value)
                expected = EXPECTED_COUNTER[node.name]
                assert expected in rendered, (
                    f"{path}: {node.name}() passes the WRONG page-shaped counter to {HELPER_NAME}() "
                    f"— expected {expected}, got {rendered}. A listing-shaped counter on a detail "
                    f"page reads 0 on every normal load (bug class 2)."
                )
        for required in EXPECTED_COUNTER:
            assert required in seen_functions, f"{path}: no {required}() found (this test may be stale)"


@check(
    "BEHAVIORAL proof of the same bug class, on real g2.com page shapes: a COMPLETELY NORMAL "
    "product /reviews page carries DataDome's tag (every g2.com page does) and has zero listing "
    "cards by construction. Checked with the LISTING-shaped counter it reads as '0 products, "
    "possibly blocked' and reports a DataDome block on a page that was never blocked; checked with "
    "the DETAIL-shaped counter it is correctly recognised as fine and no solve is attempted. Same "
    "for the pricing page with its own counter."
)
def _():
    class _FakeClient:
        api_key = "fake"

    url = "https://www.g2.com/products/hubspot-sales-hub/reviews"
    html = product_html()
    assert captcha_solver.detect_from_html(html, gp.BOT_CHALLENGE_MARKERS), "fixture must trip the marker check"
    assert gp.count_result_cards(html) == 0, "a product page has zero listing cards, by definition"
    assert gp.count_product_page_data(html) == 1, "fixture must parse as a real product page"

    wrong_shape = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url=url, html=html,
        count_product_links=gp.count_result_cards, extra_markers=gp.BOT_CHALLENGE_MARKERS,
    )
    right_shape = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url=url, html=html,
        count_product_links=gp.count_product_page_data, extra_markers=gp.BOT_CHALLENGE_MARKERS,
    )
    assert wrong_shape["action"] != "skipped_products_present", (
        "this assertion documents the OLD bug shape, not a requirement — if it starts failing it "
        "means count_result_cards began recognising product pages, which would make the fix moot, "
        "not broken"
    )
    assert wrong_shape["action"] == "unsupported_vendor", wrong_shape
    assert right_shape["action"] == "skipped_products_present", right_shape

    pricing_url = "https://www.g2.com/products/hubspot-sales-hub/pricing"
    pricing_with_dd = PRICING_HTML.replace("<body>", "<body><script>window.DataDomeJsTag={};</script>")
    assert gp.count_pricing_tiers(pricing_with_dd) == 4
    assert gp.count_result_cards(pricing_with_dd) == 0
    pricing_right = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url=pricing_url, html=pricing_with_dd,
        count_product_links=gp.count_pricing_tiers, extra_markers=gp.BOT_CHALLENGE_MARKERS,
    )
    assert pricing_right["action"] == "skipped_products_present", pricing_right


@check("each engine's own _maybe_solve_captcha() wrapper makes count_product_links a REQUIRED keyword-only argument with no default — the same choice captcha_solver.solve_when_blocked() made, so no call site can quietly inherit a counter that is wrong for its page")
def _():
    import ast as _ast

    for path in ENGINE_FILES:
        tree = _ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
        found = False
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == "_maybe_solve_captcha":
                found = True
                kwonly = [a.arg for a in node.args.kwonlyargs]
                assert "count_product_links" in kwonly, f"{path}: not keyword-only"
                idx = kwonly.index("count_product_links")
                assert node.args.kw_defaults[idx] is None, (
                    f"{path}: count_product_links has a default — a call site could inherit the "
                    f"wrong page shape silently, which is exactly bug class 2"
                )
        assert found, f"{path}: no _maybe_solve_captcha() found (this test may be stale)"
    # And the shared module it wraps keeps the same contract.
    import inspect
    sig = inspect.signature(captcha_solver.solve_when_blocked)
    param = sig.parameters["count_product_links"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


@check(
    "DataDome fix (2026-09-22): every _maybe_solve_captcha() call site in all three engines "
    "passes proxy=/user_agent= — required for captcha_solver.CaptchaType.DATADOME_SLIDER's "
    "DataDomeSliderTask to ever succeed (it has no proxyless path, unlike every other type). "
    "Also confirms each engine's own _maybe_solve_captcha() wrapper actually accepts both params "
    "and forwards them into solve_when_blocked() rather than swallowing them."
)
def _():
    import ast as _ast

    CALL_SITE_FUNCTIONS = ("scrape_category", "scrape_product_page", "scrape_pricing_page")
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=path)

        wrapper_accepts_both = False
        wrapper_forwards_both = False
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == "_maybe_solve_captcha":
                arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
                wrapper_accepts_both = "proxy" in arg_names and "user_agent" in arg_names
                for n in _ast.walk(node):
                    if (
                        isinstance(n, _ast.Call)
                        and (
                            (isinstance(n.func, _ast.Name) and n.func.id == "solve_when_blocked")
                            or (isinstance(n.func, _ast.Attribute) and n.func.attr == "solve_when_blocked")
                        )
                    ):
                        fwd_kwargs = {kw.arg for kw in n.keywords}
                        wrapper_forwards_both = "proxy" in fwd_kwargs and "user_agent" in fwd_kwargs
        assert wrapper_accepts_both, f"{path}: _maybe_solve_captcha() doesn't accept proxy=/user_agent="
        assert wrapper_forwards_both, (
            f"{path}: _maybe_solve_captcha() doesn't forward proxy=/user_agent= into "
            f"solve_when_blocked() — DataDomeSliderTask would always fail with warning_no_proxy "
            f"even when a --proxy was configured for the run"
        )

        seen = set()
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if node.name not in CALL_SITE_FUNCTIONS:
                continue
            calls = [
                n for n in _ast.walk(node)
                if isinstance(n, _ast.Call)
                and (
                    (isinstance(n.func, _ast.Name) and n.func.id == "_maybe_solve_captcha")
                    or (isinstance(n.func, _ast.Attribute) and n.func.attr == "_maybe_solve_captcha")
                )
            ]
            for call in calls:
                seen.add(node.name)
                kwargs = {kw.arg for kw in call.keywords}
                assert "proxy" in kwargs and "user_agent" in kwargs, (
                    f"{path}: {node.name}()'s call to _maybe_solve_captcha() is missing "
                    f"proxy=/user_agent= — {sorted(kwargs)}"
                )
        assert seen == set(CALL_SITE_FUNCTIONS), (
            f"{path}: expected all of {CALL_SITE_FUNCTIONS} to call _maybe_solve_captcha() with "
            f"a captcha check present, only found calls in {sorted(seen)} (this test may be stale)"
        )


@check(
    "DataDome fix (2026-09-22): every engine has a code path that calls "
    "captcha_solver.parse_datadome_cookie() and applies the result via ITS OWN driver's native "
    "cookie API when a DataDome slider challenge is solved (build_injection_script() returns None "
    "for this type on purpose — a solve that's never applied is the exact 'documented feature "
    "doesn't work' anti-pattern this family has shipped before)."
)
def _():
    src = {path: (ROOT / path).read_text(encoding="utf-8") for path in ENGINE_FILES}

    for path, text in src.items():
        assert "parse_datadome_cookie" in text, f"{path}: never imports/calls parse_datadome_cookie()"
        assert "DATADOME_SLIDER" in text, f"{path}: has no DATADOME_SLIDER-specific branch at all"

    # Each engine's own native cookie call, not a generic string check —
    # the point is that the RIGHT api for THAT driver is used.
    assert "context.add_cookies(" in src["playwright_scraper.py"], (
        "playwright_scraper.py: no context.add_cookies(...) call — Playwright's own native cookie API"
    )
    assert "driver.add_cookie(" in src["selenium_scraper.py"], (
        "selenium_scraper.py: no driver.add_cookie(...) call — Selenium's own native cookie API"
    )
    assert "page.setCookie(" in src["puppeteer_scraper.py"], (
        "puppeteer_scraper.py: no page.setCookie(...) call — pyppeteer's own native cookie API"
    )
    # And each reloads/refreshes afterward — a cookie sitting in the jar
    # with nothing re-requesting the page is a solve that does nothing.
    assert "page.reload(" in src["playwright_scraper.py"]
    assert "driver.refresh()" in src["selenium_scraper.py"]
    assert "page.reload(" in src["puppeteer_scraper.py"]


# --------------------------------------------------------------------------- #
# env_config — G2_* keys, placeholder detection, precedence (CLAUDE.md §17)
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses G2_ prefixed keys, not a leftover SHEIN_/LIDL_/other-site name from the repo this was ported from")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("G2_"), f"unexpected env key {key!r}"
    assert set(env_config.ENV_KEYS) == {"TWOCAPTCHA_KEY", "G2_PROXY", "G2_CDP_ENDPOINT", "G2_URL"}


@check("env_config has exactly ONE _is_placeholder implementation and it treats a braced {...} fragment as unset (CLAUDE.md §17's named defect class: two independent checks for the same thing WILL drift apart)")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("ws://{login}-zone-scraping_browser:{password}@cb.2captcha.com:9222")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag, and every engine's argparse destinations line up with ENV_KEYS' targets")
def _():
    import argparse
    import os as _os

    ns = argparse.Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["G2_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["G2_PROXY"]

    for mod in ENGINE_MODULES:
        args = _parse(mod, ["--category", "crm"])
        for dest in env_config.ENV_KEYS.values():
            assert hasattr(args, dest), f"{mod.__name__}: no --{dest.replace('_', '-')} for an ENV_KEYS target"


@check(".env.example round-trips through the real loader with every credential reading as unset — a copied example must never be treated as a real credential")
def _():
    import argparse
    ns = argparse.Namespace(**{dest: None for dest in env_config.ENV_KEYS.values()})
    env_config.apply_env(ns, dotenv_path=str(ROOT / ".env.example"))
    for dest in env_config.ENV_KEYS.values():
        assert getattr(ns, dest) is None, (
            f"a pasted .env.example value was accepted as a real credential for {dest!r}: "
            f"{getattr(ns, dest)!r}"
        )


# --------------------------------------------------------------------------- #
# g2_parser — URL shapes, robots.txt, the three page parsers, the two scales
# --------------------------------------------------------------------------- #
@check("URL builders produce the confirmed-real g2.com shapes (a product's canonical URL ALWAYS ends in /reviews; page=1 is emitted as the bare URL)")
def _():
    assert gp.category_url("crm") == "https://www.g2.com/categories/crm"
    assert gp.category_url("crm", 1) == "https://www.g2.com/categories/crm"
    assert gp.category_url("crm", 4) == "https://www.g2.com/categories/crm?page=4"
    assert gp.product_url("hubspot-sales-hub") == "https://www.g2.com/products/hubspot-sales-hub/reviews"
    assert gp.pricing_url("hubspot-sales-hub") == "https://www.g2.com/products/hubspot-sales-hub/pricing"
    assert gp.grids_json_url("crm") == "https://www.g2.com/categories/crm/grids.json"


@check("slug extraction round-trips for both product and category URLs (the engines rebuild every ?page=N URL from the slug rather than string-editing the caller's --url)")
def _():
    assert gp.slug_from_product_url("https://www.g2.com/products/hubspot-sales-hub/reviews") == "hubspot-sales-hub"
    assert gp.category_slug_from_url("https://www.g2.com/categories/crm?page=3") == "crm"
    assert gp.category_slug_from_url("https://www.g2.com/categories/crm/grids.json") is None
    assert gp.category_slug_from_url("https://www.g2.com/products/x/reviews") is None


@check(
    "BUG found live-testing 2026-09-22, now fixed: page_number_from_url() recovers the `?page=N` "
    "a caller's --url carried, the missing counterpart to category_slug_from_url() that every "
    "engine's scrape_category() rebuilds its pagination from. Before this, this module's OWN "
    "docstring example one function up (`--url '.../categories/crm?page=2'`) silently started "
    "over at page 1 instead of picking up at page 2."
)
def _():
    assert gp.page_number_from_url("https://www.g2.com/categories/crm?page=3") == 3
    assert gp.page_number_from_url("https://www.g2.com/categories/crm") == 1
    assert gp.page_number_from_url(None) == 1
    # Garbage/non-positive page values degrade to "start over", not a crash.
    assert gp.page_number_from_url("https://www.g2.com/categories/crm?page=abc") == 1
    assert gp.page_number_from_url("https://www.g2.com/categories/crm?page=0") == 1
    assert gp.page_number_from_url("https://www.g2.com/categories/crm?page=-5") == 1


@check(
    "all three engines' scrape_category() actually START their pagination loop at "
    "gp.page_number_from_url(start_url), not a hardcoded 1 — the structural half of the check "
    "above, since scrape_category() needs a real browser to exercise behaviourally offline"
)
def _():
    for path in ENGINE_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "start_page = gp.page_number_from_url(start_url) if slug else 1" in src, (
            f"{path}: scrape_category() must derive its starting page from the caller's --url, "
            f"not assume page 1"
        )
        assert "for page_num in range(start_page, start_page + args.max_pages):" in src, (
            f"{path}: pagination loop must start at start_page, not a hardcoded 1"
        )
        # The two `page_num == 1` special cases ("this is the first request
        # of the run") must have moved to `page_num == start_page` too — a
        # stray literal `1` here would silently misfire the "first page
        # failed" / "first page had zero products" branches whenever
        # start_page != 1.
        assert "page_num == 1" not in src, (
            f"{path}: a literal 'page_num == 1' survived the start_page fix — "
            f"should be 'page_num == start_page'"
        )


@check("make_sku prefers the URL SLUG (the one identifier present on BOTH a listing card and that product's own detail page) and falls back to a DETERMINISTIC fingerprint, never a random value — diff_runs.py must see the same sku for the same product across two runs")
def _():
    assert gp.make_sku("hubspot-sales-hub", "https://www.g2.com/products/hubspot-sales-hub/reviews") == "hubspot-sales-hub"
    a = gp.make_sku(None, "https://www.g2.com/products/x/reviews")
    b = gp.make_sku(None, "https://www.g2.com/products/x/reviews")
    assert a == b and a.startswith("fp_")


@check("is_disallowed_path honours the STRICTER ClaudeBot rule set by default: /products/{slug}/reviews (this scraper's target) is ALLOWED, /products/{slug}/reviews/<anything> is DISALLOWED — and ai_crawler_rules=False makes the difference auditable rather than baked in")
def _():
    allowed = "/products/hubspot-sales-hub/reviews"
    sub = "/products/hubspot-sales-hub/reviews/2"
    assert gp.is_disallowed_path(allowed) is False
    assert gp.is_disallowed_path(sub) is True
    assert gp.is_disallowed_path(sub + "?filters=x") is True
    assert gp.is_disallowed_path("/products/hubspot-sales-hub/reviews/") is True
    # Full URLs, not just bare paths.
    assert gp.is_disallowed_path("https://www.g2.com" + sub) is True
    assert gp.is_disallowed_path("https://www.g2.com" + allowed) is False
    # The general User-agent:* group alone does NOT carry this rule — that
    # is exactly the difference the AI-crawler group adds, and the reason
    # this repo opts into the stricter set by default.
    assert gp.is_disallowed_path(sub, ai_crawler_rules=False) is False
    assert gp.is_disallowed_path("/categories/crm") is False
    assert gp.is_disallowed_path("") is False


@check("is_known_scrape_target allowlists exactly the four URL shapes this repo can parse, and never accepts a /reviews/ form that is_disallowed_path would then refuse (two guards must not disagree about the same URL)")
def _():
    assert gp.is_known_scrape_target("https://www.g2.com/categories/crm")
    assert gp.is_known_scrape_target("https://www.g2.com/categories/crm?page=9")
    assert gp.is_known_scrape_target("https://www.g2.com/categories/crm/grids.json")
    assert gp.is_known_scrape_target("https://www.g2.com/products/hubspot-sales-hub/reviews")
    assert gp.is_known_scrape_target("https://www.g2.com/products/hubspot-sales-hub/pricing")
    assert not gp.is_known_scrape_target("https://www.g2.com/products/hubspot-sales-hub/reviews/")
    assert not gp.is_known_scrape_target("https://www.g2.com/sponsored/x")
    assert not gp.is_known_scrape_target("")


@check("parse_category_listing reads the confirmed-real card shape: the data-event-options JSON blob (product/product_id/vendor_id/category/product_type), the .elv-star-wrapper 0-5 rating, the isolated review count, the image, and a canonical /reviews product URL")
def _():
    html = listing_html(cards=3)
    products = gp.parse_category_listing(html, "crm", "https://www.g2.com/categories/crm")
    assert len(products) == 3, len(products)
    p = products[0]
    assert p.sku == "product-1" and p.product_slug == "product-1"
    assert p.source == "g2.com"
    assert p.title == "Product 1"
    assert p.category == "CRM", "G2's own display name from the card JSON beats the slug"
    assert p.product_url == "https://www.g2.com/products/product-1/reviews"
    assert p.image_url and p.image_url.startswith("https://images.g2crowd.com/")
    assert p.rating_5 == 4.4
    assert p.rating_10 is None, "the 0-10 composite score exists only on a product page"
    assert p.review_count == 27555 and p.review_count_source == "listing_card"
    assert p.product_id == 501 and p.vendor_id == 469 and p.category_id == 179
    assert p.product_type == "Software"
    assert p.brand is None, "no vendor NAME is exposed on a listing card — only vendor_id"
    assert p.price is None and p.currency is None and p.price_source is None, (
        "g2.com carries no price on a listing card at all"
    )


@check("parse_category_listing falls back to the caller's category slug when the card JSON has none, and one malformed card costs only itself — never the other rows on the page (CLAUDE.md §6)")
def _():
    good = _card("ok-product", "OK Product", 777)
    broken = (
        '<div class="content-card category-product-card">'
        "<a data-event-options='{not valid json' href=\"/products/broken/reviews\">Broken</a>"
        "</div>"
    )
    linkless = '<div class="content-card category-product-card"><span>no link at all</span></div>'
    html = f"<html><body>{good}{broken}{linkless}</body></html>"
    products = gp.parse_category_listing(html, "crm", "https://www.g2.com/categories/crm")
    skus = {p.sku for p in products}
    assert "ok-product" in skus, "a healthy card must survive its broken siblings"
    assert "broken" in skus, "a card with unparseable JSON still yields its URL-derived row"
    broken_row = next(p for p in products if p.sku == "broken")
    assert broken_row.category == "crm", "falls back to the slug the caller was iterating"
    assert broken_row.product_id is None


@check("safe_parse_category_listing degrades an unexpected parser exception to an empty page (which the engine records as a FAILED page -> EXIT_PARTIAL) instead of crashing a run and discarding its sibling pages")
def _():
    import unittest.mock as mock

    def _boom(*_a, **_kw):
        raise RuntimeError("selector engine exploded")

    with mock.patch.object(gp, "parse_category_listing", _boom):
        assert gp.safe_parse_category_listing(listing_html(), "crm", "u") == []


@check("count_result_cards is the LISTING-shaped counter and never raises into a captcha decision")
def _():
    assert gp.count_result_cards(listing_html(cards=5)) == 5
    assert gp.count_result_cards(product_html()) == 0
    assert gp.count_result_cards(PRICING_HTML) == 0
    assert gp.count_result_cards("") == 0
    assert gp.count_result_cards("<html><body>not even close</body></html>") == 0


@check("pagination is DISCOVERED from the markup, never hardcoded: has_next_page reads the Next control's disabled state, current_page_number reads G2's own current marker, and a page with no pagination block at all stops the loop")
def _():
    assert gp.has_next_page(listing_html(next_disabled=False)) is True
    assert gp.has_next_page(listing_html(next_disabled=True)) is False
    assert gp.has_next_page("<html><body>no pagination here</body></html>") is False
    assert gp.current_page_number(listing_html()) == 1
    assert gp.current_page_number("<html><body>nothing</body></html>") is None


@check("parse_product_page reads the SoftwareApplication JSON-LD (skipping the BreadcrumbList beside it) and puts G2's 0-10 composite score in rating_10 — NOT rating_5, and never both from one page")
def _():
    url = "https://www.g2.com/products/hubspot-sales-hub/reviews"
    p = gp.parse_product_page(product_html(best_rating=10, rating_value=8.9), url)
    assert p is not None
    assert p.title == "HubSpot Sales Hub"
    assert p.sku == "hubspot-sales-hub" and p.product_slug == "hubspot-sales-hub"
    assert p.rating_10 == 8.9
    assert p.rating_5 is None, "a 0-10 composite score must never land in the 0-5 column"
    assert p.review_count == 14333 and p.review_count_source == "json_ld"
    assert p.image_url and p.image_url.startswith("https://images.g2crowd.com/")
    assert p.price is None and p.currency is None, "a product page carries no price on g2.com"


@check("parse_product_page takes the rating SCALE from the page's own bestRating rather than assuming it: bestRating 5 fills rating_5 (and leaves rating_10 empty), and an unrecognised bestRating leaves BOTH empty rather than guessing which column a number belongs in")
def _():
    url = "https://www.g2.com/products/hubspot-sales-hub/reviews"
    five = gp.parse_product_page(product_html(best_rating=5, rating_value=4.5), url)
    assert five is not None
    assert five.rating_5 == 4.5 and five.rating_10 is None

    ten = gp.parse_product_page(product_html(best_rating=10, rating_value=8.9), url)
    assert ten.rating_10 == 8.9 and ten.rating_5 is None
    # Never both, from one page, in either direction.
    for row in (five, ten):
        assert not (row.rating_5 is not None and row.rating_10 is not None)

    weird = gp.parse_product_page(product_html(best_rating=100, rating_value=87.0), url)
    assert weird is not None
    assert weird.rating_5 is None and weird.rating_10 is None, (
        "an unknown scale must leave both columns empty, not land a number in the wrong one"
    )


@check("parse_product_page returns None (never raises) for a page with no usable SoftwareApplication block, and count_product_page_data is the matching DETAIL-shaped counter")
def _():
    url = "https://www.g2.com/products/x/reviews"
    assert gp.parse_product_page("<html><body>nothing here</body></html>", url) is None
    assert gp.parse_product_page(listing_html(), url) is None
    empty_block = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"SoftwareApplication"}</script></head><body></body></html>'
    )
    assert gp.parse_product_page(empty_block, url) is None, "an empty SoftwareApplication block is not a product"
    assert gp.count_product_page_data(product_html()) == 1
    assert gp.count_product_page_data(listing_html()) == 0
    assert gp.count_product_page_data("") == 0


@check("parse_pricing_page reads the confirmed-real tier line shape, skips the page's own summary sentence and footnote (both of which contain a $ amount), and count_pricing_tiers is the matching PRICING-shaped counter")
def _():
    tiers = gp.parse_pricing_page(PRICING_HTML, "https://www.g2.com/products/hubspot-sales-hub/pricing")
    assert len(tiers) == 4, [t.tier_name for t in tiers]
    names = [t.tier_name for t in tiers]
    assert names == ["Free HubSpot CRM", "Sales Hub Starter", "Sales Hub Professional", "Sales Hub Enterprise"]
    assert tiers[0].price == 0.0 and tiers[0].currency == "USD"
    assert tiers[1].price == 20.0 and tiers[1].price_unit == "1 Core Seat Per Month"
    assert tiers[1].raw_line == "Sales Hub Starter — $20 / 1 Core Seat Per Month"
    assert all("pricing editions" not in (t.raw_line or "").lower() for t in tiers)
    assert gp.count_pricing_tiers(PRICING_HTML) == 4


@check("parse_pricing_page degrades to [] on non-matching HTML rather than raising — it is BEST-EFFORT text parsing (g2.com ships no pricing JSON-LD at all), and an empty list means 'no pricing could be read', never 'this product is free'")
def _():
    for html in ("", "<html><body>Contact us for pricing.</body></html>", listing_html(), product_html(),
                 "<html><body><div>Some Tier — free</div></body></html>"):
        result = gp.parse_pricing_page(html, "https://www.g2.com/products/x/pricing")
        assert result == [], result
        assert gp.count_pricing_tiers(html) == 0
    # A price with no currency symbol is prose, not a tier.
    assert gp.parse_pricing_page("<html><body><div>Starter — 20 per month</div></body></html>") == []


@check("apply_pricing_tiers fills price/currency from the LOWEST priced tier and stamps price_source='pricing_page_text', distinct from the structured sources other family members use, so a consumer can tell a best-effort text-scraped price apart at a glance")
def _():
    product = _mk_product("hubspot-sales-hub")
    assert product.price is None
    tiers = gp.parse_pricing_page(PRICING_HTML)
    returned = gp.apply_pricing_tiers(product, tiers)
    assert returned is product, "documented as mutate-and-return"
    assert product.price == 0.0 and product.currency == "USD"
    assert product.price_source == "pricing_page_text"
    # A no-op when nothing is priced — never a zero written over a real value.
    untouched = _mk_product("other", price=42.0)
    gp.apply_pricing_tiers(untouched, [])
    assert untouched.price == 42.0


@check("diagnose_unexpected_page tells a real listing, a product page and a DataDome wall apart without anyone having to grep a captured page by hand")
def _():
    listing = gp.diagnose_unexpected_page(listing_html(cards=3))
    assert "category-cards=3" in listing and "datadome-tag=yes" in listing
    product = gp.diagnose_unexpected_page(product_html(datadome=False))
    assert "software-application-jsonld=yes" in product and "category-cards=0" in product
    wall = gp.diagnose_unexpected_page(DATADOME_WALL_HTML)
    assert "category-cards=0" in wall and "datadome-tag=yes" in wall
    assert "diagnostic unavailable" not in gp.diagnose_unexpected_page("")


@check("a full listing page round-trips through finish_run() as a clean 'complete' run, with the g2-specific columns intact in both JSON and CSV")
def _():
    products = gp.parse_category_listing(listing_html(cards=4), "crm", "https://www.g2.com/categories/crm")
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=products, out_path=out, fmt="json", engine="playwright",
            url="https://www.g2.com/categories/crm", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        rows = json.loads(Path(out).read_text())
        assert len(rows) == 4
        assert set(rows[0]) == set(output_writer.PRODUCT_FIELD_NAMES)
        assert rows[0]["rating_5"] == 4.4 and rows[0]["rating_10"] is None
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "complete" and meta["engine"] == "playwright"

        out_csv = str(Path(td) / "out.csv")
        output_writer.write_csv(products, out_csv)
        header = Path(out_csv).read_text().splitlines()[0]
        assert header.split(",") == output_writer.PRODUCT_FIELD_NAMES


@check("the pricing-only row an engine builds for a `--url .../pricing` run is identical in all three engines, and is [] when no tier could be read (so the run reports 'empty' rather than writing a row with nothing in it)")
def _():
    url = "https://www.g2.com/products/hubspot-sales-hub/pricing"
    tiers = gp.parse_pricing_page(PRICING_HTML, url)
    rows = [mod._pricing_only_product(url, tiers) for mod in ENGINE_MODULES]
    for row in rows:
        assert len(row) == 1
        p = row[0]
        assert p.sku == "hubspot-sales-hub" and p.product_slug == "hubspot-sales-hub"
        assert p.product_url == "https://www.g2.com/products/hubspot-sales-hub/reviews"
        assert p.price == 0.0 and p.price_source == "pricing_page_text"
        assert p.title is None and p.rating_5 is None, "nothing invented that the page didn't carry"
    for mod in ENGINE_MODULES:
        assert mod._pricing_only_product(url, []) == []


@check(
    "END-TO-END through each engine's own run(): the browserless --scraper-api path, driven by a "
    "fake client that returns the synthetic fixtures, produces a written file plus sidecar and the "
    "right exit code for all three page shapes in all three engines — a category listing "
    "(EXIT_OK), a product /reviews page (EXIT_OK), and a DataDome wall (EXIT_BLOCKED, with NO file "
    "and NO sidecar written, per CLAUDE.md §9). This is the closest this repo can get to an "
    "end-to-end engine test without network access to g2.com."
)
def _():
    import unittest.mock as mock

    class _FakeClient:
        def __init__(self, body, status=200):
            self._body, self._status = body, status

        def scrape_url(self, url, timeout=60):
            return scraper_api_client.ScrapeResult(target_status=self._status, headers={}, body=self._body)

    cases = [
        ("category", ["--category", "crm"], listing_html(cards=4), output_writer.EXIT_OK, 4),
        ("product", ["--product", "hubspot-sales-hub"], product_html(), output_writer.EXIT_OK, 1),
        ("pricing", ["--url", "https://www.g2.com/products/hubspot-sales-hub/pricing"], PRICING_HTML,
         output_writer.EXIT_OK, 1),
        ("blocked", ["--category", "crm"], DATADOME_WALL_HTML, output_writer.EXIT_BLOCKED, 0),
    ]
    for mod in ENGINE_MODULES:
        for label, argv, body, expected_code, expected_rows in cases:
            with tempfile.TemporaryDirectory() as td:
                out = str(Path(td) / "out.json")
                args = _parse(mod, argv + ["--scraper-api", "--twocaptcha-key", "k", "--out", out])
                with mock.patch.object(mod, "TwoCaptchaClient", lambda *a, **kw: _FakeClient(body)):
                    code = asyncio_run_maybe(mod, args)
                assert code == expected_code, f"{mod.__name__}/{label}: got exit {code}, expected {expected_code}"
                if expected_code == output_writer.EXIT_OK:
                    rows = json.loads(Path(out).read_text())
                    assert len(rows) == expected_rows, f"{mod.__name__}/{label}: {len(rows)} rows"
                    meta = json.loads(Path(f"{out}.meta.json").read_text())
                    assert meta["status"] == "complete" and meta["engine"] == mod.ENGINE_NAME
                else:
                    assert not Path(out).exists(), f"{mod.__name__}/{label}: a blocked, zero-product run wrote a file"
                    assert not Path(f"{out}.meta.json").exists(), (
                        f"{mod.__name__}/{label}: a failed run must never leave a sidecar"
                    )


# --------------------------------------------------------------------------- #
# diff_runs — added/removed/changed, refuses a non-complete run
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/price-changed correctly against two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a", price=10.0), _mk_product("b", price=20.0)],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a", price=12.0), _mk_product("c", price=30.0)],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["c"], result["added"]
        assert result["removed"] == ["b"], result["removed"]
        assert len(result["changed"]) == 1 and result["changed"][0]["sku"] == "a"


@check("diff_runs refuses to compare a non-'complete' run rather than silently diffing partial data")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — the old run is 'partial', not 'complete'")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# scraper_api_client — family-shared, unmodified
# --------------------------------------------------------------------------- #
@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api / --scraper-api-url overrides, not the module-level defaults")
def _():
    client = scraper_api_client.TwoCaptchaClient(
        "fakekey", api_base="https://mock.example.test", scraper_api_base="https://mock-scraper.example.test",
    )
    assert client.api_base == "https://mock.example.test" != scraper_api_client.API_BASE
    assert client.scraper_api_base == "https://mock-scraper.example.test" != scraper_api_client.SCRAPER_API_BASE


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
