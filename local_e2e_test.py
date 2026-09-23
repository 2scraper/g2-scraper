#!/usr/bin/env python3
"""local_e2e_test.py — runs REAL engines, with REAL browsers, through the
REAL pipeline (navigation, retries, parsing, pagination, captcha
detection/solve, output writing, exit codes) against a local stand-in for
g2.com instead of the live site. `smoke_test.py` proves the architecture
offline with no browser at all; this proves the pipeline actually moves
data through a real browser without needing network access to g2.com
itself — the gap `smoke_test.py`'s own module docstring names as still
open, and the ONE thing CLAUDE.md's testing checklist (§10) explicitly asks
for beyond the offline suite.

**Added 2026-09-22, after this repo's first-ever live run** (previously
blocked by this project's build environment's egress policy). That first
run found and fixed two real bugs (see CHANGELOG.md's "first real
end-to-end test run" entry) — this script is what found them, trimmed
down into something repeatable rather than a one-off throwaway.

**What this does NOT prove**: that g2.com's real markup still matches the
CONFIRMED shapes captured 2026-09-22 (g2_parser.py's module docstring).
The fixtures below are the exact same synthetic-but-confirmed-shaped HTML
`smoke_test.py` already uses. Run `TESTING.md` step 2 against the real
site for that — this script is what to run FIRST, and after any change to
an engine's control flow, because it needs no real g2.com access and no
2Captcha key to do it.

**Requires at least one engine driver installed** (`pip install -r
requirements-playwright.txt` etc. — see README "Installing"). A scenario
for an engine whose driver isn't installed is skipped, not failed, same
posture as the rest of this family toward missing drivers.

Run directly: `python3 local_e2e_test.py` (playwright only, if that's all
you have installed) or `python3 local_e2e_test.py --engines playwright,selenium,puppeteer`.
"""
from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import g2_parser as gp  # noqa: E402

SOLVED_COOKIE_VALUE = "SOLVED-BY-LOCAL-E2E-TEST"
RESULTS = []  # (name, ok, detail)


def check(name):
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
            print(f"  PASS  {name}")
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
            print(f"  FAIL  {name}\n        {exc}")
        except Exception as exc:  # noqa: BLE001
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        return fn
    return decorator


# --------------------------------------------------------------------------- #
# Fixtures — identical shapes to smoke_test.py's (CONFIRMED, not invented)
# --------------------------------------------------------------------------- #
def _card(slug, name, product_id, rating="4.4", count="27,555"):
    opts = json.dumps({
        "product_id": product_id, "product_uuid": f"16e299ae-0000-0000-0000-{product_id:012d}",
        "product": name, "vendor_id": 469, "product_type": "Software", "category": "CRM",
        "category_id": 179, "resource_type": "Category", "resource_id": 179,
        "list_type": None, "is_onboarding": False, "name": "Event::Products::ListItemClicked",
    })
    return (
        '<div class="content-card category-product-card x-category-product-card">'
        f"<a data-event-options='{opts}' href=\"/products/{slug}/reviews\">{name}</a>"
        f'<div class="elv-star-wrapper">{rating}/5 '
        f'<span class="elv-star-wrapper__desc__count">({count})</span></div>'
        f'<img src="https://images.g2crowd.com/uploads/product/image/{slug}.png"></div>'
    )


def _pagination(next_disabled=False, current=1):
    next_classes = "pagination__component" + (" pagination__component--disabled" if next_disabled else "")
    return (
        '<ul class="pagination" aria-label="Pagination">'
        f'<li class="pagination__component pagination__page-number pagination__page-number--current">{current}</li>'
        '<li class="pagination__component pagination__page-number">2</li>'
        f'<li class="{next_classes}">Next</li></ul>'
    )


def listing_html(*, cards=3, next_disabled=False, current=1, offset=0):
    body = "".join(_card(f"product-{offset+i}", f"Product {offset+i}", 500 + offset + i) for i in range(1, cards + 1))
    dd = '<script>window.DataDomeJsTag = {};window.dataDomeOptions={endpoint:"https://dd.g2.com/js/"};</script>'
    return f"<html><head><title>Best CRM Software in 2026 | G2</title>{dd}</head><body>{body}{_pagination(next_disabled, current)}</body></html>"


def product_html():
    node = {
        "@context": "https://schema.org", "@type": "SoftwareApplication", "name": "HubSpot Sales Hub",
        "url": "http://127.0.0.1/products/hubspot-sales-hub/reviews",
        "image": "https://images.g2crowd.com/uploads/product/image/hubspot.png",
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": 8.9, "bestRating": 10, "worstRating": 0, "reviewCount": 14333},
        "review": [],
    }
    dd = '<script>window.DataDomeJsTag = {};</script>'
    return f'<html><head><title>HubSpot Sales Hub Reviews | G2</title><script type="application/ld+json">{json.dumps(node)}</script>{dd}</head><body>reviews</body></html>'


PRICING_HTML = (
    "<html><head><title>HubSpot Sales Hub Pricing 2026 | G2</title></head><body>"
    "<p>HubSpot Sales Hub offers 4 pricing editions, starting from $0 to $150.</p>"
    "<div>Free HubSpot CRM — $0</div><div>Sales Hub Starter — $20 / 1 Core Seat Per Month</div>"
    "</body></html>"
)

DATADOME_WALL_HTML = (
    '<html><head><title>g2.com</title></head><body><script src="https://dd.g2.com/js/"></script>'
    '<script>window.DataDomeJsTag = {};</script><p>Please enable JS</p></body></html>'
)

DATADOME_SLIDER_WALL_HTML = (
    '<html><head><title>g2.com</title></head><body><script src="https://dd.g2.com/js/"></script>'
    '<script>window.DataDomeJsTag = {};</script>'
    '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=abc123'
    '&amp;hash=deadbeef&amp;cid=xyz789&amp;t=fe&amp;referer=https%3A%2F%2Fwww.g2.com%2Fcategories%2Fcrm"'
    ' height="600" width="100%"></iframe></body></html>'
)


# --------------------------------------------------------------------------- #
# Local servers: g2.com stand-in, 2Captcha stand-in, a minimal forward proxy
# --------------------------------------------------------------------------- #
class _G2Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        page = int(qs.get("page", ["1"])[0])
        cookie_header = self.headers.get("Cookie", "") or ""

        if parsed.path == "/categories/crm":
            if page == 1:
                self._send(listing_html(cards=3, next_disabled=False, current=1, offset=0))
            else:
                self._send(listing_html(cards=2, next_disabled=True, current=2, offset=3))
            return
        if parsed.path == "/categories/blocked":
            self._send(DATADOME_WALL_HTML)
            return
        if parsed.path == "/categories/slider":
            if f"datadome={SOLVED_COOKIE_VALUE}" in cookie_header:
                self._send(listing_html(cards=3, next_disabled=True, current=1, offset=0))
            else:
                self._send(DATADOME_SLIDER_WALL_HTML)
            return
        if parsed.path == "/products/hubspot-sales-hub/reviews":
            self._send(product_html())
            return
        if parsed.path == "/products/hubspot-sales-hub/pricing":
            self._send(PRICING_HTML)
            return
        self._send("<html><body>not found</body></html>", status=404)


class _CaptchaHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _reply(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/createTask":
            task = payload.get("task", {})
            if task.get("type") == "DataDomeSliderTask":
                missing = [k for k in ("proxyType", "proxyAddress", "proxyPort", "userAgent") if not task.get(k)]
                if missing:
                    self._reply({"errorId": 1, "errorCode": "ERROR_MISSING_FIELD", "errorDescription": f"missing {missing}"})
                    return
            self._reply({"errorId": 0, "taskId": 42})
            return
        if self.path == "/getTaskResult":
            self._reply({"errorId": 0, "status": "ready", "solution": {"cookie": f"datadome={SOLVED_COOKIE_VALUE}; Path=/; Secure; SameSite=Lax"}})
            return
        self._reply({"errorId": 1, "errorCode": "ERROR_UNKNOWN_METHOD"}, status=404)


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        import urllib.request
        req = urllib.request.Request(self.path, headers=dict(self.headers.items()))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"proxy error: {exc}".encode())


def _start(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# --------------------------------------------------------------------------- #
# Engine runner — same monkeypatch-BASE_URL trick, in-process
# --------------------------------------------------------------------------- #
def _run(engine: str, argv, *, chromium_path=None):
    """Runs one engine's real main() against argv, returns its exit code.
    Runs in a subprocess so each engine gets a clean import (and so a
    crash in one doesn't take the whole test script down)."""
    script_name = f"{engine}_scraper.py"
    script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import g2_parser
g2_parser.BASE_URL = {g2_parser_base_url!r}
sys.argv = [{script_name!r}] + {argv!r}
"""
    if engine == "puppeteer" and chromium_path:
        script += f"""
import functools
import puppeteer_scraper as mod
mod.pyppeteer_launch = functools.partial(mod.pyppeteer_launch, executablePath={chromium_path!r})
sys.exit(mod.main())
"""
    else:
        script += f"""
import {engine}_scraper as mod
sys.exit(mod.main())
"""
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT), capture_output=True, text=True, timeout=90)
    return proc.returncode, proc.stdout, proc.stderr


def _engine_installed(engine: str) -> bool:
    try:
        __import__({"playwright": "playwright", "selenium": "selenium", "puppeteer": "pyppeteer"}[engine])
        return True
    except ImportError:
        return False


def main():
    global g2_parser_base_url
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engines", default="playwright,selenium,puppeteer", help="comma-separated subset to run")
    parser.add_argument("--chromium-path", default=None, help="explicit Chromium executable (pyppeteer only, if auto-download is blocked)")
    args = parser.parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    g2_srv, g2_port = _start(_G2Handler)
    cap_srv, cap_port = _start(_CaptchaHandler)
    proxy_srv, proxy_port = _start(_ProxyHandler)
    g2_parser_base_url = f"http://127.0.0.1:{g2_port}"

    print(f"local fake-g2 on :{g2_port}, fake-2captcha on :{cap_port}, fake-proxy on :{proxy_port}\n")

    for engine in engines:
        if not _engine_installed(engine):
            print(f"=== {engine}: SKIPPED (driver not installed) ===")
            continue
        print(f"=== {engine} ===")

        @check(f"{engine}: healthy 2-page category run (--category crm)")
        def _(engine=engine):
            with tempfile.TemporaryDirectory() as d:
                out = str(Path(d) / "out.json")
                code, stdout, stderr = _run(engine, ["--category", "crm", "--max-pages", "5", "--out", out, "--headless"], chromium_path=args.chromium_path)
                assert code == 0, f"exit {code}, stderr tail: {stderr[-800:]}"
                products = json.loads(Path(out).read_text())
                assert len(products) == 5, f"expected 5 products, got {len(products)}"

        @check(f"{engine}: --url with ?page=2 actually starts at page 2 (regression for the bug fixed 2026-09-22)")
        def _(engine=engine):
            with tempfile.TemporaryDirectory() as d:
                out = str(Path(d) / "out.json")
                url = f"http://127.0.0.1:{g2_port}/categories/crm?page=2"
                code, stdout, stderr = _run(engine, ["--url", url, "--max-pages", "3", "--out", out, "--headless"], chromium_path=args.chromium_path)
                assert code == 0, f"exit {code}, stderr tail: {stderr[-800:]}"
                skus = {p["sku"] for p in json.loads(Path(out).read_text())}
                assert skus == {"product-4", "product-5"}, f"expected page 2's cards, got {skus}"

        @check(f"{engine}: product page fills rating_10, leaves rating_5 empty")
        def _(engine=engine):
            with tempfile.TemporaryDirectory() as d:
                out = str(Path(d) / "out.json")
                url = f"http://127.0.0.1:{g2_port}/products/hubspot-sales-hub/reviews"
                code, stdout, stderr = _run(engine, ["--url", url, "--out", out, "--headless"], chromium_path=args.chromium_path)
                assert code == 0, f"exit {code}, stderr tail: {stderr[-800:]}"
                p = json.loads(Path(out).read_text())[0]
                assert p["rating_10"] == 8.9 and p["rating_5"] is None, p

        @check(f"{engine}: DataDome wall with no proxy/key -> EXIT_BLOCKED(3), no output file")
        def _(engine=engine):
            with tempfile.TemporaryDirectory() as d:
                out = str(Path(d) / "out.json")
                url = f"http://127.0.0.1:{g2_port}/categories/blocked"
                code, stdout, stderr = _run(engine, ["--url", url, "--out", out, "--headless", "--block-retries", "0"], chromium_path=args.chromium_path)
                assert code == 3, f"expected exit 3, got {code}, stderr tail: {stderr[-800:]}"
                assert not Path(out).exists(), "a blocked run must not write output"

        @check(
            f"{engine}: BUG fixed 2026-09-23 — a --proxy-file rotation survives the FIRST proxy "
            f"having a totally dead navigation (connection refused), landing on a later live proxy "
            f"instead of the whole run aborting as remote_api_error. Confirmed live 2026-09-22: a "
            f"real 10-proxy run stopped after one navigation failure on proxy #1, never trying the "
            f"other 9 (see CHANGELOG.md)."
        )
        def _(engine=engine):
            import socket as _socket
            # A port nothing is listening on — Chromium reports this as a
            # proxy connection failure almost instantly (no NAV_TIMEOUT_MS
            # wait), so this test stays fast.
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _s.bind(("127.0.0.1", 0))
            dead_port = _s.getsockname()[1]
            _s.close()
            with tempfile.TemporaryDirectory() as d:
                proxies_path = Path(d) / "proxies.txt"
                proxies_path.write_text(
                    f"http://127.0.0.1:{dead_port}\nhttp://127.0.0.1:{proxy_port}\n", encoding="utf-8"
                )
                out = str(Path(d) / "out.json")
                code, stdout, stderr = _run(engine, [
                    "--category", "crm", "--max-pages", "1", "--out", out, "--headless",
                    "--proxy-file", str(proxies_path), "--block-retries", "3",
                ], chromium_path=args.chromium_path)
                assert code == 0, (
                    f"expected exit 0 (rotated to the second, live proxy), got {code} — "
                    f"remote_api_error(5) here means the fix regressed; stderr tail: {stderr[-1200:]}"
                )
                products = json.loads(Path(out).read_text())
                assert len(products) == 3, f"expected the 1-page category's 3 products, got {products}"

        if engine == "playwright":
            @check(f"{engine}: full DataDomeSliderTask solve round trip (cookie applied, page reloaded, healthy content served)")
            def _(engine=engine):
                with tempfile.TemporaryDirectory() as d:
                    out = str(Path(d) / "out.json")
                    url = f"http://127.0.0.1:{g2_port}/categories/slider"
                    code, stdout, stderr = _run(engine, [
                        "--url", url, "--proxy", f"http://127.0.0.1:{proxy_port}",
                        "--twocaptcha-key", "FAKEKEY", "--captcha-api", f"http://127.0.0.1:{cap_port}",
                        "--solve-captcha", "always", "--out", out, "--headless",
                    ], chromium_path=args.chromium_path)
                    assert code == 0, f"exit {code}, stderr tail: {stderr[-800:]}"
                    products = json.loads(Path(out).read_text())
                    assert len(products) == 3, f"expected the healed page's 3 products, got {len(products)}"

    print()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"local_e2e_test: {passed}/{len(RESULTS)} checks passed")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
