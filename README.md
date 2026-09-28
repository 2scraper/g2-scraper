# g2-scraper

![tests](https://github.com/2scraper/g2-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/g2-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![local-first](https://img.shields.io/badge/local--first-yes-success)

A scraper for g2.com's software marketplace. Give it a category slug, a
product slug, or a g2.com URL, and it returns a flat list of products:
title, G2 category, both of G2's rating scales, review count, G2's own
product/vendor ids, image and product link. Three engines (Playwright,
Selenium, Puppeteer/pyppeteer) with an identical CLI, output schema and
exit codes, so you can pick whichever fits your stack. JSON or CSV output.

## Local-first

No 2Captcha account, proxy or key is required to run this. The default is
an ordinary local headless Chromium. `--proxy` / `--cdp-endpoint` /
`--fingerprint` / `--scraper-api` are opt-in for volume, a specific exit
country, or running through a managed remote browser.

## Install

Pick one engine (installing more than one into the same environment isn't
supported — see "Engines"):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                    # pyppeteer
```

Copy `.env.example` to `.env` — leave it blank for a normal first run and
fill in what you need later. `python3 env_config.py` shows what was picked
up, without ever printing a secret.

## Credentials

None of these belong on a command line — `ps`, shell history and crash
reports would leak them permanently. All three are read from `.env` /
the environment by `env_config.py`:

| Variable | What it's for |
|---|---|
| `TWOCAPTCHA_KEY` | One key for captcha solving, the Scraping Browser API, the Fingerprint API and the Scraper API. Not required for a normal local run |
| `G2_PROXY` | A proxy for a locally-launched browser, e.g. `http://login:password@host:port`. Blank = scrape from this machine's IP |
| `G2_CDP_ENDPOINT` | A ready-made CDP connection string (typically a 2Captcha Scraping Browser API session). Opt-in — Selenium can't use a credentialed one, see "Engines" |

`G2_URL` is also read, as a convenience default for `--url`.

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

# through 2Captcha's Scraping Browser API (opt-in)
python3 playwright_scraper.py --category crm --cdp-endpoint "$G2_CDP_ENDPOINT"
```

`selenium_scraper.py` and `puppeteer_scraper.py` take the identical flags
and produce the identical output — see "Engines" for the one place
Selenium behaves differently.

### Three page shapes, one `--url`

| URL shape | Parser | What a row gets |
|---|---|---|
| `https://www.g2.com/categories/{slug}` (`?page=N`) | category listing | `rating_5`, `review_count`, `category`, `product_id`, `product_uuid`, `vendor_id`, `category_id`, `product_type`, image |
| `https://www.g2.com/products/{slug}/reviews` | product page | `rating_10`, `review_count`, title, image |
| `https://www.g2.com/products/{slug}/pricing` | pricing page | `price`, `currency`, `price_source`; nothing else is invented |

A URL is checked against `robots.txt` and against the shapes this repo
actually parses before any request is made; anything else is refused with
exit `2` rather than fetched and discarded.

### Flags

All three engines expose the identical 32 flags (`--headless`/`--headful`
counted as one toggle).

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
| `--block-retries` | `2` | Retry a blocked zero-product scrape this many extra times. With `--proxy-file`, every attempt advances to the next live proxy |

**Pricing**

| Flag | Default | Notes |
|---|---|---|
| `--with-pricing` | off | Also fetch each collected product's `/pricing` page and fill `price`/`currency`/`price_source`. An unreadable pricing page just leaves that row's price empty — it never fails the run |
| `--pricing-limit` | `10` | With `--with-pricing`, fetch at most this many pricing pages |

**Output**

| Flag | Default | Notes |
|---|---|---|
| `--format` | `json` | `json` or `csv` |
| `--out` | `g2_results.<format>` | Output path |
| `--allow-empty` | off | Write output even if zero products were found |
| `--dump-html` | off | Save the last fetched page's HTML as `<out stem>_debug.html` |

**Browser**

| Flag | Default | Notes |
|---|---|---|
| `--headless` / `--headful` | headless | |
| `--cdp-endpoint` | — | Connect to a remote CDP session instead of launching locally (or `G2_CDP_ENDPOINT`) |
| `--fingerprint` | off | Fetch and apply a 2Captcha Fingerprint API profile (ignored with `--cdp-endpoint`) |
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
| `--solve-captcha` | `when-blocked` | `off` / `when-blocked` / `always`. Attempts 2Captcha's `DataDomeSliderTask` when a slider challenge is detected — **this requires `--proxy`/`--proxy-file`**, there is no proxyless path for this captcha type. Over `--cdp-endpoint`, DataDome coverage instead depends on the Scraping Browser API's own auto-solve |
| `--twocaptcha-key` | — | (or `TWOCAPTCHA_KEY`) |
| `--min-score` | `0.3` | 2Captcha's own `minScore` field on a reCAPTCHA v3 task |
| `--captcha-api` | — | Override the 2Captcha REST base URL (testing only) |
| `--scraper-api` | off | Fetch via 2Captcha's browserless Scraper API instead of launching any browser. Requires a key. `--max-pages`/`--page-delay`/`--proxy`/`--cdp-endpoint`/`--fingerprint` are ignored in this mode — a single static fetch has no pagination loop and brings its own exit IP. By itself this mode has no captcha solving and no documented way to pin the exit country/locale — see `--scraper-api-cdp` |
| `--scraper-api-timeout` | `60` | Seconds 2Captcha itself waits for the page (1-120) |
| `--scraper-api-url` | — | Override the Scraper API base URL (testing only) |
| `--scraper-api-cdp` | off | Route `--scraper-api`'s fetch through a 2Captcha Scraping Browser CDP session (their `cdpurl` field) instead of their own default pool — chains the Scraper API to 2Captcha's OWN Scraping Browser, not a caller-supplied `--cdp-endpoint` (that flag stays ignored in `--scraper-api` mode). Requires `--scraper-api`. This is what gives `--scraper-api` real captcha auto-solve for DataDome and exit-country pinning. **Wired and covered by `smoke_test.py`, not yet exercised against a real 2Captcha/g2.com session** — see `CHANGELOG.md`/`TESTING.md` |
| `--scraper-api-country` | — | Exit country for `--scraper-api-cdp`'s Scraping Browser session, e.g. `us` (ignored without `--scraper-api-cdp`) |
| `--scraper-api-profile-id` | — | Reuse a specific Scraping Browser profile id across runs for `--scraper-api-cdp`, instead of the default pool (ignored without `--scraper-api-cdp`) |

## Output contract

`Product` columns, in order:

```
sku, source, category, title, brand, price, currency, price_source, product_url,
image_url, scraped_at,
rating_5, rating_10, review_count, review_count_source, product_id, product_uuid,
product_slug, vendor_id, category_id, product_type
```

- **`sku` is the product's URL slug** (`hubspot-sales-hub`), not the
  numeric `product_id`. It's the one identifier present on both a listing
  card and that product's own detail page, so rows for the same product
  join on it. `product_id` is kept in its own column too.
- **`brand` is always empty.** No vendor *name* field is available — a
  listing card's JSON only carries a numeric `vendor_id`, which is kept in
  its own column instead of being guessed at.
- **`price` / `currency` / `price_source` are empty unless
  `--with-pricing`** (or a direct `--url .../pricing` run). When filled,
  `price` is the lowest parsed tier.
- **Two rating columns, never merged:** `rating_5` is the 0-5 star rating
  from a listing card; `rating_10` is G2's own 0-10 composite score from a
  product page. A listing row leaves `rating_10` empty and vice versa.
  Nothing here averages or converts between them.
- **`review_count_source`** is `listing_card` or `json_ld` — a product's
  review count can differ slightly between the two pages (G2's own
  cache/render timing), so this records which one a row's number came
  from.

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero products (nothing written) · `5` remote API error · `6` partial.
Every completed/partial run writes a `<out>.meta.json` sidecar with
`status`, `stop_reason`, `engine`, `url`, `pages_requested`,
`pages_completed`, `failed_pages`, `product_count`, `price_confirmed_pct`
and timestamps. A failed/empty/blocked/remote-API-error run writes no
output and no sidecar, so it can never overwrite a previous good run.
`--allow-empty` only opts out of the "don't write an empty result" part of
that guard — it never turns a blocked or remote-API-error run into
`complete`.

`sample_output.json` / `sample_output.csv` show the schema with clearly
labelled fictional rows (`[FICTIONAL SAMPLE ROW — not a live capture]` in
each `title`).

## Pagination

A category listing paginates with `?page=N`. Each next URL is rebuilt from
the slug; termination is based on data (a page with zero cards or no new
SKU ends the listing), not on the rendered Next control, which is kept
only as a diagnostic. `--max-pages` and `--max-results` remain hard caps.
Rows are deduped by `sku` in page order, and a run that failed some pages
but still collected products reports `partial` (exit `6`), never a
plausible-looking `complete`.

## robots.txt

`https://www.g2.com/robots.txt` has a second rule group naming AI
crawlers (`GPTBot`, `ClaudeBot`, `Google-Extended`, and others), which adds
one rule the general group doesn't have: `Disallow: /products/*/reviews/*`
— sub-paths one level below a product's reviews page. This scraper honours
that stricter set by default. It does not cover the bare
`/products/{slug}/reviews` page, which is this scraper's actual target and
is unaffected. A `--url` matching a disallowed path is refused with exit
`2` before any request is made.

## Engines

Playwright is primary; Selenium and Puppeteer/pyppeteer are parity
implementations — all three agree on exit codes and the `Product` schema.
Real, stated limits (properties of the drivers, not of g2.com):

- **Selenium can't use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port`; the Scraping
  Browser API's `ws://login:pass@host:port` shape needs an authenticated
  WebSocket upgrade, which only Playwright and pyppeteer support.
  `selenium_scraper.py` refuses a credentialed `--cdp-endpoint` outright
  (exit `2`) instead of silently dropping the credential.
- **Selenium's `--proxy-server` can't authenticate at all.** A `--proxy`
  with credentials has them stripped before reaching Chrome, with a loud
  warning.
- **pyppeteer is effectively unmaintained** — shipped for parity, not as a
  recommendation.
- Install **exactly one** engine per environment: Playwright and pyppeteer
  declare mutually unsatisfiable `pyee` pins, and pyppeteer collides with
  Selenium's `urllib3` pin. Use a separate venv per engine.

## Known limitations

- **DataDome needs a proxy to be solved.** `--solve-captcha` attempts
  2Captcha's `DataDomeSliderTask` automatically, but only with
  `--proxy`/`--proxy-file` set — there's no proxyless path for this
  captcha type. Without a proxy, a DataDome wall is reported honestly as
  exit `3`, not retried against a solver that was never actually tried.
- **`parse_pricing_page()` is best-effort text matching**, since g2.com
  ships no pricing JSON-LD. Empty output from it means "couldn't read",
  never "free".
- **`https://www.g2.com/categories/{slug}/grids.json`** is G2's own
  ranked-subset endpoint and is not used by this scraper — it only ranks
  part of a category, and this repo ships no parser for its response. A
  `--url` pointed at it is refused with a message saying so.
- **`brand` is always empty** — see "Output contract".
- **Review text and author names are not in the output.**

## Development

```bash
python3 smoke_test.py               # offline checks, no engine driver required
python3 env_config.py               # shows what .env / the environment applied, never a secret
python3 diff_runs.py a.json b.json  # added / removed / changed / source_changed between two runs
```

`g2_parser.py` is the only file carrying any g2.com-specific knowledge —
selectors, URL shapes and quirks. Everything else (`output_writer.py`,
`proxy_pool.py`, `captcha_solver.py`, `fingerprint_client.py`,
`scraper_api_client.py`, `diff_runs.py`, `env_config.py`) is generic. See
`CONTRIBUTING.md` before opening a PR.

CI runs the offline suite on Python 3.9 and 3.12, one `engine-smoke` job
per engine in its own virtualenv, a Docker build that builds/runs/launches
a real browser, and two opt-in live canaries that skip cleanly without
their required secret.

## License

MIT — see `LICENSE`.
