#!/usr/bin/env python3
"""captcha_solver.py — detection + solving policy. Comments may name a site
for context; no site-specific selector or URL lives here. Per-site markers
are passed in by the caller (see BOT_CHALLENGE_MARKERS in g2_parser.py)
rather than hardcoded, so this module stays reusable across the family.

Policy, matching the family's hard-won rule that "detected != blocking":

  - Detection stays broad — run it on every page, unconditionally.
  - A captcha WIDGET present on a page whose products are already rendered
    guards nothing and is not solved: `--solve-captcha when-blocked` (the
    default) only pays for a solve when `count_product_links(html) == 0`.
  - A missing key or a solver-side error is a WARNING, never a crash — the
    run continues, and only reports EXIT_BLOCKED if the page really was
    gated (see output_writer.EXIT_BLOCKED).

**`count_product_links` is a page-SHAPE-specific callable, and passing the
wrong one is a real, already-shipped-and-fixed bug class** (shein-scraper,
2026-09-22 — see that repo's CHANGELOG and this repo's own `g2_parser.py`
module docstring). It answers "does this page ALREADY have the real
content it is supposed to have, so don't pay for a solve" — which means a
LISTING page must pass a listing-card counter, a product-DETAIL page must
pass a "did the detail parser find real data" counter, and a pricing page
its own counter. A listing-shaped counter used on a detail page returns 0
by construction on every completely normal page load, which turns any
stray generic marker into a false "possibly blocked" read and, when a real
sitekey also happens to be present, into a wasted PAID solve. There is no
safe default here: every call site passes its own.

**DataDome (g2.com's own bot defense — `window.DataDomeJsTag`,
`dd.g2.com/js/`, confirmed live) IS solvable — corrected 2026-09-22.**
The original version of this module claimed "no automated solve path at
all," treating DataDome the same as PerimeterX. That was wrong, caught by
Roman (2Captcha's own team) pointing at
https://2captcha.com/api-docs/datadome-slider-captcha: **2Captcha ships a
dedicated `DataDomeSliderTask`** for exactly this vendor's own interstitial
slider challenge. `CaptchaType.DATADOME_SLIDER` below maps to it. Two real
constraints from that same page, both enforced in `_task_payload()` rather
than documented-and-ignored (CLAUDE.md §17's standing lesson):

  - **No proxyless variant exists.** `proxyType`/`proxyAddress`/
    `proxyPort` are required fields, not optional ones — DataDome
    validates the solved challenge against the IP that presents it
    afterward, so `solve_when_blocked()`'s `proxy=` argument is mandatory
    for this type specifically (every other type here defaults to
    proxyless). Calling it for a DataDome challenge with no proxy returns
    a `"warning_no_proxy"` action, not a crash and not a silent skip.
  - **The solution is a `Set-Cookie`-shaped string** (`"datadome=...;
    Path=/; Secure; SameSite=Lax"`), not a token for a hidden form field —
    structurally closer to a session credential than to a Turnstile/
    reCAPTCHA response. `build_injection_script()` below does not cover
    it (writing a *Secure* cookie via `document.cookie` from JS is
    unreliable/blocked depending on scheme); `parse_datadome_cookie()`
    turns the raw string into a dict shaped for each driver's own NATIVE
    cookie-setting API instead (Playwright `context.add_cookies`,
    Selenium `driver.add_cookie`, pyppeteer `page.setCookie`) — the
    engine still owns actually calling its own driver, same split as
    everywhere else in this module.

**This is still unconfirmed against a real, live g2.com capture** — no
engine in this family has ever seen g2.com actually present a visible
DataDome slider (only the always-on `window.DataDomeJsTag`/cookie, which
is DataDome's silent device-check, not a challenge). The iframe `src`
pattern `_DATADOME_IFRAME_RE` looks for
(`geo.captcha-delivery.com/captcha/?...&t=fe...`) is 2Captcha's own
documented shape for the vendor generally, not something captured from
g2.com specifically — same "standard vendor convention, unconfirmed on
this site" honesty posture as the GeeTest patterns below. A bare DataDome
marker with NO such iframe present (the ordinary case, expected on most
page loads) still has nothing to solve — `identify_unsupported_vendor()`
and the `"unsupported_vendor"` action (restored from skyscanner-scraper,
which hit the identical situation with PerimeterX, which genuinely has no
2Captcha task type at all) still fire for THAT case, now meaning "DataDome
is present but not currently challenging this request," not "DataDome can
never be solved."

No page-execution primitive (page.evaluate / execute_script) crosses this
module's boundary: `build_injection_script()` below only BUILDS a plain
JavaScript source string — driver-agnostic, since it's just text — and the
engine (which already speaks its own driver's dialect) is the one that
actually runs it. That split is the same one `solve_when_blocked()` uses
for detection/solving: this module owns policy and JS-source construction,
never the act of running JS against a real page.

**Until 2026-09-21, no engine in this family actually injected a solved
token back into a locally-launched (non `--cdp-endpoint`) browser at
all** — every engine's own `_maybe_solve_captcha` only logged "solved", a
real gap Roman asked about directly after GeeTest support was added
without it. `build_injection_script()` closes that gap using each widget
type's OWN standard, publicly-documented client-integration convention
(a hidden `g-recaptcha-response` textarea for reCAPTCHA v2, a
`cf-turnstile-response` field for Turnstile, and so on) — **not** anything
confirmed against a real g2.com (or any family site's) actual widget
markup, which has never been captured for ANY type. It is the same
"standard convention, explicitly unconfirmed against this site" honesty
posture `g2_parser.py`'s own best-effort parsers use. reCAPTCHA v3
has no such convention — it's invisible, and the token is typically
consumed the instant the SITE'S OWN JavaScript resolves
`grecaptcha.execute()`'s promise (often straight into an XHR, never read
back off a DOM element) — so `build_injection_script()` returns `None` for
it rather than guess at site-specific code, which this shared module's own
charter says does not belong here. None of this has been exercised against
g2.com, whose only confirmed defense (DataDome) is in the
`identify_unsupported_vendor()` bucket above, not the solvable one.

Over `--cdp-endpoint`, none of this applies: 2Captcha's own
`Captcha.setAutoSolve` CDP domain (see each engine's own
`_enable_scraping_browser_auto_solve`) solves AND injects entirely inside
their infrastructure, for whichever widget types their Scraping Browser
extension recognizes (confirmed live, 2026-09-14: Turnstile, Amazon WAF,
Yandex SmartCaptcha, Lemin — GeeTest was NOT in that confirmed list,
though it may be covered and simply wasn't seen in that one capture).
"""
from __future__ import annotations

import html as html_module  # module param name `html` shadows the stdlib module below
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

# Generic signals any family site might show. A per-site BOT_CHALLENGE_MARKERS
# list (product_parser.py) is unioned with this, never a replacement for it.
GENERIC_BOT_CHALLENGE_MARKERS: Sequence[str] = (
    "cf-turnstile", "challenges.cloudflare.com", "cdn-cgi/challenge-platform",
    "g-recaptcha", "recaptcha/api.js", "grecaptcha",
    "h-captcha", "hcaptcha.com/1/api.js",
    "px-captcha", "perimeterx",
    "datadome",
    "attention required", "just a moment", "verify you are human",
)


class CaptchaType(str, Enum):
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    GEETEST_V3 = "geetest_v3"
    GEETEST_V4 = "geetest_v4"
    # 2026-09-22: maps to 2Captcha's DataDomeSliderTask. See module
    # docstring for why this is NOT the same shape as everything else
    # above (mandatory proxy, cookie-shaped solution, unconfirmed-on-g2.com
    # iframe pattern).
    DATADOME_SLIDER = "datadome_slider"


_SITEKEY_PATTERNS = {
    CaptchaType.CLOUDFLARE_TURNSTILE: re.compile(
        r'class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.RECAPTCHA_V2: re.compile(
        r'class="[^"]*g-recaptcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.HCAPTCHA: re.compile(
        r'class="[^"]*h-captcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
}
# v3 ships no visible widget — the sitekey rides on the loader's `render=`
# query param instead of a data-sitekey attribute.
_RECAPTCHA_V3_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=([\w-]+)")
_RECAPTCHA_EXPLICIT_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=explicit")

# reCAPTCHA v2's sitekey doesn't always ship as static `<div class="g-
# recaptcha" data-sitekey="...">` markup, the only shape
# `_SITEKEY_PATTERNS[RECAPTCHA_V2]` above can see — CONFIRMED live on a
# SIBLING family site (shein.com, 2026-09-21), never on g2.com, which has
# no confirmed reCAPTCHA at all (only DataDome). Kept because the gap it
# closes is generic, not site-specific: the site
# loads `https://www.google.com/recaptcha/api.js` and has a live
# `window.grecaptcha` v2 object (`render`/`execute`/`getResponse`/
# `reset`/`ready` — not `.enterprise`) on every normal page, but the
# sitekey itself lives in a JS config object under a site-chosen key
# name (`window.gbCommonInfo.GOOGLE_VERIFY_SITEKEY`), presumably handed
# to `grecaptcha.render()` programmatically later (e.g. after a
# suspicious login attempt) rather than rendered into the initial HTML
# at all. The static-markup regex above would silently MISS this real,
# live sitekey — a real detection gap, not a hypothetical one. reCAPTCHA
# sitekeys have a fixed, distinctive shape regardless of what variable
# or markup carries them (`6L` + 38 more URL-safe-base64 characters, 40
# total, confirmed against Google's own documented format and against
# shein.com's actual `6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j`) — so
# this searches for that shape ANYWHERE in the page, gated on the v2
# loader actually being present so it can't misfire on an unrelated
# 40-character token elsewhere that happens to start with "6L".
_RECAPTCHA_SITEKEY_ANYWHERE_RE = re.compile(r"\b(6L[\w-]{38})\b")
_RECAPTCHA_V2_LOADER_RE = re.compile(r"recaptcha/api\.js")

# GeeTest — added 2026-09-21 in shein-scraper after shein.com's own risk
# gateway (captcha_type=909) correlated
# with GeeTest via three circumstantial signals documented in
# that repo's parser module docstring (the site's global stylesheet defines
# .geetest_wind/.geetest_panel on EVERY page, robots.txt disallows
# /geetest/, and the numeric captcha_type code is consistent with GeeTest's
# own convention) — NOT a confirmed vendor string the way every other
# CaptchaType below was found (an actual `cf-turnstile`/`g-recaptcha`/
# `h-captcha` class or loader URL, seen live). No family site, shein.com
# included, has ever had its actual GeeTest widget markup captured — only
# a waiting/redirect shell page, never what renders when a real challenge
# is actually shown. These two patterns are therefore UNCONFIRMED
# best-effort, built from GeeTest's own public client-integration
# documentation (a `data-captcha-id`/`captchaId` attribute for v4; `gt`
# and `challenge` fields together for v3 — both are 32-character
# hex-like ids in GeeTest's own docs), not a shein.com-specific fact.
# `# TODO: verify live` applies here exactly like `g2_parser.py`'s own
# best-effort pricing-page parser — update these the moment a real
# capture exists.
_GEETEST_V4_ID_PATTERNS = (
    re.compile(r'data-captcha-id=["\']([a-f0-9]{32})["\']'),
    re.compile(r'captchaId["\']?\s*:\s*["\']([a-f0-9]{32})["\']'),
)
_GEETEST_V3_RE = re.compile(
    r'gt["\']?\s*:\s*["\']([a-f0-9]{32})["\'][^{}]*?challenge["\']?\s*:\s*["\']([a-f0-9]{32})["\']',
    re.S,
)

# DataDome's own interstitial slider — confirmed shape from 2Captcha's
# published integration docs (https://2captcha.com/api-docs/datadome-
# slider-captcha and their "how to bypass DataDome captcha" guide), NOT
# from a captured g2.com page (see module docstring). The query string is
# long and vendor-controlled (`initialCid`/`hash`/`cid`/`t`/`referer`
# among others) so this only anchors on host+path and takes everything up
# to the closing quote, rather than trying to name every param.
_DATADOME_IFRAME_RE = re.compile(
    r'<iframe\b[^>]*\bsrc=(["\'])'
    r'(https://geo\.captcha-delivery\.com/captcha/\?[^"\']+)\1',
    re.I,
)


@dataclass
class CaptchaSignal:
    captcha_type: CaptchaType
    sitekey: Optional[str]
    invisible: bool = False
    # GeeTest-only fields (see the module-docstring note above on why
    # GeeTest doesn't fit the plain sitekey shape every other type uses).
    gt: Optional[str] = None
    challenge: Optional[str] = None
    captcha_id: Optional[str] = None
    api_server: Optional[str] = None
    # DATADOME_SLIDER-only: the challenge iframe's own `src`, unescaped —
    # 2Captcha's `captchaUrl` field, "the value of the src parameter for
    # the iframe element" per their own docs. No other type uses this.
    captcha_url: Optional[str] = None


# The Scraping Browser API's managed Chromium ships 2Captcha's OWN
# captcha-solving extension pre-installed (confirmed live, 2026-09-14:
# extension id kjmkgkdkpedkejedfhmfcenooemhbpbo, content scripts named
# .../captcha/{turnstile,amazon_waf,yandex,lemin}/{interceptor,hunter}.js).
# `page.content()` over --cdp-endpoint captures THOSE injected <script>
# tags too, and their src/data attributes contain marker substrings like
# "cf-turnstile" regardless of whether the actual page has a real widget —
# on a real StockX 403 that turned out to be a plain, static Cloudflare
# WAF page with no captcha at all ("Sorry, you have been blocked" — no
# `data-sitekey` anywhere), this alone made GENERIC_BOT_CHALLENGE_MARKERS
# fire and identify_widget() correctly find nothing to solve, which then
# logged as a confusing "captcha-like marker detected but no known
# widget/sitekey" rather than a clean "no captcha present". Stripped here
# so the extension's own always-there code never counts as the page's.
_EXTENSION_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bchrome-extension://[^>]*>.*?</script>', re.I | re.S
)


def _strip_extension_noise(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html)


def detect_from_html(html: str, extra_markers: Sequence[str] = ()) -> bool:
    """Broad detection: True if ANY known marker string appears. Cheap and
    deliberately over-inclusive — see module docstring on why detection and
    blocking are decided separately."""
    haystack = _strip_extension_noise(html).lower()
    for marker in (*GENERIC_BOT_CHALLENGE_MARKERS, *extra_markers):
        if marker.lower() in haystack:
            return True
    return False


# Markers whose VENDOR is known, but for which `identify_widget()` found
# no CURRENTLY-solvable challenge on THIS page. Restored VERBATIM from
# skyscanner-scraper's captcha_solver.py (which added it 2026-09-20 after
# 2Captcha's own team confirmed PerimeterX has no task type at all —
# "there is no solve path here, full stop" for that vendor specifically);
# shein-scraper's copy — the newest sibling and this repo's porting
# source — had dropped it, which per CLAUDE.md §7 reads as a divergence
# rather than a decision, since nothing in shein-scraper needed it.
#
# `"datadome"` in this dict does NOT mean the same thing as
# `"perimeterx"`/`"cloudflare_managed_challenge"` any more (corrected
# 2026-09-22 — see module docstring). DataDome DOES have a real 2Captcha
# task type (`CaptchaType.DATADOME_SLIDER`), already caught by
# `identify_widget()` above when its slider iframe is present. This entry
# is reached only for the OTHER case: DataDome's marker is present
# (`window.DataDomeJsTag`, a `datadome` cookie — on every g2.com page,
# confirmed live) but no `geo.captcha-delivery.com/captcha/` iframe was
# found, i.e. DataDome is watching this request but not currently
# challenging it. Without this branch that case reports the vague
# `detected_unidentified_widget` ("we failed to parse it"); with it, the
# run reports `unsupported_vendor` + `vendor="datadome"` ("present, no
# active challenge to solve here"). Neither wording should be read as "not
# solvable at all" for DataDome any more — that claim was the bug Roman
# caught.
#
# Checked ONLY after `identify_widget()` already returned None for a marker
# that DID match `GENERIC_BOT_CHALLENGE_MARKERS`/`extra_markers` — a real
# Turnstile / reCAPTCHA / hCaptcha / GeeTest / DataDome-slider widget is
# solvable and is already caught by `identify_widget()` before this is
# ever consulted.
_UNSUPPORTED_VENDOR_MARKERS = {
    "perimeterx": ("px-captcha", "perimeterx"),
    "datadome": ("datadome",),
    "cloudflare_managed_challenge": ("cdn-cgi/challenge-platform", "just a moment", "attention required"),
}


def identify_unsupported_vendor(html: str) -> Optional[str]:
    """Best-effort: names WHICH known bot-defense vendor is present, for the
    case where a marker matched but `identify_widget()` could not turn it
    into a solvable `(type, sitekey)` pair — because no such pair exists
    for this vendor, not because extraction failed. Returns a short vendor
    name for an honest log message; the caller must not treat this as
    something actionable (see `solve_when_blocked`'s `"unsupported_vendor"`
    action, which reports EXIT_BLOCKED same as any other block, just with
    an accurate reason instead of a vague one)."""
    haystack = _strip_extension_noise(html).lower()
    for vendor, markers in _UNSUPPORTED_VENDOR_MARKERS.items():
        if any(m in haystack for m in markers):
            return vendor
    return None


def identify_widget(html: str) -> Optional[CaptchaSignal]:
    """Best-effort: which widget, and its sitekey. Reconciles the v3-vs-
    v2-invisible ambiguity by trusting the LOADER'S query param over any
    wrapper metadata — a site can label its own wrapper "v3" while shipping
    a `render=explicit` (v2-invisible) loader; sending v3 parameters for a
    v2-invisible widget buys a token the site rejects. `render=<sitekey>`
    means v3; `render=explicit` means v2."""
    html = _strip_extension_noise(html)
    m = _RECAPTCHA_V3_LOADER_RE.search(html)
    if m and not _RECAPTCHA_EXPLICIT_LOADER_RE.search(html):
        return CaptchaSignal(CaptchaType.RECAPTCHA_V3, sitekey=m.group(1))
    for ctype, pattern in _SITEKEY_PATTERNS.items():
        m = pattern.search(html)
        if m:
            return CaptchaSignal(ctype, sitekey=m.group(1))
    # Fallback for a reCAPTCHA v2 sitekey that isn't in static markup at
    # all — see _RECAPTCHA_SITEKEY_ANYWHERE_RE's module-level comment for
    # the live shein.com evidence this closes a real gap for.
    if _RECAPTCHA_V2_LOADER_RE.search(html):
        m = _RECAPTCHA_SITEKEY_ANYWHERE_RE.search(html)
        if m:
            return CaptchaSignal(CaptchaType.RECAPTCHA_V2, sitekey=m.group(1))
    # GeeTest — v4 first (current-generation; see the UNCONFIRMED note on
    # the patterns themselves, above).
    for pattern in _GEETEST_V4_ID_PATTERNS:
        m = pattern.search(html)
        if m:
            return CaptchaSignal(CaptchaType.GEETEST_V4, sitekey=None, captcha_id=m.group(1))
    m = _GEETEST_V3_RE.search(html)
    if m:
        return CaptchaSignal(CaptchaType.GEETEST_V3, sitekey=None, gt=m.group(1), challenge=m.group(2))
    # DataDome slider — checked last, after every solvable-without-proxy
    # type above, since it's the only type here that REQUIRES a proxy to
    # solve at all (see module docstring); a caller with no proxy still
    # gets an honest "unsupported_vendor"/"warning_no_proxy" outcome
    # rather than a silently-skipped detection.
    m = _DATADOME_IFRAME_RE.search(html)
    if m:
        return CaptchaSignal(
            CaptchaType.DATADOME_SLIDER, sitekey=None,
            captcha_url=html_module.unescape(m.group(2)),
        )
    return None


def _task_payload(
    signal: CaptchaSignal, page_url: str, *, proxyless: bool, min_score: float = 0.3,
    proxy: Optional[dict] = None, user_agent: Optional[str] = None,
) -> dict:
    # GeeTest's task shape has no `websiteKey` at all (v3 sends `gt`/
    # `challenge`, v4 sends `captchaId`) — confirmed against 2Captcha's own
    # published API reference for GeeTestTask(Proxyless)/GeeTestV4Task
    # (Proxyless), unlike identify_widget()'s extraction patterns above,
    # which are NOT confirmed against a real shein.com capture.
    if signal.captcha_type == CaptchaType.DATADOME_SLIDER:
        # No proxyless variant exists for this type (confirmed against
        # 2Captcha's own published spec — see module docstring); `proxy`
        # is required here regardless of the `proxyless` flag the caller
        # passed for every OTHER type. Raising (not silently proceeding
        # without a proxy) matches this module's existing "a solver-side
        # error is a WARNING, never silently wrong" policy — the caller
        # (solve_when_blocked) turns this specific message into its own
        # named `"warning_no_proxy"` action rather than the generic
        # `"warning_solver_error"` bucket, so a log/metrics consumer can
        # tell "no key" / "no proxy" / "solver failed" apart.
        if not proxy or not proxy.get("address") or not proxy.get("port"):
            raise TwoCaptchaError(
                "DataDomeSliderTask requires a proxy (type/address/port) — "
                "DataDome validates the solved challenge against the IP that "
                "presents it afterward, so there is no proxyless path for this "
                "vendor; none was supplied to solve_when_blocked()"
            )
        if not user_agent:
            raise TwoCaptchaError(
                "DataDomeSliderTask requires user_agent — 2Captcha's own docs "
                "say to send the SAME modern browser UA the challenge will be "
                "presented back to; none was supplied to solve_when_blocked()"
            )
        task = {
            "type": "DataDomeSliderTask",
            "websiteURL": page_url,
            "captchaUrl": signal.captcha_url,
            "userAgent": user_agent,
            "proxyType": proxy.get("type", "http"),
            "proxyAddress": proxy["address"],
            "proxyPort": proxy["port"],
        }
        if proxy.get("login"):
            task["proxyLogin"] = proxy["login"]
        if proxy.get("password"):
            task["proxyPassword"] = proxy["password"]
        return task
    if signal.captcha_type == CaptchaType.GEETEST_V4:
        return {
            "type": "GeeTestV4TaskProxyless" if proxyless else "GeeTestV4Task",
            "websiteURL": page_url,
            "captchaId": signal.captcha_id,
        }
    if signal.captcha_type == CaptchaType.GEETEST_V3:
        task = {
            "type": "GeeTestTaskProxyless" if proxyless else "GeeTestTask",
            "websiteURL": page_url,
            "gt": signal.gt,
            "challenge": signal.challenge,
        }
        if signal.api_server:
            task["geetestApiServerSubdomain"] = signal.api_server
        return task

    type_map = {
        CaptchaType.CLOUDFLARE_TURNSTILE: "TurnstileTaskProxyless" if proxyless else "TurnstileTask",
        CaptchaType.RECAPTCHA_V2: "RecaptchaV2TaskProxyless" if proxyless else "RecaptchaV2Task",
        CaptchaType.RECAPTCHA_V3: "RecaptchaV3TaskProxyless",
        CaptchaType.HCAPTCHA: "HCaptchaTaskProxyless" if proxyless else "HCaptchaTask",
    }
    try:
        task_type = type_map[signal.captcha_type]
    except KeyError:
        # A CaptchaType with no 2Captcha task type behind it. Nothing in
        # this module can produce one today (`identify_widget()` only ever
        # returns members that are mapped, and a vendor with no solve path
        # — DataDome, PerimeterX — is deliberately NOT a CaptchaType at
        # all; see `identify_unsupported_vendor()`). This guard exists so
        # that if a future member IS added without a mapping, the run gets
        # `solve_when_blocked`'s documented "a solver-side error is a
        # WARNING, never a crash" behaviour instead of a bare KeyError
        # escaping through a `except TwoCaptchaError` that never sees it.
        raise TwoCaptchaError(
            f"no 2Captcha task type is defined for {signal.captcha_type.value} — "
            f"it cannot be solved automatically"
        ) from None
    task = {"type": task_type, "websiteURL": page_url, "websiteKey": signal.sitekey}
    if signal.captcha_type == CaptchaType.RECAPTCHA_V3:
        # 2Captcha's own field name for RecaptchaV3TaskProxyless (confirmed
        # against their API reference) — the token 2Captcha hands back is
        # produced to clear THIS threshold; it is a solve-request input, not
        # something to validate against the response afterwards. `--min-
        # score` is the CLI knob; omitting it would leave 2Captcha's own
        # server-side default in effect, silently, which is the same
        # "documented flag nobody reads" shape this family has shipped
        # before (CLAUDE.md §17) — so this module always sends one.
        task["minScore"] = min_score
    return task


def solve_when_blocked(
    *,
    client: TwoCaptchaClient,
    page_url: str,
    html: str,
    count_product_links: Callable[[str], int],
    extra_markers: Sequence[str] = (),
    proxyless: bool = True,
    min_score: float = 0.3,
    proxy: Optional[dict] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """The default `--solve-captcha when-blocked` policy. Cheap first: count
    product links on the page AS-IS — no readiness wait, no scroll — because
    a page whose products are already rendered is not blocked, and running
    a 20s readiness wait first would burn it even when solving is what
    actually makes products appear on a page that IS gated.

    `count_product_links` MUST be shaped for the page the CALLER is
    actually looking at — see the module docstring's second paragraph for
    the shipped-and-fixed bug this caused in shein-scraper. In this repo
    the three correct choices live in `g2_parser.py`:
    `count_result_cards` (a category-listing page),
    `count_product_page_data` (a product-detail `/reviews` page) and
    `count_pricing_tiers` (a `/pricing` page). This module deliberately
    has NO default for it: a keyword-only required argument is what makes
    a caller stop and pick.

    `proxy` / `user_agent` matter for exactly one type today:
    `CaptchaType.DATADOME_SLIDER` (see module docstring) has no proxyless
    path at all, unlike every other type here. Every other type ignores
    both. `proxy` is a plain dict: `{"type": "http"|"socks4"|"socks5",
    "address": str, "port": int, "login": str|None, "password": str|None}`
    — shaped to pass straight through from whatever `proxy_pool.py`
    already handed the engine for its own browser launch, not a new
    credential source.
    """
    if not detect_from_html(html, extra_markers):
        return {"action": "no_captcha_detected"}

    if count_product_links(html) > 0:
        return {"action": "skipped_products_present"}

    signal = identify_widget(html)
    if signal is None:
        vendor = identify_unsupported_vendor(html)
        if vendor:
            return {"action": "unsupported_vendor", "vendor": vendor}
        return {"action": "detected_unidentified_widget"}

    try:
        task = _task_payload(
            signal, page_url, proxyless=proxyless, min_score=min_score,
            proxy=proxy, user_agent=user_agent,
        )
        token = client.solve_and_wait(task)
        return {"action": "solved", "captcha_type": signal.captcha_type.value, "token": token}
    except TwoCaptchaAuthError as exc:
        return {"action": "warning_no_key", "detail": str(exc)}
    except TwoCaptchaError as exc:
        # DataDomeSliderTask's own two required-input checks above raise
        # this same exception type (this module's policy is "solver error
        # is a warning, never a crash," and a missing prerequisite is no
        # exception) — split out by message prefix so a caller/metrics
        # consumer can tell "we never even tried, we're missing a proxy"
        # apart from "we tried 2Captcha and it failed," rather than both
        # landing in one generic bucket.
        if signal.captcha_type == CaptchaType.DATADOME_SLIDER and "requires a proxy" in str(exc):
            return {"action": "warning_no_proxy", "detail": str(exc)}
        return {"action": "warning_solver_error", "detail": str(exc)}


def build_injection_script(captcha_type: CaptchaType, solution: str) -> Optional[str]:
    """Build (never run) the JavaScript that writes a solved captcha back
    into the page, for an engine to hand to its own page.evaluate /
    execute_script. See the module docstring for the honesty caveat that
    applies to every branch here: each uses that widget's own standard,
    publicly-documented integration convention, NOT anything confirmed
    against a real family-site capture. `solution` is `result["token"]`
    from `solve_when_blocked()` — a plain string for
    Turnstile/reCAPTCHA-v2/hCaptcha, a JSON-encoded solution dict for
    GeeTest v3/v4 (see `scraper_api_client.solve_and_wait`'s docstring).

    Returns `None` for reCAPTCHA v3 (see module docstring — no generic,
    site-agnostic injection point exists for it) and for any type this
    function doesn't recognize, rather than build JS that would silently
    do nothing.
    """
    token_js = json.dumps(solution)

    if captcha_type == CaptchaType.CLOUDFLARE_TURNSTILE:
        return f"""(function() {{
  var token = {token_js};
  var el = document.querySelector('[name="cf-turnstile-response"]') || document.getElementById('cf-turnstile-response');
  if (el) {{ el.value = token; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
  var widget = document.querySelector('.cf-turnstile');
  var cb = widget && widget.getAttribute('data-callback');
  if (cb && typeof window[cb] === 'function') {{ window[cb](token); }}
  return !!el || !!cb;
}})();"""

    if captcha_type == CaptchaType.RECAPTCHA_V2:
        return f"""(function() {{
  var token = {token_js};
  var el = document.getElementById('g-recaptcha-response') || document.querySelector('[name="g-recaptcha-response"]');
  if (el) {{ el.style.display = ''; el.value = token; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
  var widget = document.querySelector('.g-recaptcha');
  var cb = widget && widget.getAttribute('data-callback');
  if (cb && typeof window[cb] === 'function') {{ window[cb](token); }}
  return !!el || !!cb;
}})();"""

    if captcha_type == CaptchaType.HCAPTCHA:
        return f"""(function() {{
  var token = {token_js};
  var el = document.querySelector('[name="h-captcha-response"]') || document.getElementById('h-captcha-response');
  if (el) {{ el.value = token; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
  var widget = document.querySelector('.h-captcha');
  var cb = widget && widget.getAttribute('data-callback');
  if (cb && typeof window[cb] === 'function') {{ window[cb](token); }}
  return !!el || !!cb;
}})();"""

    if captcha_type == CaptchaType.GEETEST_V3:
        # 2Captcha's GeeTestTask(Proxyless) response fields are named
        # challenge/validate/seccode (confirmed against their API
        # reference); the page-side hidden fields most public GeeTest v3
        # integrations read are conventionally named with a "geetest_"
        # prefix — both spellings are tried since 2Captcha's own examples
        # are inconsistent about which one a given site expects.
        return f"""(function() {{
  var sol = JSON.parse({token_js});
  var fields = {{
    geetest_challenge: sol.challenge || sol.geetest_challenge,
    geetest_validate: sol.validate || sol.geetest_validate,
    geetest_seccode: sol.seccode || sol.geetest_seccode
  }};
  var setAny = false;
  Object.keys(fields).forEach(function(name) {{
    var val = fields[name];
    if (val == null) return;
    var el = document.getElementsByName(name)[0] || document.getElementById(name);
    if (el) {{ el.value = val; el.dispatchEvent(new Event('change', {{bubbles: true}})); setAny = true; }}
  }});
  return setAny;
}})();"""

    if captcha_type == CaptchaType.GEETEST_V4:
        # No standard hidden-field convention is publicly documented for
        # v4 the way v3 has one — 2Captcha's own docs say to pass the
        # solution to "the page's geetest_validate callback", without a
        # fixed name. This is the weakest-confidence branch here: it
        # stashes the solution where page code COULD read it, dispatches a
        # custom event, and calls one plausible global callback name IF
        # present — none of which is confirmed for any real site.
        return f"""(function() {{
  var sol = JSON.parse({token_js});
  window.__2captcha_geetest_v4_solution = sol;
  try {{ document.dispatchEvent(new CustomEvent('geetest_v4_solved', {{detail: sol}})); }} catch (e) {{}}
  if (typeof window.geetest_validate_callback === 'function') {{
    window.geetest_validate_callback(sol);
    return true;
  }}
  return false;
}})();"""

    return None


def parse_datadome_cookie(solution: str) -> dict:
    """`DATADOME_SLIDER`'s counterpart to `build_injection_script()` — but a
    DIFFERENT shape of counterpart, because the solution itself is a
    different shape (see module docstring): not a token for a hidden
    field, a `Set-Cookie`-style string. `solution` is `result["token"]`
    from `solve_when_blocked()`, which for this type is actually
    `scraper_api_client.solve_and_wait()`'s GeeTest-shaped JSON-encoded-
    dict fallback (2Captcha's `solution` here has a `cookie` key, not
    `token`/`gRecaptchaResponse`, so that method's existing fallback
    branch already carries it through correctly — no client change
    needed, just this parser on the receiving end).

    Returns a dict shaped for a driver's OWN native cookie-setting call
    (Playwright `context.add_cookies([...])`, Selenium `driver.add_cookie
    ({...})`, pyppeteer `page.setCookie({...})`) — `{"name", "value",
    "path", "secure", "same_site"}`. Deliberately does NOT set `domain`:
    that's the one field genuinely site-specific, so it's left to the
    caller (the page's own host) rather than guessed here. Raises
    TwoCaptchaError (never a bare exception) if `solution` isn't the
    cookie shape this type is supposed to produce — a malformed solve
    result is exactly the "solver-side error is a warning, never a crash"
    case this module's policy already covers everywhere else.
    """
    try:
        parsed = json.loads(solution)
        raw_cookie = parsed["cookie"]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise TwoCaptchaError(
            f"DataDomeSliderTask solution did not carry a usable cookie: {solution!r}"
        ) from exc

    # A real Set-Cookie header value: "datadome=VALUE; Path=/; Secure; SameSite=Lax"
    first, *attr_parts = raw_cookie.split(";")
    if "=" not in first:
        raise TwoCaptchaError(f"DataDomeSliderTask cookie had no name=value pair: {raw_cookie!r}")
    name, value = first.strip().split("=", 1)
    attrs: dict = {}
    for part in attr_parts:
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.strip().lower()] = v.strip()
        else:
            attrs[part.lower()] = True

    return {
        "name": name.strip(),
        "value": value.strip(),
        "path": attrs.get("path", "/"),
        "secure": bool(attrs.get("secure", True)),
        "same_site": attrs.get("samesite", "Lax"),
    }
