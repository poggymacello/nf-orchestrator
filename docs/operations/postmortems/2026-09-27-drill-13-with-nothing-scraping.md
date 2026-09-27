# Postmortem — drill 13: the same outage, with nothing scraping

**Date:** 2026-09-27 · **Duration of the staged incident:** ~15 minutes across two runs · **Severity:** Sev-1 (staged: a real outage)

**Summary:** Drill 12 found the status route telling callers the truth during an outage on a
starved host. It turned out the route only did so because Prometheus happened to be scraping: every
scrape runs a 3s readiness probe, and that probe was warm in the shared cache. This drill repeated
the incident with nothing scraping.

On its own, the status route blamed itself for a real outage. It said *"the delay is the
orchestrator's own host, not the cluster"* 48 seconds after the control plane froze. `/readyz`
got the opposite condition wrong: on the same starved host it reported a **healthy** cluster as
unreachable. Each route was right exactly where the other was wrong.

## What was staged

A throwaway kind cluster (`nf-drill13`, v1.35.0, port 45435) with ten releases. The orchestrator and
eight busy loops were pinned to one core, as in drills 11 and 12. There was no Prometheus and no
other scraper, so the only probes run were the ones API calls asked for.

The drill had three phases:

1. A control run with the cluster healthy.
2. The node frozen (`docker pause`).
3. The node stopped (`docker stop`), so that connections were refused.

`/deployments/<nf>` and `/readyz` were sampled alternately throughout.

The first attempt to start the load from Git Bash failed halfway and left eight extra busy loops
running (32 processes instead of 16). The drill was reset to eight before any sample was taken, so
the numbers stay comparable with drills 11 and 12.

## Hypotheses, and what happened

| | Predicted | Observed |
| --- | --- | --- |
| H1: status route, frozen cluster | `HostOverloaded` for as long as the host is starved | **Held.** From 09:22:18 to 09:23:02 (44s, well past the 30s window), every response was "...could not start its readiness probe within 0.78–2.17s, so nothing can be said about the cluster: the delay is the orchestrator's own host, not the cluster" |
| H2: `/readyz`, frozen cluster | Unreachable, so the service disagrees with itself | **Held**, but it meant less than predicted: `/readyz` said the same thing with the cluster **healthy** (finding 2) |
| H3: control run | Status calls either finish or fail as `HostOverloaded` | Held |
| H4: stopped cluster | Refused immediately, so unreachable with no timeout | **Held:** both routes returned 503 with the refusal text within 0.5–1.8s |
| H5 | Nothing pages, because nothing scrapes | By construction |

## Findings

**1. The status route blamed the orchestrator for a real outage.** When a call timed out, the route
asked the probe first whether the delay was local. That probe was held to 0.75s. On the starved
host, kubectl couldn't start within that time, so the route's only conclusion was "this host".
The message then contradicted itself: "nothing can be said about the cluster: the delay is ... not
the cluster". **Fixed.**

**2. `/readyz` reported a healthy cluster as unreachable.** This wasn't predicted. In the control
run, 2 of 3 readiness calls returned `503 kubectl get did not answer within 3s`. The readiness
check ran as a plain kubectl call, and a timed-out readiness call is never checked for evidence,
because that would mean probing itself. So a slow host read as a dead cluster. Anything that uses
`/readyz` to decide whether the orchestrator can reach its cluster would have been told no.
**Fixed.**

**3. On Windows, `max_age=0` could return a cached answer.** This surfaced while unit-testing the
fix, not in the drill. The cache check was `age <= max_age`. Windows' monotonic clock ticks about
every 15ms, so an answer cached within the same tick has age exactly 0. The retry from finding 1,
asking for a fresh probe, got back the very probe that had never started. **Fixed.**

**4. A slower 503 on a starved host. Accepted.** Settling the probe costs time. On the starved host
a status 503 now takes 7–10s: the 3s call, the 0.75s probe, and up to 3s for the retry. Before, it
took 4–8s. That buys the right answer about which end is failing. A healthy host never pays it,
because there the first probe starts.

## The fix

[ADR-0020](../../design/adr/0020-an-inconclusive-probe-is-asked-again.md).

- **A probe that never started is asked once more, with 3s** (`settled_probe`). That is the same
  bound as the scrape's own probe, which drill 12 showed does start on this host. The existing
  evidence (the host's share, recent answers, reachability) then decides as before.
- **`/readyz` is the probe.** It runs one kubectl at `-v=6`, so there are four distinct outcomes:
  - answered: `ok`
  - answered with an error, such as a refusal: kubectl's own error text, classified like any other
    reply
  - started but never answered: unreachable
  - never started: `HostOverloaded`, a 503 with `Retry-After`
- **`max_age <= 0` means measure now,** whatever the clock says.
- **The messages say one thing each:** either *"the delay is this host's, not the cluster's"*, with
  both measurements, or *"it is too overloaded to tell whether the cluster is answering"*.

## Verification

The same incident, on the fixed build, with nothing scraping:

| | Before | After |
| --- | --- | --- |
| `/deployments`, healthy cluster | 503 "nothing can be said", or 200 | 503 "the API server answered its readiness probe in **34–146ms** while this host took **2.00–2.73s**: the delay is this host's, not the cluster's", or 200 |
| `/readyz`, healthy cluster | **503 unreachable** (2 of 3) | 503 "did not start within 3s: this host is too overloaded to tell" (1 of 3), then 200 |
| `/deployments`, frozen cluster | "the orchestrator's own host, not the cluster" for 44s+ | busy ("answered another call moments ago") for 21s, then **unreachable from 09:27:36** |
| `/readyz`, frozen cluster | unreachable | unreachable |
| after unfreezing | — | both routes 200 within 10s, host still starved |

In the frozen phase, the 21-second busy window is day 29's 30-second recent-success rule, counted
from the last call that succeeded before the freeze. It is working as designed (drill 10, finding 7).

## What this drill did not cover

- Prometheus scraping *and* the cache going cold between scrapes (for example, a 15s scrape
  interval against the 2s cache lifetime). The request path now settles its own probe, so it
  shouldn't matter, but that wasn't observed.
- Memory or I/O pressure. It's still only CPU.
