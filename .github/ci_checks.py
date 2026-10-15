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
# Files exempt from SPECIFIC rules only, never wholesale. smoke_test.py
# carries deliberate credential-shaped masking fixtures (user:pass URLs,
# key= assignments) but must still be scanned for everything else, so a
# pasted real key in it is caught like anywhere else.
RULE_EXEMPTIONS = {
    ".env.example": {"credentialed_url"},
    "smoke_test.py": {"credentialed_url", "secret_assignment", "env_assignment"},
    ".github/ci_checks.py": {"credentialed_url", "secret_assignment", "env_assignment"},
}

TOKEN_RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
}
URL_CREDENTIALS = re.compile(r"\b(?:https?|wss?)://([^\s/@:]+):([^\s/@]+)@", re.I)
_SECRET_NAMES = r"(?:api[_-]?key|TWOCAPTCHA_KEY|G2_PROXY|G2_CDP_ENDPOINT|CLAUDE_CODE_OAUTH_TOKEN)"
# A quoted value after a secret-shaped name. Every quote may be preceded by a
# backslash: inside a JSON-string fixture the file holds `\"apiKey\": \"...\"`,
# never `"apiKey": "..."`, and a bare-quote pattern matched nothing there.
SECRET_ASSIGNMENT = re.compile(
    r"\b" + _SECRET_NAMES + r"\\?['\"]?\s*(?:=|:)\s*\\?['\"]([^'\"\\]{8,})\\?['\"]",
    re.I,
)
# An UNQUOTED `.env`-style line (`TWOCAPTCHA_KEY=abc...`), which is exactly
# what a `.env.bak` holds. Anchored to the start of a line and to upper-case
# names so ordinary code (`api_key = args.key`) does not match.
ENV_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?:TWOCAPTCHA_KEY|G2_PROXY|G2_CDP_ENDPOINT|CLAUDE_CODE_OAUTH_TOKEN|[A-Z0-9_]*API_KEY)"
    r"\s*=\s*([^\s#'\"]{8,})"
)
# A 2Captcha key is 32 lower-case hex characters. Nothing in this repo needs
# a bare 32-hex string, so there is no allowlist for the shape: a site id of
# that shape would be removed from the fixture rather than exempted.
_IDENTIFIER_VALUE = re.compile(r"^[a-z_]+$")
TWOCAPTCHA_KEY_SHAPE = re.compile(r"(?<![0-9A-Fa-f])[0-9a-fA-F]{32}(?![0-9A-Fa-f])")
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


# Directories that are never this repo's own source: a node_modules tree, a
# cache, and ANY virtualenv. A virtualenv is recognised by the file that
# defines one, `pyvenv.cfg`, never by its name alone: the README tells people
# to make one venv per engine, and a venv called anything not on a name list
# was walked into, where pip's vendored code is full of credential-shaped
# strings. The name list stays as a cheap first filter.
_NOT_SOURCE_DIR = re.compile(r"(^|/)(\.venv[^/]*|venv|env|node_modules|__pycache__|\.git)(/|$)")


def _inside_virtualenv(relative: str) -> bool:
    parts = Path(relative).parts[:-1]
    for i in range(1, len(parts) + 1):
        if (ROOT.joinpath(*parts[:i]) / "pyvenv.cfg").is_file():
            return True
    return False


def _git_paths(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *args], cwd=ROOT, check=True, capture_output=True,
    )
    return [p.decode("utf-8", "surrogateescape") for p in result.stdout.split(b"\0") if p]


def repository_files() -> list[str]:
    """Tracked files, untracked non-ignored files, and IGNORED `.env.*`
    copies (`.env.bak`, `.env.local`, ...). The last group is ignored on
    purpose and is still exactly where a live key sits one rename away from
    a commit, so it is read too. `.env` itself is the user's working
    configuration and is not scanned. All paths are relative to the
    repository, so a clone under /tmp, build/ or out/ scans the same set."""
    paths = _git_paths("--cached", "--others", "--exclude-standard")
    backups = [
        p for p in _git_paths("--others", "--ignored", "--exclude-standard")
        if Path(p).name.startswith(".env.") and Path(p).name != ".env.example"
    ]
    seen: set[str] = set()
    out: list[str] = []
    for p in paths + backups:
        if p in seen or _NOT_SOURCE_DIR.search(p) or _inside_virtualenv(p):
            continue
        seen.add(p)
        out.append(p)
    return out


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


def scan() -> tuple[list[tuple[str, int, str]], int]:
    """Returns (findings, number of files actually read)."""
    findings: list[tuple[str, int, str]] = []
    read = 0
    for relative in repository_files():
        exempt = RULE_EXEMPTIONS.get(relative, set())
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        read += 1
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule, pattern in TOKEN_RULES.items():
                if rule not in exempt and pattern.search(line):
                    findings.append((relative, line_no, rule))
            if "credentialed_url" not in exempt:
                for match in URL_CREDENTIALS.finditer(line):
                    if not is_placeholder(match.group(1), match.group(2)):
                        findings.append((relative, line_no, "credentialed_url"))
            if "secret_assignment" not in exempt:
                for match in SECRET_ASSIGNMENT.finditer(line):
                    value = match.group(1)
                    # A bare lower-case identifier with no digit is a NAME
                    # (env_config's `"TWOCAPTCHA_KEY": "twocaptcha_key"`
                    # maps a variable to an argparse destination), not a key.
                    if _IDENTIFIER_VALUE.match(value):
                        continue
                    if not is_placeholder(value, value):
                        findings.append((relative, line_no, "secret_assignment"))
            if "env_assignment" not in exempt:
                for match in ENV_ASSIGNMENT.finditer(line):
                    value = match.group(1)
                    if not is_placeholder(value, value):
                        findings.append((relative, line_no, "env_assignment"))
            if "twocaptcha_key_shape" not in exempt and TWOCAPTCHA_KEY_SHAPE.search(line):
                findings.append((relative, line_no, "twocaptcha_key_shape"))
    return findings, read


def main() -> int:
    findings, read = scan()
    if read == 0:
        # A scan that read nothing would otherwise print "passed".
        print("credential scan failed: no files were scanned", file=sys.stderr)
        return 1
    if findings:
        for path, line, rule in findings:
            print(f"{path}:{line}: possible committed credential ({rule})")
        print("credential scan failed", file=sys.stderr)
        return 1
    print(f"credential scan passed ({read} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
