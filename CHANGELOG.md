# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage.

## [Unreleased]

> **Behaviour change.** A run whose first page never loaded through a proxy
> pool on every `--block-retries` attempt now ends `remote_api_error`, exit
> 5, instead of `blocked`, exit 3. Rotation is unchanged: each attempt still
> moves to the next exit. A real block or challenge on the last attempt is
> still exit 3.

### Fixed

- A first page that timed out through the proxy on every attempt was reported
  as a site block (exit 3), even though the site never answered. A proxy that rejects its credential (HTTP 407) shows
  up in Chromium as exactly this navigation timeout. All three engines now
  report it as content never obtained.

### Changed

- CI only: both canary jobs bill a paid route (`G2_PROXY`, `G2_CDP_ENDPOINT`),
  so the canary now runs weekly (Mondays 07:17 UTC) instead of daily; a manual
  dispatch still runs it.

## [0.2.0] - 2026-10-15

> **Behaviour changes for existing scripts (2026-10-14).** Exit codes:
> rows gathered and then a block or remote error now exit `6` (`partial`),
> not `3`/`5`; a served page that yields nothing usable (a page-1 parser
> exception, a product page with no product data) now exits `5`
> (`parser_found_nothing`), not `4`; an HTTP 404 target is no longer
> "blocked" — it is fetched once and exits `4`, not `3`. A CSV cell starting
> with `=`, `+`, `-`, `@`, tab, CR or LF now gets a leading `'`. A paid
> captcha solve is now limited to one per page per run. `--proxy-block-retries`
> now rejects negative values. `G2_URL` no longer overrides a typed
> `--category`/`--product`, and `.env.example` ships it commented out.

### Fixed — 2026-10-14

- **One paid solve per page.** Every `--block-retries` attempt re-ran the
  scrape and could buy a fresh DataDome solve for the same page: measured
  with a stubbed solver, one blocked page bought 3 solves at the default
  `--block-retries 2` and 6 at 5. The per-page allowance now lives on the
  run's single 2Captcha client and is checked immediately before each
  purchase, in all three engines; retries and proxy rotation do not reset
  it. A page that has used its allowance is reported as blocked.
- **Atomic output.** JSON, CSV and the `.meta.json` sidecar are written to a
  temporary file in the same directory, fsynced and renamed into place. A
  failure part-way through used to leave a truncated file where the previous
  good output was. New files get `0o666 & ~umask`; an existing file keeps
  its mode.
- **CSV formula guard**, with `csv_cells_escaped` in the sidecar (see the
  note above). Numbers and the JSON output are untouched.
- **Outcome classification** (see the note above), and the sidecar's
  `stop_reason` now says why a run stopped instead of repeating `status`.
- **`G2_URL` precedence**: a `G2_URL` in the environment or `.env` used to
  replace a typed `--category`/`--product` silently.
- **`.gitignore`** now ignores every `.env*` copy (`.env.bak`, `.env.local`,
  …) and keeps `.env.example`; `.dockerignore` likewise.
- **Credential scan** (`.github/ci_checks.py`): now catches an unquoted
  `.env` line, a bare 32-hex key, and JSON-escaped quotes; reads ignored
  `.env.*` backups; recognises any virtualenv by its `pyvenv.cfg`; scans
  `smoke_test.py` for every rule except its deliberate masking fixtures;
  and fails if it read no files.
- **Wording**: comments no longer state what 2Captcha cannot solve — a
  Cloudflare challenge page or PerimeterX is described as something this
  repo does not implement. `--scraper-api-cdp`'s DataDome auto-solve is
  described as unverified everywhere (README, help, this file).
  `--fp-tags` examples use a single tag (`Windows`).
- Code comments and checks no longer cite a private maintainers' file;
  the rules are stated in place.

### Added — 2026-09-28, `--scraper-api-cdp`: Scraping Browser routing and country pinning for `--scraper-api`

- `--scraper-api` on its own has two documented gaps: no captcha solving
  at all (a solved token has no live page/DOM in that mode to be injected
  into, so g2.com's DataDome challenge goes unsolved there), and no way to
  pin the exit country/locale. `scraper_api_client.scrape_url()` already
  had an untested `cdp_url` parameter for exactly this — 2Captcha's
  Scraper API accepts a caller-supplied CDP session (their `cdpurl`
  field) instead of using their own default browser pool — but nothing
  in this codebase had ever called it with one.
- `--scraper-api-cdp` (all three engines) fills that parameter with
  `TwoCaptchaClient.scraping_browser_connection_url()`'s own output —
  chaining the Scraper API to 2Captcha's OWN Scraping Browser product,
  not a caller-supplied `--cdp-endpoint` (kept separate on purpose: an
  arbitrary CDP session isn't known to support this field the way
  2Captcha's own does, and `--scraper-api` already ignores
  `--cdp-endpoint` for the same reason). The intent is to give
  `--scraper-api` the Scraping Browser's own captcha auto-solve and
  country pinning; whether that auto-solve clears g2.com's DataDome
  challenge is UNVERIFIED (never run against g2.com). Country pinning is
  via the new
  `--scraper-api-country`/`--scraper-api-profile-id` flags (the latter
  reuses a profile across runs the same way `scraping_browser_connection_
  url()`'s own docstring already recommends for `--cdp-endpoint`).
- `--scraper-api-cdp` requires `--scraper-api` (`EXIT_BAD_USAGE`
  otherwise, not a silent no-op); `--scraper-api-country`/
  `--scraper-api-profile-id` without `--scraper-api-cdp` log a warning
  rather than doing nothing silently.
- **Honesty note, same as every other live-testing gap in this repo**:
  wired and covered by `smoke_test.py` (structural checks across all
  three engines, plus a behavioral one that injects a fake Scraper API
  response and confirms the built `cdpurl` — country, profile id — really
  reaches `scrape_url()`, and that plain `--scraper-api` is unaffected
  when the flag is absent). None of this has been exercised against a
  real 2Captcha/g2.com session yet — see TESTING.md's "Scraper API"
  section. This is a real, tested-as-wired fix, not a claim that captcha
  solving or country pinning have been confirmed working live.

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

[Unreleased]: https://github.com/2scraper/g2-scraper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2scraper/g2-scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/2scraper/g2-scraper/releases/tag/v0.1.0
