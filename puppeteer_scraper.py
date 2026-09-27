#!/usr/bin/env python3
"""puppeteer_scraper.py — pyppeteer engine, parity copy of
playwright_scraper.py (same CLI flags, same exit codes and status
semantics — see output_writer.finish_run). Not the primary engine
(Playwright is); kept for parity, and because it — like Playwright, unlike
Selenium — CAN open an authenticated `ws://login:pass@host:port` CDP
session, so it is the second engine able to arm the Scraping Browser API's
own `Captcha.setAutoSolve`.

Named and shaped like every sibling repo's own `puppeteer_scraper.py`
(Python + pyppeteer, not a separate Node.js file) — this repo follows that
family convention for the same reason: one shared output contract and one
set of family modules across all three engines.

pyppeteer itself is effectively unmaintained (its own README points at
Playwright) — this file exists for parity/completeness, not as a
recommendation to prefer it.

**Auto-solve coverage.** All three of this engine's page-loading functions
— `scrape_category()`, `scrape_product_page()` and `scrape_pricing_page()`
— take `autosolve` and actually call `_enable_scraping_browser_auto_solve()`
with it. This file is where the family's one real regression of that kind
happened: shein-scraper's puppeteer `scrape_product_page()` accepted an
`autosolve` parameter and silently never armed anything, so a direct
`--url <product page> --cdp-endpoint …` run got no auto-solve and no
warning. smoke_test.py now enforces the invariant structurally (AST-level,
per function), so it cannot regress here unnoticed.

**g2.com's confirmed defense is DataDome — corrected 2026-09-22: it IS
solvable.** This repo originally claimed no automated solve path existed
for it at all; that was wrong (Roman, from 2Captcha's own team, pointed at
https://2captcha.com/api-docs/datadome-slider-captcha — a dedicated
`DataDomeSliderTask`). `--solve-captcha` now attempts it when a slider
challenge is detected, but it is the ONE type in this family with no
proxyless path: `--proxy`/`--proxy-file` is required. Without a proxy, a
wall still reports `unsupported_vendor`/`vendor="datadome"` + EXIT_BLOCKED
(honest, not a bug). DataDome's own tag also ships on every healthy
g2.com page, which is why a bare marker match never counts as a block on
its own here.

Chromium binary: sourced from `PYPPETEER_EXECUTABLE_PATH` /
`PUPPETEER_EXECUTABLE_PATH` if set (handy for reusing an existing
Playwright/system Chromium instead of pyppeteer's own bundled download),
otherwise pyppeteer's own default.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from pyppeteer import connect as pyppeteer_connect
    from pyppeteer import launch as pyppeteer_launch
    from pyppeteer.errors import NetworkError, PageError, TimeoutError as PyppeteerTimeoutError
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    pyppeteer_launch = None
    pyppeteer_connect = None
    NetworkError = PageError = PyppeteerTimeoutError = Exception
    _PYPPETEER_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PYPPETEER_IMPORT_ERROR = None

import env_config
import g2_parser as gp
from captcha_solver import (
    CaptchaType, build_injection_script, detect_from_html, parse_datadome_cookie, solve_when_blocked,
)
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "puppeteer"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_S = 1.5  # g2.com is server-rendered Rails/PJAX, not an SPA
                        # (confirmed — g2_parser.py): nothing has to hydrate
                        # before the content is readable, so this is slack
                        # for DataDome's own async checks, not for rendering.
                        # Deliberately shorter than an SPA sibling's 3s.
MIN_CARD_MATCHES = gp.MIN_CARD_MATCHES

# See playwright_scraper.py's identical constant — `unsupported_vendor`
# (the EXPECTED outcome of a real DataDome wall) is still-blocked exactly
# like the rest; the honest vendor name is the only difference.
STILL_BLOCKED_ACTIONS = (
    "warning_no_key",
    "warning_no_proxy",
    "warning_proxy_banned",
    "warning_solver_error",
    "detected_unidentified_widget",
    "unsupported_vendor",
)

log = logging.getLogger("puppeteer_scraper")

_CHROMIUM_EXECUTABLE = os.environ.get("PYPPETEER_EXECUTABLE_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")


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
        description="g2.com B2B software listing/review scraper — pyppeteer (Puppeteer) engine",
        epilog="Credentials belong in .env / G2_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="Full g2.com category/product/pricing URL (or set G2_URL) — overrides --category/--product")
    p.add_argument("--category", default=None, help="A category slug, e.g. 'crm'")
    p.add_argument("--product", default=None, help="A product slug, e.g. 'hubspot-sales-hub'")
    p.add_argument("--max-results", type=_positive_int, default=60)
    p.add_argument("--max-pages", type=_positive_int, default=5)
    p.add_argument("--page-delay", type=_nonnegative_float, default=1.5)
    p.add_argument(
        "--with-pricing", action="store_true",
        help="Also fetch each collected product's /pricing page and fill price/currency/"
             "price_source from its cheapest tier — see playwright_scraper.py's copy of this "
             "help text. OFF by default: one extra request per product, and the pricing parser "
             "is explicitly the least reliable one in this repo (no pricing JSON-LD exists).",
    )
    p.add_argument("--pricing-limit", type=_positive_int, default=10)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=_nonnegative_int, default=2)
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument(
        "--block-retries", type=_nonnegative_int, default=2,
        help="On a blocked, zero-product outcome, retry the whole scrape this many extra times. "
             "With --proxy-file each attempt advances to the next live proxy and launches a fresh "
             "browser; a fixed CDP endpoint keeps the provider's configured identity.",
    )
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
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
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set G2_CDP_ENDPOINT)")
    p.add_argument(
        "--scraper-api", action="store_true",
        help="Fetch via 2Captcha's Scraper API (scraper.2captcha.com) instead of launching any "
             "local or --cdp-endpoint browser — a single browserless HTTP call. Requires "
             "--twocaptcha-key/TWOCAPTCHA_KEY. NOT confirmed against g2.com and NOT a documented "
             "DataDome bypass. --max-pages/--page-delay/--proxy/--cdp-endpoint/--fingerprint are "
             "IGNORED in this mode; set together, they log a warning rather than silently doing "
             "nothing.",
    )
    p.add_argument("--scraper-api-timeout", type=_positive_int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"g2_results.{fmt}"


def _resolve_start_url(args: argparse.Namespace) -> Tuple[Optional[str], str]:
    """See playwright_scraper._resolve_start_url — identical contract and
    identical two-guard order (robots.txt first, then the allowlist of URL
    shapes this repo actually has a parser for)."""
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


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        try:
            return await pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None)
        except Exception as exc:
            # CLAUDE.md §8: the endpoint (credentials included) is repeated
            # in the driver's own exception text. Redact, and drop the
            # original from the chain so it cannot resurface via "the above
            # exception was the direct cause of…".
            raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if proxy is not None:
        # CLAUDE.md §3: the launch arg carries the SERVER only; the
        # login/password go through page.authenticate() below, never argv.
        args.append(proxy.pyppeteer_launch_arg())
    kwargs = dict(headless=headless, args=args)
    if _CHROMIUM_EXECUTABLE:
        kwargs["executablePath"] = _CHROMIUM_EXECUTABLE
    return await pyppeteer_launch(**kwargs)


async def _authenticate_if_needed(page, proxy: Optional[Proxy]) -> None:
    if proxy is not None:
        auth = proxy.pyppeteer_auth_dict()
        if auth:
            await page.authenticate(auth)


async def _enable_scraping_browser_auto_solve(page) -> None:
    """Arm 2Captcha's Scraping Browser `Captcha.setAutoSolve` CDP domain.

    Only meaningful over `--cdp-endpoint`. **Every function in this engine
    that loads a real page calls this when `autosolve` is set** — see this
    module's docstring for the real regression that makes this worth
    stating twice, and smoke_test.py for the AST-level check that keeps it
    true."""
    try:
        client = await page.target.createCDPSession()
        client.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        client.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        client.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await client.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str,
    count_product_links, min_score: float = 0.3, page=None,
    proxy: Optional[Proxy] = None, user_agent: Optional[str] = None,
) -> Optional[dict]:
    """`count_product_links` is REQUIRED and has NO default, on purpose —
    see playwright_scraper._maybe_solve_captcha's docstring for the full
    reasoning and for the shipped-and-fixed bug (shein-scraper, 2026-09-22)
    a default caused. The three correct choices live in g2_parser.py:

        category listing page  ->  gp.count_result_cards
        product /reviews page  ->  gp.count_product_page_data
        /pricing page          ->  gp.count_pricing_tiers

    `proxy` / `user_agent` — added 2026-09-22 alongside
    `captcha_solver.CaptchaType.DATADOME_SLIDER` (this repo's original
    claim that DataDome had no automated solve path at all was WRONG —
    2Captcha ships a dedicated `DataDomeSliderTask`; see captcha_solver.py's
    module docstring). Forwarded unchanged for every other captcha type
    (ignored there); they matter only when a page turns out to be a
    DataDome slider challenge, the one type with no proxyless path.
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
        # warning_proxy_banned below — see playwright_scraper.py's
        # identical comment for the full story.
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_proxy_banned":
        # Confirmed live, 2026-09-22 — see captcha_solver.py's module
        # docstring and playwright_scraper.py's identical branch.
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
            # Set-Cookie-shaped solution, not a hidden-field token —
            # build_injection_script() returns None for this type on
            # purpose. pyppeteer's Page.setCookie(*cookies) mirrors
            # puppeteer.js's own API (name/value/domain/path/secure/
            # sameSite dicts).
            if page is not None:
                try:
                    cookie = parse_datadome_cookie(result["token"])
                    host = (urlparse(url).hostname or "").lower()
                    apex = host[4:] if host.startswith("www.") else host
                    # No Domain attribute rides in 2Captcha's own example
                    # response — defaults to the parent domain (".g2.com")
                    # because that's DataDome's own real-world convention,
                    # NOT something confirmed against an actual g2.com
                    # Set-Cookie header (never captured — see
                    # captcha_solver.py's module docstring).
                    cookie["domain"] = f".{apex}" if apex else host
                    await page.setCookie({
                        "name": cookie["name"], "value": cookie["value"],
                        "domain": cookie["domain"], "path": cookie["path"],
                        "secure": cookie["secure"], "sameSite": cookie["same_site"],
                    })
                    log.info(
                        "Applied the solved DataDome cookie via page.setCookie (domain=%s, confirmed "
                        "against pyppeteer==2.0.0's actual Page.setCookie signature/source in this "
                        "environment) and reloading — this repo has never seen g2.com actually "
                        "present this challenge, so whether the reload then clears it is UNCONFIRMED.",
                        cookie["domain"],
                    )
                    await page.reload({"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
                    await asyncio.sleep(READINESS_WAIT_S)
                except Exception as exc:  # noqa: BLE001 — solved-but-apply-failed degrades, never crashes
                    log.warning("DataDome solved but applying the cookie / reloading failed: %s", exc)
        else:
            # See captcha_solver.build_injection_script's docstring for the
            # honesty caveat: each branch uses that widget's own STANDARD,
            # publicly documented convention, never anything confirmed against
            # a real g2.com capture.
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
        # no slider iframe was on THIS page. For other vendors in this
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


async def _goto_with_retries(page, url: str, *, retries: int, retry_delay: float,
                             proxy_pool: Optional[ProxyPool] = None, proxy: Optional[Proxy] = None,
                             ) -> Tuple[Optional[int], Optional[str]]:
    """Returns `(http_status, last_error)`. `last_error is not None` means
    every attempt failed — a failed page, never a crash (CLAUDE.md §6)."""
    last_error: Optional[str] = None
    status: Optional[int] = None
    for attempt in range(retries + 1):
        try:
            response = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
            status = response.status if response is not None else None
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
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
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool = False, user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """Paginate a category listing via `?page=N`. Returns
    `(products, blocked, remote_api_error, pages_completed, failed_pages)`.

    CLAUDE.md §6: one page that fails to load or parse becomes a recorded
    failed page, never a crash that discards its siblings' rows."""
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
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        if proxy_pool is not None and proxy is not None and not args.cdp_endpoint:
            return [], True, False, 0, [1]
        return [], False, True, 0, []
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

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
                    # treats as fatal (breaks immediately) — that defeated
                    # --proxy-file rotation entirely the moment ANY one
                    # proxy in the list failed to load the very first page
                    # (confirmed live: a 10-proxy run with --block-retries
                    # 9 stopped after a single navigation timeout on
                    # attempt 6). A different proxy/exit may well succeed
                    # where this one didn't, so treat it like a block
                    # instead — the caller's --block-retries loop then
                    # rotates to proxy_pool.next() rather than aborting
                    # the whole run. _goto_with_retries() already reported
                    # a hard dead-proxy marker (if this was one) via
                    # report_failure(dead=True); a generic timeout isn't
                    # one of those and stays in rotation for a later
                    # attempt.
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
            log.warning("Listing page %d returned HTTP %d — treating as blocked, not empty.", page_num, status)
            blocked = True

        # DataDome's tag ships on EVERY g2.com page, healthy ones included,
        # so a bare marker match means nothing on its own — a page is
        # blocked only when the marker matches AND the page failed to
        # render its own content (MIN_CARD_MATCHES).
        cards_present = gp.count_result_cards(html) >= MIN_CARD_MATCHES
        captcha_detected = detect_from_html(html, gp.BOT_CHALLENGE_MARKERS)
        if captcha_detected and not cards_present:
            blocked = True
        if captcha_detected:
            captcha_result = await _maybe_solve_captcha(
                html=html, url=page_url, client=client, policy=args.solve_captcha,
                min_score=args.min_score, page=page,
                # LISTING-page-shaped counter.
                count_product_links=gp.count_result_cards,
                proxy=proxy, user_agent=user_agent,
            )
            if captcha_result and captcha_result.get("action") == "solved":
                # See playwright_scraper.scrape_category's identical
                # 2026-09-22 addition: without this, a solve on THIS
                # page_num only ever paid off starting next page_num.
                await asyncio.sleep(READINESS_WAIT_S)
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
                    "current g2.com markup, or g2.com served a different page than the listing. "
                    "Re-run with --dump-html to inspect the captured page.",
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
            args=args, products=merged, page=page,
            client=client, autosolve=autosolve,
            proxy=proxy, user_agent=user_agent,
        )

    if args.dump_html and last_html:
        Path(_dump_path(args.out)).write_text(last_html, encoding="utf-8")

    await browser.close()
    return merged, blocked, remote_api_error, pages_completed, failed_pages


async def scrape_product_page(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool = False, user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """One product's canonical `/reviews` page, parsed from its
    `SoftwareApplication` JSON-LD. Same 5-tuple as scrape_category().

    This is the exact function shape that shipped broken in shein-scraper
    (it took `autosolve` and never armed it). Here it arms auto-solve
    before the navigation, like its two siblings in this file."""
    blocked = False
    proxy = proxy_pool.next() if proxy_pool else None
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        if proxy_pool is not None and proxy is not None and not args.cdp_endpoint:
            return [], True, False, 0, [1]
        return [], False, True, 0, []
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

    status, last_error = await _goto_with_retries(
        page, start_url, retries=args.retries, retry_delay=args.retry_delay,
        proxy_pool=proxy_pool, proxy=proxy,
    )
    if last_error is not None:
        await browser.close()
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
            # DETAIL-page-shaped counter — the listing-shaped one reads 0 on
            # every normal product page by construction (bug class 2 in
            # g2_parser.py's module docstring).
            count_product_links=gp.count_product_page_data,
            proxy=proxy, user_agent=user_agent,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            await asyncio.sleep(READINESS_WAIT_S)
            html = await page.content()
        elif captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
            if gp.count_product_page_data(html) == 0:
                blocked = True

    product = gp.parse_product_page(html, url=start_url)
    products = [product] if product else []

    if products and args.with_pricing:
        await _enrich_with_pricing(
            args=args, products=products, page=page,
            client=client, autosolve=autosolve,
            proxy=proxy, user_agent=user_agent,
        )

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
    await browser.close()

    if not products and not blocked:
        log.warning(
            "Product page rendered but no usable SoftwareApplication JSON-LD was found (%s) — "
            "see g2_parser.parse_product_page(). Re-run with --dump-html to inspect the page.",
            gp.diagnose_unexpected_page(html),
        )
    return products, blocked, False, 1, []


async def scrape_pricing_page(
    *, args: argparse.Namespace, start_url: str, page,
    client: Optional[TwoCaptchaClient], autosolve: bool = False,
    proxy: Optional[Proxy] = None, user_agent: Optional[str] = None,
) -> Tuple[List[gp.PricingTier], bool, bool]:
    """Load ONE `/products/{slug}/pricing` page on an ALREADY-OPEN page and
    return `(tiers, blocked, remote_api_error)`.

    The third real page shape, and it gets the same captcha treatment as
    the other two — **including arming `Captcha.setAutoSolve`** when asked.
    DataDome could in principle gate any page here, so "it is only an
    enrichment" is not a reason to leave a page-loading path uncovered.
    Re-arming on an already-armed page is harmless (the CDP call is
    idempotent and its failure is already non-fatal), and it keeps this
    function correct when it is called with a freshly-opened page.

    Tiers come back EMPTY, never as an exception, when the page doesn't
    match the expected line shape. Empty means "no pricing could be read",
    never "this product is free"."""
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

    status, last_error = await _goto_with_retries(
        page, start_url, retries=args.retries, retry_delay=args.retry_delay
    )
    if last_error is not None:
        log.warning("Pricing page failed to load: %s", last_error)
        return [], False, True

    blocked = bool(status is not None and status >= 400)
    html = await page.content()
    if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS):
        captcha_result = await _maybe_solve_captcha(
            html=html, url=start_url, client=client, policy=args.solve_captcha,
            min_score=args.min_score, page=page,
            # PRICING-page-shaped counter.
            count_product_links=gp.count_pricing_tiers,
            proxy=proxy, user_agent=user_agent,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            await asyncio.sleep(READINESS_WAIT_S)
            html = await page.content()
        elif captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
            if gp.count_pricing_tiers(html) == 0:
                blocked = True

    return gp.parse_pricing_page(html, url=start_url), blocked, False


async def _enrich_with_pricing(
    *, args: argparse.Namespace, products: List[Product], page,
    client: Optional[TwoCaptchaClient], autosolve: bool = False,
    proxy: Optional[Proxy] = None, user_agent: Optional[str] = None,
) -> None:
    """`--with-pricing`: fill price/currency/price_source in place from each
    product's own `/pricing` page, up to `--pricing-limit` products. Never
    fails the run — an unreadable or blocked pricing page leaves that row's
    price empty and logs it, because an additive enrichment must never cost
    the rows it was decorating."""
    filled = 0
    attempted = 0
    for product in products[: args.pricing_limit]:
        slug = product.product_slug or gp.slug_from_product_url(product.product_url)
        if not slug:
            continue
        attempted += 1
        try:
            tiers, blocked, remote_error = await scrape_pricing_page(
                args=args, start_url=gp.pricing_url(slug), page=page,
                client=client, autosolve=autosolve,
                proxy=proxy, user_agent=user_agent,
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


async def scrape_pricing_only(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool = False, user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """A `--url .../pricing` run: launches/connects, opens a page, arms
    auto-solve via scrape_pricing_page(), and shapes the result into
    run()'s uniform 5-tuple."""
    proxy = proxy_pool.next() if proxy_pool else None
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        if proxy_pool is not None and proxy is not None and not args.cdp_endpoint:
            return [], True, False, 0, [1]
        return [], False, True, 0, []
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)

    tiers, blocked, remote_api_error = await scrape_pricing_page(
        args=args, start_url=start_url, page=page, client=client, autosolve=autosolve,
        proxy=proxy, user_agent=user_agent,
    )
    if args.dump_html:
        try:
            Path(_dump_path(args.out)).write_text(await page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001 — a debug dump never fails a run
            pass
    await browser.close()

    products = _pricing_only_product(start_url, tiers)
    if remote_api_error and proxy_pool is not None and proxy is not None:
        blocked, remote_api_error = True, False
    if not products and not blocked and not remote_api_error:
        log.warning(
            "Pricing page returned no recognisable tier lines — g2.com ships no pricing JSON-LD, "
            "so this parser is best-effort text matching and an empty result means 'no pricing "
            "could be read', never 'this product is free' (see g2_parser.py)."
        )
    failed_pages = [1] if (blocked or remote_api_error) and not products else []
    return products, blocked, remote_api_error, 0 if failed_pages else 1, failed_pages


def _pricing_only_product(start_url: str, tiers) -> List[Product]:
    """See playwright_scraper._pricing_only_product — the one honest row
    that can be built from a pricing page alone. Returns [] when no tier
    was readable, so the run reports `empty` rather than writing a row with
    nothing in it."""
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


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, mode: str, client: TwoCaptchaClient,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """`--scraper-api`'s own fetch path — see playwright_scraper.py's copy
    of this function for the full rationale (identical logic, duplicated
    per engine per this family's own convention, CLAUDE.md §4). One
    browserless HTTP call via plain `requests` (nothing here needs
    `await`), no pagination loop, no live DOM to inject into."""
    try:
        result = client.scrape_url(start_url, timeout=args.scraper_api_timeout)
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
        products = [p for p in [gp.parse_product_page(html, url=start_url)] if p]
        if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS) and gp.count_product_page_data(html) == 0:
            blocked = True
        if not products and not blocked:
            log.warning("Product page fetched via Scraper API but no usable SoftwareApplication "
                        "JSON-LD was found (%s).", gp.diagnose_unexpected_page(html))
        return products, blocked, False, 1, []

    if mode == "pricing":
        tiers = gp.parse_pricing_page(html, url=start_url)
        if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS) and gp.count_pricing_tiers(html) == 0:
            blocked = True
        products = _pricing_only_product(start_url, tiers)
        if not products and not blocked:
            log.warning("Pricing page fetched via Scraper API but no recognisable tier lines were "
                        "found (%s) — this parser is best-effort text matching, see g2_parser.py.",
                        gp.diagnose_unexpected_page(html))
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

    if args.scraper_api:
        # A whole separate, browserless code path — no pyppeteer is needed
        # at all here (CLAUDE.md §6), so the pyppeteer_launch-is-None check
        # below is skipped entirely.
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        if args.proxy or args.proxy_file or args.cdp_endpoint or args.fingerprint:
            log.warning(
                "--scraper-api ignores --proxy/--proxy-file/--cdp-endpoint/--fingerprint — this "
                "mode brings its own exit IP/device via 2Captcha's own infrastructure."
            )
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api, scraper_api_base=args.scraper_api_url)
        merged: List[Product] = []
        blocked = remote_api_error = False
        pages_completed, failed_pages = 0, []
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, pages_completed, failed_pages = _scrape_via_scraper_api(
                args=args, start_url=start_url, mode=mode, client=client,
            )
            if remote_api_error or not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (Scraper API attempt %d/%d) — retrying the same fetch.",
                    block_attempt + 1, args.block_retries + 1,
                )
                await asyncio.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=pages_completed, failed_pages=failed_pages,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    if pyppeteer_launch is None:
        print(f"Error: pyppeteer is not installed ({_PYPPETEER_IMPORT_ERROR}). "
              f"pip install -r requirements-puppeteer.txt", file=sys.stderr)
        return EXIT_CRASH

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None
    if args.cdp_endpoint and proxy_pool is not None:
        log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        proxy_pool = None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"

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
    try:
        if mode == "product":
            scrape_fn = scrape_product_page
        elif mode == "pricing":
            scrape_fn = scrape_pricing_only
        else:
            scrape_fn = scrape_category
        # A proxy pool advances once per attempt; a fixed CDP endpoint keeps
        # the provider's configured identity.
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, pages_completed, failed_pages = await scrape_fn(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client,
                autosolve=autosolve, user_agent=user_agent,
            )
            if not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (attempt %d/%d) — retrying; a proxy pool advances to its next live exit.",
                    block_attempt + 1, args.block_retries + 1,
                )
                await asyncio.sleep(args.retry_delay)
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    return finish_run(
        products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
        pages_requested=args.max_pages if mode == "category" else 1,
        pages_completed=pages_completed, failed_pages=failed_pages,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.get_event_loop().run_until_complete(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
