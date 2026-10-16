# Contributing

How to build, test and change nf-orchestrator, and the standards a change has to meet before it
merges. Security problems go through [SECURITY.md](SECURITY.md), not an issue.

## Development setup

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11 or newer (CI runs 3.11) | The service and its tests |
| Docker | any recent | kind, and the scanners and promtool, which run in containers |
| [kind](https://kind.sigs.k8s.io/) | any recent | The local Kubernetes cluster |
| kubectl | within one minor version of the cluster | Every read the orchestrator makes |
| Helm | **4.x** (CI pins 4.2.2) | Every deploy. Helm 3 does not refuse field-ownership conflicts, and the end-to-end tests would pass for the wrong reason |

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
make install                                   # pip install -e ".[dev]"
make up                                        # kind cluster "nf-orchestrator"
uvicorn orchestrator:app --port 8000
```

Every setting is an environment variable; see the [configuration reference](docs/reference/configuration.md).

## Make targets

| Target | Does | Needs |
|---|---|---|
| `make lint` | `ruff check .` | — |
| `make test` | Unit tests: no cluster, no network, no helm or kubectl on `PATH` | — |
| `make rules` | `promtool check rules` and `promtool test rules` on the alert rules | Docker |
| `make e2e` | End-to-end tests against the real cluster | `make up` |
| `make trivy` | The same two Trivy scans CI runs | Docker |
| `make scan` | The private-term scan | `BANNED_TERMS` set |
| `make up` / `make down` | Create or delete the kind cluster | Docker, kind |

## Making a change

1. Branch from `main`, and change `main` only through a pull request.
2. Keep commits single-purpose: a fix, its tests and the doc it changes can share a commit; an
   unrelated cleanup cannot. Write the subject in the imperative, in Conventional Commit style
   (`fix:`, `feat(metrics):`, `docs(operations):`, `test:`, `ci:`), and say in the body **why**,
   with the evidence.
3. A behaviour change needs a test that fails without it. For a fix found by a drill, run the new
   tests against `main`'s code and say in the pull request how many failed there.
4. Update the documentation in the same pull request (see [documentation standards](#documentation-standards)).
5. All five CI jobs must pass. Nothing merges red.

## Tests

| Suite | Where | What it guards |
|---|---|---|
| Unit | `tests/` | Classification, reconciliation and metrics logic. A guard fails any unit test that runs helm or kubectl for real, including from a worker thread |
| End-to-end | `tests/e2e/` | The real API against a real kind cluster: deploy, drift, conflict `409`, repair, a failing image, teardown. Skipped unless `NF_E2E=1` |
| Alert rules | `monitoring/tests/` | promtool replays of the series drills produced, including series that stop ([ADR-0019](docs/design/adr/0019-alert-rules-are-tested-against-drill-series.md)) |
| Documentation | `tests/test_doc_claims.py`, `tests/test_reference_docs.py`, `tests/test_alert_rule_tests.py` | Numbers in the README, the reference pages against the code, every alert's severity and runbook, every relative link |

## CI gates

CI runs five jobs on every branch push and pull request:

| Job | Checks |
|---|---|
| `lint-test` | `ruff`, the unit tests, `promtool check rules` and `promtool test rules` |
| `secret-scan` | Gitleaks over the **full history**: a key committed and later deleted is still in the pack file |
| `banned-terms` | Tracked files against a private term list supplied as a secret. Reports a location and list index, never the term, and **fails closed** when the list is absent ([ADR-0010](docs/design/adr/0010-leak-gates-must-not-leak.md)) |
| `vuln-scan` | Trivy: the chart for misconfiguration, the installed dependencies for CVEs. Fails on HIGH and CRITICAL ([ADR-0011](docs/design/adr/0011-scan-thresholds-and-a-hardened-stand-in.md)) |
| `e2e` | The end-to-end suite on two pinned Kubernetes versions, with Helm 4.2.2 |

Third-party actions are pinned to commit SHAs and scanner images to digests, because a tag can be
moved onto different code.

To run the term scan locally, one term per line, with `#` for comments:

```bash
BANNED_TERMS="$(cat your-term-list.txt)" make scan
```

## Documentation standards

The documentation is part of the product and is reviewed like code.

- **True of the code at the commit.** Anything not built is said to be not built. The reference
  pages, the alert table and the README's numbers are tested against the code, so a change that
  makes them false fails CI.
- **Lead with what the page is.** One sentence on what the page is for, then the content. Concrete
  numbers, commands and output over adjectives.
- **Runbooks contain only what was run.** Every command in a runbook was run during a drill, with
  real output shown, or the step is marked **(not drilled)**.
- **Postmortems are blameless and written from captured output.** The hypotheses are written down
  before the drill, and the postmortem says which ones held. Severity, timeline, findings (each
  *fixed* or *accepted*), the fix, verification against the same conditions, and what was not
  covered.
- **Decisions are ADRs.** A decision with alternatives worth recording gets an ADR in
  [`docs/design/adr/`](docs/design/adr/) (MADR): copy the latest, take the next number, record the
  context, the decision, the consequences and the rejected alternatives. ADRs are not rewritten
  afterwards; a later ADR refines or supersedes them.
- **Logs are history.** The [daily log](docs/daily-log/) and [build log](docs/build-log/) record
  what happened when; they are not updated to match later code.

### Content checklist

This is a self-directed learning project and a generic re-implementation built only from public
sources. Before every commit:

- [ ] No real IP addresses, MAC addresses, or internal hostnames.
- [ ] No private organisation or repository names, internal codenames, or internal URLs.
- [ ] No personal names other than the author's.
- [ ] No institution, employer, lab, or research-group name.
- [ ] No internal chart names, service names, namespaces, or config values from any non-public system.
- [ ] No credentials, tokens, keys, or secrets, even ones that look fake.
- [ ] Every O-RAN statement cites a public source (O-RAN SC or O-RAN Alliance published documents), with a link.

Credentials and the private term list are also checked mechanically by CI. The rest of the list is
checked by hand.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
