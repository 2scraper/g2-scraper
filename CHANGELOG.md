# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage.

## [Unreleased]

## [0.1.0] - 2026-09-27

First tagged release. Three engines (Playwright primary, Selenium and
Puppeteer for parity), one output contract, DataDome recognized and
(with a proxy) solvable via 2Captcha's `DataDomeSliderTask`, local-first
by default.

### Added

- Three engines for g2.com — `playwright_scraper.py` (primary),
  `selenium_scraper.py` and `puppeteer_scraper.py` — scrape category
  listings (`/categories/{slug}`, paginated with `?page=N`), product
  pages (`/products/{slug}/reviews`) and pricing pages
  (`/products/{slug}/pricing`), with an identical CLI flag set, exit
  codes and `Product` schema across all three. A single `--url`
  auto-routes to whichever of the three page shapes it was given;
  `--category` and `--product` build the URL from a slug.
- Two rating scales kept as separate columns: `rating_5` (the category
  card's star rating) and `rating_10` (the product page's composite
  score), never averaged or merged. `review_count_source` records which
  page a row's review count came from, since the two pages can disagree
  by small amounts.
- Best-effort pricing behind `--with-pricing`: fetches each product's
  `/pricing` page and fills `price`/`currency` from the cheapest tier,
  stamping `price_source="pricing_page_text"`. Off by default, capped by
  `--pricing-limit`; an unreadable pricing page just leaves that row's
  price empty.
- Optional 2Captcha integration, never required: proxies
  (`--proxy`/`--proxy-file`), the Scraping Browser API over CDP
  (`--cdp-endpoint`), the Fingerprint API (`--fingerprint`), the
  browserless Scraper API (`--scraper-api`), and captcha detection/solving
  (`--solve-captcha`). DataDome is solvable via `DataDomeSliderTask` once
  a proxy is configured; without one, a DataDome wall reports
  `unsupported_vendor` + `EXIT_BLOCKED` rather than a solve attempt that
  was never actually tried.
- robots.txt honoured at the stricter AI-crawler setting by default: a
  disallowed `--url` is refused before any request, not fetched and
  discarded.
- JSON and CSV output with a documented schema, a `.meta.json` sidecar on
  every completed/partial run, and the family exit-code contract (`0`
  complete, `1` crash, `2` bad usage, `3` blocked, `4` zero products, `5`
  remote API error, `6` partial). A failed or empty run writes neither
  file, so it can never overwrite a previous good result.
- `diff_runs.py` for comparing two completed runs (added / removed /
  price-changed / source-changed).
- Offline test suite (`smoke_test.py`, no engine driver required) and a
  real-browser local end-to-end suite (`local_e2e_test.py`, no live
  g2.com access needed). CI runs both on two Python versions plus one
  `engine-smoke` job per engine, builds and exercises the Docker image,
  and ships a daily `canary` workflow against the live site.
- Docs: `README.md`, `TESTING.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `landing.md`/`landing.html`, and
  `sample_output.json`/`sample_output.csv` (clearly labelled fictional
  rows demonstrating the schema).

### Fixed

- A parser exception on a listing page now degrades that page to a
  clean failed-page state instead of being read as an empty result.
- Category pagination now terminates on data (zero cards or no new SKU)
  rather than trusting the rendered Next control, so a selector change
  can't silently truncate a run; `--url ".../categories/{slug}?page=N"`
  now correctly resumes at page N instead of restarting at page 1.
- DataDome's challenge iframe is now recognized in both shapes g2.com
  actually serves (`geo.captcha-delivery.com/captcha/?...` and
  `.../interstitial/?...`) — only the first was previously detected.
- `DataDomeSliderTask` solve attempts now fall back to the live page's
  own `navigator.userAgent` when `--fingerprint` wasn't used, instead of
  failing the required-user-agent check silently.
- A 2Captcha rejection for an already-flagged proxy IP now reports a
  distinct `warning_proxy_banned` outcome instead of the generic
  solver-error bucket; a related silent gap (`warning_no_proxy` having no
  log line in any engine) is also fixed.
- Proxy rotation no longer aborts the whole run on the first proxy's
  navigation failure — it now advances to the next proxy in
  `--proxy-file`, in category, product and pricing modes, across all
  three engines. `playwright_scraper.py` gained the dead-proxy detection
  the other two engines already had.
- The Docker image removes `tests/fixtures` after the build-time smoke
  suite runs, matching the CI assertion that the published image ships
  no test suite.
- The live canaries skip cleanly without a configured proxy/CDP secret
  and, when enabled, require a complete three-page result — blocked,
  empty, remote-error and partial outcomes no longer produce a
  misleading green canary.
- `@claude` triggers are restricted to owners, members and collaborators.
  Credential detection has one implementation shared by the offline
  suite and CI; test-only modules are no longer packaged.

### Known limitations

- **`https://www.g2.com/categories/{slug}/grids.json`** is a real,
  sanctioned G2 endpoint but is not wired in — it ranks only a subset of
  a category, its field names were never captured, and this repo ships
  no parser for its response.
- **`g2_parser.GENERAL_DISALLOWED_PATTERNS` is empty** — robots.txt's
  general `User-agent: *` group hasn't been transcribed yet.
  `is_known_scrape_target()` is the conservative guard in the meantime.
- **`parse_pricing_page()` is best-effort text matching** — g2.com ships
  no pricing JSON-LD, so this is the parser most likely to break first if
  G2 restyles the page.

[Unreleased]: https://github.com/2scraper/g2-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/g2-scraper/releases/tag/v0.1.0
