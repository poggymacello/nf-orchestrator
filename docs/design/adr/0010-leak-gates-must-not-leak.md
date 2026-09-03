# ADR-0010: The leak gates must not themselves leak

- **Status:** Accepted
- **Date:** 2026-09-03
- **Delivers:** the two CI gates
  [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) has listed as `(planned, M5)` since M1.

## Context

This project is a generic re-implementation built only from public sources. Its
[leak-scrub checklist](../../../CONTRIBUTING.md) — no employer, institution, internal hostname,
codename or credential — has been a manual step before every commit for sixteen days. A checklist a
human runs from memory is the weakest control in the repository.

Two different problems hide under "leak scanning", and they need different tools:

- **Credentials** have recognisable shapes. A scanner with a rule set finds them without being told
  what to look for, and needs to search *history*, because a key committed in July and deleted in
  August is still in the pack file.
- **Private terms** have no shape at all. `acme-corp` looks exactly like `stand-in-nf` to any
  regular expression. The only way to find them is to be given the list — and the list is itself the
  most sensitive thing involved.

## Decision

**Gitleaks scans the full history.** A `secret-scan` job runs the pinned
`zricethezav/gitleaks` image with `--exit-code 1` over a `fetch-depth: 0` checkout. Pinned by image
digest rather than a tag, matching the SHA-pinning decision made for actions in M5's e2e work.

**The private-term list never enters the repository.** `scripts/scan_banned_terms.py` reads
`BANNED_TERMS` from the environment; in CI that comes from a repository secret. Committing the list
of things that must never be committed would defeat the exercise, and would put every private term
into the public history permanently.

**A match never prints the term — including through the path.** This is the part that needed
thinking about. Reporting `docs/notes.md:12: contains "acme-corp"` would publish the private term
into a public CI log, which is the leak the gate exists to prevent. GitHub masks a secret's *value*
in logs, but the secret here is the whole newline-separated list; an individual term inside it is
**not** masked. So a match is reported as a location plus an index into the list:

```
banned-terms: 107 match(es) found
  README.md:22: matches term #1 (redacted)
  docs/build-log/m3-***.md:1: matches term #1 (redacted)
```

The `***` in that path is not decoration. Running the scanner against a term that appears in a
*filename* leaked it 49 times through the location alone, before paths were redacted too. The
operator holds the list and can map `#1` back to a term; the log cannot.

**The gate fails closed.** An unset or empty `BANNED_TERMS` exits `2` with an explanation, not `0`.
An unconfigured scan is not a passing scan, and a security gate that silently does nothing when
misconfigured is worse than no gate, because it reports success.

**Scope: tracked files in the working tree, not history.** Unlike Gitleaks, the term scanner reads
`git ls-files`. A private term in a deleted file is not caught. That is a real gap, accepted because
the alternative — replaying every term against every revision — is slow, and because the manual
scrub has covered every commit so far. Recorded here rather than left implicit.

## Verified on 2026-09-03

Gitleaks over the repository as it stands:

```
INF 52 commits scanned.
INF no leaks found
```

Then, because a scanner nobody has watched fail is not a verified scanner, against files that do
contain secrets:

| Fixture | Result |
|---|---|
| `ghp_`-shaped personal access token | detected, exit 1 |
| `-----BEGIN RSA PRIVATE KEY-----` block | detected, exit 1 |
| AWS key id and secret | detected, exit 1 |
| AWS's *published example* key (`AKIAIOSFODNN7EXAMPLE`) | **not detected**, exit 0 |

The last row is the useful one. The first negative test used the canonical example credentials from
AWS's own documentation, which Gitleaks allowlists deliberately — so the test passed when it should
have failed, and a gate verified only with example values would have looked correct while catching
nothing.

The term scanner, in its three states:

```
$ BANNED_TERMS="" python scripts/scan_banned_terms.py
BANNED_TERMS is empty or unset. This gate fails closed: ...          exit 2

$ BANNED_TERMS="acme-corp\nnorthwind" python scripts/scan_banned_terms.py
banned-terms: clean against 2 terms                                  exit 0

$ BANNED_TERMS="reconciler" python scripts/scan_banned_terms.py
banned-terms: 107 match(es) found                                    exit 1
  lines of output containing the term itself: 0    (49 before paths were redacted)
```

## Consequences

- **Good:** the leak-scrub checklist stops depending on memory for its two mechanical items.
- **Good:** history is covered for credentials, which is where a deleted-but-committed key hides.
- **Good:** the term list stays out of the repository and out of the logs, so the gate cannot become
  the leak.
- **Cost:** the `banned-terms` job **fails until `BANNED_TERMS` is set** on the repository. That is
  the fail-closed choice working as intended, but it means CI is red until one command is run:
  `gh secret set BANNED_TERMS < your-term-list.txt`.
- **Cost:** a pull request from a fork cannot read the secret, so this job would fail there. This is
  a single-author project with no external contributors; if that changes, the job needs a
  `pull_request_target` split or an explicit exemption, and neither is worth building now.
- **Cost:** redacting the path makes some reports harder to read — `docs/build-log/m3-***.md` is
  less obvious than the real name. Legibility loses to leakage.
- **Cost:** substring matching produces false positives on ordinary words that happen to contain a
  term. The list's author controls that by choosing terms carefully.

## Alternatives considered

- **Commit the term list.** Rejected outright: it publishes exactly what it protects.
- **Print the matched term to make reports readable.** Rejected: public CI logs. The index-plus-
  location report is less convenient and does not leak.
- **Hash the paths instead of redacting them.** Rejected: an opaque digest tells the operator
  nothing, and the redacted path keeps most of its diagnostic value.
- **Let an unconfigured scan pass with a warning.** Rejected: a gate that reports success while
  doing nothing is the failure mode that makes people trust an unprotected repository.
- **`gitleaks/gitleaks-action`.** Rejected in favour of running the pinned image directly: one less
  third-party action to trust, no licence-key question, and the same command runs locally.
- **Scan the working tree only, for secrets too.** Rejected: the entire value of secret scanning is
  finding what was committed and then deleted.
