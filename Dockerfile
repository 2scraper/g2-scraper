# Playwright engine only (the primary one, local-first — see README) —
# Selenium/pyppeteer need their own images per requirements-*.txt (the
# three engines' pins are mutually unsatisfiable in one environment; see
# requirements-puppeteer.txt).
#
# The COPY list below is the whole point of building this image in CI
# (CLAUDE.md §11 — nothing else builds it, which is how earlier family
# members shipped a broken one). It was written before the engine scripts
# and smoke_test.py existed, then reconciled against them once they
# landed: this repo's smoke_test.py builds every fixture inline, so there
# is no `tests/` directory to copy, and it never reads sample_output.*, so
# those are documentation rather than a build input and stay out of the
# published image entirely.
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
# build the same way a missing .py module above would. It is the ONLY
# non-.py build input: every HTML/JSON fixture in this repo's smoke_test.py
# is built inline, so there is no fixtures directory to copy.
COPY .env.example ./

RUN python3 smoke_test.py

# The test suite is needed to VERIFY the build above, not to run the
# scraper — the image should carry no test suite and no stray .env (a .env
# was never COPYed here in the first place; this is the "no test suite"
# half of that same rule). Strip it from the final layer rather than
# leaving it in a published image.
RUN rm -rf smoke_test.py __pycache__

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
