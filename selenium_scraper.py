#!/usr/bin/env python3
"""selenium_scraper.py — Selenium engine, parity copy of
playwright_scraper.py (same CLI flags, same exit codes, same status
semantics — see output_writer.finish_run). Selenium is not the primary
engine (Playwright is); it exists for parity, not because it is preferred.

Same hard engine limits as the rest of the family (CLAUDE.md §6), stated
here rather than left for a user to discover from a failed run:

  - **Selenium CANNOT use an AUTHENTICATED remote CDP endpoint.**
    chromedriver's `debuggerAddress` takes a bare `host:port`; Playwright's
    `connect_over_cdp` and pyppeteer's `connect` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    A `--cdp-endpoint` carrying credentials (the Scraping Browser API
    shape) is refused outright here with EXIT_BAD_USAGE — no half-working
    attempt — with a pointer to playwright_scraper.py / puppeteer_scraper.py.
  - **Consequently this engine has no `Captcha.setAutoSolve` path at all.**
    That CDP domain belongs to 2Captcha's managed Scraping Browser, which
    is exactly the thing this engine cannot authenticate to. The classic
    captcha-API + local-injection path (`_maybe_solve_captcha` below) works
    here identically to the other two engines; only the managed auto-solve
    is absent. smoke_test.py exempts this file from the "every function
    taking `autosolve` must arm it" check for this reason, and for no
    other.
  - **Selenium's `--proxy-server` cannot authenticate at all.** A `--proxy`
    with a login/password has its credentials STRIPPED before being handed
    to Chrome, and this engine WARNS rather than silently dropping them.

None of which matters for g2.com's actual defense: DataDome has no
automated solve path at 2Captcha or anywhere else, on any engine (see
g2_parser.py's module docstring). This engine reports a DataDome wall as
`unsupported_vendor`/`vendor="datadome"` + EXIT_BLOCKED, same as its two
siblings.

Chrome binary: normally auto-detected by Selenium/Selenium Manager from a
regular Chrome/Chromium install. Set `CHROME_BIN` or `SELENIUM_CHROME_BIN`
to point at a specific binary instead; `SELENIUM_CHROMEDRIVER_PATH` /
`CHROMEDRIVER_PATH` pin a specific chromedriver (Selenium Manager's
default network auto-resolve fails outright in an offline/network-
restricted environment).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    webdriver = None
    WebDriverException = Exception
    Options = None
    Service = None
    _SELENIUM_IMPORT_ERROR = _IMPORT_ERROR
else:
    _SELENIUM_IMPORT_ERROR = None

import env_config
import g2_parser as gp
from captcha_solver import CaptchaType, build_injection_script, detect_from_html, solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "selenium"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_S = 30
READINESS_WAIT_S = 1.5  # g2.com is server-rendered Rails/PJAX, not an SPA
                        # (confirmed — g2_parser.py): nothing has to hydrate
                        # before the content is readable, so this is slack
                        # for DataDome's own async checks, not for rendering.
                        # Deliberately shorter than an SPA sibling's 3s.
MIN_CARD_MATCHES = gp.MIN_CARD_MATCHES

# See playwright_scraper.py's identical constant: `unsupported_vendor`
# (the EXPECTED outcome of a real DataDome wall on g2.com) counts as
# still-blocked exactly like the rest — the honest vendor name is the only
# difference, not the exit code.
STILL_BLOCKED_ACTIONS = (
    "warning_no_key",
    "warning_solver_error",
    "detected_unidentified_widget",
    "unsupported_vendor",
)

_CHROME_BINARY = os.environ.get("SELENIUM_CHROME_BIN") or os.environ.get("CHROME_BIN")
_CHROMEDRIVER_PATH = os.environ.get("SELENIUM_CHROMEDRIVER_PATH") or os.environ.get("CHROMEDRIVER_PATH")

# Selenium Manager phones home to plausible.io with usage stats by default
# (confirmed live on earlier family members) — opted out the same way here,
# before this repo's own SECURITY.md promise is tested.
os.environ.setdefault("SE_AVOID_STATS", "true")

log = logging.getLogger("selenium_scraper")


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
        description="g2.com B2B software listing/review scraper — Selenium engine",
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
        help="On a blocked, zero-product outcome, retry this many extra times before giving up — "
             "'retry before you rotate', not a proxy swap. Selenium/chromedriver cannot "
             "authenticate a remote --cdp-endpoint at all (see this file's module docstring), so "
             "unlike Playwright/Puppeteer this only re-runs against the same local browser + "
             "--proxy exit, never a managed session identity.",
    )
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument(
        "--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked",
        help="g2.com's confirmed defense is DataDome, which has NO automated solve path anywhere — "
             "this flag governs detection/reporting and any OTHER widget type that might appear, "
             "it does not promise a solve.",
    )
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None,
                   help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
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


def _cdp_endpoint_has_credentials(cdp_endpoint: str) -> bool:
    parts = urlparse(cdp_endpoint)
    return bool(parts.username or parts.password)


def _build_driver(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str], user_agent: Optional[str] = None):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if _CHROME_BINARY:
        options.binary_location = _CHROME_BINARY
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    if cdp_endpoint:
        # Only ever reached for a CREDENTIAL-FREE endpoint — run() refuses a
        # credentialed one outright (EXIT_BAD_USAGE) before getting here.
        options.debugger_address = urlparse(cdp_endpoint).netloc.split("@")[-1]
        return webdriver.Chrome(options=options)

    if proxy is not None:
        if proxy.has_auth:
            log.warning(
                "Selenium's --proxy-server cannot authenticate — using %s:%s "
                "with credentials STRIPPED, not silently dropped.",
                proxy.host, proxy.port,
            )
        options.add_argument(f"--proxy-server={proxy.server_only()}")

    if Service and _CHROMEDRIVER_PATH:
        service = Service(executable_path=_CHROMEDRIVER_PATH)
    elif Service:
        service = Service()  # Selenium Manager: resolves/downloads over the network
    else:
        service = None
    return webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)


# Selenium has no driver-native Response object for a navigation, so the
# HTTP status comes from the Navigation Timing API instead — the same
# approach every sibling repo's selenium_scraper.py uses. It matters here:
# DataDome answers with a 403 rather than a redirect, so the status is this
# engine's most direct block signal.
_STATUS_JS = (
    "try { return performance.getEntriesByType('navigation')[0].responseStatus || 0; } "
    "catch (e) { return 0; }"
)


def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str,
    count_product_links, min_score: float = 0.3, driver=None,
) -> Optional[dict]:
    """`count_product_links` is REQUIRED and has NO default, on purpose —
    see playwright_scraper._maybe_solve_captcha's docstring for the full
    reasoning and for the shipped-and-fixed bug (shein-scraper, 2026-09-22)
    a default caused. The three correct choices live in g2_parser.py:

        category listing page  ->  gp.count_result_cards
        product /reviews page  ->  gp.count_product_page_data
        /pricing page          ->  gp.count_pricing_tiers
    """
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=count_product_links,
        extra_markers=gp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "no_captcha_detected":
        pass
    elif action == "skipped_products_present":
        log.info("Captcha-like marker present but this page's content is already rendered — not solving.")
    elif action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
        # See captcha_solver.build_injection_script's docstring for the
        # honesty caveat: each branch uses that widget's own STANDARD,
        # publicly documented convention, never anything confirmed against
        # a real g2.com capture.
        if driver is not None:
            script = build_injection_script(CaptchaType(result["captcha_type"]), result["token"])
            if script is None:
                log.info(
                    "No generic injection point for %s — token was solved but not written into "
                    "the page (this is expected for reCAPTCHA v3; see captcha_solver.py).",
                    result.get("captcha_type"),
                )
            else:
                try:
                    injected = driver.execute_script(f"return {script}")
                    log.info(
                        "Injected solved %s into the page (found a target element/callback: %s) "
                        "— unconfirmed whether a real g2.com widget reads this "
                        "standard-convention field/callback.",
                        result.get("captcha_type"), bool(injected),
                    )
                except WebDriverException as exc:
                    log.warning("Captcha solved but injecting it into the page failed: %s", exc)
    elif action == "unsupported_vendor":
        # The EXPECTED outcome on g2.com — see playwright_scraper.py's copy
        # of this branch. A confirmed gap, not a bug: 2Captcha has no task
        # type for DataDome at all.
        log.warning(
            "%s challenge detected — no automated solve exists for this defense at 2Captcha or "
            "anywhere else (confirmed gap, not a bug). Reporting this run as blocked. Your levers "
            "are --block-retries (retry the same session) or a different --proxy exit; this engine "
            "cannot use a --cdp-endpoint session identity at all (see its module docstring).",
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


def _goto_with_retries(driver, url: str, *, retries: int, retry_delay: float,
                       proxy_pool: Optional[ProxyPool] = None, proxy: Optional[Proxy] = None,
                       ) -> Tuple[Optional[int], Optional[str]]:
    """Returns `(http_status, last_error)`. `last_error is not None` means
    every attempt failed — a failed page, never a crash (CLAUDE.md §6)."""
    last_error: Optional[str] = None
    status: Optional[int] = None
    for attempt in range(retries + 1):
        try:
            driver.set_page_load_timeout(NAV_TIMEOUT_S)
            driver.get(url)
            time.sleep(READINESS_WAIT_S)
            try:
                reported = driver.execute_script(_STATUS_JS)
                status = int(reported) if reported else None
            except WebDriverException:
                pass
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except WebDriverException as exc:
            message = redact_credentials(str(exc))
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, retries + 1, url, message)
            if attempt < retries:
                time.sleep(retry_delay)
    return status, last_error


def scrape_category(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """Paginate a category listing via `?page=N`. Returns
    `(products, blocked, remote_api_error, pages_completed, failed_pages)`
    — the same 5-tuple every scrape function in this family returns.

    CLAUDE.md §6: one page that fails to load or parse is recorded in
    `failed_pages` and the loop continues; it never discards the pages that
    already succeeded (that's EXIT_PARTIAL rather than a lost run)."""
    blocked = False
    remote_api_error = False
    failed_pages: List[int] = []
    pages_completed = 0

    slug = gp.category_slug_from_url(start_url) or args.category
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)

    seen_skus: set = set()
    pages: List[List[Product]] = []
    last_html = ""

    try:
        for page_num in range(1, args.max_pages + 1):
            page_url = gp.category_url(slug, page_num) if slug else start_url
            status, last_error = _goto_with_retries(
                driver, page_url, retries=args.retries, retry_delay=args.retry_delay,
                proxy_pool=proxy_pool, proxy=proxy,
            )
            if last_error is not None:
                log.error("Listing page %d permanently failed to load: %s", page_num, last_error)
                failed_pages.append(page_num)
                if page_num == 1:
                    remote_api_error = True
                    break
                continue

            try:
                html = driver.page_source
            except WebDriverException as exc:
                log.warning("Could not read page %d's content: %s", page_num, exc)
                failed_pages.append(page_num)
                continue
            last_html = html

            if status is not None and status >= 400:
                log.warning("Listing page %d returned HTTP %d — treating as blocked, not empty.", page_num, status)
                blocked = True

            # DataDome's tag ships on EVERY g2.com page, healthy ones
            # included, so a bare marker match means nothing on its own —
            # a page is blocked only when the marker matches AND the page
            # failed to render its own content (MIN_CARD_MATCHES).
            cards_present = gp.count_result_cards(html) >= MIN_CARD_MATCHES
            captcha_detected = detect_from_html(html, gp.BOT_CHALLENGE_MARKERS)
            if captcha_detected and not cards_present:
                blocked = True
            if captcha_detected:
                captcha_result = _maybe_solve_captcha(
                    html=html, url=page_url, client=client, policy=args.solve_captcha,
                    min_score=args.min_score, driver=driver,
                    # LISTING-page-shaped counter.
                    count_product_links=gp.count_result_cards,
                )
                if captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
                    if gp.count_result_cards(html) == 0:
                        blocked = True

            products = gp.safe_parse_category_listing(html, category_slug=slug, page_url=page_url)
            if not products:
                if page_num == 1 and not blocked:
                    log.warning(
                        "No products recognised on the first listing page (%s) — either this "
                        "category genuinely has no results, g2_parser.py's card selectors need "
                        "updating for the current g2.com markup, or g2.com served a different page "
                        "than the listing. Re-run with --dump-html to inspect the captured page.",
                        gp.diagnose_unexpected_page(html),
                    )
                else:
                    log.info("Listing page %d yielded zero cards — treating that as the end of the category.", page_num)
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
            if page_num >= args.max_pages:
                break
            if not gp.has_next_page(html):
                log.info("Page %d's own pagination advertises no Next page — stopping.", page_num)
                break
            time.sleep(args.page_delay)

        merged = merge_pages(pages)[: args.max_results]

        if args.with_pricing and merged:
            _enrich_with_pricing(args=args, products=merged, driver=driver, client=client)

        if args.dump_html and last_html:
            Path(_dump_path(args.out)).write_text(last_html, encoding="utf-8")
    finally:
        driver.quit()

    return merged, blocked, remote_api_error, pages_completed, failed_pages


def scrape_product_page(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """One product's canonical `/reviews` page, parsed from its
    `SoftwareApplication` JSON-LD. Same 5-tuple as scrape_category()."""
    blocked = False
    proxy = proxy_pool.next() if proxy_pool else None
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)

    try:
        status, last_error = _goto_with_retries(
            driver, start_url, retries=args.retries, retry_delay=args.retry_delay,
            proxy_pool=proxy_pool, proxy=proxy,
        )
        if last_error is not None:
            log.error("Product page permanently failed to load: %s", last_error)
            return [], False, True, 0, []

        if status is not None and status >= 400:
            log.warning("Product page returned HTTP %d — treating as blocked, not empty.", status)
            blocked = True

        html = driver.page_source
        if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS):
            captcha_result = _maybe_solve_captcha(
                html=html, url=start_url, client=client, policy=args.solve_captcha,
                min_score=args.min_score, driver=driver,
                # DETAIL-page-shaped counter — the listing-shaped one reads
                # 0 on every normal product page by construction (bug class
                # 2 in g2_parser.py's module docstring).
                count_product_links=gp.count_product_page_data,
            )
            if captcha_result and captcha_result.get("action") == "solved":
                # No pagination loop here to pick the injection up on a later
                # round — re-read once after a beat so a solved token is not
                # one nothing ever re-reads.
                time.sleep(READINESS_WAIT_S)
                html = driver.page_source
            elif captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
                if gp.count_product_page_data(html) == 0:
                    blocked = True

        product = gp.parse_product_page(html, url=start_url)
        products = [product] if product else []

        if products and args.with_pricing:
            _enrich_with_pricing(args=args, products=products, driver=driver, client=client)

        if args.dump_html:
            Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
    finally:
        driver.quit()

    if not products and not blocked:
        log.warning(
            "Product page rendered but no usable SoftwareApplication JSON-LD was found (%s) — "
            "see g2_parser.parse_product_page(). Re-run with --dump-html to inspect the page.",
            gp.diagnose_unexpected_page(html),
        )
    return products, blocked, False, 1, []


def scrape_pricing_page(
    *, args: argparse.Namespace, start_url: str, driver,
    client: Optional[TwoCaptchaClient],
) -> Tuple[List[gp.PricingTier], bool, bool]:
    """Load ONE `/products/{slug}/pricing` page on an ALREADY-BUILT driver
    and return `(tiers, blocked, remote_api_error)`.

    The third real page shape, and it gets the same captcha treatment as
    the other two — DataDome could in principle gate any page here, so "it
    is only an enrichment" is not a reason to leave a page-loading path
    uncovered. (No `autosolve` parameter: this engine has no
    `Captcha.setAutoSolve` path at all — see the module docstring.)

    Tiers come back EMPTY, never as an exception, when the page doesn't
    match the expected line shape. An empty list means "no pricing could be
    read", never "this product is free"."""
    status, last_error = _goto_with_retries(
        driver, start_url, retries=args.retries, retry_delay=args.retry_delay
    )
    if last_error is not None:
        log.warning("Pricing page failed to load: %s", last_error)
        return [], False, True

    blocked = bool(status is not None and status >= 400)
    html = driver.page_source
    if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS):
        captcha_result = _maybe_solve_captcha(
            html=html, url=start_url, client=client, policy=args.solve_captcha,
            min_score=args.min_score, driver=driver,
            # PRICING-page-shaped counter.
            count_product_links=gp.count_pricing_tiers,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            time.sleep(READINESS_WAIT_S)
            html = driver.page_source
        elif captcha_result and captcha_result.get("action") in STILL_BLOCKED_ACTIONS:
            if gp.count_pricing_tiers(html) == 0:
                blocked = True

    return gp.parse_pricing_page(html, url=start_url), blocked, False


def _enrich_with_pricing(*, args: argparse.Namespace, products: List[Product], driver,
                         client: Optional[TwoCaptchaClient]) -> None:
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
            tiers, blocked, remote_error = scrape_pricing_page(
                args=args, start_url=gp.pricing_url(slug), driver=driver, client=client,
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
        time.sleep(args.page_delay)
    if attempted:
        log.info("--with-pricing: filled a price for %d of %d products looked up.", filled, attempted)


def scrape_pricing_only(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, List[int]]:
    """A `--url .../pricing` run: builds its own driver, delegates the page
    load and captcha handling to scrape_pricing_page(), and shapes the
    result into run()'s uniform 5-tuple."""
    proxy = proxy_pool.next() if proxy_pool else None
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)
    try:
        tiers, blocked, remote_api_error = scrape_pricing_page(
            args=args, start_url=start_url, driver=driver, client=client,
        )
        if args.dump_html:
            try:
                Path(_dump_path(args.out)).write_text(driver.page_source, encoding="utf-8")
            except WebDriverException:
                pass
    finally:
        driver.quit()
    products = _pricing_only_product(start_url, tiers)
    if not products and not blocked and not remote_api_error:
        log.warning(
            "Pricing page returned no recognisable tier lines — g2.com ships no pricing JSON-LD, "
            "so this parser is best-effort text matching and an empty result means 'no pricing "
            "could be read', never 'this product is free' (see g2_parser.py)."
        )
    return products, blocked, remote_api_error, 0 if remote_api_error else 1, []


def _pricing_only_product(start_url: str, tiers) -> List[Product]:
    """See playwright_scraper._pricing_only_product — the one honest row
    that can be built from a pricing page alone (identity from the URL,
    price from the cheapest tier, everything else left empty rather than
    invented). Returns [] when no tier was readable."""
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
    browserless HTTP call, no pagination loop, no live DOM to inject into,
    so `--solve-captcha` is a documented no-op in this mode."""
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
    products = gp.safe_parse_category_listing(html, category_slug=slug, page_url=start_url)
    if detect_from_html(html, gp.BOT_CHALLENGE_MARKERS) and gp.count_result_cards(html) < MIN_CARD_MATCHES:
        blocked = True
    if not products and not blocked:
        log.warning(
            "No products recognised in the Scraper API response (%s) — inspect the fetched page "
            "with --dump-html before assuming a parser regression.",
            gp.diagnose_unexpected_page(html),
        )
    return products[: args.max_results], blocked, False, 1, []


def run(args: argparse.Namespace) -> int:
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
        # A whole separate, browserless code path — no Selenium/chromedriver
        # is needed at all here (CLAUDE.md §6), so none of the checks below
        # (webdriver import, --cdp-endpoint credential shape) apply.
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
                time.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=pages_completed, failed_pages=failed_pages,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    if args.cdp_endpoint and _cdp_endpoint_has_credentials(args.cdp_endpoint):
        print(
            "Error: --cdp-endpoint carries credentials — Selenium/chromedriver's "
            "debuggerAddress takes a bare host:port and cannot authenticate a "
            "remote session. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser API.", file=sys.stderr,
        )
        return EXIT_BAD_USAGE
    if webdriver is None:
        print(f"Error: selenium is not installed ({_SELENIUM_IMPORT_ERROR}). "
              f"pip install -r requirements-selenium.txt", file=sys.stderr)
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
        # "Retry before you rotate" — see playwright_scraper.py's copy of
        # this comment and --block-retries' help text.
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, pages_completed, failed_pages = scrape_fn(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client, user_agent=user_agent,
            )
            if not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (attempt %d/%d) — retrying before giving up.",
                    block_attempt + 1, args.block_retries + 1,
                )
                time.sleep(args.retry_delay)
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
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
