# Changelog

Notable changes, newest first, following the spirit of [Keep a Changelog](https://keepachangelog.com/).
The project has no tagged releases; `main` is the only supported version. Entries are grouped by the
pull request that merged them, and the full reasoning for each is in the linked postmortem or ADR.

## Unreleased

### Added
- A runtime check of helm's major version, before every write, in `/readyz` and in every scrape. A
  helm other than 4 is `HostMisconfigured`, HTTP `500` naming the version found
  ([drill 16](docs/operations/postmortems/2026-10-16-drill-16-the-wrong-helm.md),
  [ADR-0023](docs/design/adr/0023-the-tools-are-checked-not-assumed.md)).

### Fixed
- With Helm 3 installed, every deploy was answered `502` ("the cluster refused") while `/readyz`
  reported ready. Helm rejecting the orchestrator's own arguments is now classified as this host's
  problem.

## 2026-10-16 — [#31](https://github.com/poggymacello/nf-orchestrator/pull/31)

### Added
- Reference documentation, checked against the code by `tests/test_reference_docs.py`:
  [configuration](docs/reference/configuration.md), [API and error model](docs/reference/api.md),
  [metrics](docs/reference/metrics.md), [alerts](docs/reference/alerts.md).
- `SECURITY.md`, this changelog, and a link check over every relative link in the repository.
- `HostMisconfigured`: a missing or non-executable helm or kubectl is HTTP `500` naming the tool,
  with the `nf_orchestrator_misconfigured` gauge and the `OrchestratorMisconfigured` page
  ([drill 15](docs/operations/postmortems/2026-10-16-drill-15-the-orchestrator-cannot-spawn-its-tools.md),
  [ADR-0022](docs/design/adr/0022-a-failed-spawn-is-classified-by-its-cause.md)).
- A `runbook_url` annotation on every alert.

### Changed
- `/readyz` requires helm and kubectl on `PATH` before probing the API server.
- `fork` failing with `EAGAIN` or `ENOMEM` is `HostExhausted` (`503` with `Retry-After`).
- The drill container image is defined in `docs/operations/manifests/drill.Dockerfile`, with
  checksum-verified tools.

### Fixed
- A failed spawn was reported as the cluster being unreachable, with a bare `500`, and `/readyz`
  answered `200` with helm missing.
- Alert severities were inconsistent (`warn` and `warning`); all are now `page` or `warning`.

## 2026-10-06 — [#30](https://github.com/poggymacello/nf-orchestrator/pull/30)
### Fixed
- A host that cannot start threads is `HostExhausted` (`503`, `Retry-After`) instead of a cluster
  refusal (`502`) or an outage page. The Go runtime's stack no longer reaches callers
  ([drill 14](docs/operations/postmortems/2026-10-06-drill-14-a-host-that-cannot-start-threads.md),
  [ADR-0021](docs/design/adr/0021-a-host-out-of-capacity-is-not-a-slow-host.md)).
### Added
- `nf_orchestrator_exhausted`; `OrchestratorHostOverloaded` also fires on exhaustion.

## 2026-09-27 — [#29](https://github.com/poggymacello/nf-orchestrator/pull/29)
### Fixed
- With no scrape keeping a probe warm, status calls blamed the orchestrator's host for a real
  outage, and `/readyz` reported a healthy cluster unreachable. A probe that never started is asked
  again, and `/readyz` is the probe
  ([drill 13](docs/operations/postmortems/2026-09-27-drill-13-with-nothing-scraping.md),
  [ADR-0020](docs/design/adr/0020-an-inconclusive-probe-is-asked-again.md)).

## 2026-09-26 — [#28](https://github.com/poggymacello/nf-orchestrator/pull/28)
### Fixed
- `ClusterThrottlingOrchestrator` could page during an outage, because `avg_over_time` averages only
  surviving samples; the host's measurement vanished during an outage
  ([drill 12](docs/operations/postmortems/2026-09-26-drill-12-a-starved-host-and-a-frozen-cluster.md)).
### Added
- promtool unit tests for the alert rules, run in CI
  ([ADR-0019](docs/design/adr/0019-alert-rules-are-tested-against-drill-series.md)).

## 2026-09-24 — [#27](https://github.com/poggymacello/nf-orchestrator/pull/27)
### Fixed
- A CPU-starved orchestrator host paged as a throttling cluster. The probe now separates the
  cluster's round trip from this host's time; `HostOverloaded` and `OrchestratorHostOverloaded` added
  ([drill 11](docs/operations/postmortems/2026-09-24-drill-11-the-orchestrator-host-is-starved.md),
  [ADR-0018](docs/design/adr/0018-the-probe-measures-two-ends.md)).

## 2026-09-23 — [#26](https://github.com/poggymacello/nf-orchestrator/pull/26)
### Fixed
- A queueing API server was reported as an orchestrator budget problem, or as unreachable. The
  probe's duration is now evidence, and reachability takes two witnesses
  ([drill 10](docs/operations/postmortems/2026-09-23-drill-10-the-api-server-queues-instead-of-refusing.md),
  [ADR-0017](docs/design/adr/0017-reachability-needs-more-than-one-witness.md)).

## 2026-09-17 — [#25](https://github.com/poggymacello/nf-orchestrator/pull/25)
### Fixed
- API Priority and Fairness rejections were reported as an outage. `ClusterBusy` is `503` with
  `Retry-After`, with its own gauge and alert
  ([drill 9](docs/operations/postmortems/2026-09-17-drill-9-the-api-server-sheds-load.md),
  [ADR-0016](docs/design/adr/0016-busy-is-not-unreachable.md)).

## 2026-09-16 — [#24](https://github.com/poggymacello/nf-orchestrator/pull/24)
### Fixed
- Against a slow API server, concurrent reads left every release half-read. Scrape work is admitted
  in waves ([drill 8](docs/operations/postmortems/2026-09-16-drill-8-a-slow-api-server.md),
  [ADR-0015](docs/design/adr/0015-admit-scrape-work-in-waves.md)).

## 2026-09-15 — [#23](https://github.com/poggymacello/nf-orchestrator/pull/23)
### Changed
- The scrape makes `2 + 2N` subprocess calls instead of `1 + 4N`, and `helm list` is paged past 256
  releases.

## 2026-09-14 — [#22](https://github.com/poggymacello/nf-orchestrator/pull/22)
### Fixed
- Twenty healthy releases pushed a scrape past Prometheus's timeout. The scrape has one budget, and
  releases it could not read are named (`nf_release_reported`, `NFReleaseUnreported`)
  ([drill 7](docs/operations/postmortems/2026-09-14-drill-7-the-scrape-outgrows-its-budget.md),
  [ADR-0014](docs/design/adr/0014-the-scrape-has-one-budget.md)).

## 2026-09-13 — [#20](https://github.com/poggymacello/nf-orchestrator/pull/20), [#21](https://github.com/poggymacello/nf-orchestrator/pull/21), [#19](https://github.com/poggymacello/nf-orchestrator/pull/19)
### Fixed
- Cluster calls were bounded only by Go's TLS handshake default, so the outage alert had no data
  during an outage. Every call is bounded; `OrchestratorScrapeFailing` added
  ([drill 6](docs/operations/postmortems/2026-09-13-drill-6-frozen-control-plane.md)).
### Added
- Tests that fail the build when a front-door number in the documentation stops being true.

## 2026-09-12 — [#18](https://github.com/poggymacello/nf-orchestrator/pull/18)
### Fixed
- A recovered network function read `FAILED` forever, because evicted pods are never deleted
  ([drill 5](docs/operations/postmortems/2026-09-12-drill-5-disk-pressure-and-eviction.md)).

## 2026-09-09 — [#15](https://github.com/poggymacello/nf-orchestrator/pull/15), [#16](https://github.com/poggymacello/nf-orchestrator/pull/16), [#17](https://github.com/poggymacello/nf-orchestrator/pull/17)
### Added
- `POST /intents/translate`: free text to a candidate intent that passes the same schema and is
  never deployed by translation
  ([ADR-0012](docs/design/adr/0012-the-translator-is-untrusted-input.md)). An optional model-backed
  translator behind the same protocol, not yet run against the live API
  ([ADR-0013](docs/design/adr/0013-the-claude-translator-is-opt-in-and-unverified.md)).
### Fixed
- Ambiguous text was resolved by tuple order instead of refused
  ([drill 4](docs/operations/postmortems/2026-09-09-drill-4-steering-the-translator-with-text.md)).

## 2026-09-03 – 2026-09-04 — [#13](https://github.com/poggymacello/nf-orchestrator/pull/13), [#14](https://github.com/poggymacello/nf-orchestrator/pull/14)
### Added
- CI security gates: Gitleaks over the full history, a fail-closed private-term scan, Trivy over the
  chart and dependencies; a hardened stand-in chart; a pinned Kubernetes test matrix
  ([ADR-0010](docs/design/adr/0010-leak-gates-must-not-leak.md),
  [ADR-0011](docs/design/adr/0011-scan-thresholds-and-a-hardened-stand-in.md)).

## 2026-09-02 — [#6](https://github.com/poggymacello/nf-orchestrator/pull/6) to [#12](https://github.com/poggymacello/nf-orchestrator/pull/12)
### Added
- Failure drills 1–3 with postmortems and runbooks; lifecycle gauges and alert rules; the explicit
  repair route; end-to-end tests against a real kind cluster in CI.
### Fixed
- `INSTANTIATED` was reported for a partly running deployment, and an unreachable cluster was
  reported as a lifecycle state
  ([ADR-0006](docs/design/adr/0006-instantiated-means-the-intent-is-satisfied.md)). The operation
  state is split from the network function state
  ([ADR-0009](docs/design/adr/0009-the-operation-is-not-the-network-function.md)).

## 2026-07-22 – 2026-07-24 — [#2](https://github.com/poggymacello/nf-orchestrator/pull/2) to [#5](https://github.com/poggymacello/nf-orchestrator/pull/5)
### Added
- Intent schema and validation, the Helm deploy engine, the reconciler with lifecycle states and
  teardown, and the first Prometheus instrumentation.
