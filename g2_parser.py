#!/usr/bin/env python3
"""g2_parser.py — this IS the g2.com site knowledge, and the only file in
this repo that carries any (CLAUDE.md §5/§7: the seven family-shared
modules beside it — `output_writer.py`, `proxy_pool.py`,
`captcha_solver.py`, `fingerprint_client.py`, `scraper_api_client.py`,
`diff_runs.py`, `env_config.py` — carry no site knowledge and were copied
from the newest sibling, shein-scraper, with only deliberate, documented
changes). If you find yourself adding a g2.com selector, URL shape or
quirk to any other module, it belongs here instead.

This is the B2B-software-marketplace analog of shein-scraper's
`shein_parser.py`, lidl-scraper's `lidl_parser.py`, skyscanner-scraper's
`flight_parser.py`, stockx-scraper's `product_parser.py` and
perplexity-scraper's `page_parser.py`.

===========================================================================
READ THIS BEFORE TRUSTING ANYTHING BELOW (CLAUDE.md §15: an unverified
claim gets stated plainly, not glossed over)
===========================================================================

Everything marked CONFIRMED came from a real, live browser capture of
www.g2.com taken on 2026-09-22 — a browser-rendering tool, NOT this repo's
own engines, which have never run against this site (the same distinction
shein-scraper's TESTING.md draws, and the same one perplexity-scraper's own
incident turns on). Everything marked BEST-EFFORT or UNCONFIRMED is a
reasoned guess and is written so that being wrong degrades to empty output,
never to a crash or to a wrong number.

**CONFIRMED — site architecture.** g2.com is server-rendered (Rails/PJAX),
not a client-side SPA. Product data is in the delivered HTML; nothing has
to hydrate before it is readable. The practical consequence for whoever
writes the engine scripts next: `READINESS_WAIT_MS` (CLAUDE.md §5) can be
materially SHORTER here than on an SPA family member — the only thing
worth waiting on is DataDome's own asynchronous checks, not content.

**CONFIRMED — bot protection is DataDome. CORRECTED 2026-09-22: it IS
solvable.** `window.DataDomeJsTag`, `window.dataDomeOptions.endpoint =
"https://dd.g2.com/js/"`, version 5.10.0, and a `datadome` cookie were all
observed live. `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` already
contains the string `"datadome"`, so DETECTION works with no extra wiring.
This module originally shipped the claim above — no `CaptchaType` member
for DataDome, on the theory that no 2Captcha task type existed for it at
all, the same bucket as PerimeterX on skyscanner.com. That claim was
WRONG: Roman, from 2Captcha's own team, pointed at
https://2captcha.com/api-docs/datadome-slider-captcha — a dedicated
`DataDomeSliderTask` for exactly this vendor's own interstitial slider
challenge. `captcha_solver.CaptchaType.DATADOME_SLIDER` now exists and is
wired end-to-end in all three engines (cookie-based, requires a proxy — no
proxyless path exists for it, unlike every other type this repo handles).
`captcha_solver.identify_unsupported_vendor()` still fires for DataDome,
but now means something narrower: the tag is present with no slider
iframe currently on the page (the ordinary case on most loads), not "this
vendor can never be solved." A DataDome wall with nothing solvable on it
still reports `unsupported_vendor` + `vendor="datadome"` and EXIT_BLOCKED
— honest about being blocked right now, no longer implying there's nothing
to buy at all.

**CONFIRMED — category listing page**, e.g.
`https://www.g2.com/categories/crm`, paginated with `?page=N` (111 pages
for the `crm` slug alone at capture time; other categories differ, so page
counts are DISCOVERED from the markup — see `has_next_page()` — never
hardcoded per category).

  - Each card: `div.content-card.category-product-card.x-category-product-card`
    (this module matches on `.category-product-card`, the stable middle
    class, not the `x-`-prefixed behavioural one).
  - The product link inside it: `a[data-event-options][href*="/products/"]`,
    whose `href` is the canonical product URL, shaped
    `https://www.g2.com/products/{slug}/reviews`.
  - **`data-event-options` is a JSON-encoded object** (HTML-entity encoded
    in source; BeautifulSoup hands it back decoded) and is by far the best
    source on the page — structured fields instead of scraped visible
    text:
      `{"product_id":506,"product_uuid":"16e299ae-…","product":"Agentforce
        Sales (formerly Salesforce Sales Cloud)","vendor_id":469,
        "product_type":"Software","category":"CRM","category_id":179,
        "resource_type":"Category","resource_id":179,"list_type":null,
        "is_onboarding":false,"name":"Event::Products::ListItemClicked"}`
  - Rating + review count: `.elv-star-wrapper`'s text reads like
    `"4.4/5\\n(27,555)"`. The count is more reliably isolated at
    `.elv-star-wrapper__desc__count` (`"(27,555)"`).
  - Image: the card's `<img>` `src` (an `images.g2crowd.com` URL).
  - Pagination: `ul.pagination[aria-label="Pagination"]` with
    `li.pagination__component.pagination__page-number` items (the current
    one also carries `pagination__page-number--current`) and Prev/Next
    controls that gain `pagination__component--disabled` at either edge.

**CONFIRMED — product detail page**, e.g.
`https://www.g2.com/products/hubspot-sales-hub/reviews`. The canonical
product URL always ends in `/reviews`; there is no separate bare
`/products/{slug}` landing page. It carries several
`<script type="application/ld+json">` blocks, of which exactly one is
useful: `"@type": "SoftwareApplication"`, with `name`, an
`aggregateRating` (`bestRating: 10, worstRating: 0`, e.g.
`ratingValue: 8.9`, `reviewCount: 14333`) and a `review` array of roughly
ten embedded reviews, each with `author.name`, `name`, `reviewBody`,
`datePublished`, `dateModified` and its own `reviewRating.ratingValue` on
a 0-5 scale. The `BreadcrumbList` / `Organization` / `WebPage` blocks on
the same page carry no product data and are skipped.

**TWO RATING SCALES — the single most important thing in this file.** The
listing card's star rating is 0-5. The product page's `aggregateRating` is
G2's own 0-10 composite score. An individual embedded review is 0-5 again.
`output_writer.Product` keeps these as `rating_5` and `rating_10`, and
this module never averages, converts or unifies them. If a future change
makes it tempting to add a plain `rating` column: don't.

Individual reviews show their author as first name + last initial
("Excel M."). That is **G2's own public anonymization convention**, not
anything this scraper does — `# TODO(readme):` if review text is ever
surfaced in output, the README must say so in those terms, so nobody reads
it as scraper-side redaction.

**CONFIRMED-ABSENT, so BEST-EFFORT — the pricing page**,
`https://www.g2.com/products/{slug}/pricing`. Confirmed: it has NO
structured pricing JSON-LD at all (only generic `BreadcrumbList` /
`Organization` / `WebPage` / `FAQPage` blocks). Tiers are plain rendered
text in a recognizable shape:

    HubSpot Sales Hub offers 4 pricing editions, starting from $0 to $150.
    Free HubSpot CRM — $0
    Sales Hub Starter — $20 / 1 Core Seat Per Month
    Sales Hub Professional — $100 / 1 Sales Seat Per Month
    Sales Hub Enterprise — $150 / 1 Sales Seat Per Month
    *Pricing information is supplied by the software provider …

`parse_pricing_page()` is therefore a line-shape text parser and is
**explicitly less reliable than the JSON-LD-backed listing and detail
parsers**. It returns an empty list rather than raising when the page
doesn't match, and `# TODO(readme):` the README must rank it that way
plainly rather than presenting all three parsers as equally solid.

**CONFIRMED — official machine-readable resources.** G2 publishes
`https://www.g2.com/llms.txt` (a site index for LLMs) and
`https://www.g2.com/ai-instructions` (a methodology/trust page addressed
to AI assistants by name), and serves
`https://www.g2.com/categories/{slug}/grids.json` — G2's own JSON API for
that category's Grid® ranking data. That endpoint is a SANCTIONED API, not
a workaround, but it is also NOT a substitute for the listing: it covers
only the top-RANKED subset of a category (253 products for `crm`, against
1661 found by paginating the category DOM). It belongs in an engine as an
additive, on-by-default ENRICHMENT merged onto DOM-scraped rows (a
`--no-enrich-grids` opt-OUT), never as the primary listing source, which
would silently under-report by 85%. `grids_json_url()` below builds the
URL; this module deliberately ships no parser for the response, because
its actual field names were never captured — see "Left for the next
stage".

**CONFIRMED — robots.txt has a stricter group for AI crawlers, and this
repo obeys it.** `https://www.g2.com/robots.txt` has the usual
`User-agent: *` group, plus a second group listing `GPTBot`, `ClaudeBot`,
`Google-Extended`, `Applebot-Extended`, `Meta-ExternalAgent`, `Amazonbot`
and `CCBot` together, which adds exactly one rule the general group does
not have: `Disallow: /products/*/reviews/*`. That covers sub-paths one
level BELOW a product's reviews page (pagination/filter variants of the
review listing) — not the bare `/products/{slug}/reviews` page itself,
which is this scraper's product-detail target and is unaffected.
`is_disallowed_path()` honours the STRICTER ClaudeBot rule set by default,
because this scraper is built and run by Claude and G2's own
`/ai-instructions` page states that robots.txt is authoritative for
AI-assistant access. This costs the scraper nothing: deeper review
pagination was never in scope.

===========================================================================
TWO BUG CLASSES THE ENGINE SCRIPTS (NEXT STAGE) MUST NOT REINTRODUCE
===========================================================================

Both were found and fixed for real in shein-scraper on 2026-09-22. They
are written here, in the file the engine author will definitely read,
rather than only in a sibling repo's CHANGELOG.

**1. Captcha auto-solve must be armed on EVERY code path that touches a
real page, from day one.** shein-scraper's `puppeteer_scraper.py` had a
`scrape_product_page()` that ACCEPTED an `autosolve` parameter and never
called `_enable_scraping_browser_auto_solve()` with it — asymmetric with
its own `scrape_search()` and with the other two engines. A user running
`--url <product page> --cdp-endpoint …` silently got no auto-solve and no
warning that coverage was missing. There are three real page shapes in
this repo (category listing, product detail, pricing) and every one of
them, in every engine, arms auto-solve when asked. No exceptions, and a
structural (AST-level, not grep-level) smoke test for it.

**2. `count_product_links` must be shaped for the page the CALL SITE is
actually looking at.** `captcha_solver.solve_when_blocked()` takes a
`count_product_links` callable to answer "does this page already have real
content, so don't pay for a solve". shein-scraper defaulted it to the
listing-shaped `count_result_cards`, which returns 0 on a product-DETAIL
page *by construction* — so every normal detail-page load read as "0
products, possibly blocked" the moment any generic marker appeared on it.
Best case a false warning; worst case a real, PAID 2Captcha solve on a
page that was never blocked. The fix is an explicit per-call-site counter,
and this module ships all three:

    category listing page  ->  count_result_cards(html)
    product /reviews page  ->  count_product_page_data(html)
    /pricing page          ->  count_pricing_tiers(html)

`captcha_solver.solve_when_blocked()` has no default for this argument on
purpose. An engine's `_maybe_solve_captcha()` (or equivalent) must accept
an override and every call site must pass one of the three above.
DataDome is unsolvable regardless, so on g2.com the practical cost of
getting this wrong is a false warning rather than wasted spend — but the
habit is what keeps the family from re-shipping the expensive version.

===========================================================================
LEFT FOR THE NEXT STAGE (deliberate gaps, not oversights)
===========================================================================

  - `GENERAL_DISALLOWED_PATTERNS` below is EMPTY. g2.com is blocked by
    this environment's network egress policy, so `robots.txt`'s general
    `User-agent: *` group could not be re-fetched and transcribed here,
    and guessing its paths would be worse than leaving it visibly
    unfilled. `is_disallowed_path()` is pattern-driven, so filling it in
    from a real fetch is a one-line change with no code edit.
    `is_known_scrape_target()` is the conservative guard in the meantime:
    an allowlist of the four URL shapes this scraper actually uses.
  - No `grids.json` response parser: the endpoint is confirmed to exist
    and confirmed to cover only ranked products, but its field names were
    never captured. Adding guessed keys to the shared output contract is
    the exact failure `fingerprint_client.py`'s own locale/timezone note
    documents. Whoever captures a real response adds the parser here and
    its tail fields to `output_writer.Product`.
"""
from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("g2_parser")

# `Product` is re-exported here so an engine can import the row model and
# the parser from one place, exactly as shein_parser.py's callers do. It is
# DEFINED in output_writer.py (CLAUDE.md §9: the output contract, including
# each repo's site-specific tail, lives in that module) — see its docstring
# for what every g2.com-specific field means and which page it comes from.
__all__ = [
    "Product", "PricingTier", "BASE_URL", "SOURCE", "MIN_CARD_MATCHES",
    "BOT_CHALLENGE_MARKERS", "GENERAL_DISALLOWED_PATTERNS",
    "AI_CRAWLER_EXTRA_DISALLOWED_PATTERNS", "is_disallowed_path",
    "is_known_scrape_target", "category_url", "product_url", "pricing_url",
    "grids_json_url", "slug_from_product_url", "category_slug_from_url",
    "make_sku",
    "parse_category_listing", "safe_parse_category_listing",
    "count_result_cards", "has_next_page", "current_page_number",
    "extract_json_ld", "parse_product_page", "count_product_page_data",
    "parse_pricing_page", "count_pricing_tiers", "apply_pricing_tiers",
    "diagnose_unexpected_page", "now_iso",
]

BASE_URL = "https://www.g2.com"
SOURCE = "g2.com"

# CLAUDE.md §5: the site's own threshold for "did this page actually render
# results". A single stray card-shaped element must not read as a rendered
# listing — mirrors shein-scraper's / stockx-scraper's own MIN_CARD_MATCHES.
MIN_CARD_MATCHES = 2

# Per-site markers, passed to `captcha_solver.detect_from_html()` as
# `extra_markers` — a UNION with `GENERIC_BOT_CHALLENGE_MARKERS`, never a
# replacement for it. Unlike shein-scraper's, these are corroboration
# rather than the only detection path: the generic list already carries
# `"datadome"`, which fires on g2.com on its own. These add the two
# g2.com-specific spellings observed live, so a log line can say WHICH
# DataDome deployment was seen.
BOT_CHALLENGE_MARKERS: tuple = (
    "datadomejstag",
    "dd.g2.com",
)

# --------------------------------------------------------------------------- #
# robots.txt compliance
#
# See the module docstring for why the AI-crawler group is the one this
# repo honours. Both lists use robots.txt's own wildcard syntax: `*`
# matches any run of characters INCLUDING `/` (robots.txt is not a glob —
# `/` is not special), and a trailing `$` anchors the end of the path.
# --------------------------------------------------------------------------- #

# The `User-agent: *` group's own rules. DELIBERATELY EMPTY — see "Left for
# the next stage" in the module docstring. g2.com is unreachable from the
# environment this file was written in, so transcribing this group would
# have meant inventing paths. Fill it from a real fetch of
# https://www.g2.com/robots.txt; no code change is needed, only this tuple.
GENERAL_DISALLOWED_PATTERNS: tuple = ()

# The EXTRA rule the AI-crawler group (GPTBot, ClaudeBot, Google-Extended,
# Applebot-Extended, Meta-ExternalAgent, Amazonbot, CCBot) adds on top of
# the general group — CONFIRMED live, 2026-09-22. Note what it does and
# does not cover: `/products/hubspot-sales-hub/reviews` (the bare product
# detail page, this scraper's target) is NOT matched, because the pattern
# requires a `/` after `reviews`; `/products/hubspot-sales-hub/reviews/`
# and anything below it IS matched, which is the review-listing pagination
# and filter space this scraper has no reason to enter.
AI_CRAWLER_EXTRA_DISALLOWED_PATTERNS: tuple = (
    "/products/*/reviews/*",
)


def _robots_pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """robots.txt matching, not fnmatch: a rule is a PREFIX match, `*` is
    any run of characters (including `/`), and a trailing `$` anchors the
    end of the path."""
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(f"^{regex}$" if anchored_end else f"^{regex}")


_COMPILED_DISALLOW_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _compiled(pattern: str) -> "re.Pattern[str]":
    cached = _COMPILED_DISALLOW_CACHE.get(pattern)
    if cached is None:
        cached = _robots_pattern_to_regex(pattern)
        _COMPILED_DISALLOW_CACHE[pattern] = cached
    return cached


def is_disallowed_path(path: str, *, ai_crawler_rules: bool = True) -> bool:
    """True when robots.txt forbids fetching `path`. Accepts a bare path or
    a full URL (the query string is included in the match, because
    robots.txt rules can and do target query variants).

    `ai_crawler_rules` defaults to True — the STRICTER rule set, i.e. the
    general `User-agent: *` group PLUS the extra
    `/products/*/reviews/*` disallow that g2.com's robots.txt gives
    `ClaudeBot` and its peers. This scraper is built and run by Claude, and
    g2.com's own `/ai-instructions` page says robots.txt is authoritative
    for AI-assistant access, so the stricter set is the honest default.
    Passing False evaluates only the general group; it exists so the
    difference between the two sets is auditable (and testable) rather than
    baked in, not as a way to opt out of compliance.
    """
    if not path:
        return False
    if path.startswith("http://") or path.startswith("https://"):
        parsed = urlparse(path)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if not path.startswith("/"):
        path = "/" + path
    patterns: Sequence[str] = GENERAL_DISALLOWED_PATTERNS
    if ai_crawler_rules:
        patterns = (*GENERAL_DISALLOWED_PATTERNS, *AI_CRAWLER_EXTRA_DISALLOWED_PATTERNS)
    return any(_compiled(p).search(path) for p in patterns)


# The four URL shapes this scraper actually uses. `is_known_scrape_target()`
# is an ALLOWLIST, and is the conservative half of the robots story while
# `GENERAL_DISALLOWED_PATTERNS` is still empty: a caller-supplied `--url`
# that isn't one of these is something this parser has no parser for
# anyway, so an engine can refuse it with a clear message instead of
# fetching a page it will then fail to read.
_KNOWN_TARGET_PATTERNS = (
    re.compile(r"^/categories/[^/]+/?$"),
    re.compile(r"^/categories/[^/]+/grids\.json$"),
    # No trailing slash accepted on `/reviews` on purpose: the canonical
    # form has none, and `/products/{slug}/reviews/` IS matched by the
    # AI-crawler `Disallow: /products/*/reviews/*` rule. Accepting it here
    # would let an engine allowlist a URL that `is_disallowed_path()` then
    # refuses — two guards disagreeing about the same URL.
    re.compile(r"^/products/[^/]+/reviews$"),
    re.compile(r"^/products/[^/]+/pricing/?$"),
)


def is_known_scrape_target(url_or_path: str) -> bool:
    """True for a category listing, a category grids.json, a product
    `/reviews` page or a product `/pricing` page — the only four shapes
    this module can parse. Ignores the query string (`?page=N` is fine)."""
    if not url_or_path:
        return False
    path = url_or_path
    if path.startswith("http://") or path.startswith("https://"):
        path = urlparse(path).path
    else:
        path = path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return any(p.match(path) for p in _KNOWN_TARGET_PATTERNS)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def category_url(category_slug: str, page: Optional[int] = None) -> str:
    """`https://www.g2.com/categories/{slug}` (+ `?page=N`). `page=1` is
    emitted as the bare URL, matching what a human's first click produces."""
    slug = category_slug.strip("/")
    url = f"{BASE_URL}/categories/{slug}"
    if page and page > 1:
        url = f"{url}?page={page}"
    return url


def product_url(product_slug: str) -> str:
    """The canonical product URL. It ALWAYS ends in `/reviews` — g2.com has
    no separate bare `/products/{slug}` landing page (confirmed)."""
    return f"{BASE_URL}/products/{product_slug.strip('/')}/reviews"


def pricing_url(product_slug: str) -> str:
    return f"{BASE_URL}/products/{product_slug.strip('/')}/pricing"


def grids_json_url(category_slug: str) -> str:
    """G2's own Grid® ranking JSON for a category. A sanctioned API, but a
    RANKED SUBSET only — see the module docstring: enrichment, never the
    primary listing source."""
    return f"{BASE_URL}/categories/{category_slug.strip('/')}/grids.json"


_PRODUCT_SLUG_RE = re.compile(r"/products/([^/?#]+)")
# Anchored to the WHOLE path, with nothing allowed after the slug: a
# `/categories/crm/grids.json` URL has a category slug in it but is a
# different endpoint, not a listing page anything can be paginated from,
# so it must NOT come back as "crm" here.
_CATEGORY_SLUG_RE = re.compile(r"^/categories/([^/?#]+)/?$")


def slug_from_product_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _PRODUCT_SLUG_RE.search(url)
    return m.group(1) if m else None


def category_slug_from_url(url: Optional[str]) -> Optional[str]:
    """The `crm` out of `https://www.g2.com/categories/crm?page=3`.

    Added for the engine scripts (stage 2): all three of them paginate by
    rebuilding `category_url(slug, page)` for each page rather than
    string-editing whatever `--url` the caller typed, and every one of them
    needs the slug back out of a caller-supplied URL to do that. Knowing
    that a category slug is the path segment after `/categories/` is
    site knowledge, so it lives here rather than being re-derived with a
    regex in each engine (this module's own docstring: if you find yourself
    adding a g2.com URL shape to another module, it belongs here).
    Returns None for a URL that isn't a category listing at all —
    `grids.json` included, since that's a different endpoint, not a page
    this can be paginated from."""
    if not url:
        return None
    path = url
    if path.startswith("http://") or path.startswith("https://"):
        path = urlparse(path).path
    else:
        path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    m = _CATEGORY_SLUG_RE.match(path)
    return m.group(1) if m else None


def make_sku(product_slug: Optional[str], url: Optional[str]) -> str:
    """The product's URL slug, which is the one identifier present on BOTH
    a category card and that product's own detail page — see
    `output_writer.Product`'s docstring for why that beats the numeric
    `product_id` as a `sku`. Falls back to a DETERMINISTIC fingerprint of
    the URL, never a random or run-scoped value, so `diff_runs.py` still
    matches the same row across two runs."""
    if product_slug:
        return product_slug
    return "fp_" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Path 1: the category listing page — PRIMARY, and JSON-backed despite
# being DOM-located: every card carries a `data-event-options` JSON blob.
# --------------------------------------------------------------------------- #
CARD_SELECTOR = ".category-product-card"
_CARD_LINK_SELECTOR = 'a[data-event-options][href*="/products/"]'
_STAR_WRAPPER_SELECTOR = ".elv-star-wrapper"
_REVIEW_COUNT_SELECTOR = ".elv-star-wrapper__desc__count"

_RATING_5_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*5")
_COUNT_RE = re.compile(r"[\d,]+")


def _card_nodes(soup: BeautifulSoup) -> list:
    return soup.select(CARD_SELECTOR)


def _event_options(link) -> Dict[str, Any]:
    """Parse a card link's `data-event-options` JSON. BeautifulSoup already
    decodes the HTML entities (`&quot;` -> `"`) when handing back an
    attribute value; `html.unescape` runs anyway as a belt-and-braces step
    for a caller that fed us pre-decoded or oddly-encoded markup. Returns
    {} rather than raising — a malformed blob costs its own card's extra
    fields, not the page."""
    raw = link.get("data-event-options")
    if not raw:
        return {}
    for candidate in (raw, html_module.unescape(raw)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    log.debug("card data-event-options was present but not JSON-parseable")
    return {}


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rating_5_from_card(card) -> Optional[float]:
    """`.elv-star-wrapper`'s text reads `"4.4/5\\n(27,555)"` — the `/5` is
    matched explicitly rather than "take the first number", so a markup
    change that swaps in a different scale yields None (a missing value)
    instead of a number on the wrong scale, which is the failure this
    module cares most about avoiding."""
    node = card.select_one(_STAR_WRAPPER_SELECTOR)
    if not node:
        return None
    m = _RATING_5_RE.search(node.get_text(" ", strip=True))
    return _as_float(m.group(1)) if m else None


def _review_count_from_card(card) -> Optional[int]:
    node = card.select_one(_REVIEW_COUNT_SELECTOR)
    if node is None:
        return None
    m = _COUNT_RE.search(node.get_text(strip=True))
    return _as_int(m.group(0).replace(",", "")) if m else None


def _image_from_card(card) -> Optional[str]:
    img = card.select_one("img")
    if img is None:
        return None
    for attr in ("src", "data-src", "data-deferred-image-src"):
        value = img.get(attr)
        if value:
            return value
    return None


def _card_to_product(card, *, category_slug: Optional[str], page_url: Optional[str]) -> Optional[Product]:
    link = card.select_one(_CARD_LINK_SELECTOR)
    if link is None:
        return None
    href = link.get("href")
    if not href:
        return None
    url = urljoin(page_url or BASE_URL, href)
    slug = slug_from_product_url(url)

    opts = _event_options(link)
    title = opts.get("product") or link.get_text(" ", strip=True) or None
    # `category` prefers G2's own display name from the card's JSON ("CRM")
    # and falls back to the slug the caller was iterating ("crm") — never
    # left empty when we know which listing this row came from.
    category = opts.get("category") or category_slug

    return Product(
        sku=make_sku(slug, url),
        source=SOURCE,
        category=category,
        title=title,
        # No vendor NAME is exposed on a listing card — only `vendor_id`,
        # which is kept in its own field below. See Product's docstring for
        # why this column stays empty rather than being filled with
        # something that isn't a brand.
        brand=None,
        # g2.com carries no price on a listing card at all; pricing lives on
        # a separate page (see `parse_pricing_page` / `apply_pricing_tiers`).
        price=None,
        currency=None,
        price_source=None,
        product_url=url,
        image_url=_image_from_card(card),
        scraped_at=_now_iso(),
        rating_5=_rating_5_from_card(card),
        rating_10=None,  # 0-10 G2 score exists only on the product page
        review_count=_review_count_from_card(card),
        review_count_source="listing_card",
        product_id=_as_int(opts.get("product_id")),
        product_uuid=opts.get("product_uuid") or None,
        product_slug=slug,
        vendor_id=_as_int(opts.get("vendor_id")),
        category_id=_as_int(opts.get("category_id")),
        product_type=opts.get("product_type") or None,
    )


def parse_category_listing(
    html: str,
    category_slug: Optional[str] = None,
    page_url: Optional[str] = None,
) -> List[Product]:
    """Parse ONE category-listing page into a list of `Product`.

    Rows are PARTIAL by design: `rating_10` is always None here (the 0-10
    G2 score exists only on a product page) and so are `price` /
    `currency` / `price_source` (g2.com has no price on a listing card).
    An engine that also fetches the detail or pricing page merges those in.

    Per CLAUDE.md §6, one malformed card degrades to a skipped card, never
    a lost page: every card is parsed inside its own try/except so a single
    bad `data-event-options` blob or missing link cannot discard the other
    nineteen rows the page did produce.
    """
    soup = BeautifulSoup(html, "html.parser")
    products: List[Product] = []
    skipped = 0
    for card in _card_nodes(soup):
        try:
            product = _card_to_product(card, category_slug=category_slug, page_url=page_url)
        except Exception:  # noqa: BLE001 — one bad card never costs the page
            log.exception("category card raised while parsing — skipping just this card")
            skipped += 1
            continue
        if product is None:
            skipped += 1
            continue
        products.append(product)
    if skipped:
        log.warning("%d of %d category cards on this page could not be parsed",
                    skipped, skipped + len(products))
    return products


def safe_parse_category_listing(
    html: str,
    category_slug: Optional[str] = None,
    page_url: Optional[str] = None,
) -> List[Product]:
    """CLAUDE.md §6 wrapper: an unexpected parse exception degrades this one
    page to an empty result (which the engine records as a FAILED page ->
    EXIT_PARTIAL), never a crash that discards its siblings' rows."""
    try:
        return parse_category_listing(html, category_slug, page_url)
    except Exception:  # noqa: BLE001
        log.exception("parse_category_listing raised — degrading this page to empty")
        return []


def count_result_cards(html: str) -> int:
    """**The LISTING-page-shaped counter** for
    `captcha_solver.solve_when_blocked(count_product_links=...)`.

    Cheap presence check — no readiness wait, no scroll, per that
    function's own policy. Pass this ONLY from a category-listing call
    site: on a product-detail or pricing page it returns 0 by construction,
    which is exactly the mis-wiring documented as bug class 2 in this
    module's docstring. Those pages have `count_product_page_data()` and
    `count_pricing_tiers()` respectively.
    """
    try:
        return len(_card_nodes(BeautifulSoup(html, "html.parser")))
    except Exception:  # noqa: BLE001 — a counter must never raise into a captcha decision
        log.exception("count_result_cards raised — reporting 0")
        return 0


# --------------------------------------------------------------------------- #
# Pagination — DISCOVERED from the markup, never a hardcoded page count.
# (`crm` had 111 pages at capture time; every category differs.)
# --------------------------------------------------------------------------- #
_PAGINATION_SELECTOR = "ul.pagination"
_PAGE_COMPONENT_SELECTOR = ".pagination__component"
_DISABLED_CLASS = "pagination__component--disabled"
_CURRENT_CLASS = "pagination__page-number--current"


def has_next_page(html: str) -> bool:
    """True when this listing page's own pagination advertises a NEXT page.

    Reads the "Next" control and checks for `pagination__component--
    disabled`, which g2.com adds at either edge. Returns False when there
    is no pagination block at all (a single-page category).

    This is the cheap signal. The AUTHORITATIVE stop condition an engine
    should also honour is simpler and immune to markup changes: keep
    requesting `?page=N+1` until a fetched page yields zero
    `.category-product-card` elements (`count_result_cards(html) == 0`).
    Use both — this one to stop a page early, that one as the backstop.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        pagination = soup.select_one(_PAGINATION_SELECTOR)
        if pagination is None:
            return False
        for component in pagination.select(_PAGE_COMPONENT_SELECTOR):
            text = component.get_text(" ", strip=True).lower()
            if "next" not in text:
                continue
            classes = component.get("class") or []
            # The disabled class can sit on the component itself or on a
            # child (g2.com uses the former; the descendant check costs
            # nothing and makes a nested variant fail closed).
            if _DISABLED_CLASS in classes or component.select_one(f".{_DISABLED_CLASS}"):
                return False
            return True
        return False
    except Exception:  # noqa: BLE001
        log.exception("has_next_page raised — reporting False (stop paginating)")
        return False


def current_page_number(html: str) -> Optional[int]:
    """The page number g2.com's own pagination marks as current, or None."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(f".{_CURRENT_CLASS}")
        if node is None:
            return None
        m = _COUNT_RE.search(node.get_text(strip=True))
        return _as_int(m.group(0).replace(",", "")) if m else None
    except Exception:  # noqa: BLE001
        log.exception("current_page_number raised — reporting None")
        return None


# --------------------------------------------------------------------------- #
# Path 2: the product detail page — `SoftwareApplication` JSON-LD
# --------------------------------------------------------------------------- #
def extract_json_ld(html: str) -> List[dict]:
    """Every `<script type="application/ld+json">` block on the page, as
    dicts. A block that isn't valid JSON is skipped, not raised on."""
    soup = BeautifulSoup(html, "html.parser")
    out: List[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict):
                out.append(node)
    return out


def _is_type(node: dict, wanted: str) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return wanted in node_type
    return node_type == wanted


def _software_application_node(html: str) -> Optional[dict]:
    """The one useful JSON-LD block on a product page. The `BreadcrumbList`
    / `Organization` / `WebPage` blocks beside it carry no product data and
    are deliberately ignored."""
    for node in extract_json_ld(html):
        if _is_type(node, "SoftwareApplication"):
            return node
    return None


def _first_image(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    if isinstance(value, dict):
        return value.get("url") or None
    return None


def parse_product_page(html: str, url: str) -> Optional[Product]:
    """Parse a product `/reviews` page's `SoftwareApplication` JSON-LD into
    ONE `Product`. Returns None when the block is absent or unusable.
    **Never raises** — a caller uses the None to decide "this page didn't
    give us a product", not to handle an exception.

    The 0-10 composite score lands in `rating_10`, NOT `rating_5`, and the
    two are never reconciled (see this module's docstring and
    `output_writer.Product`'s). The scale is taken from the page's own
    `bestRating` rather than assumed: `bestRating: 10` (what g2.com
    ships) fills `rating_10`; a `bestRating: 5` — which g2.com is not
    known to serve here, but which a markup change could introduce —
    fills `rating_5` instead. Anything else leaves BOTH empty rather than
    guessing which column a number belongs in.
    """
    try:
        node = _software_application_node(html)
        if node is None:
            return None
        name = node.get("name")
        agg = node.get("aggregateRating")
        agg = agg if isinstance(agg, dict) else {}
        rating_value = _as_float(agg.get("ratingValue"))
        best_rating = _as_float(agg.get("bestRating"))
        review_count = _as_int(agg.get("reviewCount"))

        rating_5: Optional[float] = None
        rating_10: Optional[float] = None
        if rating_value is not None:
            if best_rating is None or best_rating == 10:
                rating_10 = rating_value
            elif best_rating == 5:
                rating_5 = rating_value
            else:
                log.warning(
                    "product page aggregateRating had an unrecognised bestRating (%r) — "
                    "leaving both rating columns empty rather than guessing the scale",
                    agg.get("bestRating"),
                )

        if name is None and rating_value is None and review_count is None:
            # A SoftwareApplication block with nothing in it is not a product.
            return None

        canonical = node.get("url") or url
        slug = slug_from_product_url(canonical) or slug_from_product_url(url)

        return Product(
            sku=make_sku(slug, canonical),
            source=SOURCE,
            # G2's category for the product is on the page's BreadcrumbList,
            # not on this node; the listing parser is where `category` gets
            # filled, from the listing the row came from.
            category=None,
            title=name,
            brand=None,  # see Product's docstring — no confirmed vendor NAME field
            price=None,
            currency=None,
            price_source=None,
            product_url=canonical,
            image_url=_first_image(node.get("image")),
            scraped_at=_now_iso(),
            rating_5=rating_5,
            rating_10=rating_10,
            review_count=review_count,
            review_count_source="json_ld" if review_count is not None else None,
            # `product_id` / `product_uuid` / `vendor_id` / `category_id`
            # are listing-card fields (`data-event-options`); no confirmed
            # equivalent exists in this JSON-LD block, so they stay empty
            # here and a caller merges them in from the listing row via
            # `sku` (the slug — which is exactly why `sku` is the slug).
            product_slug=slug,
        )
    except Exception:  # noqa: BLE001 — CLAUDE.md §6: never crash a run on one page
        log.exception("parse_product_page raised — treating this page as unparseable")
        return None


def count_product_page_data(html: str) -> int:
    """**The DETAIL-page-shaped counter** for
    `captcha_solver.solve_when_blocked(count_product_links=...)`.

    Answers the only question that matters on a product page: did the real
    detail parser already find real data here? 1 if yes, 0 if not. This is
    the counter bug class 2 (module docstring) exists to make unmissable —
    passing the listing-shaped `count_result_cards()` here instead returns
    0 on every perfectly normal product page, because a detail page has no
    search-result cards by construction.
    """
    try:
        node = _software_application_node(html)
        if node is None:
            return 0
        agg = node.get("aggregateRating")
        has_rating = isinstance(agg, dict) and agg.get("ratingValue") is not None
        return 1 if (node.get("name") or has_rating) else 0
    except Exception:  # noqa: BLE001 — a counter must never raise into a captcha decision
        log.exception("count_product_page_data raised — reporting 0")
        return 0


# --------------------------------------------------------------------------- #
# Path 3: the pricing page — BEST-EFFORT TEXT PARSING, and the least
# reliable thing in this file by a wide margin. g2.com ships NO pricing
# JSON-LD (confirmed), so there is nothing structured to fall back to.
# Everything here degrades to an empty list; nothing here raises.
# --------------------------------------------------------------------------- #
@dataclass
class PricingTier:
    """One parsed pricing row, e.g.
    `Sales Hub Starter — $20 / 1 Core Seat Per Month`.

    `price_unit` is the free text after the `/` (`"1 Core Seat Per Month"`)
    and is left as G2 printed it — normalising it into a billing period
    would be inventing structure the page does not have. `currency` is
    INFERRED from the symbol; `raw_line` is kept so a consumer can always
    see what was actually on the page.
    """
    tier_name: str
    price: Optional[float]
    price_unit: Optional[str] = None
    currency: Optional[str] = None
    raw_line: Optional[str] = None


# Confirmed symbols only. An unrecognised symbol yields currency=None
# rather than a guessed code.
_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}

# `Tier Name — $20 / 1 Core Seat Per Month`. G2 uses an em dash; en dash
# and hyphen are accepted too, since the separator is presentational and
# has no confirmed stability. The price must carry a currency symbol —
# without one a line is prose, not a tier.
_TIER_LINE_RE = re.compile(
    r"^(?P<name>[^\n]{1,120}?)\s*[—–-]\s*"
    r"(?P<symbol>[$€£])\s*(?P<price>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*/\s*(?P<unit>[^\n]{1,80}?))?\s*$"
)

# Lines that look like a tier but are not one: the page's own footnote and
# summary sentence both contain a `$` amount.
_NOT_A_TIER_MARKERS = (
    "pricing information is supplied",
    "pricing editions",
    "final cost negotiations",
)


def parse_pricing_page(html: str, url: str = "") -> List[PricingTier]:
    """Best-effort parse of `/products/{slug}/pricing` into tiers.

    **Explicitly less reliable than `parse_category_listing()` and
    `parse_product_page()`**, both of which read structured JSON. This one
    reads rendered prose against a line shape observed once, live, on one
    product. It returns `[]` — never raises, never partially-credible
    garbage — when the page doesn't match, and a caller should treat an
    empty list as "no pricing could be read", not as "this product is
    free". `# TODO(readme):` the README must state this ranking plainly
    (CLAUDE.md §15).

    `url` is accepted for symmetry with the other parsers and for log
    context; nothing is derived from it.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n")
        tiers: List[PricingTier] = []
        seen: set = set()
        for raw_line in text.splitlines():
            line = " ".join(raw_line.split())
            if not line or line.startswith("*"):
                continue
            lowered = line.lower()
            if any(marker in lowered for marker in _NOT_A_TIER_MARKERS):
                continue
            m = _TIER_LINE_RE.match(line)
            if not m:
                continue
            name = m.group("name").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            symbol = m.group("symbol")
            unit = m.group("unit")
            tiers.append(PricingTier(
                tier_name=name,
                price=_as_float(m.group("price").replace(",", "")),
                price_unit=unit.strip() if unit else None,
                currency=_CURRENCY_BY_SYMBOL.get(symbol),
                raw_line=line,
            ))
        if not tiers:
            log.info("pricing page yielded no recognisable tier lines%s",
                     f" ({url})" if url else "")
        return tiers
    except Exception:  # noqa: BLE001 — CLAUDE.md §6: degrade, never crash
        log.exception("parse_pricing_page raised — returning no tiers")
        return []


def count_pricing_tiers(html: str) -> int:
    """**The PRICING-page-shaped counter** for
    `captcha_solver.solve_when_blocked(count_product_links=...)`, for an
    engine that gives the pricing page the same captcha treatment as the
    other two. Same rule as the other counters: pass the one that matches
    the page you are actually looking at (module docstring, bug class 2).
    """
    try:
        return len(parse_pricing_page(html))
    except Exception:  # noqa: BLE001
        log.exception("count_pricing_tiers raised — reporting 0")
        return 0


def apply_pricing_tiers(product: Product, tiers: Sequence[PricingTier]) -> Product:
    """Fill a `Product`'s `price` / `currency` / `price_source` from parsed
    pricing tiers, using the LOWEST priced tier (which is what G2's own
    "starting from $X" summary reports, and the only choice that is
    comparable across products).

    `price_source` is set to `"pricing_page_text"` — deliberately distinct
    from the `"json_ld"` / `"embedded_json"` values other family members
    use, so `diff_runs.py`'s `source_changed` bucket and any human reading
    a row can tell a best-effort text-scraped price apart from a
    structured one without consulting docs. Mutates and returns the same
    object; a no-op when there are no priced tiers.
    """
    priced = [t for t in tiers if t.price is not None]
    if not priced:
        return product
    cheapest = min(priced, key=lambda t: t.price)  # type: ignore[arg-type,return-value]
    product.price = cheapest.price
    product.currency = cheapest.currency
    product.price_source = "pricing_page_text"
    return product


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.I)


def diagnose_unexpected_page(html: str) -> str:
    """Best-effort one-line summary of what a page actually contains, for
    the "zero products found, but not flagged as blocked" warning an engine
    logs when a listing URL comes back empty. Not a classifier and not a
    verdict — just enough surface detail (the page's own `<title>`, whether
    each page shape's own structural marker is present at all, whether
    DataDome's own tag is on the page) that nobody has to reach for
    `--dump-html` and grep a captured page by hand to tell "this category
    genuinely ended" apart from "g2.com served something else entirely".
    """
    try:
        m = _TITLE_RE.search(html)
        title = m.group(1).strip() if m else ""
        lowered = html.lower()
        return (
            f"title={title!r}, "
            f"category-cards={count_result_cards(html)}, "
            f"software-application-jsonld={'yes' if _software_application_node(html) else 'no'}, "
            f"datadome-tag={'yes' if 'datadome' in lowered else 'no'}, "
            f"page-bytes={len(html)}"
        )
    except Exception:  # noqa: BLE001 — a diagnostic must never be the thing that crashes a run
        log.exception("diagnose_unexpected_page raised")
        return f"diagnostic unavailable, page-bytes={len(html)}"


def now_iso() -> str:
    """The exact `scraped_at` stamp format every row in this repo carries.

    Made public for the engine scripts (stage 2): a `--url .../pricing`
    run builds its one row in the engine (there is no listing card and no
    `SoftwareApplication` JSON-LD on that page to build it from), and it
    must stamp that row the same way every parser-built row is stamped.
    Three engines reaching into a private `_now_iso` — or each formatting
    its own timestamp slightly differently — is how a column drifts."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Kept so the parser internals above read unchanged; `now_iso()` is the
# name a caller outside this module should use.
_now_iso = now_iso
