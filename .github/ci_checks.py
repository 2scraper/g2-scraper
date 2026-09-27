#!/usr/bin/env python3
"""Repository-wide credential guard used by both CI and smoke_test.py.

Only paths and rule names are printed.  A suspicious value is never echoed
back into a CI log, because an exception/report is a credential leak too.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ALLOWLIST = {
    ".env.example",
    "smoke_test.py",  # deliberate credential-shaped masking fixtures
    ".github/ci_checks.py",  # contains the detector patterns themselves
}

TOKEN_RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
}
URL_CREDENTIALS = re.compile(r"\b(?:https?|wss?)://([^\s/@:]+):([^\s/@]+)@", re.I)
SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|TWOCAPTCHA_KEY|G2_PROXY|G2_CDP_ENDPOINT|CLAUDE_CODE_OAUTH_TOKEN)"
    r"\s*(?:=|:)\s*['\"]([^'\"]{8,})['\"]",
    re.I,
)
PLACEHOLDER_WORDS = (
    "user", "username", "login", "pass", "password", "example", "fake",
    "secret", "redacted", "changeme", "proxy-user", "proxy-pass", "your",
    "test",
)
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _words(token: str) -> set[str]:
    """Split into whole alphanumeric-run words on any other character
    (`-`, `_`, `:`, ...), lowercased. `"user-zone-x"` -> `{"user", "zone",
    "x"}`; `"myrealuser1234"` has no separator, so it stays one word and
    is never mistaken for the bare word `"user"`."""
    return {w for w in _WORD_SPLIT_RE.split(token.lower()) if w}


# PLACEHOLDER_WORDS itself may contain multi-word phrases (`"proxy-user"`);
# expand it once into the set of whole words actually being matched against.
_PLACEHOLDER_WORD_SET = {w for phrase in PLACEHOLDER_WORDS for w in _words(phrase)}


# Directories that are never this repo's own source, however they got onto
# disk — a virtualenv (any of them: CI's engine-smoke job makes one PER
# engine, .venv-playwright/.venv-selenium/.venv-puppeteer, not just
# `.venv`) or a node_modules tree carries a huge amount of third-party
# test/fixture/license text that legitimately contains credential-shaped
# strings (an SPDX license list, a URL-parsing test's own sample
# credentialed URLs). `.gitignore` is supposed to keep these out of
# `git ls-files --others --exclude-standard` already, but relying on that
# alone is exactly the "two independent checks for the same thing WILL
# drift apart" trap CLAUDE.md §17 warns about for placeholder detection —
# a renamed/added venv directory silently reopens this the same way
# `.venv-<engine>` did the first time. Filtering by name here too means a
# stale or incomplete .gitignore degrades to redundant, not broken.
_NOT_SOURCE_DIR = re.compile(r"(^|/)(\.venv[^/]*|venv|env|node_modules|__pycache__|\.git)(/|$)")


def repository_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, check=True, capture_output=True,
    )
    paths = [p.decode("utf-8", "surrogateescape") for p in result.stdout.split(b"\0") if p]
    return [p for p in paths if not _NOT_SOURCE_DIR.search(p)]


def is_placeholder(user: str, password: str) -> bool:
    """True when `user`/`password` look like a documentation placeholder
    rather than a real credential.

    This must be a whole-word check, not substring containment: an
    earlier version checked `word in f"{user}:{password}".lower()`, which
    read ANY real credential containing a common fragment as a
    placeholder and silently skipped it — `myrealuser1234:myrealpass5678`
    matched on "user" and "pass" and was never scanned. Splitting on
    non-alphanumeric characters first (`_words()`) fixes that while still
    matching this repo's own documented placeholder examples, whose words
    are the whole token or are `-`/`_`-delimited: `login:password`
    (README's `G2_PROXY` row, proxy_pool.py), `user:pass`
    (selenium_scraper.py's docstring), and `user-zone-x:s3cr3tpassword`
    (TESTING.md #10's redaction-test example) all still match on their
    `login`/`password`/`user`/`pass` word.
    """
    combined = f"{user}{password}"
    if "{" in combined or "}" in combined:
        return True
    return bool((_words(user) | _words(password)) & _PLACEHOLDER_WORD_SET)


def scan() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for relative in repository_files():
        if relative in FIXTURE_ALLOWLIST:
            continue
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule, pattern in TOKEN_RULES.items():
                if pattern.search(line):
                    findings.append((relative, line_no, rule))
            for match in URL_CREDENTIALS.finditer(line):
                if not is_placeholder(match.group(1), match.group(2)):
                    findings.append((relative, line_no, "credentialed_url"))
            for match in SECRET_ASSIGNMENT.finditer(line):
                value = match.group(1)
                if not is_placeholder(value, value):
                    findings.append((relative, line_no, "secret_assignment"))
    return findings


def main() -> int:
    findings = scan()
    if findings:
        for path, line, rule in findings:
            print(f"{path}:{line}: possible committed credential ({rule})")
        print("credential scan failed", file=sys.stderr)
        return 1
    print("credential scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
