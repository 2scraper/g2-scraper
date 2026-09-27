# Playwright engine only (the primary one, local-first — see README) —
# Selenium/pyppeteer need their own images per requirements-*.txt (the
# three engines' pins are mutually unsatisfiable in one environment; see
# requirements-puppeteer.txt).
#
# The COPY list below is the whole point of building this image in CI
# (CLAUDE.md §11 — nothing else builds it, which is how earlier family
# members shipped a broken one). It was written before the engine scripts
# and smoke_test.py existed, then reconciled against them once they
# landed: this repo's smoke_test.py builds almost every fixture inline —
# the one exception is tests/fixtures/g2_datadome_interstitial.html
# (added 2026-09-22, a real scrubbed g2.com capture — see
# captcha_solver.py's module docstring), which is why that one file now
# gets its own COPY line below rather than a whole tests/ directory. It
# never reads sample_output.*, so that stays documentation rather than a
# build input and stays out of the published image entirely.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements-playwright.txt \
    && playwright install --with-deps chromium

# smoke_test.py imports puppeteer_scraper.py and selenium_scraper.py at
# module level UNCONDITIONALLY (only each engine's own driver import is
# guarded, not the file's presence) — leaving either module out here makes
# `RUN python3 smoke_test.py` crash the build with a plain
# ModuleNotFoundError, before ever reaching the guarded-import checks it
# exists to run. That is exactly how an earlier family member shipped a
# broken image (CLAUDE.md §11). Copying the .py file costs nothing:
# selenium/pyppeteer aren't installed in this image, so their own
# try/except ImportError guard is what fires, the same as smoke_test.py's
# no-engine-installed path in CI.
COPY env_config.py proxy_pool.py output_writer.py captcha_solver.py \
     fingerprint_client.py scraper_api_client.py diff_runs.py \
     g2_parser.py playwright_scraper.py puppeteer_scraper.py \
     selenium_scraper.py smoke_test.py ./
# smoke_test.py reads this one straight off disk (the ENV_KEYS <->
# .env.example sync check, CLAUDE.md §17) — leaving it out crashes the
# build the same way a missing .py module above would.
COPY .env.example ./
# The two real (scrubbed) HTML captures smoke_test.py reads off disk
# rather than building inline — leaving either out crashes the build with
# a FileNotFoundError the moment RUN python3 smoke_test.py reaches its
# check, same failure class as the two omissions above.
COPY tests/fixtures/g2_datadome_interstitial.html tests/fixtures/g2_datadome_captcha_banned_ip.html tests/fixtures/

RUN python3 smoke_test.py

# The test suite is needed to VERIFY the build above, not to run the
# scraper — the image should carry no test suite and no stray .env (a .env
# was never COPYed here in the first place; this is the "no test suite"
# half of that same rule). Strip it from the final layer rather than
# leaving it in a published image.
RUN rm -rf smoke_test.py tests __pycache__

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
