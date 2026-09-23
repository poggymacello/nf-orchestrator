# ADR-0017: Reachability needs more than one witness, and slowness is one of them

- **Status:** Accepted
- **Date:** 2026-09-23
- **Closes:** findings 1, 2 and 3 of
  [drill 10](../../operations/postmortems/2026-09-23-drill-10-the-api-server-queues-instead-of-refusing.md)
- **Refines:** [ADR-0016](0016-busy-is-not-unreachable.md), which made a single readiness probe the
  test for "busy rather than gone"

## Context

ADR-0016 answered "is the cluster there?" with one question asked once: does `/readyz` answer inside
0.75 seconds. That worked for drill 9, where the API server rejected the orchestrator instantly and
had all the capacity in the world for a probe.

Drill 10 configured the same flow control to **queue** rather than reject. Two assumptions broke.

The first was that a slow answer implies an absent server. `/readyz` is served on the exempt
`probes` FlowSchema — the API server's own counters confirm the drill never touched it — and it was
still slow, 0.45s to 3.58s against 0.06 – 0.25s idle, because a server saturated with expensive
lists is slow on every path. Most probes therefore missed their bound, and missing the bound was the
whole test. The orchestrator reported `nf_cluster_reachable 0` for a cluster that was answering its
other calls, which is the page that sends someone to restart a healthy control plane.

The second was that back-pressure produces errors to classify. Under queueing nothing is refused and
nothing says why: calls just take longer, releases miss the scrape budget, and the resulting
`nf_releases_unreported{reason="deadline"}` is exactly what drill 7's over-budget scrape produced —
an orchestrator-side incident with an orchestrator-side runbook.

## Decision

**1. Two independent witnesses, and either will do.** After a call is killed for silence, the
orchestrator calls the cluster reachable if the probe answers **or** if any other call completed
within `NF_RECENT_SUCCESS` (30s). Only when neither holds is it `ClusterUnreachable`. The witnesses
fail for different reasons — one is a fresh cheap request, the other is work that already succeeded
— and a cluster that is stopped, frozen or stalled satisfies neither.

**2. How long the probe took is a measurement, not a side effect.** Each scrape times one probe on a
thread beside its work and publishes `nf_cluster_probe_seconds`. A probe that takes ≥ `NF_SLOW_PROBE`
(0.5s), or does not come back at all, sets `nf_cluster_busy 1` **even when every release was read
successfully**. `/readyz` is the cheapest thing the API server serves; if it is slow for us, we are
not being served, whatever the releases say.

**3. The probe is asked once per `PROBE_TTL` (2s), by whoever asks first.** Concurrent callers wait
for that answer instead of starting a kubectl each. A scrape's probe fills the same cache, so a wave
of timed-out releases adds nothing.

## Consequences

- An incident where the cluster is merely slow is now named as throttling rather than as an
  orchestrator budget problem, and `nf_cluster_probe_seconds` shows the number the naming rests on.
- **A cluster that dies within 30 seconds of answering is called busy first.** Measured at ~24
  seconds during the drill, bounded by `NF_RECENT_SUCCESS`, and `ClusterUnreachable` pages up to
  that much later. Mislabelling sustained throttling as an outage was judged worse than delaying an
  outage page by under a minute; `NF_RECENT_SUCCESS=0` turns the second witness off.
- The busy signal is per-scrape and can flap around its threshold, so
  `ClusterThrottlingOrchestrator` asks what share of a two-minute window was busy rather than
  requiring an unbroken run of it.
- `nf_cluster_probe_seconds` cannot separate a slow cluster from an orchestrator host too loaded to
  start a process. Both mean the orchestrator is not being served promptly, which is what the alert
  claims and all it claims.
- The evidence is process-wide mutable state, which tests must reset between cases; `tests/conftest.py`
  does it for every test rather than leaving it to the ones that remember.

## Alternatives considered

**Raise `PROBE_TIMEOUT` until a slow probe fits inside it.** The probe has to fit in the scrape
budget alongside the read timeout — 3s + 0.75s against 4s — so there was nothing to raise it with,
and a probe long enough to catch drill 10's 3.58s would have to outlive the scrape that asked for it.

**Treat every timed-out call as busy.** It would have been right for drills 9 and 10 and wrong for
2, 6 and 7, which are the incidents where someone has to go and look at the control plane.

**Infer back-pressure from the scrape's own duration.** The orchestrator cannot tell its own
slowness from the cluster's by timing work whose size it controls: that is precisely the confusion
drill 10 walked into. The probe is the reference measurement because its cost does not depend on how
many releases exist.
