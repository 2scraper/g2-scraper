#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for the g2-scraper family
member. Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on CLI
flags, exit codes, run status and whether a run crashes or spends money,
CLAUDE.md §4).

**Local-first, same principle as every other family member** — this
launches an ordinary local headless Chromium by default and does NOT
require a 2Captcha Scraping Browser (`--cdp-endpoint`) session to run.
`--proxy` / `--cdp-endpoint` / `--fingerprint` / `--scraper-api` are
opt-in power options, not proven-necessary defaults.

**Read this before trusting a run** (CLAUDE.md §15). g2_parser.py's
selectors, URL shapes and JSON-LD blocks all came from a real, live
capture of www.g2.com — but from a browser-rendering tool, NOT from this
engine. No engine in this repo has ever run against g2.com: the build
environment's egress policy blocks the host outright. Everything below is
code-review-verified and offline-verified against `smoke_test.py`, and
nothing here has been proven end-to-end against the live site.

**g2.com is protected by DataDome — corrected 2026-09-22: it IS solvable.**
This repo originally claimed DataDome had no automated solve path at all;
that was wrong (Roman, from 2Captcha's own team, pointed at
https://2captcha.com/api-docs/datadome-slider-captcha). 2Captcha ships a
dedicated `DataDomeSliderTask` for DataDome's own interstitial slider
challenge (confirmed live: `window.DataDomeJsTag`, `dd.g2.com/js/`,
v5.10.0), and this engine solves it via `--solve-captcha` — but it is the
ONE captcha type in this family with no proxyless path: solving it
requires `--proxy`/`--proxy-file`. Without a proxy configured, a DataDome
slider challenge still reports `unsupported_vendor`/`vendor="datadome"`
(honest, not a bug — see `_maybe_solve_captcha`'s docstring). Whether the
slider iframe pattern this engine detects matches g2.com's ACTUAL live
markup is still unconfirmed — no engine in this family has ever seen
g2.com present it. Note also that DataDome's own tag ships on EVERY
g2.com page, healthy ones included, so a bare marker match is never
treated as a block on its own: a page is only blocked when the marker
matches AND the page's own content is absent (`MIN_CARD_MATCHES` below,
and the page-shaped counters described under `_maybe_solve_captcha`).

Three real page shapes, three parsers, three captcha call sites:

    https://www.g2.com/categories/{slug}[?page=N]   -> scrape_category()
    https://www.g2.com/products/{slug}/reviews      -> scrape_product_page()
    https://www.g2.com/products/{slug}/pricing      -> scrape_pricing_page()

Example:
    python3 playwright_scraper.py --category crm --max-pages 3 --out crm.json
    python3 playwright_scraper.py --product hubspot-sales-hub --with-pricing
    python3 playwright_scraper.py --url "https://www.g2.com/categories/crm?page=2"

Pagination is `?page=N`. The loop reconstructs the next URL and stops on
`--max-pages`, `--max-results`, zero cards, or no NEW sku. G2's Next control
is logged as a diagnostic hint but is never the sole stop condition, so a
selector change cannot silently truncate a successful run.

**`grids.json` enrichment is deliberately NOT wired up here.**
`g2_parser.grids_json_url()` builds the URL for G2's own sanctioned
category-ranking JSON, but its response's field names were never
captured, so this repo ships no parser for it (see g2_parser.py's "Left
for the next stage"). A `--no-enrich-grids` opt-out flag over an
enrichment that cannot actually parse anything would be a documented flag
that silently does nothing — the exact shape CLAUDE.md §17 names as its
own defect class. Whoever captures a real response adds the parser to
g2_parser.py and the flag here, together.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import g2_parser as gp
from captcha_solver import (
    CaptchaType, build_injection_script, detect_from_html, parse_datadome_cookie, solve_when_blocked,
)
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import (
    Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials,
)
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "playwright"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_MS = 1_500  # g2.com is server-rendered Rails/PJAX, NOT an SPA
                           # (confirmed — see g2_parser.py): product data is in
                           # the delivered HTML and nothing has to hydrate
                           # before it is readable, so this is materially
                           # SHORTER than an SPA sibling's (shein-scraper uses
                           # 3s). What it buys is slack for DataDome's own
                           # asynchronous checks to settle, not for content.
MIN_CARD_MATCHES = gp.MIN_CARD_MATCHES  # 2 — a single stray card-shaped
                                        # element must not read as a listing.

# Every `solve_when_blocked()` outcome that means "this page is still
# gated". `unsupported_vendor` belongs here as much as the others: for
# g2.com it is the EXPECTED outcome of a real block (DataDome), and it is
# reported as EXIT_BLOCKED exactly like the rest — the only difference is
# that the log line names the vendor instead of saying "no known widget
# could be extracted", which would be a misleading way to describe a
# defense that has no widget to extract in the first place. Restored from
# skyscanner-scraper, which hit the identical situation with PerimeterX.
STILL_BLOCKED_ACTIONS = (
    "warning_no_key",
    "warning_no_proxy",
    "warning_proxy_banned",
    "warning_solver_error",
    "detected_unidentified_widget",
    "unsupported_vendor",
)

log = logging.getLogger("playwright_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def _nonnegative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="g2.com B2B software listing/review scraper — Playwright engine",
        epilog="Credentials belong in .env / G2_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="Full g2.com category/product/pricing URL (or set G2_URL) — overrides --category/--product")
    p.add_argument("--category", default=None, help="A category slug, e.g. 'crm' (https://www.g2.com/categories/crm)")
    p.add_argument("--product", default=None, help="A product slug, e.g. 'hubspot-sales-hub' — its canonical page always ends in /reviews")
    p.add_argument("--max-results", type=_positive_int, default=60, help="Cap on number of products collected")
    p.add_argument("--max-pages", type=_positive_int, default=5, help="Hard cap on ?page=N listing pages requested")
    p.add_argument("--page-delay", type=_nonnegative_float, default=1.5, help="Delay between listing pages, seconds")
    p.add_argument(
        "--with-pricing", action="store_true",
        help="Also fetch each collected product's /pricing page and fill price/currency/price_source "
             "from its cheapest tier. OFF by default because it costs one extra request per product "
             "and because g2_parser.parse_pricing_page() is explicitly the least reliable parser in "
             "this repo (g2.com ships NO pricing JSON-LD — it is line-shape text parsing). An "
             "unreadable pricing page leaves the row's price empty; it never fails the run.",
    )
    p.add_argument("--pricing-limit", type=_positive_int, default=10, help="With --with-pricing, fetch at most this many pricing pages")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None, help="Output path (default: g2_results.<format>)")
    p.add_argument("--retries", type=_nonnegative_int, default=2, help="Retries on a page's navigation failure")
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument(
        "--block-retries", type=_nonnegative_int, default=2,
        help="On a blocked, zero-product outcome, retry the whole scrape this many extra times. "
             "With --proxy-file each attempt advances to the next live proxy and creates a fresh "
             "context; direct/CDP runs reuse their fixed exit identity.",
    )
    p.add_argument("--proxy", default=None, help="A single proxy, e.g. http://login:pass@host:port (or set G2_PROXY)")
    p.add_argument("--proxy-file", default=None, help="One proxy per line, same formats as --proxy")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None, help="(or set TWOCAPTCHA_KEY)")
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument(
        "--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked",
        help="g2.com's confirmed defense is DataDome. This flag governs whether 2Captcha's "
             "DataDomeSliderTask is attempted when a slider challenge is detected — REQUIRES "
             "--proxy/--proxy-file, since that task type has no proxyless path — and also arms "
             "the Scraping Browser API's own auto-solve over --cdp-endpoint. NOTE: those two are "
             "mutually exclusive, not additive — --cdp-endpoint nulls out any local proxy, so "
             "DataDomeSliderTask never runs there; DataDome coverage under --cdp-endpoint depends "
             "entirely on 2Captcha's own auto-solve, which is unconfirmed for DataDome specifically "
             "(see captcha_solver.py). Without a proxy and without --cdp-endpoint, a DataDome "
             "challenge is reported as blocked, not solved.",
    )
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set G2_CDP_ENDPOINT) — opt-in, not required for a normal run")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows,Chrome'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument(
        "--scraper-api", action="store_true",
        help="Fetch via 2Captcha's Scraper API (scraper.2captcha.com) instead of launching any local "
             "or --cdp-endpoint browser — a single browserless HTTP call run entirely on 2Captcha's "
             "own infrastructure. Requires --twocaptcha-key/TWOCAPTCHA_KEY. A GENUINELY DIFFERENT "
             "product from --cdp-endpoint's Scraping Browser API — see scraper_api_client.py. NOT "
             "confirmed against g2.com (nothing in this repo is), and NOT a documented DataDome "
             "bypass. --max-pages/--page-delay/--proxy/--cdp-endpoint/--fingerprint are IGNORED in "
             "this mode (a single static fetch has no pagination loop, and brings its own exit "
             "IP/device) — set together, they log a warning rather than silently doing nothing. By "
             "itself this mode has no captcha solving (a solved token has nothing to inject into — "
             "no live page/DOM here) and no documented way to pin the exit country/locale. See "
             "--scraper-api-cdp for the fix to both.",
    )
    p.add_argument("--scraper-api-timeout", type=_positive_int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument(
        "--scraper-api-cdp", action="store_true",
        help="Route --scraper-api's fetch through a 2Captcha Scraping Browser CDP session (their "
             "'cdpurl' field on the Scraper API task) instead of their own default browser pool — "
             "chaining two 2Captcha products together, not pointing this at a caller-supplied "
             "--cdp-endpoint (that flag stays ignored in --scraper-api mode, see its help text: an "
             "arbitrary CDP session isn't known to support this field the way 2Captcha's own does). "
             "This is what actually gets --scraper-api real captcha auto-solve on g2.com's DataDome "
             "challenge and exit-country pinning — see scraper_api_client.TwoCaptchaClient."
             "scraping_browser_connection_url and scrape_url's own docstrings for exactly what "
             "2Captcha documents. Requires --scraper-api. WIRED BUT NOT YET LIVE-TESTED: the "
             "underlying 'cdpurl' field is documented by 2Captcha but this codebase had never "
             "exercised it before this flag existed — confirm it live before relying on it "
             "(TESTING.md).",
    )
    p.add_argument("--scraper-api-country", default=None, help="Exit country for --scraper-api-cdp's Scraping Browser session, e.g. 'us' (ignored without --scraper-api-cdp)")
    p.add_argument("--scraper-api-profile-id", default=None, help="Reuse a specific Scraping Browser profile id across runs for --scraper-api-cdp, instead of the default pool (ignored without --scraper-api-cdp; see scraping_browser_connection_url's docstring on why reuse is preferred)")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero products were found")
    p.add_argument("--dump-html", action="store_true", help="Save the last fetched page's HTML next to --out, on success too")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"g2_results.{fmt}"


def _resolve_start_url(args: argparse.Namespace) -> Tuple[Optional[str], str]:
    """Returns `(url, mode)`. `mode` is one of `"category"`, `"product"`,
    `"pricing"` when `url` is usable, or one of `"disallowed"`,
    `"unknown_shape"`, `"missing"` when `url` is None and run() needs to
    print the matching error.

    Two guards, in this order, both from g2_parser.py: robots.txt
    (`is_disallowed_path`, honouring the STRICTER ClaudeBot rule set by
    default — see that function's docstring), then the allowlist of URL
    shapes this repo actually has a parser for (`is_known_scrape_target`).
    Refusing an unparseable shape up front beats fetching a page just to
    fail to read it.
    """
    if args.url:
        if gp.is_disallowed_path(args.url):
            return None, "disallowed"
        if not gp.is_known_scrape_target(args.url):
            return None, "unknown_shape"
        if "/grids.json" in args.url:
            # `is_known_scrape_target()` accepts this shape because it IS a
            # real, sanctioned g2.com endpoint — but g2_parser.py ships no
            # parser for the response (its field names were never
            # captured; see "Left for the next stage"). Refusing here with
            # a clear message beats fetching JSON and reporting a confusing
            # zero-card listing.
            return None, "grids_unsupported"
        if "/pricing" in args.url:
            return args.url, "pricing"
        if "/products/" in args.url:
            return args.url, "product"
        return args.url, "category"
    if args.category:
        return gp.category_url(args.category), "category"
    if args.product:
        return gp.product_url(args.product), "product"
    return None, "missing"


def _dump_path(out_path: str) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug.html"


async def _new_context(browser: Browser, proxy: Optional[Proxy], user_agent: Optional[str]) -> BrowserContext:
    kwargs = {}
    if proxy is not None:
        # CLAUDE.md §3: credentials go into Playwright's own authenticated
        # proxy dict, never into an argv flag.
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    if user_agent:
        kwargs["user_agent"] = user_agent
    return await browser.new_context(**kwargs)


async def _enable_scraping_browser_auto_solve(context: BrowserContext, page: Page) -> None:
    """Arm 2Captcha's Scraping Browser `Captcha.setAutoSolve` CDP domain.

    Only meaningful over `--cdp-endpoint` (a locally-launched Chromium has
    no such domain and logs one warning). **Every function in this engine
    that loads a real page calls this when `autosolve` is set** —
    `scrape_category()`, `scrape_product_page()` AND `scrape_pricing_page()`
    — because a page that silently never armed auto-solve is a coverage gap
    a user only discovers from a failed run. shein-scraper shipped exactly
    that gap (its puppeteer `scrape_product_page()` accepted an `autosolve`
    parameter and never used it) and smoke_test.py now enforces the
    invariant structurally, at AST level, for this repo's three engines.
    """
    try:
        session = await context.new_cdp_session(page)
        session.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        session.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        session.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str,
    count_product_links, min_score: float = 0.3, page: Optional[Page] = None,
    proxy: Optional[Proxy] = None, user_agent: Optional[str] = None,
) -> Optional[dict]:
    """`count_product_links` is REQUIRED and has NO default, on purpose.

    It answers "does this page already have the real content it is
    supposed to have, so don't pay for a solve" — which is a different
    question on each of this repo's three page shapes, and g2_parser.py
    ships one counter per shape:

        category listing page  ->  gp.count_result_cards
        product /reviews page  ->  gp.count_product_page_data
        /pricing page          ->  gp.count_pricing_tiers

    Passing the listing-shaped counter on a detail page returns 0 by
    construction on every completely normal load, which turns any stray
    marker into a false "possibly blocked" read and, when a real sitekey
    also happens to be present, into a wasted PAID 2Captcha solve. That is
    a shipped-and-fixed bug in shein-scraper (2026-09-22), written up in
    g2_parser.py's module docstring as bug class 2, and it is why
    `captcha_solver.solve_when_blocked()` made this argument keyword-only
    with no default — and why this wrapper does the same rather than
    re-introducing a default one call site happens to be right about.
    On g2.com getting this wrong on the LISTING/DETAIL/PRICING split still
    means a false warning rather than money either way, since DataDome's
    own tag is present on every page and only a real slider iframe (see
    below) is ever billable — the habit is what keeps the family from
    re-shipping the expensive version.

    `proxy` / `user_agent` — added 2026-09-22 alongside
    `captcha_solver.CaptchaType.DATADOME_SLIDER` (this repo's original
    claim that DataDome had no automated solve path at all was WRONG;
    2Captcha ships a dedicated `DataDomeSliderTask` — see
    captcha_solver.py's module docstring for the full correction). Both
    are forwarded to `solve_when_blocked()` unchanged for every OTHER
    captcha type (they're simply ignored there) — they matter only the
    moment a page turns out to be a DataDome slider challenge, the one
    type in this family with no proxyless path at all. Pass whichever
    `Proxy` (or `None`) and UA string THIS call's own page/context is
    actually using.
    """
    if policy == "off" or client is None:
        return None
    # Bug found live-testing 2026-09-22: `user_agent` was ONLY ever
    # non-None via --fingerprint (see run()) — meaning a run with
    # --proxy/--twocaptcha-key/--solve-captcha but WITHOUT --fingerprint
    # could never solve a real DataDome slider at all, always hitting
    # captcha_solver.py's "requires user_agent" guard, even though neither
    # README nor TESTING.md ever documented --fingerprint as a
    # prerequisite (only a proxy is documented as required). Falling back
    # to the ACTUAL live page's own navigator.userAgent when no
    # --fingerprint UA was supplied is not just a workaround — 2Captcha's
    # own docs say to send "the SAME modern browser UA the challenge will
    # be presented back to", and the real page's own UA is a strictly more
    # correct answer to that than a Fingerprint-API string could ever be
    # when the two might not even match the browser actually solving it.
    effective_user_agent = user_agent
    if effective_user_agent is None and page is not None:
        try:
            effective_user_agent = await page.evaluate("navigator.userAgent")
        except Exception as exc:  # noqa: BLE001 — best-effort fallback only
            log.warning("Could not read the live page's navigator.userAgent for a DataDome solve attempt: %s", exc)
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=count_product_links,
        extra_markers=gp.BOT_CHALLENGE_MARKERS, min_score=min_score,
        proxy=proxy.to_2captcha_task_dict() if proxy else None, user_agent=effective_user_agent,
    )
    action = result.get("action")
    if action == "no_captcha_detected":
        pass
    elif action == "skipped_products_present":
        log.info("Captcha-like marker present but this page's content is already rendered — not solving.")
    elif action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_no_proxy":
        # Pre-existing gap, found and fixed 2026-09-22 alongside
        # warning_proxy_banned below: this action had NO branch at all
        # here before today — captcha_solver.py has returned it since
        # DataDomeSliderTask's proxy-required guard was added, but nothing
        # ever logged it, so a DataDome challenge with no --proxy set
        # reported EXIT_BLOCKED (via the separate captcha_detected-and-no-
        # cards check above, unaffected by this gap) with no explanation
        # of why a solve was never even attempted.
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_proxy_banned":
        # Confirmed live, 2026-09-22 (Roman, real proxy-mode run against
        # g2.com — see captcha_solver.py's module docstring): 2Captcha
        # itself refuses a DataDomeSliderTask when the challenge URL's own
        # `t` marker says DataDome already has this exact proxy exit
        # flagged. Retrying this exact exit will not help. With --proxy-file,
        # the next --block-retries attempt advances the pool automatically;
        # a single --proxy value must be replaced by the operator.
        log.warning(
            "Captcha solve refused by 2Captcha (this proxy's exit IP is already flagged by "
            "DataDome, not a transient error): %s — a different --proxy/--proxy-file exit "
            "(a fresh session) is the fix; retrying on this same one will not help.",
            result.get("detail"),
        )
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
        if result.get("captcha_type") == CaptchaType.DATADOME_SLIDER.value:
            # DataDome's solution is a Set-Cookie-shaped string, not a
            # token for a hidden field — build_injection_script() returns
            # None for this type on purpose (see its docstring);
            # parse_datadome_cookie() is its counterpart, and applying it
            # means the browser's own COOKIE JAR, not the DOM. A cookie
            # alone changes nothing until the NEXT request carries it, so
            # (unlike the generic branch below) this also reloads.
            if page is not None:
                try:
                    cookie = parse_datadome_cookie(result["token"])
                    host = (urlparse(url).hostname or "").lower()
                    apex = host[4:] if host.startswith("www.") else host
                    # No Domain attribute rides in 2Captcha's own example
                    # response — this defaults to the parent domain
                    # (".g2.com", not ".www.g2.com") because that's
                    # DataDome's own real-world convention (one cookie
                    # covers every subdomain), NOT something confirmed
                    # against an actual g2.com Set-Cookie header, which no
                    # engine in this family has ever captured (see
                    # captcha_solver.py's module docstring on this same
                    # unconfirmed-vendor-convention posture applying here).
                    cookie["domain"] = f".{apex}" if apex else host
                    cookie["sameSite"] = cookie.pop("same_site", "Lax")
                    await page.context.add_cookies([cookie])
                    log.info(
                        "Applied the solved DataDome cookie to the browser context (domain=%s) "
                        "and reloading — this repo has never seen g2.com actually present this "
                        "challenge, so whether the reload then clears it is UNCONFIRMED.",
                        cookie["domain"],
                    )
                    await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    await page.wait_for_timeout(READINESS_WAIT_MS)
                except Exception as exc:  # noqa: BLE001 — solved-but-apply-failed degrades, never crashes
                    log.warning("DataDome solved but applying the cookie / reloading failed: %s", exc)
        else:
            # Actually write the solution back into the page — see
            # captcha_solver.build_injection_script's docstring for the honesty
            # caveat: it uses each widget's own STANDARD, publicly documented
            # convention, never anything confirmed against a real g2.com
            # capture. reCAPTCHA v3 has no such convention and returns None.
            if page is not None:
                script = build_injection_script(CaptchaType(result["captcha_type"]), result["token"])
                if script is None:
                    log.info(
                        "No generic injection point for %s — token was solved but not written into "
                        "the page (this is expected for reCAPTCHA v3; see captcha_solver.py).",
                        result.get("captcha_type"),
                    )
                else:
                    try:
                        injected = await page.evaluate(script)
                        log.info(
                            "Injected solved %s into the page (found a target element/callback: %s) "
                            "— unconfirmed whether a real g2.com widget reads this "
                            "standard-convention field/callback.",
                            result.get("captcha_type"), bool(injected),
                        )
                    except Exception as exc:  # noqa: BLE001 — a failed injection degrades, never crashes the run
                        log.warning("Captcha solved but injecting it into the page failed: %s", exc)
    elif action == "unsupported_vendor":
        # The EXPECTED outcome on a healthy g2.com page load. For
        # "datadome" specifically this does NOT mean unsolvable any more
        # (corrected 2026-09-22 — see captcha_solver.py's module
        # docstring): it means DataDome's always-present tag was seen but
        # no slider iframe was on THIS page, so there's nothing for
        # DataDomeSliderTask to solve right now. For other vendors in this
        # bucket (PerimeterX, a Cloudflare managed challenge) it still
        # means "confirmed, no 2Captcha task type exists at all."
        log.warning(
            "%s marker present, no currently-solvable challenge found on this page — reporting "
            "this run as blocked. If this vendor is 'datadome' and a slider challenge was expected, "
            "confirm --proxy/--proxy-file is set (DataDomeSliderTask has no proxyless path); "
            "otherwise --block-retries retries the scrape (and advances a --proxy-file pool), "
            "or use a different "
            "--proxy exit, or a --cdp-endpoint session with its own device identity.",
            result.get("vendor"),
        )
    elif action == "detected_unidentified_widget":
        log.warning(
            "A bot-mitigation marker was detected but no known widget/sitekey could be extracted. "
            "On g2.com the expected block is DataDome, which reports as 'unsupported_vendor' "
            "instead — seeing THIS message means something other than the confirmed defense "
            "appeared; re-run with --dump-html and inspect the captured page."
        )
    return result


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """CLAUDE.md §8: `connect_over_cdp` repeats a failed
    `ws://login:password@…` endpoint in its own message AND in a four-line
    "Call log" — up to five copies of a live credential in one exception.
    Redact, and drop the original from the chain (`from None`) so its
    unredacted text cannot resurface via "the above exception was the
    direct cause of…"."""
    try:
        return await pw.chromium.connect_over_cdp(cdp_endpoint)
    except Exception as exc:
        raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None


async def _goto_with_retries(page: Page, url: str, *, retries: int, retry_delay: float,
                              proxy_pool: Optional[ProxyPool] = None, proxy: Optional[Proxy] = None,
                              ) -> Tuple[Optional[int], Optional[str]]:
    """Returns `(http_status, last_error)`. `last_error is not None` means
    every attempt failed — the caller treats that as a remote/navigation
    failure for THAT page, never a crash (CLAUDE.md §6).

    Parity fix 2026-09-23: `selenium_scraper.py`/`puppeteer_scraper.py`'s
    versions of this function have always been proxy-pool aware — they
    call `is_proxy_dead_error()` on a failed attempt and
    `proxy_pool.report_failure(proxy, dead=True)` when it matches one of
    `PROXY_DEAD_MARKERS`, so a proxy that Chromium itself reports as
    unreachable gets excluded from rotation immediately rather than
    waiting on `--block-retries`. This engine's version was missing that
    entirely (no `proxy_pool`/`proxy` params at all) — a real CLAUDE.md §4
    parity gap, found while investigating why a `--proxy-file` run wasn't
    rotating cleanly."""
    last_error: Optional[str] = None
    status: Optional[int] = None
    for attempt in range(retries + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            status = response.status if response is not None else None
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            message = redact_credentials(str(exc))
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, retries + 1, url, message)
            if attempt < retries:
                await asyncio.sleep(retry_delay)
    return status, last_error


async def scrape_category(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """Paginate a category listing via `?page=N`.

    Returns `(products, blocked, remote_api_error, pages_completed,
    failed_pages)` — the same 5-tuple every scrape function in this repo
    (and its siblings) returns, so run() treats them uniformly.

    CLAUDE.md §6: one page that fails to load or fails to parse is recorded
    in `failed_pages` and the loop moves on; it never discards the pages
    that already succeeded. That is what turns into EXIT_PARTIAL rather
    than losing a whole run to one bad page.
    """
    blocked = False
    remote_api_error = False
    failed_pages: List[int] = []
    pages_completed = 0

    slug = gp.category_slug_from_url(start_url) or args.category
    # Bug fixed 2026-09-22: this loop used to always start at page_num=1,
    # silently discarding any `?page=N` the caller's own `--url` carried —
    # see gp.page_number_from_url()'s docstring for the full story (its own
    # docstring example, `--url ".../categories/crm?page=2"`, didn't work).
    start_page = gp.page_number_from_url(start_url) if slug else 1
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    context = await _new_context(browser, proxy, user_agent)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    seen_skus: set = set()
    pages: List[List[Product]] = []
    last_html = ""

    for page_num in range(start_page, start_page + args.max_pages):
        page_url = gp.category_url(slug, page_num) if slug else start_url
        status, last_error = await _goto_with_retries(
            page, page_url, retries=args.retries, retry_delay=args.retry_delay,
            proxy_pool=proxy_pool, proxy=proxy,
        )
        if last_error is not None:
            log.error("Listing page %d permanently failed to load: %s", page_num, last_error)
            failed_pages.append(page_num)
            if page_num == start_page:
                if proxy_pool is not None and proxy is not None:
                    # Bug fixed 2026-09-23: this used to be an unconditional
                    # remote_api_error, which run()'s block_attempt loop
                    # treats as fatal (breaks immediately, same as a crash)
                    # — that defeated --proxy-file rotation entirely the
                    # moment ANY one proxy in the list failed to load the
                    # very first page (confirmed live: a run with 10 fresh
                    # proxies and --block-retries 9 stopped after a single
                    # navigation timeout on attempt 6, never trying 7-10).
                    # A different proxy/exit may well succeed where this
                    # one didn't, so treat it like a block instead — that
                    # lets the caller's --block-retries loop rotate to
                    # proxy_pool.next() rather than aborting the whole run.
                    # _goto_with_retries() already reported a hard dead-proxy
                    # marker (if this was one) via report_failure(dead=True);
                    # a generic timeout isn't one of those and stays in
                    # rotation for a later attempt.
                    blocked = True
                    break
                # No alternative identity exists (no proxy pool, or a
                # --cdp-endpoint session providing its own exit) — nothing
                # was ever collected and the very first request never
                # completed: that's a remote/transport failure for the
                # whole run (EXIT_REMOTE_API_ERROR), not an empty category
                # and not a crash.
                remote_api_error = True
                break
            continue

        try:
            html = await page.content()
        except Exception as exc:  # noqa: BLE001 — one unreadable page degrades to a failed page
            log.warning("Could not read page %d's content: %s", page_num, exc)
            failed_pages.append(page_num)
            continue
        last_html = html

        if status == 404 and page_num > start_page:
            log.info("Listing probe page %d returned HTTP 404 — treating it as catalogue exhaustion.", page_num)
            break
        if status is not None and status >= 400:
            # DataDome answers with a 403 rather than a redirect, so a >=400
            # here is the single most direct block signal this engine has.
            log.warning("Listing page %d returned HTTP %d — treating as blocked, not empty.", page_num, status)
            blocked = True
            if proxy_pool is not None and proxy is not None and status in (403, 429):
                proxy_pool.report_failure(proxy, dead=True)
        elif status is not None and proxy_pool is not None and proxy is not None:
            proxy_pool.report_success(proxy)

        # DataDome's own tag is on EVERY g2.com page, healthy ones included
        # (confirmed), and `GENERIC_BOT_CHALLENGE_MARKERS` already contains
        # "datadome" — so a bare marker match means nothing by itself here.
        # A page counts as blocked only when the marker matches AND the
        # page failed to render its own content (MIN_CARD_MATCHES, the
        # site's own "did this actually render results" threshold).
        cards_present = gp.count_result_cards(html) >= MIN_CARD_MATCHES
        captcha_detected = detect_from_html(html, gp.BOT_CHALLENGE_MARKERS)
        if captcha_detected and not cards_present:
            blocked = True
        if captcha_detected:
            captcha_result = await _maybe_solve_captcha(
                html=html, url=page_url, client=client, policy=args.solve_captcha,
                min_score=args.min_score, page=page,
                # LISTING-page-shaped counter — see _maybe_solve_captcha's docstring.
                count_product_links=gp.count_result_cards,
                proxy=proxy, user_agent=user_agent,
            )
            if captcha_result and captcha_result.get("action") == "solved":
                # Added 2026-09-22 alongside DataDome support: a cookie-based
                # solve (or a token one, for that matter) benefits THIS
                # page_num only if something re-reads the page afterward —
                # without this, a solve here only ever paid off starting
                # next page_num, silently wasting the page it was solved
                # for. Matches scrape_product_page/scrape_pricing_page's
                # existing post-solve re-read.
                await page.wait_for_timeout(READINESS_WAIT_MS)
                html = await page.content()
            if captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
                if gp.count_result_cards(html) == 0:
                    blocked = True

        parse_result = gp.parse_category_listing_safely(
            html, category_slug=slug, page_url=page_url,
        )
        if parse_result.failed:
            if page_num not in failed_pages:
                failed_pages.append(page_num)
            log.error("Listing page %d could not be parsed; continuing with the next reconstructed page URL.", page_num)
            continue
        products = parse_result.products
        if not products:
            if page_num == start_page and not blocked:
                log.warning(
                    "No products recognised on the first listing page (%s) — either this category "
                    "genuinely has no results, g2_parser.py's card selectors need updating for the "
                    "current g2.com markup, or g2.com served a different page than the listing for "
                    "this request. Re-run with --dump-html to inspect the captured page.",
                    gp.diagnose_unexpected_page(html),
                )
            else:
                log.info("Listing page %d yielded zero cards — treating that as the end of the category.", page_num)
            if page_num == start_page:
                pages_completed += 1
            break

        pages_completed += 1
        new_skus = {_sku_key(p) for p in products} - seen_skus
        if not new_skus:
            # A page that repeats only already-seen products isn't empty,
            # but isn't new either — output_writer.sku_key's own docstring
            # names this exact distinction. Stop rather than loop.
            log.info("Listing page %d added no new products — stopping pagination.", page_num)
            break
        seen_skus |= new_skus
        pages.append(products)
        if products:
            blocked = False

        if sum(len(pg) for pg in pages) >= args.max_results:
            break
        if page_num >= start_page + args.max_pages - 1:
            break
        if not gp.has_next_page(html):
            log.info(
                "Page %d's pagination advertises no Next page; probing the reconstructed next URL "
                "and stopping only when it yields no new product data.", page_num,
            )
        await asyncio.sleep(args.page_delay)

    merged = merge_pages(pages)[: args.max_results]

    if args.with_pricing and merged:
        await _enrich_with_pricing(
            args=args, products=merged, browser=browser, proxy_pool=proxy_pool,
            client=client, autosolve=autosolve, user_agent=user_agent,
        )

    if args.dump_html and last_html:
        Path(_dump_path(args.out)).write_text(last_html, encoding="utf-8")

    await context.close()
    return merged, blocked, remote_api_error, pages_completed, failed_pages


async def scrape_product_page(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """A `--url`/`--product` pointed at one product's canonical `/reviews`
    page, parsed from its `SoftwareApplication` JSON-LD. Same 5-tuple
    contract as scrape_category()."""
    blocked = False
    proxy = proxy_pool.next() if proxy_pool else None
    context = await _new_context(browser, proxy, user_agent)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    status, last_error = await _goto_with_retries(
        page, start_url, retries=args.retries, retry_delay=args.retry_delay,
        proxy_pool=proxy_pool, proxy=proxy,
    )
    if last_error is not None:
        await context.close()
        log.error("Product page permanently failed to load: %s", last_error)
        if proxy_pool is not None and proxy is not None:
            return [], True, False, 0, [1]
        return [], False, True, 0, [1]

    if status is not None and status >= 400:
        log.warning("Product page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True

    html = await page.content()
    if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS):
        captcha_result = await _maybe_solve_captcha(
            html=html, url=start_url, client=client, policy=args.solve_captcha,
            min_score=args.min_score, page=page,
            # DETAIL-page-shaped counter. Passing gp.count_result_cards here
            # would read 0 on every perfectly normal product page (a detail
            # page has no listing cards by construction) — bug class 2 in
            # g2_parser.py's module docstring.
            count_product_links=gp.count_product_page_data,
            proxy=proxy, user_agent=user_agent,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            # There is no pagination loop here to pick the injection up on a
            # later round, so re-read the page after a beat — otherwise a
            # solved token is one nothing ever re-reads.
            await page.wait_for_timeout(READINESS_WAIT_MS)
            html = await page.content()
        elif captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
            if gp.count_product_page_data(html) == 0:
                blocked = True

    product = gp.parse_product_page(html, url=start_url)
    products = [product] if product else []

    if products and args.with_pricing:
        await _enrich_with_pricing(
            args=args, products=products, browser=browser, proxy_pool=proxy_pool,
            client=client, autosolve=autosolve, user_agent=user_agent,
        )

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
    await context.close()

    if not products and not blocked:
        log.warning(
            "Product page rendered but no usable SoftwareApplication JSON-LD was found (%s) — "
            "see g2_parser.parse_product_page(). Re-run with --dump-html to inspect the page.",
            gp.diagnose_unexpected_page(html),
        )
    return products, blocked, False, 1, []


async def scrape_pricing_page(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> Tuple[List[gp.PricingTier], bool, bool]:
    """Load ONE `/products/{slug}/pricing` page and return
    `(tiers, blocked, remote_api_error)`.

    This is the third real page shape, and it gets the same captcha
    treatment as the other two — including arming `Captcha.setAutoSolve`
    when asked. DataDome could in principle gate any page on this site, so
    "the pricing page is only an enrichment" is not a reason to leave a
    page-loading path uncovered.

    Tiers come back EMPTY, never as an exception, when the page doesn't
    match the expected line shape (g2.com ships no pricing JSON-LD —
    g2_parser.parse_pricing_page() is explicitly the least reliable parser
    in this repo). An empty list means "no pricing could be read", never
    "this product is free".
    """
    proxy = proxy_pool.next() if proxy_pool else None
    context = await _new_context(browser, proxy, user_agent)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    status, last_error = await _goto_with_retries(
        page, start_url, retries=args.retries, retry_delay=args.retry_delay,
        proxy_pool=proxy_pool, proxy=proxy,
    )
    if last_error is not None:
        await context.close()
        log.warning("Pricing page failed to load: %s", last_error)
        if proxy_pool is not None and proxy is not None:
            return [], True, False
        return [], False, True

    blocked = bool(status is not None and status >= 400)
    html = await page.content()
    if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS):
        captcha_result = await _maybe_solve_captcha(
            html=html, url=start_url, client=client, policy=args.solve_captcha,
            min_score=args.min_score, page=page,
            # PRICING-page-shaped counter (bug class 2, g2_parser.py).
            count_product_links=gp.count_pricing_tiers,
            proxy=proxy, user_agent=user_agent,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            await page.wait_for_timeout(READINESS_WAIT_MS)
            html = await page.content()
        elif captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
            if gp.count_pricing_tiers(html) == 0:
                blocked = True

    tiers = gp.parse_pricing_page(html, url=start_url)
    await context.close()
    return tiers, blocked, False


async def _enrich_with_pricing(
    *, args: argparse.Namespace, products: List[Product], browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> None:
    """`--with-pricing`: fill price/currency/price_source in place from each
    product's own `/pricing` page, up to `--pricing-limit` products.

    Never fails the run: a pricing page that is blocked, unreachable or
    simply unreadable leaves that row's price empty and logs it. The
    enrichment is additive by definition, so degrading it is always
    preferable to losing the listing/detail rows it was decorating."""
    filled = 0
    attempted = 0
    for product in products[: args.pricing_limit]:
        slug = product.product_slug or gp.slug_from_product_url(product.product_url)
        if not slug:
            continue
        attempted += 1
        try:
            tiers, blocked, remote_error = await scrape_pricing_page(
                args=args, start_url=gp.pricing_url(slug), browser=browser,
                proxy_pool=proxy_pool, client=client, autosolve=autosolve, user_agent=user_agent,
            )
        except Exception as exc:  # noqa: BLE001 — enrichment never costs the rows it decorates
            log.warning("Pricing lookup for %s raised — leaving its price empty: %s", slug, exc)
            continue
        if blocked or remote_error:
            log.warning("Pricing page for %s was blocked/unreachable — leaving its price empty.", slug)
            continue
        if tiers:
            gp.apply_pricing_tiers(product, tiers)
            filled += 1
        await asyncio.sleep(args.page_delay)
    if attempted:
        log.info("--with-pricing: filled a price for %d of %d products looked up.", filled, attempted)


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, mode: str, client: TwoCaptchaClient,
    cdp_url: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """`--scraper-api`'s own fetch path: one browserless HTTP call to
    2Captcha's Scraper API — no local/CDP browser, no pagination loop (a
    single static snapshot cannot paginate itself), no live DOM to inject
    a solved token into. Returns the same 5-tuple as the browser paths.

    `--solve-captcha` is a documented no-op in this mode for exactly that
    reason: solving would hand back a token with nowhere to put it.
    `--block-retries` (retry the same call) is the only mitigation this
    mode has without `cdp_url` — on g2.com even a live browser has no
    DataDome solve.

    `cdp_url`, when --scraper-api-cdp set one (see run()), routes this
    fetch through 2Captcha's own Scraping Browser instead of their
    default pool — real captcha auto-solve for the DataDome challenge and
    country pinning happen on 2Captcha's side of that session, not in
    this function; there is nothing this function itself needs to do
    differently to benefit from it beyond passing it through."""
    try:
        result = client.scrape_url(start_url, timeout=args.scraper_api_timeout, cdp_url=cdp_url)
    except TwoCaptchaAuthError as exc:
        log.error("Scraper API: %s", exc)
        return [], False, True, 0, []
    except TwoCaptchaError as exc:
        log.error("Scraper API request failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, []

    html = result.body
    blocked = False
    if result.target_status is not None and result.target_status >= 400:
        log.warning("Scraper API: target page returned HTTP %d — treating as blocked.", result.target_status)
        blocked = True

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")

    if mode == "product":
        product = gp.parse_product_page(html, url=start_url)
        products = [product] if product else []
        # Same page-shaped reasoning as the browser paths, just without a
        # solve step: a marker match only means "blocked" when the page's
        # own content is missing. DataDome's tag is on every g2.com page.
        if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS) and gp.count_product_page_data(html) == 0:
            blocked = True
        if not products and not blocked:
            log.warning(
                "Product page fetched via Scraper API but no usable SoftwareApplication JSON-LD "
                "was found (%s).", gp.diagnose_unexpected_page(html),
            )
        return products, blocked, False, 1, []

    if mode == "pricing":
        tiers = gp.parse_pricing_page(html, url=start_url)
        if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS) and gp.count_pricing_tiers(html) == 0:
            blocked = True
        products = _pricing_only_product(start_url, tiers)
        if not products and not blocked:
            log.warning(
                "Pricing page fetched via Scraper API but no recognisable tier lines were found "
                "(%s) — this parser is best-effort text matching, see g2_parser.py.",
                gp.diagnose_unexpected_page(html),
            )
        return products, blocked, False, 1, []

    slug = gp.category_slug_from_url(start_url) or args.category
    parse_result = gp.parse_category_listing_safely(html, category_slug=slug, page_url=start_url)
    if parse_result.failed:
        return [], blocked, False, 0, [gp.page_number_from_url(start_url) or 1]
    products = parse_result.products
    if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS) and gp.count_result_cards(html) < MIN_CARD_MATCHES:
        blocked = True
    if not products and not blocked:
        log.warning(
            "No products recognised in the Scraper API response (%s) — inspect the fetched page "
            "with --dump-html before assuming a parser regression.",
            gp.diagnose_unexpected_page(html),
        )
    return products[: args.max_results], blocked, False, 1, []


def _pricing_only_product(start_url: str, tiers) -> List[Product]:
    """A `--url .../pricing` run has no listing card and no
    `SoftwareApplication` JSON-LD to build a row from — only the slug in
    the URL and whatever tiers were parsed. This builds the one honest row
    that can be made from that: identity fields from the URL, price fields
    from the cheapest tier, everything else left empty rather than
    invented. Returns [] when no tier was readable, so the run reports
    `empty` rather than writing a row with nothing in it."""
    if not tiers:
        return []
    slug = gp.slug_from_product_url(start_url)
    product = Product(
        sku=gp.make_sku(slug, start_url),
        source=gp.SOURCE,
        category=None,
        title=None,
        brand=None,
        price=None,
        currency=None,
        price_source=None,
        product_url=gp.product_url(slug) if slug else start_url,
        image_url=None,
        scraped_at=gp.now_iso(),
        product_slug=slug,
    )
    return [gp.apply_pricing_tiers(product, tiers)]


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    start_url, mode = _resolve_start_url(args)
    if not start_url:
        if mode == "disallowed":
            print(f"Error: --url {args.url!r} matches a robots.txt-disallowed path "
                  f"(g2.com's AI-crawler group disallows /products/*/reviews/*) — refusing.", file=sys.stderr)
        elif mode == "grids_unsupported":
            print(f"Error: --url {args.url!r} is g2.com's own Grid(R) ranking JSON. It is a "
                  f"sanctioned endpoint, but this repo ships no parser for its response "
                  f"(g2_parser.py, 'Left for the next stage') — scrape the category listing "
                  f"itself instead, which also covers far more than the ranked subset.", file=sys.stderr)
        elif mode == "unknown_shape":
            print(f"Error: --url {args.url!r} is not a g2.com URL shape this repo can parse. "
                  f"Supported: /categories/{{slug}}, /products/{{slug}}/reviews, "
                  f"/products/{{slug}}/pricing.", file=sys.stderr)
        else:
            print("Error: provide --url, --category, or --product", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)

    if args.scraper_api_cdp and not args.scraper_api:
        print("Error: --scraper-api-cdp requires --scraper-api", file=sys.stderr)
        return EXIT_BAD_USAGE

    if args.scraper_api:
        # A whole separate, browserless code path — no Playwright import is
        # needed at all here (this mode runs even with no engine driver
        # installed, CLAUDE.md §6), so the async_playwright-is-None check
        # below is skipped entirely.
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        if args.proxy or args.proxy_file or args.cdp_endpoint or args.fingerprint:
            log.warning(
                "--scraper-api ignores --proxy/--proxy-file/--cdp-endpoint/--fingerprint — this "
                "mode brings its own exit IP/device via 2Captcha's own infrastructure."
            )
        if (args.scraper_api_country or args.scraper_api_profile_id) and not args.scraper_api_cdp:
            log.warning(
                "--scraper-api-country/--scraper-api-profile-id are ignored without "
                "--scraper-api-cdp — there is no Scraping Browser session for them to apply to."
            )
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api, scraper_api_base=args.scraper_api_url)
        cdp_url = None
        if args.scraper_api_cdp:
            # Constructed once, reused across every --block-retries attempt
            # below — same reuse-a-profile principle scraping_browser_
            # connection_url's own docstring recommends, not a fresh
            # session minted per attempt.
            cdp_url = client.scraping_browser_connection_url(
                country=args.scraper_api_country, profile_id=args.scraper_api_profile_id,
            )
        merged: List[Product] = []
        blocked = remote_api_error = False
        pages_completed, failed_pages = 0, []
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, pages_completed, failed_pages = _scrape_via_scraper_api(
                args=args, start_url=start_url, mode=mode, client=client, cdp_url=cdp_url,
            )
            if remote_api_error or not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (Scraper API attempt %d/%d) — retrying the same fetch.",
                    block_attempt + 1, args.block_retries + 1,
                )
                time.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=pages_completed, failed_pages=failed_pages,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    merged: List[Product] = []
    blocked = remote_api_error = False
    pages_completed, failed_pages = 0, []
    cdp_connect_failed = False
    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                try:
                    browser = await _connect_over_cdp(pw, args.cdp_endpoint)
                except RuntimeError as exc:
                    log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                    cdp_connect_failed = True
                    browser = None
            else:
                browser = await pw.chromium.launch(headless=args.headless)

            if cdp_connect_failed:
                remote_api_error = True
            else:
                autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"
                if mode == "product":
                    scrape_fn = scrape_product_page
                elif mode == "pricing":
                    scrape_fn = _scrape_pricing_only
                else:
                    scrape_fn = scrape_category
                # Each attempt gets a fresh context. A proxy pool advances to
                # its next live exit; direct/CDP runs keep their fixed exit.
                for block_attempt in range(args.block_retries + 1):
                    merged, blocked, remote_api_error, pages_completed, failed_pages = await scrape_fn(
                        args=args, start_url=start_url, browser=browser, proxy_pool=proxy_pool,
                        client=client, autosolve=autosolve, user_agent=user_agent,
                    )
                    if not (blocked and not merged):
                        break
                    if block_attempt < args.block_retries:
                        log.warning(
                            "Blocked with zero products (attempt %d/%d) — retrying the scrape; "
                            "a proxy pool advances to its next live exit, while direct/CDP keeps its fixed identity.",
                            block_attempt + 1, args.block_retries + 1,
                        )
                        await asyncio.sleep(args.retry_delay)
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    return finish_run(
        products=merged,
        out_path=args.out,
        fmt=args.format,
        engine=ENGINE_NAME,
        url=start_url,
        pages_requested=args.max_pages if mode == "category" else 1,
        pages_completed=pages_completed,
        failed_pages=failed_pages,
        blocked=blocked,
        remote_api_error=remote_api_error,
        allow_empty=args.allow_empty,
        started_at=started_at,
        price_confirmed_pct=price_confirmed_pct,
    )


async def _scrape_pricing_only(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """Adapter so a `--url .../pricing` run returns run()'s uniform 5-tuple.
    The page load, the captcha handling and the auto-solve arming all
    happen inside scrape_pricing_page(); this only shapes the result."""
    tiers, blocked, remote_api_error = await scrape_pricing_page(
        args=args, start_url=start_url, browser=browser, proxy_pool=proxy_pool,
        client=client, autosolve=autosolve, user_agent=user_agent,
    )
    products = _pricing_only_product(start_url, tiers)
    if not products and not blocked and not remote_api_error:
        log.warning(
            "Pricing page returned no recognisable tier lines — g2.com ships no pricing JSON-LD, "
            "so this parser is best-effort text matching and an empty result means 'no pricing "
            "could be read', never 'this product is free' (see g2_parser.py)."
        )
    failed_pages = [1] if (blocked or remote_api_error) and not products else []
    return products, blocked, remote_api_error, 0 if failed_pages else 1, failed_pages


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
