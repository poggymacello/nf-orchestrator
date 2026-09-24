# ADR-0018: The probe measures two ends, and a slow host is its own failure

- **Status:** Accepted
- **Date:** 2026-09-24
- **Closes:** findings 1 to 4 and 6 of
  [drill 11](../../operations/postmortems/2026-09-24-drill-11-the-orchestrator-host-is-starved.md)
- **Refines:** [ADR-0017](0017-reachability-needs-more-than-one-witness.md), whose
  `nf_cluster_probe_seconds` was wall-clock time, and
  [ADR-0016](0016-busy-is-not-unreachable.md), whose rule made a timed-out call "busy" whenever the
  probe answered

## Context

The orchestrator talks to the cluster through child processes. Every duration it measures is the
sum of two things: starting helm or kubectl on its own host, and the cluster's answer. Drills 9 and
10 pushed only the second of these, so every signal built from them assumed that slowness was the
cluster's.

Drill 11 pushed only the first. With the orchestrator's CPU starved and the cluster untouched, a
probe took about 1.1s of wall time while client-go measured the round trip at 31–80ms. Day 29's
signal paged `ClusterThrottlingOrchestrator`. With that fixed, day 28's rule still read a helm call
that overran its bound on the starved host as "the cluster is not serving this client", because
the probe answered.

## Decision

**1. The probe measures two ends.** kubectl runs at `-v=6`, and the probe reads the round trip
client-go logs for the `/readyz` response. That round trip is the cluster's time, published as
`nf_cluster_probe_seconds`. The rest of the wall time is the host's, published as
`nf_orchestrator_probe_local_seconds`. Only the cluster's time can make the cluster busy.

**2. A probe that never started is inconclusive.** If it is killed with nothing logged, the
cluster was never asked. It is not evidence of slowness, and all of its time is counted as the
host's. If it is killed after logging but before any response, it was waiting on the server, and
it counts as slow, as drill 10 needs.

**3. A timed-out call has three outcomes, not two.** The host is checked first, because both of
the other outcomes blame the cluster. `HostOverloaded` applies when the probe shows the cluster
prompt and this host slow, or when the host could not start the probe at all. After that comes
`ClusterBusy` (ADR-0016/0017), then `ClusterUnreachable`. `HostOverloaded` is a 503 with
`Retry-After`, and its detail names the host.

**4. The scrape's own probe gets most of the budget** (`NF_SCRAPE_PROBE_TIMEOUT`, 3s). It runs
beside the work, so it does not need the 0.75s bound a failed call is held to. A probe that is
long enough to finish on a slow host is what makes decision 1 possible there.

**5. A slow host pages on its own series:** `OrchestratorHostOverloaded`, warning severity,
pointing at the orchestrator's machine.

## Consequences

- The cluster is blamed only on the cluster's own measurement. Drill 10's queueing still reads as
  busy (round trip 1.0–2.2s), and a starved host no longer does (round trip 34–53ms).
- The decision now depends on the wording of kubectl's log. If that line stops matching, the
  probe falls back to wall time, which is day 29's behaviour: over-attributing to the cluster, not
  going blind. A unit test pins the line exactly as kubectl v1.36.1 printed it.
- While the host cannot start kubectl at all, nothing is said about the cluster, including when
  the cluster is actually down. The host alert fires during that window, and the postmortem
  records it as an accepted limit.
- The tests had been modelling a stalled server as a timeout with an empty log. That is what a
  starved host produces. The fakes now carry the log a real stalled server produced.

## Alternatives considered

**Time `kubectl version --client` as a reference and subtract it.** That costs a second process
for every probe, and it measures a different process start, not the one that did the asking.
client-go's own measurement comes free with the process we already run.

**Measure the host directly (CPU load, run queue).** That answers whether the machine is busy, not
whether the busy machine is why this call was slow. The probe's split answers the second question,
which is the one the classification needs.

**Leave the limit accepted, as day 29 did.** Drill 11 priced that choice: a false page that sends
someone to the cluster, and no page at all for the real cause.
