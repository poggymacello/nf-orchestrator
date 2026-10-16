# Alerts reference

Every alert in [`monitoring/nf-lifecycle.rules.yml`](../../monitoring/nf-lifecycle.rules.yml), with
its severity, how long its condition must hold, and the runbook it links. Each rule also carries a
`runbook_url` annotation for alerting tools.

- **`page`**: someone should act now. A network function is down or unmonitored, or the orchestrator
  is wrong about the world.
- **`warning`**: worth a look in working hours. Degraded, but not yet wrong.

Every rule has a `for:` window: stability is defined here, not in the application
([ADR-0007](../design/adr/0007-stability-is-an-alerting-concern.md)). The rules are unit-tested with
promtool against the series the drills produced, in CI
([ADR-0019](../design/adr/0019-alert-rules-are-tested-against-drill-series.md)), and
`tests/test_reference_docs.py` fails the build if this table and the rules disagree.

| Alert | Severity | For | Summary | Expression | Runbook |
|---|---|---|---|---|---|
| `NFNotInstantiated` | page | 2m | *release* has not been INSTANTIATED for 2 minutes | `nf_deployment_state{state="INSTANTIATED"} == 0` | [deployment-not-reaching-instantiated](../operations/runbooks/deployment-not-reaching-instantiated.md) |
| `NFFailed` | page | 1m | *release* is FAILED | `nf_deployment_state{state="FAILED"} == 1` | [deployment-not-reaching-instantiated](../operations/runbooks/deployment-not-reaching-instantiated.md) |
| `NFReplicaShortfall` | warning | 2m | *release* is short of the replicas its intent asked for | `nf_deployment_ready_replicas < nf_deployment_desired_replicas` | [deployment-not-reaching-instantiated](../operations/runbooks/deployment-not-reaching-instantiated.md) |
| `ClusterUnreachable` | page | 1m | The orchestrator cannot reach the cluster | `nf_cluster_reachable == 0` | [control-plane-unreachable](../operations/runbooks/control-plane-unreachable.md) |
| `NFLastOperationFailed` | warning | 5m | *release*'s last lifecycle operation failed | `nf_last_operation_state{state="FAILED"} == 1` | [deployment-not-reaching-instantiated](../operations/runbooks/deployment-not-reaching-instantiated.md) |
| `OrchestratorScrapeFailing` | page | 1m | Prometheus cannot scrape the orchestrator | `up{job="nf-orchestrator"} == 0` | [scrape-over-budget](../operations/runbooks/scrape-over-budget.md) |
| `NFReleaseUnreported` | warning | 5m | *release* has no lifecycle series, so nothing can alert on it | `nf_release_reported == 0` | [scrape-over-budget](../operations/runbooks/scrape-over-budget.md) |
| `ClusterThrottlingOrchestrator` | page | 1m | The API server is reachable but not serving the orchestrator | `(sum_over_time(nf_cluster_busy[2m]) / count_over_time(up{job="nf-orchestrator"}[2m])) >= 0.5 unless on(instance, job) nf_cluster_reachable == 0` | [cluster-throttling](../operations/runbooks/cluster-throttling.md) |
| `OrchestratorHostOverloaded` | warning | 1m | The orchestrator's own host is too slow to run its cluster calls promptly | `avg_over_time(nf_orchestrator_probe_local_seconds[2m]) >= 0.5 or (sum_over_time(nf_orchestrator_exhausted[2m]) / count_over_time(up{job="nf-orchestrator"}[2m])) >= 0.25` | [scrape-over-budget](../operations/runbooks/scrape-over-budget.md) |
| `OrchestratorMisconfigured` | page | 1m | The orchestrator cannot run helm or kubectl on its own host | `nf_orchestrator_misconfigured == 1` | [orchestrator-misconfigured](../operations/runbooks/orchestrator-misconfigured.md) |

## Telling the orchestrator's alerts apart

Five of these alerts can describe the same visible symptom, a release with no fresh state, and they
are deliberately different incidents with different responses:

| Alert | Whose fault | First action |
|---|---|---|
| `ClusterUnreachable` | The cluster | Check the control plane |
| `ClusterThrottlingOrchestrator` | The cluster's flow control, for this client | Check API Priority and Fairness; do not restart anything |
| `OrchestratorHostOverloaded` | The orchestrator's machine | Find what is using its CPU, processes or threads |
| `OrchestratorMisconfigured` | The orchestrator's install | Install helm or kubectl, or fix `PATH` |
| `OrchestratorScrapeFailing` | The orchestrator process, or a scrape outrunning Prometheus | Check `/healthz`, then the scrape duration |

Each was separated from the others by a drill that found them confused; see the
[drill index](../operations/README.md#drills-run-so-far).
