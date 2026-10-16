# Configuration reference

Every setting the orchestrator reads, its default, and the decision that set it. All settings are
environment variables read once, when the process starts; changing one needs a restart.
`tests/test_reference_docs.py` fails the build if the code reads an `NF_*` variable that is not
listed here.

## Cluster target

| Variable | Default | Meaning |
|---|---|---|
| `NF_KUBE_CONTEXT` | `kind-nf-orchestrator` | kubeconfig context every helm and kubectl call uses. Point it at a throwaway cluster for a drill instead of editing code |
| `KUBECONFIG` | kubectl's default | Standard kubeconfig location, honoured by helm and kubectl themselves. Drills used it to run as a service account rather than kind's admin identity, which API Priority and Fairness exempts |

## Call bounds

Every helm and kubectl call is a child process with a wall-clock bound
([drill 6](../operations/postmortems/2026-09-13-drill-6-frozen-control-plane.md)).

| Variable | Default | Meaning |
|---|---|---|
| `NF_READ_TIMEOUT` | `3` s | Bound on read calls: status, history, values, pod listings, the readiness check. Must fail inside Prometheus's 5 s scrape timeout |
| `NF_WRITE_TIMEOUT` | `60` s | Bound on `upgrade`, `install`, `uninstall` and `rollback` |

## Scrape

The `/metrics` collector reconciles every release at scrape time.

| Variable | Default | Meaning |
|---|---|---|
| `NF_SCRAPE_BUDGET` | `4` s | Deadline for one whole scrape. Releases not finished by then are reported as `nf_releases_unreported{reason="deadline"}`, not waited for ([ADR-0014](../design/adr/0014-the-scrape-has-one-budget.md)) |
| `NF_SCRAPE_WORKERS` | `8` | Ceiling on concurrent release reads. Work is admitted in waves that start at 2 and double while waves finish ([ADR-0015](../design/adr/0015-admit-scrape-work-in-waves.md)). Keep it below the API Priority and Fairness seats of the orchestrator's identity ([cluster throttling](../operations/runbooks/cluster-throttling.md)) and within its pids limit, at roughly ten pids per concurrent helm call ([scrape over budget](../operations/runbooks/scrape-over-budget.md#when-the-host-cannot-start-threads-at-all)) |
| `NF_SCRAPE_PROBE_TIMEOUT` | `3` s | Bound on the readiness probe each scrape runs beside its work. Must stay below `NF_SCRAPE_BUDGET` |

## Readiness probe and reachability evidence

When a call times out, the orchestrator asks `/readyz` of the API server to decide whether the
cluster is unreachable, throttling this client, or whether the delay was this host's own
([ADR-0016](../design/adr/0016-busy-is-not-unreachable.md) to
[ADR-0020](../design/adr/0020-an-inconclusive-probe-is-asked-again.md)).

| Variable | Default | Meaning |
|---|---|---|
| `NF_PROBE_TIMEOUT` | `0.75` s | Bound on the probe asked after a failed call. `NF_READ_TIMEOUT + NF_PROBE_TIMEOUT` must fit in `NF_SCRAPE_BUDGET`; a unit test pins it |
| `NF_ESCALATED_PROBE_TIMEOUT` | `3` s | Bound on the second probe, asked only when the first one never got kubectl started |
| `NF_PROBE_TTL` | `2` s | How long a probe answer is reused, so calls that time out together share one probe |
| `NF_SLOW_PROBE` | `0.5` s | A probe round trip at or above this makes the cluster *busy* (`nf_cluster_busy 1`) |
| `NF_RECENT_SUCCESS` | `30` s | How long a completed call counts as evidence the cluster is there. A cluster that dies just after answering reads *busy* for up to this long before it reads *unreachable*. `0` disables this evidence |

## Not configurable

| Setting | Value | Why fixed |
|---|---|---|
| `Retry-After` on `503` | `5` s | A queueing or throttling server publishes no estimate; this is the orchestrator's own guess and is documented as one |
| `helm list` page size | `256` | Helm's server-side default maximum; releases are read in pages of this size |
| Required helm major version | `4` | The deploy path depends on Helm 4's `--set-json` and server-side apply ([ADR-0023](../design/adr/0023-the-tools-are-checked-not-assumed.md)) |
| Tool version cache | `60` s | `helm version` is asked at most once a minute |

## Optional: model-backed translator

`orchestrator.llm.ClaudeTranslator` uses the Anthropic SDK's own credentials (`ANTHROPIC_API_KEY`)
and needs the `llm` extra (`pip install -e ".[llm]"`). It is a library class: the HTTP route
`POST /intents/translate` uses the rule-based translator, so the service never needs a key. The
model-backed translator has never been run against the live API
([ADR-0013](../design/adr/0013-the-claude-translator-is-opt-in-and-unverified.md)).
