# ADR-0007: Stability is an alerting concern, and Prometheus is the poller

- **Status:** Accepted
- **Date:** 2026-08-26
- **Closes:** action item 4 of
  [drill 1](../../operations/postmortems/2026-08-20-drill-1-pods-killed-mid-deploy.md) — "decide
  what a stable state means before alerting on it".

## Context

Drill 1 left one finding unfixed. The lifecycle state is derived per read, so a single response is
an instant, not a state: during a routine pod replacement the endpoint moved
`INSTANTIATING → INSTANTIATED → INSTANTIATING → INSTANTIATED` inside three seconds with nothing
wrong. Day 12 made the derivation correct, which made the flapping *honest* — readiness genuinely
does drop while pods are replaced — but an alert built on one read would still fire on a
non-incident.

There is a second, older gap. ADR-0005 accepted that "polling cannot report a transition the caller
did not poll for. There is no event stream." The reconciler only runs when someone calls
`GET /deployments/{name}`, so nothing observes a deployment that no human is watching.

## Decision

**Do not debounce in the application.** No hysteresis, no "state must hold for N reads before I
report it", no smoothing inside `derive_state`. The endpoint keeps answering "what is true right
now", because that is the only question it can answer honestly at request time, and a caller asking
about this instant deserves this instant.

**Define stability in the alerting rule instead, with `for:`.** Prometheus already has exactly this
mechanism: the condition must hold at every evaluation across the window before the alert fires,
and the alert sits in `pending` until then. `for:` is where "stable" is defined, per rule, by
whoever owns the alert — which is the right place for a judgement that depends on what the alert is
*for*, not on what the state means.

The rules live in [`monitoring/nf-lifecycle.rules.yml`](../../../monitoring/nf-lifecycle.rules.yml):

| Alert | Expression | `for:` | Why that window |
|---|---|---|---|
| `NFNotInstantiated` | `nf_deployment_state{state="INSTANTIATED"} == 0` | 2m | Long enough to sit through any rollout the drills produced |
| `NFFailed` | `nf_deployment_state{state="FAILED"} == 1` | 1m | `FAILED` is already a settled judgement, not a rollout in progress |
| `NFReplicaShortfall` | `nf_deployment_ready_replicas < nf_deployment_desired_replicas` | 2m | Catches drift as well as a stuck rollout |
| `ClusterUnreachable` | `nf_cluster_reachable == 0` | 1m | Survives a restart of the orchestrator or a brief blip |

**Prometheus becomes the poller.** `LifecycleCollector` in
[`orchestrator/metrics.py`](../../../orchestrator/metrics.py) is registered on the metrics registry
and reconciles *every* Helm release at scrape time, emitting:

- `nf_deployment_state{release,state}` — 1 for the current state, 0 for the other three
- `nf_deployment_desired_replicas{release}` and `nf_deployment_ready_replicas{release}`
- `nf_cluster_reachable` — 1 or 0 for the scrape as a whole

This closes the ADR-0005 gap without adding a background loop, a watch, or a process lifecycle to
manage: the scrape interval *is* the poll interval. Four series per release plus two, bounded by
release count.

**A scrape that cannot see the cluster emits no release series at all.** If `helm list` fails, the
collector reports `nf_cluster_reachable 0` and yields nothing else. Emitting zeroes or stale values
would repeat the drill-2 mistake in a new place: an unreachable cluster must not be
indistinguishable from a cluster with nothing deployed in it. The consequence is that
`nf_deployment_state` series go *stale* during an outage rather than going to zero, so
`ClusterUnreachable` is the alert that covers that window.

**One state gauge per state, not a single numeric state.** Encoding the state as an integer
(0..3) would make PromQL comparisons meaningless and the number arbitrary. A 0/1 gauge per state
labels the state in the series itself, which is what makes
`nf_deployment_state{state="FAILED"} == 1` read like the sentence it is.

## Verified against Prometheus on 2026-08-26

Prometheus 3.x in a container scraping the orchestrator on the host every 5s, with the rules above
loaded (`for=120s`, `for=60s`, `for=120s`, `for=60s` confirmed via `/api/v1/rules`).

**A transient shortfall must not fire.** Deployment scaled from 2 to 1 outside Helm, left for 57
seconds, then scaled back:

```
13:44:39 baseline                 no alerts pending or firing
13:44:39 scaled 2 -> 1
13:44:49 ready=1                  NFNotInstantiated=pending, NFReplicaShortfall=pending
13:45:35 ready=1                  NFNotInstantiated=pending, NFReplicaShortfall=pending
13:45:36 scaled back to 2
13:45:54 ready=2                  no alerts pending or firing

ALERTS{alertstate="firing"} -> none fired
```

Pending for the whole 57 seconds, then cleared. That is the mechanism doing its job: the condition
was true, and the alert still correctly declined to page.

**A sustained failure must fire.** Same release upgraded to a nonexistent image tag:

```
13:46:29 upgraded to image tag does-not-exist-13
13:46:50 FAILED gauge = 1
13:47:01 NFFailed=pending
13:47:53 NFFailed=firing        (~60s after the condition became true, matching for: 1m)

ALERTS{alertstate="firing"} -> NFFailed  release=stable-nf  severity=page
```

`NFNotInstantiated` was still `pending` at that point, since its window is 2m — the two rules
disagreeing about the same underlying truth is the intended behaviour, not a bug.

## Consequences

- **Good:** the state endpoint stays honest and simple. No debounce logic to reason about, and no
  divergence between what the API says and what the metric says.
- **Good:** the flap that drill 1 found is now provably non-paging, and the demonstration is a
  reproducible experiment rather than an argument.
- **Good:** deployments are observed on an interval whether or not anyone is looking, closing the
  ADR-0005 gap without a background controller.
- **Good:** `for:` windows are per-alert and editable by whoever owns the alerting, without touching
  the orchestrator.
- **Cost:** the scrape now costs one `helm list` plus three subprocesses per release. At 5s and one
  release this was fine; at a large release count or a tight interval it would not be, and the fix
  then is a real cache or a watch, not a shorter interval.
- **Cost:** a slow or hanging cluster call now slows the scrape, so cluster latency shows up as
  scrape latency. ~~Nothing times these subprocesses out yet.~~ **Addressed 2026-09-13** by
  [drill 6](../../operations/postmortems/2026-09-13-drill-6-frozen-control-plane.md): unbounded,
  a frozen control plane made the scrape outlast Prometheus's timeout and the `ClusterUnreachable`
  alert lost its data. Every call is now bounded inside the scrape timeout, and
  `OrchestratorScrapeFailing` covers a failed scrape.
- **Cost:** series go stale rather than to zero during an outage, so any dashboard panel over
  `nf_deployment_state` shows a gap. That is deliberate, and it means panels need
  `nf_cluster_reachable` next to them to be readable.
- **Cost:** `for:` delays detection by its own window. A genuine failure is invisible to alerting
  for up to 2 minutes. That is the trade being made, and it is stated per rule.

## Alternatives considered

- **Debounce inside `derive_state` (require N consecutive identical reads).** Rejected: it needs
  state across calls, which ADR-0005 deliberately avoids, and it would make the API lie about the
  present moment to serve a consumer that is not the API.
- **Use the Deployment's `Progressing` / `Available` conditions as a rollout-complete signal.**
  Rejected as the primary mechanism: drill 1's flap came from pods deleted externally, which does
  not change the Deployment's generation, so `Progressing` would not have moved. Worth revisiting
  as an *additional* signal, not as the definition of stable.
- **A background reconcile loop writing gauges on a timer.** Rejected: it adds a process lifecycle,
  and the scrape already provides a timer that someone else operates.
- **Alert on `rate()` of state transitions instead.** Rejected: it detects flapping, which is not
  the thing worth paging on. A deployment that is quietly and stably broken produces no transitions
  at all.
