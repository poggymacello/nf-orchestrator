# Metrics reference

Every series `GET /metrics` exposes. Lifecycle series are computed at scrape time, by reconciling
every Helm release inside one budget (`NF_SCRAPE_BUDGET`, 4 s); they describe the cluster at the
moment of the scrape. `tests/test_reference_docs.py` fails the build if the code emits a series this
page does not list.

## Lifecycle — one set per release

| Series | Type | Labels | Meaning |
|---|---|---|---|
| `nf_deployment_state` | gauge | `release`, `state` | 1 for the release's current state (`NOT_INSTANTIATED`, `INSTANTIATING`, `INSTANTIATED`, `FAILED`), 0 for the others |
| `nf_deployment_desired_replicas` | gauge | `release` | Replicas the intent asked for, from the revision actually in effect |
| `nf_deployment_ready_replicas` | gauge | `release` | Pods whose containers all report ready |
| `nf_last_operation_state` | gauge | `release`, `state` | 1 for the state of the last Helm operation (`PROCESSING`, `COMPLETED`, `FAILED`, `UNKNOWN`), 0 for the others |

## Coverage — which releases have no lifecycle series this scrape

A release with no lifecycle series has no alerting: `NFFailed` cannot fire for it. These say so
explicitly instead of letting it disappear ([ADR-0014](../design/adr/0014-the-scrape-has-one-budget.md)).

| Series | Type | Labels | Meaning |
|---|---|---|---|
| `nf_release_reported` | gauge | `release` | 1 if the release's lifecycle series were emitted this scrape, 0 if not |
| `nf_releases_unreported` | gauge | `reason` | How many listed releases emitted no lifecycle series, by why |

| `reason` | Cause | Where to look |
|---|---|---|
| `deadline` | The scrape budget ran out before the release was read | [scrape over budget](../operations/runbooks/scrape-over-budget.md), or [cluster throttling](../operations/runbooks/cluster-throttling.md) if `nf_cluster_busy` is 1 |
| `busy` | The API server was reachable and would not serve this client | [cluster throttling](../operations/runbooks/cluster-throttling.md) |
| `host` | The orchestrator's own host: too slow, out of processes or threads, or missing a tool | [scrape over budget](../operations/runbooks/scrape-over-budget.md#the-orchestrators-host-is-overloaded), [orchestrator misconfigured](../operations/runbooks/orchestrator-misconfigured.md) |
| `error` | Anything else; read the release directly for the error | [scrape over budget](../operations/runbooks/scrape-over-budget.md) |

## The cluster, as seen by this orchestrator

| Series | Type | Meaning |
|---|---|---|
| `nf_cluster_reachable` | gauge | 1 when the orchestrator could reach the cluster this scrape, 0 when it could not. **Absent** when the orchestrator could not ask at all (its own host failed), because no claim either way is honest then |
| `nf_cluster_busy` | gauge | 1 when the API server was reachable but did not serve the orchestrator promptly: throttled (429) or queueing, measured by the readiness probe's round trip ([ADR-0016](../design/adr/0016-busy-is-not-unreachable.md), [ADR-0017](../design/adr/0017-reachability-needs-more-than-one-witness.md)) |
| `nf_cluster_probe_seconds` | gauge | The API server's `/readyz` round trip this scrape, as client-go measured it: the cluster's own time, without starting kubectl ([ADR-0018](../design/adr/0018-the-probe-measures-two-ends.md)) |

## The orchestrator's own host

| Series | Type | Meaning |
|---|---|---|
| `nf_orchestrator_probe_local_seconds` | gauge | How much of this scrape's probe was spent on the orchestrator's host, mostly starting kubectl. About 0.05–0.1 s on an idle host; over 1 s on a CPU-starved one ([drill 11](../operations/postmortems/2026-09-24-drill-11-the-orchestrator-host-is-starved.md)) |
| `nf_orchestrator_exhausted` | gauge | 1 when this scrape could not start a process or thread it needed: a pids or memory limit ([ADR-0021](../design/adr/0021-a-host-out-of-capacity-is-not-a-slow-host.md)) |
| `nf_orchestrator_misconfigured` | gauge | 1 when helm or kubectl was missing or not executable ([ADR-0022](../design/adr/0022-a-failed-spawn-is-classified-by-its-cause.md)) |

## Counters

| Series | Type | Labels | Meaning |
|---|---|---|---|
| `deployments_total` | counter | `result` (`success`, `failed`), `environment` | Validated intents submitted to `POST /deployments`. Invalid bodies are rejected before this counts them |
| `deployment_repairs_total` | counter | `result`, `environment` | Calls to `POST /deployments/{name}/repair` |

Each counter also exposes `deployments_created` / `deployment_repairs_created`, the per-series
creation timestamp the Python client library adds to every counter. The library's default process
and runtime collectors are not registered; the registry holds only the series on this page.
