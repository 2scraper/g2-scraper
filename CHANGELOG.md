# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

### Added — 2026-09-22: initial repo

- **Three engines for g2.com, one output contract.**
  `playwright_scraper.py` (primary), `selenium_scraper.py` and
  `puppeteer_scraper.py` (pyppeteer) scrape category listings
  (`/categories/{slug}`, paginated with `?page=N`), product pages
  (`/products/{slug}/reviews`) and pricing pages
  (`/products/{slug}/pricing`), with an identical CLI flag set, identical
  exit codes and an identical `Product` schema across all three. A single
  `--url` auto-routes to whichever of the three page shapes it was given;
  `--category` and `--product` build the URL from a slug.
- **Two rating scales, kept separate.** `rating_5` (the 0-5 star rating on
  a category card) and `rating_10` (G2's own 0-10 composite score from a
  product page's `SoftwareApplication` JSON-LD) are two columns that are
  never averaged, converted or folded into one. `review_count_source`
  records which page a row's review count came from, because a listing
  card and that same product's `aggregateRating` genuinely disagree by
  small amounts.
- **Best-effort pricing behind `--with-pricing`.** g2.com carries no price
  on a listing card or a product page at all, and ships no pricing
  JSON-LD; `--with-pricing` fetches each product's `/pricing` page and
  fills `price`/`currency` from the cheapest tier its text parser can
  read, stamping `price_source="pricing_page_text"` so a consumer can tell
  a text-scraped price from a structured one. Off by default, capped by
  `--pricing-limit`, and a pricing page that can't be read leaves that
  row's price empty rather than failing the run.
- **2Captcha integration, opt-in and never required.** Proxies
  (`--proxy`/`--proxy-file`), the Scraping Browser API over CDP
  (`--cdp-endpoint`), the Fingerprint API (`--fingerprint`), the
  browserless Scraper API (`--scraper-api`), and captcha detection/solving
  (`--solve-captcha`). **g2.com's confirmed bot protection is DataDome,
  which is detected but not automated** — no automated solve path exists
  for it at 2Captcha or anywhere else, so a DataDome wall is reported as
  `unsupported_vendor` + `EXIT_BLOCKED` rather than silently retried
  against a solver that cannot help. The default run is a plain local
  headless Chromium with no key, no proxy and no account.
- **robots.txt honoured at the stricter AI-crawler setting by default.**
  g2.com's robots.txt gives `ClaudeBot` and its peers exactly one rule the
  general group doesn't have (`Disallow: /products/*/reviews/*`), and this
  repo obeys it: a disallowed `--url` is refused before any request, not
  fetched and discarded.
- **JSON and CSV output with a documented schema**, a `.meta.json` sidecar
  on every completed/partial run, and the family exit-code contract
  (`0` complete, `1` crash, `2` bad usage, `3` blocked, `4` zero products,
  `5` remote API error, `6` partial). A failed or empty run writes neither
  the data file nor the sidecar, so it can never overwrite a previous good
  result.
- **`diff_runs.py`** for comparing two completed runs (added / removed /
  price-changed / source-changed).
- **Offline test suite and CI.** `smoke_test.py` runs with no engine
  driver installed at all (74 checks); GitHub Actions runs it on two
  Python versions plus one `engine-smoke` job per engine in its own
  virtualenv, builds and exercises the Docker image, and ships a daily
  `canary` workflow that runs the local-first default against the real
  site and interprets the exit code rather than demanding a `0`.
- **Docs**: `README.md`, `TESTING.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `landing.md`/`landing.html`, and
  `sample_output.json`/`sample_output.csv` (clearly-labelled fictional
  rows demonstrating the schema — no live engine run exists to capture
  real ones from yet; see README "What has and hasn't been verified").

### Known at release

- **No part of this repo has been run against live g2.com.** The site
  knowledge in `g2_parser.py` comes from real browser-tool captures made
  2026-09-22 (DOM shapes, JSON-LD, robots.txt, DataDome); the engine
  scripts themselves have never made a request to g2.com, because the
  environment this repo was built in blocks the network egress they need.
  See README "What has and hasn't been verified" and `TESTING.md`.
- **`https://www.g2.com/categories/{slug}/grids.json` is real, sanctioned
  and not wired in.** `g2_parser.grids_json_url()` builds the URL, but no
  parser for the response ships and no flag calls it — its field names
  were never captured, and it covers only a ranked subset of a category
  (253 of 1661 products for `crm`).
- **`g2_parser.GENERAL_DISALLOWED_PATTERNS` is empty** — robots.txt's
  `User-agent: *` group could not be re-fetched to transcribe it, and
  guessing paths would be worse than leaving it visibly unfilled.
  `is_known_scrape_target()` is the conservative guard in the meantime.
