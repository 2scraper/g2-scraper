# Security

## Supported versions

Only the latest commit on `main` and the most recently tagged release get
security fixes. This project is pre-1.0 (see `CHANGELOG.md`) — older tags
are not backported.

## Reporting a vulnerability

Please report security issues privately — open a GitHub security advisory
on this repo, or email support@2captcha.com — rather than a public issue.
Include the version/commit and a minimal reproduction.

We aim to acknowledge a new report within 5 business days, confirm whether
it's in scope, and share a fix timeline once it's confirmed. If a report
turns out to be a real, exploitable issue, we'll credit the reporter in
`CHANGELOG.md` unless they'd rather stay anonymous.

## Scope

**In scope**: this repo's own code and dependencies — credential handling,
injection risks, anything that would make this tool leak a secret, execute
untrusted code, or misreport what it actually did (a `complete` status on a
run that silently dropped data would count, for example).

**Out of scope**: vulnerabilities in g2.com itself — report those to G2
directly, not here. The DataDome bot-mitigation layer g2.com runs (see
`g2_parser.py`'s module docstring) and how it does or doesn't trigger is
documented site behavior this repo reacts to, not a vulnerability in this
repo.

## What this tool does with credentials

- `TWOCAPTCHA_KEY`, `G2_PROXY`, `G2_CDP_ENDPOINT` are read only from `.env` or the environment (see `env_config.py`) — never from the command line, and never logged in full (`proxy_pool.py` masks credentials in every log line, keeping only host:port, which is the useful part of a log and not the secret).
- This project's own code doesn't phone home. The only network calls it makes are to `g2.com` and, when configured, `api.2captcha.com` / `cb.2captcha.com` / `scraper.2captcha.com`. **One caveat on the Selenium engine**: Selenium's own bundled Selenium Manager sends anonymous usage stats to `plausible.io` by default whenever it launches a browser, which `selenium_scraper.py` disables on your behalf (`SE_AVOID_STATS=true`, set as a default rather than forced, so it never overrides a value you set yourself) so this project's "nothing phones home" claim actually holds. Selenium Manager can still make a *separate* network call to resolve a matching `chromedriver` version if one isn't already reachable on `PATH` / `SELENIUM_CHROME_BIN` — set that variable to a Chrome/Chromium binary you already have to avoid it entirely (see `selenium_scraper.py`'s module docstring).
- This is a scraper, not an account tool: it never logs in, never touches an authenticated g2.com session, and never writes anything back to g2.com — no reviews, no forms, no lead-gen submissions, ever.
- **robots.txt compliance is deliberately stricter than the minimum.** g2.com's `robots.txt` carries a second group for AI crawlers (`GPTBot`, `ClaudeBot`, `Google-Extended`, `Applebot-Extended`, `Meta-ExternalAgent`, `Amazonbot`, `CCBot`) that adds one rule the general group lacks: `Disallow: /products/*/reviews/*`. Since this scraper is built and run by Claude, and g2.com's own `https://www.g2.com/ai-instructions` page states that robots.txt is authoritative for AI-assistant access, `g2_parser.is_disallowed_path()` honours that stricter set by default — see its docstring. It costs nothing: deeper review pagination was never in scope. One honest caveat: the general `User-agent: *` group's own rules are currently an empty list in `g2_parser.GENERAL_DISALLOWED_PATTERNS`, because the environment that file was written in could not reach g2.com to transcribe them, and guessing robots rules would be worse than leaving the gap visible. `g2_parser.is_known_scrape_target()` — an allowlist of the four URL shapes this scraper actually parses — is the conservative guard in the meantime.
