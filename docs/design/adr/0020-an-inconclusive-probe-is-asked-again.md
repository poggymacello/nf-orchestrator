# ADR-0020: An inconclusive probe is asked again, not believed

- **Status:** Accepted
- **Date:** 2026-09-27
- **Closes:** findings 1 to 3 of
  [drill 13](../../operations/postmortems/2026-09-27-drill-13-with-nothing-scraping.md)
- **Refines:** [ADR-0018](0018-the-probe-measures-two-ends.md), which made "the probe never
  started" mean `HostOverloaded`

## Context

ADR-0018 gave a timed-out call three outcomes and checked the host first. One of the host's cases
was "the probe never started". Held to 0.75s, the probe hadn't even got kubectl running, which says
the host is slow. ADR-0018 read that as "the delay is this host's".

The inference only held when something else in the system had already done better. In drill 12,
Prometheus's scrapes kept a 3s probe warm in the cache, so a request rarely ran its own 0.75s probe.
Drill 13 took the scrapes away. On a starved host with the control plane frozen, every status call
concluded "the orchestrator's own host, not the cluster" through a real outage. `/readyz` failed
the opposite way. It was a plain call that never asked for evidence, so it reported a healthy
cluster as unreachable whenever the host was too slow.

The common fault: a probe that never started is not an observation of anything a caller asked
about. It says the host is slow, and says nothing about whether the cluster is up.

## Decision

**1. A probe that never started is asked once more, with 3s, before anything is concluded.** It uses
the same bound as the scrape's own probe, which drill 12 measured starting on a starved host in
about 1.1s. Only if that second probe also fails to start does the answer become "this host is too
overloaded to tell". That is a statement about what can be known, not about which end is at fault.

**2. The readiness check is the probe.** It runs one kubectl at `-v=6` and gives four outcomes:
answered; an error in kubectl's own words, classified like any other reply (a refused connection is
unreachable); started but never answered (unreachable); never started (`HostOverloaded`). It is
still one process, and it still never probes itself.

**3. `max_age <= 0` always measures.** A cache check written as `age <= max_age` serves an answer
from the same clock tick. On Windows that tick is about 15ms.

## Consequences

- On a starved host a failing call takes longer to report: 7–10s for a 503, measured, against 4–8s
  before. A healthy host never retries, because its first probe starts.
- The orchestrator's two routes now reach the same conclusion from the same measurement. Before,
  each was right exactly where the other was wrong.
- `/readyz` can now return `HostOverloaded`, a 503 with `Retry-After`. Callers that treated every
  readiness 503 as "the cluster is gone" will now see the host named instead when it is the host.

## Alternatives considered

**Always give the request path's probe 3s.** On a healthy host the first probe answers in about
0.1s, so the bound rarely matters. But the scrape's timed-out calls also reach for evidence, and a
3s first attempt could push them past the scrape budget, which is exactly what the 0.75s bound
protects. Escalating only when the first attempt proved nothing keeps the common case unchanged.

**Size the probe's bound from the last measured start-up time.** In a cold cache, which is the case
this drill is about, there is no last measurement to use.

**Leave `/readyz` as a plain call and interpret its timeout.** A plain kubectl logs nothing, so a
killed one can't show whether it ever started. Measuring that is what `-v=6` is for.
