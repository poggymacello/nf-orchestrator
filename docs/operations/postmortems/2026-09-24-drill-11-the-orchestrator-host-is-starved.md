# Postmortem — drill 11: the orchestrator's own host is starved

**Date:** 2026-09-24 · **Duration of the staged incident:** ~40 minutes across two runs · **Severity:** Sev-2 (staged)

**Summary:** The orchestrator's CPU was starved while the cluster stayed healthy. Day 29's busy
signal is the readiness probe's wall-clock time, and that time includes starting kubectl on the
orchestrator's own machine. So `nf_cluster_busy` went to 1 on every scrape, and
`ClusterThrottlingOrchestrator` paged at 03:17:48Z. The cluster was answering the same probe in
5–8ms. The page's runbook sends whoever is on call to inspect API Priority and Fairness on a
cluster with nothing wrong, while the actual cause, a busy laptop, had no alert at all.

Day 29 had written this down as a limit it accepted. This drill checked what that limit costs,
and it was worse than the note suggested.

## What was staged

A throwaway kind cluster (`nf-drill11`, v1.35.0, API port 45433) with ten `drill11-*` releases.
The orchestrator ran as a normal process, and after it started its CPU affinity was pinned to
core 0. Eight pure-Python busy loops were pinned to that same core. The kubectl and helm processes
the orchestrator starts inherit its affinity, so they all queued for that one core. Docker
Desktop's VM, and with it the cluster, kept the other fifteen.

The hypothesis file named that last point as the drill's confound: the lab puts the cluster and
the orchestrator on the same machine. It was checked rather than assumed. While the burners ran, a
shell that wasn't pinned got `/readyz` round trips of **5–8ms**, the same as idle. So the cluster
was unaffected.

## What the orchestrator did (before the fix)

| Signal | Healthy | Host starved |
| --- | --- | --- |
| Scrape duration | 0.82s | 3.1 – 5.0s |
| `nf_cluster_probe_seconds` | 0.08 – 0.09 | 0.80 – 0.88 (every probe killed at its bound) |
| `nf_cluster_busy` | 0 | **1, every scrape** |
| Releases unreported | 0 | 5 – 8 of 10, `reason="deadline"` |
| `ClusterThrottlingOrchestrator` | inactive | **firing**, active from 03:17:48Z |

## Findings

**1. A starved host paged as a throttling cluster.** From one kubectl process, pinned to the
starved core and given as long as it needed, `-v=6` logs two different numbers. Wall time was
**0.98 – 1.17s**. The round trip, which client-go measures from request to response and which
leaves out process start-up, was **31 – 80ms**. Day 29 judged the cluster on the first number.
**Fixed.**

**2. The probe usually never reached the cluster.** Held to the orchestrator's 0.75s bound on
the starved core, 8 of 8 probes were killed, and **7 of the 8 had not logged a single line**:
kubectl had not even loaded its config. Such a probe tells us nothing about the cluster, yet day
29 had deliberately counted "did not come back" as slow. For drill 10 that was right: a queueing
server was holding a probe that had already been sent. Here it was wrong. **Fixed**, by telling
the two apart. Against drill 7's stalled TLS server, which is the case the unreachable
classification depends on, 5 of 5 killed probes had logged 11 lines, so the difference can be
measured, not just argued.

**3. After finding 1 was fixed, the same host still read as busy.** The probe split worked:
round trip 33–70ms, busy 0 on the probe's account. `nf_cluster_busy` was still 1 on 4 of 6
scrapes. The per-release reasons were all 0, which pointed at the one batched pod listing. On the
starved host that listing overran its 3s read bound, and then the probe answered. Day 28's rule
reads "timed out, but the server answers its probe" as "reachable and not serving this client".
The probe did not say that. It said the server answered in 40ms. **Fixed:** a third outcome
for a call that times out.

**4. The real cause had no alert.** Before the fix, the only other thing that fired was
`NFReleaseUnreported`, whose runbook, [scrape over budget](../runbooks/scrape-over-budget.md),
is about releases and workers, not about the machine. **Fixed:** a series and an alert of its own.

**5. The tests described a stalled server wrongly.** Every day-28 and day-29 test fake for a
stalled or frozen server raised a timeout with nothing logged. As finding 2 measured, that is what
a starved host looks like. Once the code could tell the two apart, five of those tests started
failing. The fakes were wrong, not the code: they now carry the log a real stalled server
produces, and a comment records the measurement behind it. **Fixed.**

**6. A throttled listing published no probe numbers.** In the regression run, half the queueing
scrapes took the early exit for "could not even list releases". That path emitted no probe
numbers, and the probe numbers are exactly what the throttling runbook tells you to read.
**Fixed.**

**7. A starved host still loses the occasional scrape. Accepted.** Under starvation, 1 of about
60 scrapes in five minutes ran past Prometheus's 5s timeout (the longest recorded was 4.5s, and
the rest were within budget). That isn't enough for `OrchestratorScrapeFailing`, which needs a
minute of failures. The orchestrator can't make its own machine faster, and the alert that
matters here now fires.

**8. A starved host hides the cluster. Accepted, not drilled — retired by
[drill 12](2026-09-26-drill-12-a-starved-host-and-a-frozen-cluster.md), which drilled it: the
scrape's 3s probe still starts on a starved host, and the outage was reported 22–32s after each
freeze.** Original text: When the host can't start the
probe at all, the call raises `HostOverloaded` and says "nothing can be said about the cluster".
That is true, and it also means a cluster that dies while the host is starved won't read as
unreachable until the host recovers. `OrchestratorHostOverloaded` is paging during that time.

## The fix

[ADR-0018](../../design/adr/0018-the-probe-measures-two-ends.md).

- **The probe measures two ends.** It runs kubectl at `-v=6` and reads client-go's own round
  trip from the response line. `nf_cluster_probe_seconds` is now that round trip, which is the
  cluster's time. The new `nf_orchestrator_probe_local_seconds` is the rest, which is ours. The
  cluster is judged busy on its own time only.
- **A probe that never started is inconclusive.** One killed with nothing logged says nothing
  about the cluster, and all of its time counts as the host's. One killed while waiting on the
  server (it had logged, but got no response) is still slow, as drill 10 needs.
- **The scrape's probe gets most of the budget.** It runs beside the work, so it is bounded at
  `NF_SCRAPE_PROBE_TIMEOUT` (3s) rather than the 0.75s a failed call can spare. On a starved
  host it now finishes in about 1.1s and can report both numbers.
- **`HostOverloaded`** is the outcome for a call that times out when the probe shows the cluster
  answering promptly and this host taking long, or when the host couldn't start the probe at all.
  It is a 503 with `Retry-After`, like busy, and the detail says which end is slow. In the
  scrape it counts under `nf_releases_unreported{reason="host"}` and never sets
  `nf_cluster_busy`.
- **`OrchestratorHostOverloaded`** is a warning that fires on
  `avg_over_time(nf_orchestrator_probe_local_seconds[2m]) >= 0.5` for 1m.
- If kubectl ever stops logging a readable round trip, the probe falls back to wall time. That
  would bring back day 29's behaviour, not remove the signal.

## Verification

Same starvation, fixed build:

| | Before | After |
| --- | --- | --- |
| `nf_cluster_busy` | 1 on every scrape | **0 on 8 of 8** |
| `nf_cluster_probe_seconds` | 0.80 – 0.88 (wall time, killed) | 0.034 – 0.053 (round trip) |
| `nf_orchestrator_probe_local_seconds` | — | 1.0 – 2.3 |
| `ClusterThrottlingOrchestrator` | firing | inactive |
| `OrchestratorHostOverloaded` | — | **firing**, active from 03:33:18Z |
| `ClusterUnreachable`, `OrchestratorScrapeFailing` | inactive | inactive |

About ten seconds after the burners stopped: busy 0, round trip 12ms, local 0.13s, every release
reported.

The regressions were re-run, because each is a case the new rule could have swallowed:

- **Drill 7's stalled TLS server:** `503 helm history did not answer within 3s`,
  `nf_cluster_reachable 0`, at the same ~4.0s. It still reads unreachable, because a killed probe
  against it has always logged its start.
- **Drill 10's queueing,** on this cluster with the same APF manifest. With pure queueing
  (452 responses served, all 200), busy was 1 on every scrape, the round trip was 1.03–2.2s and
  local time about 0.1s. The cluster is still blamed when the cluster is slow. This also answers
  what drill 10 left open: its load generators ran on this same machine, and the split shows the
  slowness it measured really was the server's.
- **Drill 9's rejection,** reached by accident: three load generators overflowed the queue
  (1,018 rejections with `Retry-After` 1–8s), and busy stayed 1 throughout.

## What this drill did not cover

- A starved host and a dead cluster at the same moment (finding 8).
- Memory or I/O pressure on the host rather than CPU. Only CPU was starved.
- A real separation between host and cluster. In this lab they share a machine, and the drill
  relied on CPU affinity to keep them apart. The unpinned `/readyz` check shows the affinity held,
  but it is a lab arrangement, not how anything is deployed.
