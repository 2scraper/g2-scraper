# g2-scraper

![tests](https://github.com/2scraper/g2-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/g2-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![local-first](https://img.shields.io/badge/local--first-yes-success)

g2.com B2B-software-marketplace scraper: a category slug, a product slug, or
a g2.com URL in, a flat list of products out — title, G2 category, both of
G2's rating scales, review count, G2's own product/vendor ids, image and
product link. Three engines (Playwright primary, Selenium and
Puppeteer/pyppeteer for parity), JSON or CSV output, an open, documented
`Product` schema. Part of the [2scraper](https://github.com/2scraper) family
— same output contract, exit codes and family modules as `stockx-scraper` /
`skyscanner-scraper` / `lidl-scraper` / `perplexity-scraper` /
`shein-scraper`.

> **Repo "About" panel** (CLAUDE.md §2 — GitHub-side settings, not a file):
> description *"Open-source g2.com software-review scraper — Playwright,
> Selenium and Puppeteer engines, your own infrastructure by default."*;
> topics `scraper`, `playwright`, `selenium`, `puppeteer`, `python`, `g2`.

## What has and hasn't been verified

This section is the first one on purpose. There are two different claims
here and conflating them would be the most misleading thing this README
could do.

| Area | Status | How it was actually verified |
|---|---|---|
| Offline test suite | **90/90 passing** | `python3 smoke_test.py`, run 2026-09-23, with no engine driver installed |
| g2.com page structure — category card DOM + its `data-event-options` JSON, product-page `SoftwareApplication` JSON-LD, pricing-page text, pagination markup | **Verified live, 2026-09-22** | A real browser navigating real g2.com pages (a browser tool), transcribed into `g2_parser.py` as `CONFIRMED` — **not** through this repo's own engines |
| DataDome is the bot protection | **Verified live, 2026-09-22** | `window.DataDomeJsTag`, `dataDomeOptions.endpoint = "https://dd.g2.com/js/"` v5.10.0 and a `datadome` cookie, observed on a real page |
| `robots.txt`, incl. the stricter AI-crawler group | **Verified live, 2026-09-22** | Fetched and read; the one extra rule is transcribed in `g2_parser.py` |
| `categories/{slug}/grids.json` exists and returns a ranked *subset* | **Verified live, 2026-09-22** | 253 products returned for `crm`, against 1661 found by paginating the category DOM. **Not wired into this repo** — see "Known limitations" |
| **All three engines, end-to-end, real browser/driver** | **First run 2026-09-22, extended 2026-09-23 — against a local stand-in, NOT real g2.com** | A local HTTP server serving the exact confirmed fixture shapes (reused from `smoke_test.py`) plus a local stand-in 2Captcha `createTask`/`getTaskResult` server and a local forward proxy. Every real engine ran its real browser through real navigation, pagination (incl. resuming from a `?page=N` `--url`), parsing, a full `DataDomeSliderTask` solve round trip (cookie applied via the driver's own native API, page reloaded, healthy content served on retry), a `--proxy-file` rotation past a genuinely dead first proxy (2026-09-23, the regression test for the bug below), output writing, and exit codes 0/2/3/5. Playwright and Puppeteer confirmed live in this build; Selenium's copy of the same scenario hit an unrelated chromedriver/Chromium version mismatch in this particular build environment (not a code issue — see `CHANGELOG.md`) and is covered instead by the offline structural suite. This is real proof the PIPELINE works; it is not proof g2.com's real markup still matches `g2_parser.py`'s captured shapes — see the next row |
| This repo's engine scripts run against **live g2.com specifically** | **Run THREE times for real, 2026-09-22/23 — from outside this build/test environment (whose own egress blocks g2.com entirely, confirmed the same week), all blocked** | Run 1 (`--cdp-endpoint`): HTTP 403, a real DataDome interstitial (`geo.captcha-delivery.com/interstitial/?...`, `title="DataDome Device Check"`, capture at `tests/fixtures/g2_datadome_interstitial.html`) — surfaced and fixed the `/interstitial/`-path bug below. Run 2 (`G2_PROXY`, no `--cdp-endpoint`): HTTP 403, the OTHER real DataDome shape (`.../captcha/?...`, `title="DataDome CAPTCHA"`, a real `t=bv` banned-IP marker, capture at `tests/fixtures/g2_datadome_captcha_banned_ip.html`) — reached a REAL `DataDomeSliderTask` `createTask` call for the first time, which 2Captcha itself refused (`ERROR_BAD_PARAMETERS ... your IP address is banned`). Run 3 (`--proxy-file` with 10 fresh proxies, `--block-retries 9`): 6 of 10 proxies attempted (2 `cloudflare_managed_challenge`, 2 more `t=bv` rejections) before the run stopped dead on a plain navigation timeout — not a block at all — never trying proxies 7-10. That exposed a real bug in the proxy-rotation logic itself (not a captcha/detection gap this time); see the newest `CHANGELOG.md` entry. No run has scraped a product yet — but all three are real, and each found and fixed something real |
| Pricing text parser against a live pricing page | **Parser verified against captured page text, and now against a local fixture through a real engine — not yet against a real g2.com pricing page** | The captured text shape is real; the local fixture run (above) proves the whole fetch-parse-write path works; no engine has fetched a REAL pricing page itself |
| 2Captcha integrations (proxy, `--cdp-endpoint`, fingerprint, `--scraper-api`) | **Mixed, updated after two real runs: `DataDomeSliderTask` verified end-to-end against a local stand-in (2026-09-22); the `--cdp-endpoint` run found real evidence (not proof) that the Scraping Browser's own auto-solve doesn't currently cover DataDome; the REST `DataDomeSliderTask` path now HAS reached a real 2Captcha `createTask` call against g2.com — and got a real, informative rejection (the proxy's own IP was already DataDome-flagged), not a solve** | See `captcha_solver.py`'s module docstring and `CHANGELOG.md`'s two newest entries. Still open: whether `DataDomeSliderTask` can actually solve either real shape when the proxy exit ISN'T already flagged — needs a fresh, unflagged proxy session to test, and costs real 2Captcha balance |
| CI workflows | **Written, never executed** | No GitHub remote is configured yet — `TESTING.md` step 12 is how they first run |

So: **the site knowledge is real, the architecture is tested, the whole
pipeline has been proven to work end-to-end against a local stand-in, and
it has now been run twice for real against g2.com itself — both blocked,
exactly as this repo's own detection said they should be, and each run
found (and fixed) something real: a missed challenge shape, then a silent
gap in how a real 2Captcha rejection was reported.** `TESTING.md` is the
checklist for what's still open, and a fresh (not-already-flagged) proxy
session is the single highest-value thing left to try.

## Read this before trusting a run

- **DataDome IS solvable — corrected 2026-09-22.** This README originally
  said DataDome had no automated solve path at all, the same bucket as
  PerimeterX on skyscanner.com. That was wrong: 2Captcha ships a dedicated
  `DataDomeSliderTask` for DataDome's own interstitial slider challenge
  (https://2captcha.com/api-docs/datadome-slider-captcha). `--solve-captcha`
  attempts it automatically when a slider iframe is detected — but it is
  the **one** captcha type in this family with no proxyless path:
  `--proxy`/`--proxy-file` is required, or the run gets
  `action="unsupported_vendor"`, `vendor="datadome"`, exit `3` (blocked)
  instead of a real attempt. This repo now recognizes BOTH iframe shapes
  g2.com has actually been caught serving, 2026-09-22 —
  `geo.captcha-delivery.com/interstitial/?...` (`title="DataDome Device
  Check"`, `tests/fixtures/g2_datadome_interstitial.html`) AND
  `.../captcha/?...` (`title="DataDome CAPTCHA"`,
  `tests/fixtures/g2_datadome_captcha_banned_ip.html` — this one ALSO
  confirmed 2Captcha's own documented pattern is real, not just their
  own docs) — DataDome serves a different shape depending on how risky it
  judges the request. A real `DataDomeSliderTask` attempt against the
  `/captcha/` shape WAS made (proxy mode, no `--cdp-endpoint`) and got a
  real, informative rejection — 2Captcha refuses to even try when the
  challenge's own `t=` marker says the proxy's IP is already banned
  (`action="warning_proxy_banned"`, not a crash, not silence — see
  `CHANGELOG.md`). Whether a solve actually succeeds against either shape
  with a fresh, not-already-flagged proxy is still unconfirmed. A 2Captcha
  key also still buys proxies, fingerprints and the Scraping Browser API's
  own device identity, all of which affect whether you get challenged in
  the first place.
- **`DataDomeSliderTask` and `--cdp-endpoint` do not combine.** The proxy
  this task type requires and a `--cdp-endpoint` session are mutually
  exclusive across this whole family (same rule as `--proxy` itself — a
  CDP session already carries its own exit IP), so running with
  `--cdp-endpoint` never attempts `DataDomeSliderTask`, regardless of
  `--solve-captcha`. What DOES run over `--cdp-endpoint` is 2Captcha's own
  Scraping Browser `Captcha.setAutoSolve`, armed with a wildcard
  (`{"type": "*"}`) on every page load — but whether their backend
  actually solves a DataDome slider through that mechanism has never been
  confirmed. The one live confirmation this repo has (2026-09-14) covered
  Turnstile, Amazon WAF, Yandex SmartCaptcha and Lemin; DataDome (like
  GeeTest) simply wasn't part of that capture, so its status there is
  unknown rather than "no." See `captcha_solver.py`'s module docstring and
  `TESTING.md` step 7 for the full reasoning.
- **The three parsers are not equally reliable, and the README is the
  wrong place to be vague about it.**
  - `parse_category_listing()` — **solid.** Reads the card's
    `data-event-options` attribute, which is a structured JSON object
    (product name, `product_id`, `product_uuid`, `vendor_id`, `category`,
    `category_id`, `product_type`), not scraped visible text.
  - `parse_product_page()` — **solid.** Reads the page's
    `SoftwareApplication` JSON-LD block (`name`, `aggregateRating`, an
    embedded `review` array).
  - `parse_pricing_page()` — **best-effort, and materially weaker than the
    other two.** g2.com ships **no** pricing JSON-LD at all; this is plain
    line-shape text matching against rendered page text
    (`Tier Name — $20 / 1 Core Seat Per Month`). It returns an empty list
    rather than raising when the page doesn't match, and an empty list
    means "no pricing could be read", never "this product is free". It is
    the first thing that breaks when G2 restyles a page.
- **A product's review count differs slightly between its listing card and
  its own product page, and that is not a bug.** A real example captured
  minutes apart in one session: 14,319 on the category card vs 14,333 in
  the product page's `aggregateRating.reviewCount` for the same product.
  That is G2's own cache/render timing. `review_count_source`
  (`listing_card` | `json_ld`) records which page a row's number came from
  so nobody has to relitigate it.
- **Individual reviews name their authors as first name + last initial**
  (e.g. "Excel M."), when a product page's JSON-LD carries them. **That is
  G2's own public anonymization convention on their own site**, not
  redaction this scraper performs. (This repo's `Product` rows don't carry
  review text or author names at all today — the note is here so that if
  anyone ever surfaces them, nobody misreads the format as scraper-side
  scrubbing.)
- **g2.com carries no price anywhere except the pricing page.** Neither a
  category card nor a product page has one, so `price`, `currency` and
  `price_source` are **empty on every ordinary row**. That is correct
  output, not a parsing failure — unlike a shopping-site sibling, where an
  empty price means something went wrong. `--with-pricing` is the only way
  those columns get filled, and `price_confirmed_pct` of `0` in a sidecar
  from a run without it is the expected value.

## robots.txt: this repo obeys the stricter AI-crawler rules by default

A deliberate design decision, stated here rather than buried in a
docstring. `https://www.g2.com/robots.txt` has the usual `User-agent: *`
group plus a second group naming `GPTBot`, `ClaudeBot`, `Google-Extended`,
`Applebot-Extended`, `Meta-ExternalAgent`, `Amazonbot` and `CCBot`
together, which adds exactly one rule the general group does not have:

```
Disallow: /products/*/reviews/*
```

That covers sub-paths one level *below* a product's reviews page
(pagination and filter variants of the review listing). It does **not**
cover the bare `/products/{slug}/reviews` page, which is this scraper's
product-detail target and is unaffected.

`g2_parser.is_disallowed_path()` honours that stricter set **by default**,
for two reasons: this scraper is built and run by Claude, and G2's own
`/ai-instructions` page — a methodology page they address to AI assistants
by name — states that robots.txt is authoritative for AI-assistant access.
Deeper review pagination was never in scope, so obeying the stricter rule
costs this repo nothing. The looser general-group behaviour is reachable
(`is_disallowed_path(..., ai_crawler_rules=False)`), which keeps the
difference auditable instead of baked in. A `--url` matching a disallowed
path is refused with exit `2` before any request is made — never fetched
and then discarded.

## Local-first

Like the rest of the family, this does **not** require 2Captcha's paid
products to run. The default is an ordinary local headless Chromium, no
proxy, no key, no account. `--proxy` / `--cdp-endpoint` / `--fingerprint` /
`--scraper-api` are opt-in power options for volume, a specific exit
country, or a consistent device identity.

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` — leave it blank for a normal first run and
fill in what you use later. `python3 env_config.py` shows what was picked
up without ever printing a secret.

## Credentials

Three variables, all read by `env_config.py`, all documented in
`.env.example`, none of which belongs on a command line (CLAUDE.md §3 —
`ps`, shell history and crash reports would leak it permanently):

| Variable | What it's for |
|---|---|
| `TWOCAPTCHA_KEY` | One key across captcha solving, the Scraping Browser API, the Fingerprint API and the Scraper API. Not required for a normal local run |
| `G2_PROXY` | A proxy for a locally-launched browser, e.g. `http://login:password@host:port`. Blank = scrape from this machine's IP |
| `G2_CDP_ENDPOINT` | A ready-made CDP connection string (most commonly a 2Captcha Scraping Browser API session). Opt-in. **Selenium cannot use a credentialed one** — see "Engines" |

`G2_URL` is also read, as a convenience default for `--url`. A copied
`.env.example` is treated as unset: every placeholder in it reads as
"not configured" through the one `_is_placeholder()` implementation
(CLAUDE.md §17).

## Usage

```bash
# a whole category, by slug
python3 playwright_scraper.py --category crm --max-pages 3 --format json --out g2_results.json

# one product, by slug (its canonical page always ends in /reviews)
python3 playwright_scraper.py --product hubspot-sales-hub

# any g2.com URL — auto-routed to the matching parser
python3 playwright_scraper.py --url "https://www.g2.com/categories/crm?page=2"
python3 playwright_scraper.py --url "https://www.g2.com/products/hubspot-sales-hub/reviews"
python3 playwright_scraper.py --url "https://www.g2.com/products/hubspot-sales-hub/pricing"

# a listing, with prices scraped from each product's /pricing page (opt-in)
python3 playwright_scraper.py --category crm --max-results 10 --with-pricing --pricing-limit 10

# with 2Captcha's Scraping Browser API (opt-in — see "Local-first")
python3 playwright_scraper.py --category crm --cdp-endpoint "$G2_CDP_ENDPOINT"
```

`selenium_scraper.py` and `puppeteer_scraper.py` accept the identical flag
set and produce the identical output contract — see "Engines" for the one
place Selenium genuinely can't behave the same as Playwright.

### Three page shapes, one `--url`

| URL shape | Parser | What a row gets |
|---|---|---|
| `https://www.g2.com/categories/{slug}` (`?page=N`) | `parse_category_listing` | `rating_5`, `review_count` (`listing_card`), `category`, `product_id`, `product_uuid`, `vendor_id`, `category_id`, `product_type`, image |
| `https://www.g2.com/products/{slug}/reviews` | `parse_product_page` | `rating_10`, `review_count` (`json_ld`), title, image |
| `https://www.g2.com/products/{slug}/pricing` | `parse_pricing_page` | `price`, `currency`, `price_source="pricing_page_text"` and the slug-derived identity fields; nothing else is invented |

`_resolve_start_url()` in each engine picks the mode from the URL shape
alone, behind two guards in this order: robots.txt (`is_disallowed_path`)
and then an allowlist of the shapes this repo actually has a parser for
(`is_known_scrape_target`). A `grids.json` URL is accepted by the allowlist
as a real g2.com endpoint but then refused with a message naming why (no
parser ships for its response) — better than fetching JSON and reporting a
confusing zero-card listing.

### Flags

All three engines expose the identical set — 32 flags, counting
`--headless` and `--headful` as the two halves of one toggle. A
`smoke_test.py` check asserts the three sets are identical, so they can
never drift apart.

**Target**

| Flag | Default | Notes |
|---|---|---|
| `--url` | — | Any of the three shapes above (or `G2_URL`); overrides `--category`/`--product` |
| `--category` | — | A category slug, e.g. `crm` |
| `--product` | — | A product slug, e.g. `hubspot-sales-hub` |

**Scope and pacing**

| Flag | Default | Notes |
|---|---|---|
| `--max-results` | `60` | Cap on products collected |
| `--max-pages` | `5` | Hard cap on `?page=N` listing pages requested |
| `--page-delay` | `1.5` | Seconds between listing pages |
| `--retries` | `2` | Retries on a page's navigation failure |
| `--retry-delay` | `3.0` | Seconds between those retries |
| `--block-retries` | `2` | On a blocked, zero-product outcome, retry on the **same** session/exit IP this many extra times. "Retry before you rotate" — a fresh proxy/CDP identity stays a manual decision between runs. Worth knowing here specifically: DataDome's own slider challenge IS solvable via `--solve-captcha` + a proxy (see "Read this before trusting a run" above) — without a proxy configured, retrying is one of the few other levers this repo has |

**Pricing**

| Flag | Default | Notes |
|---|---|---|
| `--with-pricing` | off | Also fetch each collected product's `/pricing` page and fill `price`/`currency`/`price_source` from its cheapest tier. Off because it costs one extra request per product and leans on the least reliable parser here. An unreadable pricing page leaves that row's price empty; it never fails the run |
| `--pricing-limit` | `10` | With `--with-pricing`, fetch at most this many pricing pages |

**Output**

| Flag | Default | Notes |
|---|---|---|
| `--format` | `json` | `json` or `csv` |
| `--out` | `g2_results.<format>` | Output path |
| `--allow-empty` | off | Write output even if zero products were found (see "Output contract" for what this does **not** do) |
| `--dump-html` | off | Save the last fetched page's HTML as `<out stem>_debug.html`, on success too |

**Browser**

| Flag | Default | Notes |
|---|---|---|
| `--headless` / `--headful` | headless | |
| `--cdp-endpoint` | — | Connect to a remote CDP session instead of launching locally (or `G2_CDP_ENDPOINT`). Opt-in |
| `--fingerprint` | off | Fetch and apply a 2Captcha Fingerprint API profile. Ignored with `--cdp-endpoint` (`fingerprint_client.refuse_if_cdp`) |
| `--fp-tags` | — | Fingerprint filter, e.g. `Windows,Chrome` |
| `--fp-country` | — | Fingerprint filter, e.g. `us` |

**Proxies**

| Flag | Default | Notes |
|---|---|---|
| `--proxy` | — | A single proxy (or `G2_PROXY`) |
| `--proxy-file` | — | One proxy per line, same formats |
| `--proxy-shuffle` | off | |
| `--proxy-block-retries` | `3` | Per-exit failure budget before the pool drops an exit |

**Captcha and 2Captcha APIs**

| Flag | Default | Notes |
|---|---|---|
| `--solve-captcha` | `when-blocked` | `off` / `when-blocked` / `always`. Attempts 2Captcha's `DataDomeSliderTask` when a slider challenge is detected (**requires `--proxy`/`--proxy-file`** — no proxyless path exists for this type), and separately arms the Scraping Browser API's own auto-solve over `--cdp-endpoint`. These two are mutually exclusive, not additive: `--cdp-endpoint` nulls out any local proxy, so `DataDomeSliderTask` never fires there — DataDome coverage under `--cdp-endpoint` depends entirely on 2Captcha's own auto-solve, which is confirmed (2026-09-14) for Turnstile/Amazon WAF/Yandex SmartCaptcha/Lemin but **unconfirmed either way for DataDome**. Without a proxy and without `--cdp-endpoint`, a DataDome challenge is just detected/reported, not solved |
| `--twocaptcha-key` | — | (or `TWOCAPTCHA_KEY`) |
| `--min-score` | `0.3` | 2Captcha's own `minScore` field on a reCAPTCHA v3 task |
| `--captcha-api` | — | Override the 2Captcha REST base URL (testing only) |
| `--scraper-api` | off | Fetch via 2Captcha's browserless Scraper API instead of launching any browser — a genuinely different product from `--cdp-endpoint`'s Scraping Browser API. Requires a key. `--max-pages`/`--page-delay`/`--proxy`/`--cdp-endpoint`/`--fingerprint` are ignored in this mode (warned, not silently dropped): a single static fetch has no pagination loop and brings its own exit IP. Not a documented DataDome bypass |
| `--scraper-api-timeout` | `60` | Seconds 2Captcha itself waits for the page (1-120, their limit) |
| `--scraper-api-url` | — | Override the Scraper API base URL (testing only) |

## Output contract

`Product` (`output_writer.py`) — family-common columns first (CLAUDE.md §9,
never reordered), g2-specific columns after:

```
sku, source, category, title, brand, price, currency, price_source, product_url,
image_url, scraped_at,
rating_5, rating_10, review_count, review_count_source, product_id, product_uuid,
product_slug, vendor_id, category_id, product_type
```

- **`sku` is the product's URL slug** (`hubspot-sales-hub`), not the numeric
  `product_id`, falling back to a deterministic fingerprint of the product
  URL only when no slug can be read. The slug is the one identifier present
  on both a listing card and that same product's own detail page, so a
  listing row and a detail row for the same product join on `sku` instead
  of landing in `added`/`removed` on every `diff_runs.py` comparison.
  `product_id` is kept in its own column for anyone who wants G2's number.
- **`brand` is always empty.** It means the vendor (the company behind the
  product), and no vendor *name* field has been confirmed anywhere — a
  listing card's JSON carries a numeric `vendor_id` and nothing else. Left
  empty rather than filled with the product name, which would be a
  different thing wearing the same column. `vendor_id` carries what is
  confirmed.
- **`price` / `currency` / `price_source` are empty unless
  `--with-pricing`** (or a direct `--url .../pricing` run). See "Read this
  before trusting a run". When filled, `price` is the **lowest** parsed
  tier and `price_source` is always `pricing_page_text` — deliberately
  distinct from the structured `json_ld`/`embedded_json` values other
  family members use, so a consumer can spot a best-effort text-scraped
  price at a glance.
- **Two rating columns, never merged:**
  - `rating_5` — the **0-to-5** star rating from a category-listing card
    (`4.4/5`). Listing rows only.
  - `rating_10` — G2's own **0-to-10** composite score, from a product
    page's `aggregateRating` (`bestRating: 10`, e.g. `8.9`). Product rows
    only.

  A listing row leaves `rating_10` empty and vice versa; a row with both
  came from a merge a caller did on purpose. The scale is read from the
  page's own `bestRating` rather than assumed, and an unrecognised
  `bestRating` leaves **both** columns empty rather than guessing which one
  a number belongs in. Each individual embedded review is on the 0-5 scale
  again — a third place these can be confused, which is why neither column
  is just called `rating`. Nothing in this repo averages or converts
  between them, and a PR adding a single `rating` column will be rejected
  (see `CONTRIBUTING.md`).
- **`review_count_source`** is `listing_card` or `json_ld` — see the
  counts-disagree note above.

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero products (and nothing was written) · `5` remote API error · `6`
partial. Every completed/partial run writes a `<out>.meta.json` sidecar
with `status`, `stop_reason`, `engine`, `url`, `pages_requested`,
`pages_completed`, `failed_pages`, `product_count`, `price_confirmed_pct`
and timestamps — **except** a failed/empty/blocked/remote-API-error run,
which writes no output and no sidecar at all, so it can never overwrite or
contradict a previous good run. `--allow-empty` opts out of the "don't
write an empty result" half of that guard **only**: it never launders a
blocked or remote-API-error run into `complete`, and neither does products
happening to be present when the run was in fact gated partway through (see
`output_writer.finish_run`'s docstring for the exact precedence rule, and
`smoke_test.py` for the regression tests that pin it).

`sample_output.json` / `sample_output.csv` demonstrate every column above.
**They are clearly-labelled fictional rows, not a capture** — invented
product names, invented ids, with `[FICTIONAL SAMPLE ROW — not a live
capture]` in each `title`. No live engine run exists to capture real rows
from yet (see the status table). They do show the real schema: a listing
row, a sparse listing row, a product-page row with `rating_10` where the
listing row has `rating_5`, and a `--with-pricing` row with
`price_source="pricing_page_text"`.

## Pagination

Confirmed real: a category listing paginates with `?page=N` (111 pages for
the `crm` slug at capture time). **Page counts are discovered from the
markup, never hardcoded per category** — `has_next_page()` reads the Next
control's disabled state and `current_page_number()` reads G2's own current
marker, so a category with three pages and one with 111 both terminate
correctly. `--max-pages` is the hard cap on top of that. Each `?page=N` URL
is rebuilt from the slug rather than string-edited from the caller's
`--url`. Rows are deduped by `sku` in page order, and a run that failed
some pages but collected products reports `partial` (exit `6`) rather than
a plausible-looking `complete`.

## Engines

Playwright is primary; Selenium and pyppeteer are parity copies — all three
agree on exit codes and the `Product` schema via the shared
`output_writer.finish_run()`. Real, stated limits (properties of the
drivers, not of g2.com):

- **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port`; the Scraping
  Browser API's `ws://login:pass@host:port` shape needs an authenticated
  WebSocket upgrade, which only Playwright's `connect_over_cdp` and
  pyppeteer's `connect` support. `selenium_scraper.py` refuses a
  credentialed `--cdp-endpoint` outright (exit `2`) rather than silently
  dropping the credential.
- **Selenium's `--proxy-server` cannot authenticate at all.** A `--proxy`
  with credentials has them stripped before reaching Chrome, with a loud
  warning — never a silent no-op.
- **pyppeteer is effectively unmaintained** (its own README points at
  Playwright) — shipped for parity, not as a recommendation.
- Install **exactly one** engine per environment — Playwright and pyppeteer
  declare mutually unsatisfiable `pyee` pins, and pyppeteer collides with
  Selenium's `urllib3` pin. Use a venv per engine, same as
  `.github/workflows/tests.yml`'s `engine-smoke` job.

`READINESS_WAIT_MS` is `1500` here, materially shorter than an SPA sibling's
`3000`: g2.com is server-rendered (Rails/PJAX), so product data is in the
delivered HTML and nothing has to hydrate before it is readable. What the
wait buys is slack for DataDome's own asynchronous checks, not for content.

## Known limitations

- **No live engine run has ever happened.** See the status table at the
  top. Everything below the architecture line — that g2.com will actually
  serve these pages to a Playwright/Selenium/pyppeteer session from your IP
  — is untested. `TESTING.md` is the checklist.
- **DataDome IS solvable — with a proxy.** `--solve-captcha` attempts
  2Captcha's `DataDomeSliderTask` automatically when a slider challenge is
  detected, but only if `--proxy`/`--proxy-file` is set (no proxyless path
  exists for this type). Without a proxy, a wall is an honest exit `3`,
  not a retry loop against a solver that was never actually tried. The
  slider-iframe pattern detected is 2Captcha's own documented vendor
  shape, unconfirmed against a real g2.com capture.
- **`parse_pricing_page()` is best-effort text matching.** No pricing
  JSON-LD exists on g2.com to back it. Empty output from it means "couldn't
  read", never "free".
- **`grids.json` is real, sanctioned, and not used by this repo.**
  `https://www.g2.com/categories/{slug}/grids.json` is G2's own JSON
  endpoint for a category's Grid® ranking data — confirmed live. It ranks
  only a subset of a category (253 products for `crm`, against 1661 found
  by paginating the DOM), so it is not a substitute for the listing on its
  own. **This repo ships no parser for its response, no enrichment, and no
  flag that calls it**; `g2_parser.grids_json_url()` builds the URL and
  nothing consumes it, and a `--url` pointed at it is refused with a
  message saying so. The reason is narrow and factual: its field names were
  never captured, and adding guessed keys to the shared output contract is
  exactly the failure mode this family has documented before. Whoever
  captures a real response can add the parser and its columns.
- **`g2_parser.GENERAL_DISALLOWED_PATTERNS` is empty.** robots.txt's
  general `User-agent: *` group could not be re-fetched to transcribe its
  paths, and guessing them would be worse than leaving the list visibly
  unfilled. `is_known_scrape_target()` — an allowlist of the four URL
  shapes this scraper uses — is the conservative guard in the meantime, and
  filling the list in from a real fetch needs no code change.
- **`brand` is always empty**, and no vendor *name* source is confirmed.
- **Review text and author names are not in the output at all.** If they
  ever are, note that G2 itself publishes review authors as first name +
  last initial.

## Development

```bash
python3 smoke_test.py      # 90 checks, no engine driver required
python3 env_config.py      # shows what .env / the environment applied, never a secret
python3 diff_runs.py a.json b.json   # added / removed / changed / source_changed between two completed runs
```

`g2_parser.py` is the only file in this repo carrying any g2.com knowledge
(CLAUDE.md §5/§7) — selectors, URL shapes, quirks and the CONFIRMED /
BEST-EFFORT split all live in its module docstring. Everything else
(`output_writer.py`, `proxy_pool.py`, `captcha_solver.py`,
`fingerprint_client.py`, `scraper_api_client.py`, `diff_runs.py`,
`env_config.py`) is family-shared and carries no site knowledge. See
`CONTRIBUTING.md` before opening a PR — especially the two rules that
matter most here: never merge the rating scales, and every page path arms
captcha auto-solve with a counter shaped for the page it's looking at.

CI: the offline suite on Python 3.9 and 3.12, one `engine-smoke` job per
engine in its own virtualenv, a Docker build that builds/runs/launches a
real browser, and a daily `canary` that runs the local-first default
against real g2.com and interprets the exit code (a crash or bad usage
fails; blocked/empty/remote-API-error is a notice, because a CI runner's
datacentre IP is close to the worst possible exit for getting past
DataDome).

## License

MIT — see `LICENSE`.
