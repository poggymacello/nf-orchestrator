# M5 — CI hardening

**Goal:** stop the project depending on one laptop and one person's memory. Run the M4 failure
drills somewhere else, and turn the two mechanical items of the leak-scrub checklist into gates that
fail the build.

Built across days 16 to 18 (2026-09-02 to 2026-09-04). Decisions in
[ADR-0010](../design/adr/0010-leak-gates-must-not-leak.md) and
[ADR-0011](../design/adr/0011-scan-thresholds-and-a-hardened-stand-in.md).

## What exists now

CI is five jobs, on every branch push as well as pull requests:

| Job | Asks |
|---|---|
| `lint-test` | Does it lint and do the unit tests pass? No cluster. |
| `secret-scan` | Is there a credential anywhere in the history? |
| `banned-terms` | Is there a private term in the tracked files? |
| `vuln-scan` | Does the chart have insecure defaults, and do the dependencies have known CVEs? |
| `e2e` | Does the lifecycle actually work, on a real cluster, on two Kubernetes versions? |

Third-party actions are pinned to commit SHAs and scanner images to digests. A tag can be moved
onto different code; a commit cannot.

## The end-to-end job

`tests/e2e/test_lifecycle_e2e.py` drives the real API against a real kind cluster. It covers the
ground the M4 drills covered by hand: a deploy reaching `INSTANTIATED` with the replica count the
intent asked for, drift applied with `kubectl scale` showing as a shortfall, a conflicting resubmit
refused with `409`, the failed operation not being reported as a failed network function, repair
taking the field back, a bad image tag reaching `FAILED`, idempotent teardown, and `/readyz`
answering.

Skipped unless `NF_E2E=1`, so `pytest` and `lint-test` stay a no-cluster run.

Helm is pinned to 4.2.2. That is not housekeeping: the server-side apply conflict these tests assert
does not exist in Helm 3, which merges client-side and would silently win. A job on Helm 3 would
pass for the wrong reason.

```
lint-test  17s
e2e        88s     6 passed in 22.15s inside the job
```

## The three security gates

**Gitleaks** runs over a `fetch-depth: 0` checkout — history, not the diff, because a key committed
in July and deleted in August is still in the pack file. Clean across all 52 commits at the time it
was added.

**The banned-terms scanner** reads its list from a repository secret, never from this repository,
and reports a match as a location plus a list index — never the term, and never a path containing
the term. A public CI log is exactly where a private term must not appear. It fails closed: unset
means fail.

**Trivy** runs twice. `config` reads the chart; `fs` reads the dependency set CI actually installed.
Both fail on HIGH and CRITICAL and print everything else.

## What broke

**Three of the four gates were wrong the first time, and all three passed their happy path.**

Gitleaks reported `no leaks found` against a file containing an AWS key pair — because the fixture
was `AKIAIOSFODNN7EXAMPLE`, AWS's own published example, which Gitleaks allowlists deliberately.
Retested with a `ghp_` token, an RSA private key block and a plausible pair: all three caught.

The banned-terms scanner redacted every line it printed and leaked the term 49 times anyway, through
the *paths*: `docs/build-log/m3-reconciler.md:1: matches term #1 (redacted)`. A file named after a
private term publishes it through the location alone.

Trivy's dependency scan reported a `-` in every column. `pyproject.toml` lists unpinned dependencies
and there is no lockfile, so there was no file it could read. It would have reported clean forever.

The pattern is the same each time: a scanner that finds nothing and a scanner that is switched off
produce identical output, so the passing test proves almost nothing. Every one of these was found by
deliberately trying to make the gate fail.

**A test that named an absent term made it present.** A test asserted the scan comes back clean
against `"a-term-that-appears-nowhere-in-this-repository"`, and passed — until the test file was
committed, at which point `git ls-files` included it and the term was in the repository, inside the
test claiming it was not.

**Hardening the chart moved a finding rather than removing it.** Clearing 2 HIGH and 3 MEDIUM by
switching to `nginx-unprivileged` introduced a new MEDIUM, because the unprivileged image comes from
a different Docker Hub path and Trivy checks images against trusted registries.

## What CI covers, and what it does not

Covered: the drills whose evidence is a state transition.

Not covered:

- **Drill 2, control plane unreachable.** Stopping the kind node mid-job would make a real failure
  indistinguishable from a flaky runner. Those paths are unit tested against the exact strings the
  drill produced.
- ~~**The `banned-terms` job until its secret is set.** It fails closed, so it is red until
  `gh secret set BANNED_TERMS` is run. That is the design working, and it means the CI badge shows
  failure in the meantime.~~ The secret was set on 2026-09-14; the job has passed since.
- **Development's own Kubernetes version.** The matrix runs v1.34.0 and v1.35.0; local development
  is on v1.36.1. Narrower than before, not closed.
- **Reproducible dependency scanning.** Dependencies are unpinned, so the scan describes today's
  resolution and a run can start failing without this repository changing.

## Checks

`ruff check .` clean, 85 unit tests and 6 end-to-end tests passing, `trivy config` and `trivy fs`
clean at HIGH and CRITICAL, Gitleaks clean over the full history.
