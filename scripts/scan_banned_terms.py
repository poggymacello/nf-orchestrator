#!/usr/bin/env python3
"""Fail the build if a private term appears anywhere in the tracked files.

CONTRIBUTING has promised this since M1. The point is the leak-scrub checklist that
is currently done by hand: no employer, institution, internal hostname or codename
should ever reach this repository.

Two rules shape the design.

**The term list lives outside the repository.** It is supplied through the
`BANNED_TERMS` environment variable, which in CI comes from a GitHub Actions secret.
Committing the list of things that must never be committed would defeat the exercise.

**A match never prints the term.** Reporting `docs/foo.md:12: contains "acme-corp"`
would publish the private term into a public CI log — the exact leak the gate exists
to prevent. GitHub masks a secret's *value* in logs, but the secret here is the whole
newline-separated list, so an individual term inside it is not masked. Matches are
reported by location and by an index into the list, and the operator looks up which
term that was.

That includes the path. A file *named* after a private term would otherwise leak it
through the location alone, which is how this script's own first test run leaked 49
times.

Exit codes: 0 clean, 1 matches found, 2 not configured.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable

NOT_CONFIGURED = 2


def parse_terms(raw: str) -> list[str]:
    """Newline- or comma-separated, `#` comments and blanks dropped, lowercased."""
    terms: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        term = chunk.strip().lower()
        if term and not term.startswith("#"):
            terms.append(term)
    return terms


def scan_text(path: str, text: str, terms: list[str]) -> list[tuple[str, int, int]]:
    """Return (path, line number, term index) for every match. Never the term itself."""
    hits: list[tuple[str, int, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for index, term in enumerate(terms):
            if term in lowered:
                hits.append((path, number, index))
    return hits


def redact(text: str, terms: list[str]) -> str:
    """Blank out any term occurring in text, case-insensitively.

    Applied to the path before printing: `docs/acme-corp-notes.md` would otherwise
    publish the term through the location of the match.
    """
    lowered = text.lower()
    for term in terms:
        start = 0
        while (found := lowered.find(term, start)) != -1:
            text = text[:found] + "***" + text[found + len(term) :]
            lowered = text.lower()
            start = found + 3
    return text


def tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in output.splitlines() if line]


def read_text(path: str) -> str | None:
    """Text content, or None if the file is binary or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (UnicodeDecodeError, OSError):
        return None


def scan(paths: Iterable[str], terms: list[str]) -> list[tuple[str, int, int]]:
    hits: list[tuple[str, int, int]] = []
    for path in paths:
        text = read_text(path)
        if text is not None:
            hits.extend(scan_text(path, text, terms))
    return hits


def main() -> int:
    raw = os.environ.get("BANNED_TERMS", "")
    terms = parse_terms(raw)
    if not terms:
        print(
            "BANNED_TERMS is empty or unset. This gate fails closed: an unconfigured "
            "scan is not a passing scan.\n"
            "Set it locally to run this by hand, or as a repository secret for CI:\n"
            "  gh secret set BANNED_TERMS < your-term-list.txt",
            file=sys.stderr,
        )
        return NOT_CONFIGURED

    hits = scan(tracked_files(), terms)
    if not hits:
        print(f"banned-terms: clean against {len(terms)} terms")
        return 0

    print(f"banned-terms: {len(hits)} match(es) found", file=sys.stderr)
    for path, number, index in hits:
        # Location and index only, with the path redacted too: the term stays out of
        # the log on purpose, and a filename can carry it just as easily as a line.
        print(
            f"  {redact(path, terms)}:{number}: matches term #{index + 1} (redacted)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
