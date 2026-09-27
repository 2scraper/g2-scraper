# Testing with real credentials and the live site

**Written 2026-09-22.** Read this section before you read the checklist —
it is the difference between what this repo has actually proven and what it
has only argued.

## Two different claims, kept apart

**What HAS been verified live: g2.com's own structure, through a browser
tool.** On 2026-09-22 a real browser navigated real g2.com pages and the
findings were transcribed into `g2_parser.py`'s module docstring, marked
`CONFIRMED`:

- DataDome is the bot protection (`window.DataDomeJsTag`,
  `window.dataDomeOptions.endpoint = "https://dd.g2.com/js/"`, v5.10.0, a
  `datadome` cookie).
- The category card's DOM shape and its `data-event-options` JSON blob, the
  `.elv-star-wrapper` 0-5 rating and the isolated review count, and the
  `ul.pagination[aria-label="Pagination"]` markup the page-loop reads.
- A product page's `SoftwareApplication` JSON-LD, including
  `aggregateRating` on the 0-10 scale and an embedded `review` array on the
  0-5 scale.
- A pricing page carrying NO pricing JSON-LD at all — only the rendered
  text lines `parse_pricing_page()` matches.
- `https://www.g2.com/robots.txt`, including the stricter AI-crawler group
  and its one extra rule.
- `https://www.g2.com/categories/{slug}/grids.json` returning a ranked
  subset of a category (253 products for `crm`, against 1661 found by
  paginating the DOM).

**What HAS since happened, 2026-09-22: real-browser end-to-end runs against
a local stand-in for g2.com, not the real site.**
`local_e2e_test.py` (see its own section below) spins up a local HTTP
server serving the exact CONFIRMED shapes above plus a stand-in 2Captcha
server, and drives each real engine's real browser through the real
pipeline against them: navigation, retries, parsing, pagination (including
resuming from a `?page=N` in `--url` — a bug this run found and fixed, see
CHANGELOG.md), captcha detection, a full `DataDomeSliderTask` solve round
trip (cookie applied via the driver's own native API, page reloaded,
healthy content served — this run found and fixed a second bug too, the
missing user-agent fallback, also in CHANGELOG.md), output writing, and
every exit code the architecture defines. Playwright and Puppeteer completed
this scenario; Selenium hit a chromedriver/Chromium mismatch in this build
and currently has structural/offline coverage instead. This is meaningful
pipeline evidence, but not a claim that all three completed live E2E here.

**What HAS also now happened, 2026-09-22 (same day, later): the first real
run of THIS repo's own engine scripts against the REAL g2.com — from a
machine outside this build/test environment's own blocked egress.** Roman,
`playwright_scraper.py --category crm --solve-captcha when-blocked
--dump-html`, a real `--cdp-endpoint` (Scraping Browser API) session and a
real 2Captcha key. Result: a genuine HTTP 403, a real DataDome interstitial
(`geo.captcha-delivery.com/interstitial/?...`, `title="DataDome Device
Check"` — scrubbed capture at `tests/fixtures/g2_datadome_interstitial.html`),
zero products, exit `3` after 3 retries on the same session. Not a
successful scrape — but real, and it earned its keep: it's what surfaced
`_DATADOME_IFRAME_RE` only ever matching `/captcha/`, never the
`/interstitial/` shape g2.com actually serves (fixed the same day — see
CHANGELOG.md), and it's the first real data point on whether 2Captcha's
Scraping Browser auto-solve covers DataDome (`Captcha.setAutoSolve` armed,
never fired `Captcha.detected` across three tries — see
`captcha_solver.py`'s module docstring for the full reasoning, including
why that's evidence and not proof).

**What HAS also now happened, 2026-09-22 (same day, later still): a THIRD
real run — proxy mode this time, `G2_PROXY` with `--cdp-endpoint`
commented out of `.env`, same command otherwise.** This reached a REAL
2Captcha `createTask` call for `DataDomeSliderTask` for the first time
ever in this repo — and 2Captcha refused it: `ERROR_BAD_PARAMETERS Your
captcha_url value contains "t=bv", that means your IP address is banned.`
DataDome had already flagged that proxy session's exit IP; 2Captcha
refuses a doomed solve rather than attempting one. This earned its keep
twice over: it confirmed the ORIGINAL `/captcha/` iframe pattern (in this
repo since before today, previously only 2Captcha's own documented shape)
is ALSO real — `title="DataDome CAPTCHA"`, a different `dd.rt` value
(`'c'`) than the earlier `/interstitial/` capture's `'i'`, scrubbed at
`tests/fixtures/g2_datadome_captcha_banned_ip.html` — and it surfaced two
silent gaps: this specific rejection had no distinct outcome
(`captcha_solver.py` now returns `action="warning_proxy_banned"` for it,
with a log line in all three engines saying plainly "rotate your proxy,
retrying won't help"), and while fixing that, tracing the code found that
`warning_no_proxy` — a real, already-tested action — had NO log branch at
all in any engine, silently falling through with zero output (never a
correctness bug, since the separate `captcha_detected and not
cards_present` check already reported `EXIT_BLOCKED` correctly — purely a
silent-diagnostics gap, now also fixed). See `CHANGELOG.md` for the full
write-up.

**What still has NOT happened: a successful scrape against real g2.com, or
a `DataDomeSliderTask` solve attempt that ISN'T immediately rejected for an
already-flagged IP.** All three real runs so far used proxy/CDP identities
DataDome had already flagged or challenged hard. Whether
`DataDomeSliderTask` can actually solve either real shape (`/captcha/` or
`/interstitial/`) when the exit IP ISN'T already burned needs a genuinely
fresh proxy session to test, and costs real 2Captcha balance to attempt —
a deliberate next step, not something to run casually.

Everything `smoke_test.py` and `local_e2e_test.py` prove, they prove
against fixtures built from those captures — real shapes, a real browser, a
real pipeline, and now one real (blocked) request to g2.com itself.
**Closing the remaining gap — a proxy-mode solve attempt, and ultimately a
successful real scrape — is this repo's single highest-value remaining
check.** Until then, treat "the parser reads this shape correctly, the
whole engine pipeline actually works, and a real run against g2.com is
detected and reported honestly" as established, and "this repo's own
DataDomeSliderTask solve actually gets through g2.com's real challenge" as
the one thing still untested.

## What `smoke_test.py` actually covers

94 checks, all offline, all passing with **no** engine driver installed
(`python3 smoke_test.py`). What they are, by category — these are the real
groupings in the file, not a generic template:

- **Engine isolation and parity (CLAUDE.md §4/§6).** All three engines
  import with none of playwright/selenium/pyppeteer present; each imports
  its driver at module level behind `try/except ImportError` (asserted
  structurally *and* behaviourally, by making the drivers unimportable in a
  subprocess); all three expose the identical `--flag` set, the same
  default output filename stem, and the same per-site constants block
  (`NAV_TIMEOUT_MS`, `READINESS_WAIT_MS`, `MIN_CARD_MATCHES`), with
  `READINESS_WAIT_MS` asserted shorter than an SPA sibling's because
  g2.com is server-rendered.
- **URL routing and robots.txt.** A `/products/*/reviews/*` URL (the one
  extra rule g2.com's robots.txt gives AI crawlers) is refused before any
  fetch; a URL shape with no parser is refused rather than fetched and
  failed on; a `grids.json` URL is refused with a message naming why; and
  each of the three real page shapes routes to its own mode in all three
  engines.
- **Block detection semantics.** DataDome's tag ships on *every* healthy
  g2.com page, so a bare marker match must never turn a good listing into
  `EXIT_BLOCKED` — checked directly, in all three engines. A real DataDome
  wall reports `action="unsupported_vendor"` + `vendor="datadome"` and is
  still treated as blocked. An HTTP 403 (DataDome answers 403, it does not
  redirect) records a failed page rather than crashing the run. A
  zero-product page that wasn't flagged as blocked logs a diagnostic
  rather than nothing.
- **Exit codes and `finish_run()` precedence (CLAUDE.md §9/§10).** Every
  exit code `0/1/2/3/4/5/6` is shown reachable, including from each
  engine's own `run()`. The precedence regressions are tested directly: a
  `remote_api_error` is never laundered into `complete` because products
  were present; a blocked run that *did* collect products is still
  `blocked`; `--allow-empty` changes only whether an empty result is
  written, never the status; a zero-product run without `--allow-empty`
  writes neither file nor sidecar.
- **The output contract.** `Product`'s family-common fields come first in
  CLAUDE.md §9's exact order with the g2-specific ones after;
  `merge_pages` dedupes by `sku`, last-write-wins, in page order;
  `write_csv` emits a header even for zero rows.
- **Credential hygiene (CLAUDE.md §8).** `proxy_pool` rejects a malformed
  proxy and masks a credentialed one; `redact_credentials` scrubs URL
  userinfo and key/token query params globally; a failed `--cdp-endpoint`
  connection under a real driver-shaped failure never leaks its
  credential; a failed Fingerprint API request never leaks the 2Captcha
  key (`requests` puts the full URL into every `HTTPError`); Selenium
  refuses a credentialed `--cdp-endpoint` outright with `EXIT_BAD_USAGE`.
- **Captcha wiring, including the two bug classes this family has already
  shipped once.** Generic detection finds DataDome with no extra wiring; a
  healthy listing page is not treated as blocked; Turnstile sitekey
  extraction and per-widget injection-script building still work; every
  engine calls `build_injection_script` from its "solved" branch. Plus, at
  AST level: every function taking an `autosolve` parameter actually arms
  auto-solve, and every `_maybe_solve_captcha()` call passes an explicit
  `count_product_links` shaped for the page that call site is looking at —
  with a behavioural proof on real page shapes that the listing-shaped
  counter would misread a normal product page as blocked.
- **`env_config` and `.env.example` (CLAUDE.md §17).** The two are asserted
  in sync in both directions; keys are `G2_`-prefixed, not a leftover from
  the repo this was ported from; there is exactly ONE `_is_placeholder`
  implementation and a braced `{...}` fragment reads as unset; `apply_env`
  never overrides an explicitly-set CLI flag; a copied `.env.example`
  round-trips through the real loader with every credential reading as
  unset.
- **`g2_parser.py` itself.** URL builders produce the confirmed-real
  shapes; slug extraction round-trips; `make_sku` prefers the slug and
  falls back to a deterministic fingerprint; `is_disallowed_path` honours
  the stricter rule set and `is_known_scrape_target` allowlists exactly
  four shapes without contradicting it; the listing parser reads the real
  card shape and isolates one malformed card from its siblings;
  `safe_parse_category_listing` degrades a parser exception to an empty
  page; pagination is discovered from the markup rather than hardcoded;
  the product parser takes the rating scale from the page's own
  `bestRating` and leaves **both** rating columns empty on an unrecognised
  one; the pricing parser skips the page's own summary sentence and
  footnote and returns `[]` (never a raise, never "free") on non-matching
  HTML; `apply_pricing_tiers` uses the lowest tier and stamps
  `price_source="pricing_page_text"`.
- **End-to-end, as close as offline gets.** A full listing page round-trips
  through `finish_run()` as a clean `complete` run with the g2 columns
  intact in both JSON and CSV; the pricing-only row for a `--url
  .../pricing` run is identical in all three engines; and each engine's
  `run()` is driven end-to-end over the browserless `--scraper-api` path
  with a fake client returning the fixtures, for all three page shapes
  (listing → `EXIT_OK`, product → `EXIT_OK`, DataDome wall →
  `EXIT_BLOCKED` with no file and no sidecar).
- **`diff_runs.py` and `scraper_api_client.py`.** Added/removed/
  price-changed against two real `finish_run()` outputs; a refusal to diff
  a non-`complete` run; the 2Captcha client rejecting a missing key and
  honouring the `--captcha-api`/`--scraper-api-url` overrides.

What `smoke_test.py` cannot do is tell you whether g2.com will serve any of
those shapes to *your* browser from *your* IP. That's the checklist below.
It also can't tell you whether a real browser, driven by this repo's own
engine code, actually gets from a page load to a written output row —
because it never launches one. `local_e2e_test.py` (next section) closes
*that* gap without needing real g2.com access at all; the numbered
checklist after it is for the one thing that still requires the real site.

## `local_e2e_test.py` — real browsers, real pipeline, no live g2.com needed

Added 2026-09-22, the same day this repo got its first real end-to-end run
(see CHANGELOG.md — that run found and fixed two real bugs neither
`smoke_test.py` nor a code review had caught). It spins up a tiny local
HTTP server serving the exact CONFIRMED fixture shapes `smoke_test.py`
already uses, plus a stand-in 2Captcha `createTask`/`getTaskResult` server,
and drives each real installed engine's real browser through the real
pipeline against them — no network access to g2.com or a 2Captcha key
required, which is exactly why this can run in CI or any sandboxed
environment `smoke_test.py` can, unlike the checklist below.

```bash
# whichever engine(s) you have installed — a driver that isn't installed
# is SKIPPED, not failed, same posture as smoke_test.py
pip install -r requirements-playwright.txt   # and/or -selenium / -puppeteer
python3 local_e2e_test.py --engines playwright
python3 local_e2e_test.py --engines playwright,selenium,puppeteer
```

What it actually proves, per engine: a healthy multi-page category run
merges correctly; `--url ".../categories/crm?page=2"` truly resumes at
page 2 (the regression test for the pagination bug this run found); a
product page fills `rating_10` and leaves `rating_5` empty; a DataDome wall
with no proxy/key reports `EXIT_BLOCKED` and writes no file; a `--proxy-file`
list with a genuinely dead first proxy (added 2026-09-23, the regression
test for the proxy-rotation bug a real 10-proxy run found — see
CHANGELOG.md) still completes in category, standalone product and standalone
pricing modes by rotating to the next live proxy, rather than aborting the
whole run as `remote_api_error`. Playwright
additionally gets the full `DataDomeSliderTask` round trip: task payload
sent to the stand-in 2Captcha server (proxy fields and a real
`navigator.userAgent`, both asserted present), solved cookie applied via
the driver's own native cookie API, page reloaded, healthy content served
on the retry.

**What this does NOT prove**: that g2.com's real markup still matches
these CONFIRMED-as-of-2026-09-22 shapes. Only the numbered checklist below,
against the real site, can tell you that. **Also note:** Selenium's own
run of this suite depends on chromedriver and Chrome/Chromium being
version-matched on your machine — a mismatch (confirmed in this repo's own
build/test environment: chromedriver 147 vs. Chromium 141) makes every
Selenium check here fail with `SessionNotCreatedException`, including ones
that have nothing to do with whatever you just changed. That's a local
environment problem, not a sign the Selenium engine code itself is broken
— `smoke_test.py`'s structural checks cover Selenium's code even when this
suite can't run it live.

If your sandbox's pyppeteer can't auto-download its bundled Chromium (a
network-egress-policy problem, same flavor as the g2.com block below, not
a bug here), point it at any installed Chromium with `--chromium-path`.

Run everything below from a normal terminal on a machine with real network
access — wherever this repo lives for you.

## 1. Basic setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env
```

Leave `.env` blank for the first run — the whole point of "local-first" is
that nothing in it is required. Fill in `TWOCAPTCHA_KEY` / `G2_PROXY` /
`G2_CDP_ENDPOINT` later, only if you want to test those specifically. Worth
knowing before you buy credit for this site: a 2Captcha key DOES buy a way
through DataDome's slider challenge (`DataDomeSliderTask`), but only
alongside a proxy — `TWOCAPTCHA_KEY` with no `G2_PROXY`/`--proxy` set still
cannot solve one. A key also buys proxies, fingerprints and the Scraping
Browser API's own device identity on their own — things that affect
whether you get challenged at all, independent of solving.

## 2. The most important run you can do: the first live engine run

```bash
python3 playwright_scraper.py --category crm \
  --max-pages 1 --max-results 20 --format json \
  --out /tmp/g2_test.json --dump-html
echo "exit code: $?"
cat /tmp/g2_test.json.meta.json 2>/dev/null || echo "(no sidecar — see below)"
```

Four outcomes, and what each one means:

- **`exit code: 0`, a `.meta.json` with `"status": "complete"` and a
  believable `product_count`**: the confirmed card shape in
  `g2_parser.py` matched a real page served to a real engine. Open
  `/tmp/g2_test.json` and actually look at a few rows — a plausible
  `title`/`rating_5`/`review_count`/`product_url` per row, `price` empty on
  every row (expected — see README), is what "confirmed" looks like. This
  is the outcome the research predicts, and it has never been observed
  from this repo's own code. Confirming it is the whole point of this step.
- **`exit code: 3` (blocked)**: check whether the log names
  `vendor="datadome"`. If it does and you were running with NO
  `--proxy`/`--proxy-file`, re-run with one — `--solve-captcha` cannot
  attempt `DataDomeSliderTask` without a proxy, so this outcome may just
  mean the solve was never tried. If it recurs WITH a proxy configured,
  that is either a genuinely failed solve (check the log for
  `warning_solver_error`) or the slider-iframe pattern this repo detects
  not matching what g2.com actually served — save a scrubbed `--dump-html`
  capture either way; **what helps the project most here is knowing
  whether the iframe pattern in `captcha_solver.py`'s
  `_DATADOME_IFRAME_RE` matched a real g2.com page at all**, since it has
  never been confirmed against one. Also note what you were running from
  (residential vs datacentre IP, fingerprint, headful vs headless) and
  whether `--block-retries` got past it regardless. If the log reports
  `detected_unidentified_widget` instead, you have found a defense this
  repo has not seen — save a scrubbed `--dump-html` capture and extend
  `g2_parser.BOT_CHALLENGE_MARKERS`.
- **`exit code: 4` (zero products), no `.meta.json` written** (by design —
  see `output_writer.finish_run`): read the diagnostic line the engine
  logs (`diagnose_unexpected_page` prints the page's own `<title>`, which
  structural markers were present and whether DataDome's tag was on it),
  then open the dumped HTML and check, in this order: (1) is
  `div.category-product-card` still the card class; (2) is
  `a[data-event-options][href*="/products/"]` still inside it and is its
  attribute still JSON; (3) did G2 rename a key inside that JSON
  (`product`, `product_id`, `category`, `vendor_id`, `product_type`).
- **The logged Next-control hint disagrees with the fetched data**: compare
  the real pagination markup against the shapes documented in
  `g2_parser.py`. This no longer truncates the run: engines probe the
  reconstructed `?page=N+1` URL and stop only on zero cards or no new SKU.

Whatever you find, **updating `g2_parser.py` to match what you actually saw
— with a saved, scrubbed fixture and a new `smoke_test.py` check against it
— is the single most valuable contribution this repo can receive** (see
`CONTRIBUTING.md`).

## 3. The product page and the pricing page, for real

```bash
# a product's own /reviews page — JSON-LD path, fills rating_10
python3 playwright_scraper.py --product hubspot-sales-hub --out /tmp/g2_product.json

# the best-effort pricing text parser, on its own
python3 playwright_scraper.py --url "https://www.g2.com/products/hubspot-sales-hub/pricing" \
  --out /tmp/g2_pricing.json

# a listing enriched with pricing (one extra request per product)
python3 playwright_scraper.py --category crm --max-results 5 \
  --with-pricing --pricing-limit 5 --out /tmp/g2_priced.json
```

Three things to look at specifically:

- The product-page row must fill `rating_10` (0-10) and leave `rating_5`
  empty. If a row comes back with both, or with a 0-10 value sitting in
  `rating_5`, that is a real bug — the scale is read from the page's own
  `bestRating` and an unrecognised one is supposed to leave both empty.
- `review_count` from the product page will differ slightly from the same
  product's listing-card count. That is G2's own cache/render timing (a
  captured example: 14,319 on the card vs 14,333 on the product page,
  minutes apart), which is why `review_count_source` exists. Don't "fix"
  it.
- `parse_pricing_page()` is the least reliable thing in this repo — plain
  text matching with no structured data behind it. An empty result means
  "no pricing could be read", never "this product is free". **If the tier
  line shape has changed, this is the parser that breaks first**, and a
  capture of the real page text is the fix.

## 4. Selenium, for real

```bash
python3 -m venv .venv-selenium   # separate venv — see README "Engines"
source .venv-selenium/bin/activate
pip install -r requirements-selenium.txt
python3 selenium_scraper.py --category crm --max-results 20 --out /tmp/g2_selenium.json
```

## 5. Puppeteer (pyppeteer), for real

```bash
python3 -m venv .venv-puppeteer
source .venv-puppeteer/bin/activate
pip install -r requirements-puppeteer.txt
python3 puppeteer_scraper.py --category crm --max-results 20 --out /tmp/g2_puppeteer.json
```

All three should produce the same columns and the same exit code for the
same URL. If they don't, that's a parity bug, and `smoke_test.py` should
grow a check that catches it.

## 6. The 2Captcha REST API, with your real key

Confirms the key against a real, billed-nothing endpoint first:

```bash
python3 -c "
import env_config
from scraper_api_client import TwoCaptchaClient
args = type('A', (), {'twocaptcha_key': None, 'proxy': None, 'cdp_endpoint': None, 'url': None})()
env_config.apply_env(args)
c = TwoCaptchaClient(args.twocaptcha_key)
print('balance: \$%.2f' % c.get_balance())
"
```

## 7. The Scraping Browser API (`--cdp-endpoint`), for real

`G2_CDP_ENDPOINT` in `.env` is picked up automatically:

```bash
python3 playwright_scraper.py --category crm --max-results 10 --out /tmp/g2_cdp.json
```

**The specific question worth answering here**: does a managed session's
own device identity and exit IP get past DataDome where a plain local run
doesn't? Nobody in this project knows yet, and it is the most useful single
data point anyone with a key can contribute. Record the exit code either
way.

**Gotcha**, same as the rest of the family: if `.env` has BOTH
`G2_CDP_ENDPOINT` and `G2_PROXY` set, the code ignores `G2_PROXY` and warns
— a CDP session already carries its own exit IP, stacking a second one on
top is a contradiction, not better cover (same for a fingerprint over
`--cdp-endpoint`, which `fingerprint_client.refuse_if_cdp` blocks
outright). Comment out whichever you're not testing.

**This same rule means `DataDomeSliderTask` (see step 5/README) never runs
under `--cdp-endpoint`.** That task requires a proxy we hold the
credentials for; with the local proxy nulled out, `_maybe_solve_captcha()`
always calls it with `proxy=None`, which the code refuses outright
(`"warning_no_proxy"`) rather than attempting a solve that would validate
against the wrong exit IP. So a `--cdp-endpoint` run's ONLY path to
solving DataDome is 2Captcha's own `Captcha.setAutoSolve`, armed with a
wildcard on every page load. **Whether their Scraping Browser backend
actually covers DataDome through that mechanism has never been confirmed
by this project** — the one live check (2026-09-14) confirmed Turnstile,
Amazon WAF, Yandex SmartCaptcha and Lemin; DataDome (like GeeTest) wasn't
part of that capture. If you run this step and a DataDome wall shows up,
the single most useful thing to record is whether it got solved anyway
(watch the log for `[Scraping Browser API] captcha solved`/`solve
failed`/`captcha detected` — see `_enable_scraping_browser_auto_solve`) —
that would be the first real data point either way.

## 8. The browserless Scraper API (`--scraper-api`), for real

```bash
python3 playwright_scraper.py --category crm --scraper-api --out /tmp/g2_scraper_api.json
```

A different product from `--cdp-endpoint`'s Scraping Browser API (see
`scraper_api_client.py`). A single static fetch: no pagination loop, and
`--proxy`/`--cdp-endpoint`/`--fingerprint`/`--max-pages`/`--page-delay` are
ignored with a warning. Not a documented DataDome bypass — expect it to be
challenged like anything else, and record what happens.

## 9. The residential proxy (`--proxy` / `G2_PROXY`), for real

```bash
python3 playwright_scraper.py --category crm --max-results 10 --out /tmp/g2_proxy.json
```

## 10. Credentials never leak — grep the real output

Per CLAUDE.md §10, this is checked against real failure output, not just a
simulated one. Force a failure with a real-shaped credential and grep
everything it wrote:

```bash
G2_CDP_ENDPOINT="ws://user-zone-x:s3cr3tpassword@cb.2captcha.com:9222" \
  python3 playwright_scraper.py --category crm --out /tmp/g2_leak.json 2>&1 \
  | tee /tmp/g2_leak.log
grep -r "s3cr3tpassword" /tmp/g2_leak.log /tmp/g2_leak.json* ; echo "grep exit: $? (1 = clean)"
```

## 11. `diff_runs.py` against two real runs

```bash
python3 playwright_scraper.py --category crm --max-results 20 --out /tmp/g2_run1.json
# ... later ...
python3 playwright_scraper.py --category crm --max-results 20 --out /tmp/g2_run2.json
python3 diff_runs.py /tmp/g2_run1.json /tmp/g2_run2.json
```

It refuses to diff a run whose sidecar isn't `complete`, on purpose.

## 12. Push to GitHub and let CI do the rest

```bash
git remote add origin git@github.com:2scraper/g2-scraper.git
git push -u origin main
git push --tags
```

Then, in the GitHub repo's Settings:

- **Secrets and variables → Actions**: add `TWOCAPTCHA_KEY`,
  `G2_CDP_ENDPOINT` / `G2_PROXY`. `canary-live` skips without `G2_PROXY`;
  once configured it requires a complete three-page run. `canary-cdp`
  behaves the same way for the managed-browser path. Also add
  `CLAUDE_CODE_OAUTH_TOKEN` (for `claude.yml` / `claude-code-review.yml` —
  both silently no-op without it, by design, rather than failing every PR
  check).
- **Actions → canary → Run workflow**: dispatch it manually at least once
  rather than waiting a day for the cron and trusting the badge blind.
  The canary passes only on exit 0, `status == "complete"`, at least three
  completed pages and the product-count floor. Without the required secret
  it skips instead of turning a predictable DataDome block into permanent
  noise. It deliberately does not assert a price floor: without
  `--with-pricing`, `price_confirmed_pct` of 0 is correct here.

## 13. What "done" looks like

- `tests.yml` green: offline checks on Python 3.9 and 3.12, all three
  `engine-smoke` matrix legs, the Docker build/run/browser-launch job, and
  the `sample_output` schema check (which is a hard check now that those
  files exist).
- At least one manually-dispatched `canary.yml` run, looked at — not just
  the badge — including whichever of the outcomes in step 2 it landed on.
- Steps 2 and 3 run at least once each on a real network, with their
  results written down: which exit code, whether DataDome fired, and
  whether the pricing text parser matched anything.
- If any of that disagreed with `g2_parser.py`: the parser updated to match
  what you actually saw, with a fixture and a new `smoke_test.py` check,
  per `CONTRIBUTING.md`.
- The README's status table updated so "never run against the live site"
  stops being true — that line is the honest statement of where this repo
  is today, and it should be the first thing a live run retires.
