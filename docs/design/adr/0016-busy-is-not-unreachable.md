# ADR-0016: A throttled cluster is busy, not unreachable

- **Status:** Accepted
- **Date:** 2026-09-17
- **Closes:** findings 1 and 2 of
  [drill 9](../../operations/postmortems/2026-09-17-drill-9-the-api-server-sheds-load.md)
- **Refines:** [ADR-0006](0006-instantiated-means-the-intent-is-satisfied.md), which made
  "unreachable" a failure rather than a state

## Context

Since drill 2 the orchestrator has had two answers for "the cluster did not give me what I asked
for": it could not be reached (503), or it answered and refused (502). Drill 6 added a third way to
arrive at the first — a call that does not answer within its bound.

Drill 9 found a case that fits neither. Under API Priority and Fairness load shedding, the API server
answered every request immediately, with 429 and `Retry-After` up to 32 seconds. client-go, inside
kubectl and helm, treats that as "wait and retry", so the child process went silent, the bound
killed it, and the orchestrator said *unreachable*. Meanwhile `/readyz` answered in 0.2 seconds.

The two incidents need opposite responses. An unreachable control plane is investigated and
restarted. A throttled client is left alone, or given more room; restarting anything makes it worse.

## Decision

**A third failure: `ClusterBusy` — reachable, and not serving this client.**

- When a call times out, the orchestrator asks `kubectl get --raw=/readyz` with a 0.75s bound. If
  that answers, the call raises `ClusterBusy`; if it does not, `ClusterUnreachable` as before. The
  readiness call itself is never probed twice.
- client-go's own wording for an exhausted 429 retry — "too many requests", "has asked us to try
  again later" — is classified as busy wherever it appears.
- HTTP: **503 with `Retry-After: 5`**. 503, because the orchestrator cannot answer the request right
  now. Not 502, which says the cluster refused the operation and invites no retry. Not 429, which
  would tell the caller that *it* is sending too much.
- Metrics: `nf_cluster_busy` 1 when any read in the scrape was busy, `nf_cluster_reachable` stays
  1, and throttled releases count under `nf_releases_unreported{reason="busy"}`. If the release list
  itself is busy, only those two gauges are emitted — the same "no release series" rule as an outage,
  with a gauge that says why.
- Alerting: `ClusterThrottlingOrchestrator` pages on `nf_cluster_busy == 1` for 2m.

The probe works because Kubernetes' default `probes` FlowSchema routes `/readyz`, `/livez` and
`/healthz` outside the priority levels that throttle ordinary requests. That is a Kubernetes
default, not a law, and the consequences say so.

## Consequences

- **Good:** throttling no longer pages as an outage, and the page that does fire says what to check.
- **Good:** drills 6 and 7 are unchanged — a frozen or stalled server fails the probe too, and was
  re-checked against drill 7's server after the change.
- **Good:** a caller gets a `Retry-After` it can act on.
- **Cost:** a timed-out call takes up to 0.75s longer, because the probe runs after it. Drill 7's
  stalled server went from 3.03s to 3.81s per status request. A test pins `READ_TIMEOUT +
  PROBE_TIMEOUT` inside the scrape budget.
- **Cost:** it depends on `/readyz` being served outside the throttled levels. A cluster whose
  administrator removed or narrowed the `probes` FlowSchema would put the probe in the same queue,
  and throttling would read as unreachable again — the pre-drill behaviour, not something worse.
- **Cost:** "busy" covers two different causes — this client being throttled, and the API server
  being overloaded but still ready. The runbook separates them with APF's own rejection counters;
  the orchestrator does not.
- **Cost:** `Retry-After: 5` is a fixed hint. The API server's own value reached 32 seconds, and it
  never reaches the orchestrator: client-go consumes it.

## Alternatives considered

- **Pass `-v=6` to kubectl and parse the retry logs on timeout.** Rejected: it works for kubectl
  only — helm does not expose client-go's retry logging — and it binds correctness to the wording of
  debug output.
- **Talk to the API server directly instead of through kubectl and helm.** It would see the 429
  itself. Rejected here: it replaces the deploy engine's whole premise (ADR-0004) to fix one
  classification, and helm's release storage would have to be read by hand.
- **Disable client-go's retries.** There is no kubectl or helm flag for it, and retrying on 429 is
  the right behaviour for an interactive user.
- **Treat any timeout as busy.** Rejected: that repeats the drill-2 mistake in the other direction,
  telling an operator a dead control plane is merely busy.
