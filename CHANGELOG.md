# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

## [0.1.0] - 2026-09-27

First tagged release of g2-scraper, the g2.com member of the
[2scraper](https://github.com/2scraper) family — three engines
(Playwright primary, Selenium and Puppeteer for parity), one output
contract, DataDome recognized and (with a proxy) solvable via 2Captcha's
`DataDomeSliderTask`, local-first by default. Everything below this line,
across every dated entry, is what shipped in this first tag; see
`README.md`'s "What has and hasn't been verified" for the honest split
between what's been confirmed live and what's architecture-tested only.

### Fixed — 2026-09-23: pre-publication audit hardening

- Listing parser exceptions now carry an explicit failed-page state through
  every engine and Scraper API path (`g2_parser.parse_category_listing_safely`
  returns a `ListingParseResult`, not a bare list), so a parser exception is
  never confused with a page that genuinely rendered zero results. The
  failed page still lands in `failed_pages` and the engine's own log either
  way; an uncommitted draft of this fix additionally reordered
  `finish_run()`'s outcome precedence so this made the run exit "partial"
  instead of "empty" even with zero products collected, citing a sibling
  repo as precedent for the reorder — that citation didn't hold up (the
  sibling checks zero_products before partial, same as here), so the
  precedence order was left exactly as CLAUDE.md §9 states it.
- Category pagination now treats G2's Next control as a diagnostic hint.
  Engines probe reconstructed `?page=N+1` URLs and terminate on zero cards or
  no new SKU, so a selector change cannot silently truncate a complete run.
- The Docker image removes `tests/fixtures` after the build-time smoke suite,
  matching the CI assertion that the published image contains no test suite.
- The live canaries now skip without a configured proxy/CDP secret and, when
  enabled, require a complete three-page result. Blocked, empty, remote-error,
  and partial outcomes no longer produce a misleading green canary.
- Public `@claude` triggers are restricted to owners, members and
  collaborators. Credential detection now has one implementation shared by
  the offline suite and CI, and test-only modules are no longer packaged.
- Dead-first-proxy rotation now covers standalone product and pricing URLs in
  all three engines, not only category mode. These failures remain explicit in
  `failed_pages`, and the local browser E2E exercises both single-page paths.

### Fixed — 2026-09-23: a real 10-proxy `--proxy-file` run stopped after one navigation failure, never rotating past it

Roman's next live test tried a whole list of 10 fresh proxies:
`--proxy-file proxies.txt --block-retries 9` (with `G2_PROXY` unset, no
`--cdp-endpoint`). The run got through 6 of the 10 (two
`cloudflare_managed_challenge` blocks, two of the already-known `t=bv`
banned-IP 2Captcha rejections from yesterday's fix) and then stopped dead
on attempt 6's proxy — a plain `Page.goto: Timeout 30000ms exceeded`, not
even a `net::ERR_*` code — never trying proxies 7 through 10 at all.

- **Bug: `scrape_category()`'s first-page-navigation-failure branch, in
  all three engines, unconditionally set `remote_api_error=True` and
  broke out of the run.** `run()`'s `block_attempt` retry loop treats
  `remote_api_error` as fatal (same as a crash — it breaks immediately),
  so the entire point of handing it a 10-proxy list was defeated the
  moment ANY single one of those proxies failed to load page 1, whether
  that proxy was genuinely dead or just slow. Fixed: when a `proxy_pool`
  with other entries is active, this now sets `blocked=True` instead,
  which the same retry loop already treats as "try again" — and trying
  again calls `proxy_pool.next()`, landing on the next proxy in the list.
  The old behaviour — a hard `remote_api_error` — is kept for the one
  case where it's still correct: no proxy pool at all (a direct
  connection, or a `--cdp-endpoint` session, which brings its own fixed
  exit IP and has nothing to rotate to).
- **Related parity gap, found while tracing the bug above and fixed the
  same day:** `selenium_scraper.py` and `puppeteer_scraper.py`'s
  `_goto_with_retries()` have always detected a hard dead-proxy signature
  (`proxy_pool.is_proxy_dead_error()` against Chromium's own
  `PROXY_DEAD_MARKERS`) and reported it via `proxy_pool.report_failure(
  proxy, dead=True)` — excluding a proxy that's flatly unreachable from
  rotation immediately, rather than waiting on `--block-retries`.
  `playwright_scraper.py` — this family's PRIMARY engine (CLAUDE.md
  §14) — never had this at all; its `_goto_with_retries()` took no
  `proxy_pool`/`proxy` parameters whatsoever. Brought to parity.
- **New regression coverage, not just a structural check this time:**
  `local_e2e_test.py` now spins up a `--proxy-file` with a genuinely dead
  first proxy (an unbound local port — Chromium reports
  `net::ERR_PROXY_CONNECTION_FAILED` near-instantly) and a live second
  proxy, and asserts the run still completes (`EXIT_OK`) rather than
  aborting as `remote_api_error` — confirmed to fail against the
  pre-fix code (exit 5) and pass against the fix, for playwright and
  puppeteer live in a real browser. `smoke_test.py` gained the
  structural counterpart for all three engines (this repo's usual "an
  engine needs a real browser to exercise behaviourally offline" split).
  Selenium's own copy of the new e2e check could not be run live in this
  particular build environment — an unrelated chromedriver/Chromium
  version mismatch (chromedriver 147 vs. this environment's Chromium
  141) fails EVERY selenium e2e check here, including the pre-existing
  ones, not just the new one — but its code is byte-identical to
  puppeteer's already-confirmed fix and is covered by the offline
  structural check (`smoke_test.py`).
- **Follow-up resolved by the pre-publication audit above:** the identical
  first-dead-proxy failure in standalone `scrape_product_page()` and pricing
  mode now becomes a rotatable blocked result when `--proxy-file` is active;
  fixed/direct identities retain the fatal remote-error outcome.
- Also observed, not itself a bug: 2 of the 6 attempts in Roman's run hit
  `cloudflare_managed_challenge`, a DIFFERENT block shape than DataDome
  with no 2Captcha task type mapped to it in this family at all — by
  design unsolvable here, not a gap in this fix.

### Fixed — 2026-09-22 (same day, latest of three): a second real live run, a real 2Captcha rejection surfaced two silent gaps

Roman's follow-up run — same command, but with `--cdp-endpoint` commented
out of `.env` so `G2_PROXY` actually drove the browser — reached a REAL
2Captcha `createTask` call for the first time ever in this repo. 2Captcha
rejected it: `ERROR_BAD_PARAMETERS Your captcha_url value contains
"t=bv", that means your IP address is banned.` — DataDome had already
flagged that proxy exit, and 2Captcha refuses a doomed solve rather than
attempting one. Two real things came out of this:

- **The `/captcha/` iframe path — this repo's ORIGINAL DataDome pattern,
  in place since before today's other fix — is now ALSO confirmed live**
  (previously only 2Captcha's own documented shape, never captured from
  g2.com itself). `title="DataDome CAPTCHA"`, `dd.rt:'c'` (vs. the
  `/interstitial/` capture's `rt:'i'` from the earlier `--cdp-endpoint`
  run) — DataDome serves a DIFFERENT challenge shape depending on how
  risky it judges the request, and this repo has now seen both live. The
  scrubbed capture (including the real `t=bv` marker) is
  `tests/fixtures/g2_datadome_captcha_banned_ip.html`.
- **Bug: a rejected DataDomeSliderTask task had no distinct outcome, and
  two related actions had NO LOG LINE AT ALL in any engine.**
  `captcha_solver.solve_when_blocked()` now returns
  `action="warning_proxy_banned"` for this specific, documented 2Captcha
  rejection (split from the generic `warning_solver_error` bucket the
  same way `warning_no_proxy` already was, for the same reason: the fix —
  rotate to a different proxy exit — is different from "retry the same
  one"). Tracing the log line down into all three engines surfaced an
  independent, pre-existing gap: `warning_no_proxy` had been a real,
  tested `captcha_solver.py` action since DataDomeSliderTask's
  proxy-required guard was added, but no engine had a log branch for it
  at all — it silently fell through the if/elif chain with **zero** log
  output (the run still correctly reported `EXIT_BLOCKED`, via the
  separate `captcha_detected and not cards_present` check, so this was
  never a correctness bug — purely a silent-diagnostics one). Both gaps
  fixed in all three engines' `_maybe_solve_captcha()`, with matching
  entries added to each engine's `STILL_BLOCKED_ACTIONS`.

### Fixed — 2026-09-22 (same day, earlier): the first real live g2.com run, one real bug found, one real question answered

Roman's own first live run — `--cdp-endpoint` (the Scraping Browser API)
plus a real 2Captcha key, `--category crm`, `--solve-captcha when-blocked`
— hit a genuine, visible DataDome challenge on g2.com for the first time
in this repo's history. Two things came out of it:

- **Bug: `captcha_solver.py`'s `_DATADOME_IFRAME_RE` only matched
  `geo.captcha-delivery.com/captcha/?...`** — 2Captcha's own published
  integration-doc shape, never confirmed against this site. g2.com's real
  challenge iframe is `geo.captcha-delivery.com/interstitial/?...`
  (`title="DataDome Device Check"`) — a different path the pattern never
  matched, so this exact real block was silently falling into
  `identify_unsupported_vendor()`'s "present, nothing to solve" bucket
  instead of ever being attempted. Now matches both paths. The scrubbed
  real capture (single-use tokens redacted, structure intact) is saved at
  `tests/fixtures/g2_datadome_interstitial.html`, with its own
  `smoke_test.py` coverage, and the Dockerfile now copies that one file in
  (previously every fixture was inline; see the Dockerfile's own comment).
- **Answered, not settled: whether 2Captcha's Scraping Browser extension
  auto-solves DataDome under `--cdp-endpoint`.** `captcha_solver.py`'s
  module docstring used to call this "genuinely unconfirmed." This run's
  `Captcha.setAutoSolve` armed cleanly and never fired `Captcha.detected`
  across three retries against the real challenge above, and that
  capture's own extension-injected script list has a per-vendor
  `interceptor.js`/`hunter.js` pair for every OTHER vendor it covers
  (Turnstile, CaptchaFox, MTCaptcha, Amazon WAF, Yandex, Lemin, Arkose
  Labs, reCAPTCHA, KeyCaptcha, GeeTest) but none for DataDome. One real
  capture, one site — not proof it can never cover DataDome — but real
  evidence pointing at "not currently," where before there was none.

**Still open, on purpose:** whether this repo's own `DataDomeSliderTask`
REST path (the one the bug fix above actually restores) can solve the
`/interstitial/` shape the way it's documented to solve `/captcha/`. That
run used `--cdp-endpoint`, which never reaches this module's solve path at
all (`G2_PROXY` gets ignored the moment a CDP session is present — its own
exit always wins). Settling this needs a run WITHOUT `--cdp-endpoint`, and
costs real 2Captcha balance to attempt, so it's a deliberate next run, not
bundled into this fix.

### Fixed — 2026-09-22 (same day, after the two entries below): first real end-to-end test run, two real bugs found and fixed

This repo had never once been driven by a real browser end-to-end before
today — every prior check was offline (`smoke_test.py`) or against fakes.
A local stand-in for g2.com (exact confirmed HTML/JSON-LD/pagination
shapes, reused from `smoke_test.py`'s own fixtures) plus a stand-in
2Captcha `createTask`/`getTaskResult` server let all three real engines
run their real browsers through the real pipeline — navigation, retries,
parsing, pagination, captcha detection, a full `DataDomeSliderTask` solve
round trip (cookie applied via each driver's own native API, page
reloaded, healthy content served on the retry), output writing and every
documented exit code — for the first time. Two real, user-facing bugs
turned up:

- **`--url ".../categories/{slug}?page=N"` silently ignored `page=N` and
  always restarted at page 1.** `scrape_category()`'s pagination loop
  rebuilds each page's URL from the slug (`category_slug_from_url()` +
  `category_url()`), but nothing ever recovered the page NUMBER the same
  way — the loop always started at a hardcoded 1. This broke `g2_parser.
  py`'s and `playwright_scraper.py`'s OWN documented usage example,
  `--url "https://www.g2.com/categories/crm?page=2"`, which looked like it
  should resume at page 2 and instead silently restarted the category from
  page 1. New `g2_parser.page_number_from_url()` recovers it (1 for
  anything missing/unparsable/non-positive — a bad value means "start
  over", not a crash); all three engines' `scrape_category()` now start
  their loop at `page_number_from_url(start_url)`, and every place that
  used to special-case `page_num == 1` to mean "the first request of this
  run" now correctly says `page_num == start_page`. Caught live: running
  `--url ".../categories/crm?page=2"` against the local fixture returned
  page 1's products until this fix, page 2's after it.
- **A `--solve-captcha`/`--proxy`/`--twocaptcha-key` run with no
  `--fingerprint` could never actually solve a real DataDome slider.**
  `user_agent` (mandatory for `DataDomeSliderTask` — see the entry below)
  was ONLY ever populated via `--fingerprint`'s Fingerprint API call;
  without it, every solve attempt hit captcha_solver.py's own "requires
  user_agent" guard and gave up, even though neither README nor
  TESTING.md ever documented `--fingerprint` as a second prerequisite —
  only a proxy was. All three engines' `_maybe_solve_captcha()` now falls
  back to the ACTUAL live page's own `navigator.userAgent` (via
  `page.evaluate`/`driver.execute_script`) whenever no `--fingerprint` UA
  was supplied — arguably more correct than a Fingerprint-API string
  regardless, since 2Captcha's own docs ask for "the SAME modern browser
  UA the challenge will be presented back to," and the real page's own UA
  is definitionally that. Caught live: a full `DataDomeSliderTask` round
  trip against a local fake 2Captcha server failed with "requires
  user_agent" until this fix, then succeeded end-to-end (cookie applied,
  page reloaded, healthy content served) after it.

`smoke_test.py` gained 2 new checks for the first bug (unit-level for
`page_number_from_url()`, structural for all three engines using it) —
83 total. The second bug has no offline-testable regression (it only
shows up against a real page/driver), so its only regression coverage is
this changelog entry and the live run that proved it — a gap worth
knowing about, not hiding.

### Clarified — 2026-09-22 (same day, after the DataDome fix below): `--cdp-endpoint` does not extend it

Roman asked directly whether the new `DataDomeSliderTask` support also
works "через cdp" (over `--cdp-endpoint`). It doesn't, and by design: the
same family-wide rule that nulls out a local `--proxy`/`G2_PROXY` the
moment `--cdp-endpoint` is set also nulls the proxy `DataDomeSliderTask`
requires, so that REST-API path never even attempts a solve in that mode
(it now returns `"warning_no_proxy"` cleanly rather than being silently
unreachable). Two docs/comments had drifted into implying otherwise —
`--solve-captcha`'s help text in `playwright_scraper.py`/
`puppeteer_scraper.py` and README's flag table both said the Scraping
Browser's own `Captcha.setAutoSolve` covers "other widget types," wording
that reads as "DataDome is excluded," which was never actually confirmed.
Corrected across `captcha_solver.py`'s module docstring, both CDP-capable
engines' `--solve-captcha` help text, `README.md` (flag table + a new
bullet under "Read this before trusting a run") and `TESTING.md` step 7:
DataDome's status under 2Captcha's CDP-side auto-solve is **unconfirmed**,
not excluded — the one live capture (2026-09-14) confirmed Turnstile,
Amazon WAF, Yandex SmartCaptcha and Lemin; DataDome (like GeeTest) simply
wasn't part of that run.

### Fixed — 2026-09-22 (same day, after initial repo below): DataDome is solvable

This repo's first version claimed g2.com's DataDome bot protection "has no
automated solve path at all" — the same bucket as PerimeterX on
skyscanner.com. That was wrong, caught by Roman (2Captcha's own team)
pointing at 2Captcha's published `DataDomeSliderTask`
(https://2captcha.com/api-docs/datadome-slider-captcha).

- `captcha_solver.py`: new `CaptchaType.DATADOME_SLIDER`, detected via the
  vendor-documented slider-iframe shape
  (`geo.captcha-delivery.com/captcha/?...&t=fe...`); builds a real
  `DataDomeSliderTask` — the one captcha type in this family with **no
  proxyless path** (a proxy and a user agent are both required, or the
  attempt is refused with a named warning rather than silently skipped or
  crashing); new `parse_datadome_cookie()` turns the cookie-shaped solution
  into a dict for a driver's native cookie API (this type's solution is a
  `Set-Cookie` string, not a hidden-field token — `build_injection_script()`
  correctly returns `None` for it).
- `proxy_pool.py`: `Proxy.to_2captcha_task_dict()` — one shared builder so
  all three engines send the DataDomeSliderTask proxy fields identically.
- All three engines: `_maybe_solve_captcha()` now threads `proxy=`/
  `user_agent=` through every one of its nine call sites (3 engines × 3
  page shapes); a solved DataDome cookie is applied via each driver's own
  native API (`context.add_cookies` / `driver.add_cookie` /
  `page.setCookie`) and the page is reloaded — a cookie sitting unused in
  the browser's cookie jar would have been a solve that silently did
  nothing, the same "documented feature doesn't actually work" failure
  mode this family has shipped before. `scrape_category()`'s pagination
  loop also gained a post-solve re-read of the current page (previously
  only `scrape_product_page()`/`scrape_pricing_page()` had one), so a solve
  now pays off on the SAME page it was triggered by, not just the next one.
- `smoke_test.py`: two new checks (81 total) — AST-level proof every call
  site passes `proxy=`/`user_agent=` and that every engine has a
  `parse_datadome_cookie()` + native-cookie-API + reload code path.
- Every doc that repeated the original wrong claim (`README.md`,
  `TESTING.md`, `landing.md`/`landing.html`, `CONTRIBUTING.md`, each
  engine's own module docstring and `--solve-captcha`/`--block-retries`
  help text) is corrected. Still true and unchanged: no engine in this
  family has ever seen g2.com actually present this challenge, so the
  slider-iframe detection pattern remains an unconfirmed, vendor-documented
  shape, not a live g2.com capture.

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
  (`--solve-captcha`). **g2.com's confirmed bot protection is DataDome**,
  solvable via 2Captcha's `DataDomeSliderTask` when `--proxy`/`--proxy-file`
  is configured (see the Fixed entry below); without a proxy a DataDome
  wall is reported as `unsupported_vendor` + `EXIT_BLOCKED` rather than
  silently retried against a solve attempt that was never actually tried.
  The default run is a plain local headless Chromium with no key, no proxy
  and no account.
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

[Unreleased]: https://github.com/2scraper/g2-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/g2-scraper/releases/tag/v0.1.0
