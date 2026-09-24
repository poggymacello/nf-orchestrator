# Postmortem — drill 10: the API server queues instead of refusing

**Date:** 2026-09-23 · **Duration of the staged incident:** ~95 minutes · **Severity:** Sev-2 (staged)

**Summary:** Kubernetes API Priority and Fairness was configured with `limitResponse: Queue`
instead of drill 9's `Reject`, so the API server held the orchestrator's requests rather than
turning them away. Nothing failed: no 429, no `Retry-After`, no error text anywhere. Every call
simply took longer. The orchestrator reported that as its own problem — the cluster reachable, not
busy, and eight to ten of ten releases unreported for `reason="deadline"` — which is drill 7's
incident and sends whoever is paged to a runbook that fixes nothing here. Intermittently it
reported something worse: `nf_cluster_reachable 0`, the page for a dead control plane, while its
own `/readyz` route answered `200 reachable` in the same minute.

## What was staged

A throwaway kind cluster (`nf-drill10`, v1.35.0) on API port 45432, ten `drill10-*` releases, and
the orchestrator running as the `nf-orchestrator` service account rather than as kind's admin
identity, which APF exempts from everything. The service account's FlowSchema pointed at a priority
level with `nominalConcurrencyShares: 1` — **3 seats**, against 244 for `workload-low` — and:

```yaml
limitResponse:
  type: Queue
  queuing: {queues: 1, handSize: 1, queueLengthLimit: 200}
```

Load came from the same generator as drill 9 (the account's own token, direct HTTPS, no process
start-up), plus 60 ballast secrets of 40 KB each so that every list was expensive enough to hold a
seat. The manifest is [`manifests/apf-queue-drill.yaml`](../manifests/apf-queue-drill.yaml).

Two things had to be tuned before the drill said anything. At 40 threads with an empty namespace
the server absorbed everything — 600 requests a second, all `200`, the orchestrator's scrape
unchanged at 1.4s. With the ballast and 160 more threads the queue filled and the server started
rejecting after all: `429` with `Retry-After` 1, 2, 4 and 8 seconds, 210 of them. **A queue that
overflows becomes drill 9**, which is worth knowing but is not what this drill was for, so
`queueLengthLimit` went from 50 to 200 and the rejections stopped. From there on the incident was
pure waiting: 11,622 and 8,916 requests served, `retry-after={}`, not one refusal.

## What the orchestrator did (before the fix)

| Signal | Healthy | Under queueing |
| --- | --- | --- |
| Scrape duration | 0.90s | 3.3 – 4.2s |
| Releases unreported | 0 | 8 – 10 of 10, all `reason="deadline"` |
| `nf_cluster_reachable` | 1 | 1, dropping to **0** on some scrapes |
| `nf_cluster_busy` | 0 | **0** |
| `/deployments/<nf>` | 200 | 200, or **503 `helm history did not answer within 3s`** |
| `/readyz` (the route) | 200 | 200 `{"status":"ready","cluster":"reachable"}` |

The last two rows are the same orchestrator seconds apart: one route saying the cluster could not
be reached, the other saying it was reachable.

## Findings

**1. Queued back-pressure is indistinguishable from the orchestrator being too slow.** Every
lifecycle series disappeared and the only thing that said so was `nf_releases_unreported{reason=
"deadline"}`, whose runbook is [scrape over budget](../runbooks/scrape-over-budget.md) — about too
many releases and too little time, which is how drill 7 read and what day 26 and 27 fixed. Nothing
pointed at the cluster, because in a queueing incident the cluster never says anything. Day 28's
busy signal is derived from errors, and there were no errors. **Fixed.**

**2. The readiness probe is not fast merely because it is exempt.** Day 28 rests on `/readyz` being
served outside the throttled flow, and it is: the API server's own counters show the probe landing
on the built-in `probes` FlowSchema at the `exempt` priority level (+18 dispatched while this drill
made 5 probe calls), never on the drill's level. It was still slow — **0.45s to 3.58s, mean 1.65s
over 8 samples**, against 0.06 – 0.25s idle — because an API server saturated with expensive lists
is slow on every path it serves. Since the probe is bounded at 0.75s, most probes during the
incident did not answer inside their bound, and "the probe did not answer" was the only test day 28
had for "the cluster is gone". So the fix that made drill 9 legible turned drill 10 into a
control-plane outage. **Fixed.**

**3. One probe per timed-out call.** The probe was asked from inside each failing call, so a wave
of eight stalled releases started eight kubectl processes asking the same question in the same
instant, of a server already over capacity. **Fixed.**

**4. The busy signal flaps, and `== 1 for 2m` cannot see it.** At moderate load the probe hovered
around its threshold and `nf_cluster_busy` alternated 1, 0, 1, 0 between scrapes.
`ClusterThrottlingOrchestrator` requires the condition to hold for a continuous two minutes, so it
would have stayed silent through an incident its own metric was describing. **Fixed in the rule.**

**5. The test guard could not see breaches on a worker thread.** The scrape now measures the probe
on a thread of its own. `tests/conftest.py` raises an `AssertionError` when a unit test shells out
to a real cluster — and an exception raised on a worker thread is a *warning* in pytest's output,
not a failure. The guard that exists because day 26's check stopped checking had the same hole.
**Fixed:** breaches are recorded and asserted after the test returns.

**6. `Retry-After: 5` is still a guess. Accepted.** A queueing server publishes no hint about how
long the wait will be — there is no header to read and no value to copy. Five seconds remains what
day 28 chose, and callers are told plainly that it is the orchestrator's number, not the cluster's.

**7. A cluster that dies just after answering reads busy before it reads unreachable. Accepted,
and measured.** This is the cost of finding 2's fix: a successful call is treated as evidence of
reachability for 30 seconds. Freezing the node at 08:30:01, seven seconds after its last successful
call, gave `503 ... the cluster answered another call moments ago` until 08:30:21 and
`503 helm history did not answer within 3s` from 08:30:25 — about 24 seconds of the wrong label on
a real outage, bounded by `NF_RECENT_SUCCESS=30` and settable to 0 to switch the evidence off.
`ClusterUnreachable` waits a minute for its own reasons, so the page arrives up to 30 seconds late.
Mislabelling minutes of throttling as an outage was judged the worse error of the two.

## The fix

**Reachability is a claim that needs evidence, and one source of evidence is not enough**
([ADR-0017](../../design/adr/0017-reachability-needs-more-than-one-witness.md)). A call killed for
silence is now called busy if *either* the probe answers *or* the cluster completed some other call
within the last 30 seconds. Both are cheap, neither is the same measurement, and a cluster that is
frozen, stopped or stalled satisfies neither.

**A probe's duration is evidence in its own right.** `/readyz` is the cheapest thing the API server
does; how long it takes to say `ok` is a measurement of how well this client is being served. Each
scrape times one probe on a thread beside the work, publishes it as `nf_cluster_probe_seconds`, and
reports `nf_cluster_busy 1` when it is slow (≥ 0.5s) — even when every release was read
successfully. That is what makes queueing visible while nothing is failing.

**The probe is asked once.** Answers are cached for `PROBE_TTL` (2s), and the scrape's own probe
populates that cache, so a wave of timed-out calls shares one answer.

## Verification, under the same load

| | Before | After |
| --- | --- | --- |
| `nf_cluster_reachable` | 1, intermittently 0 | 1 throughout |
| `nf_cluster_busy` | 0 | 1, on 94.7% of scrapes over two minutes |
| `nf_cluster_probe_seconds` | — | 0.25 – 0.80 (0.06 idle) |
| `/deployments/<nf>` when killed | `503 did not answer within 3s` | `503 ... reachable and not serving this client` + `Retry-After: 5` |
| `ClusterThrottlingOrchestrator` | inactive | **firing at 08:22:48Z**, active from 08:21:48Z |
| `ClusterUnreachable` | pending on some scrapes | inactive throughout |

Regressions re-run afterwards, because each of these is a case the new evidence could have
swallowed:

- Drill 7's stalled TLS server (handshake completes, nothing ever answers): `503 helm history did
  not answer within 3s`, `nf_cluster_reachable 0`. Unchanged, at the same ~4.0s cost as day 28.
- A frozen node (`docker pause`, drill 6): `nf_cluster_reachable 0` once the 30-second window
  expired, as finding 7 records.
- Recovery: within 8 seconds of `docker unpause`, `reachable 1 busy 0`, probe 0.079s.

## What this drill did not cover

- A queue that overflows into rejection was seen briefly and then tuned away. It is drill 9's
  incident arriving through drill 10's door, and the two signals now coexist — worth a drill of its
  own only if the mixed case behaves differently from either.
- `nf_cluster_probe_seconds` measures the orchestrator's host as well as the cluster: a machine too
  busy to start kubectl promptly looks like a server too busy to answer it. Both are "the
  orchestrator is not being served", which is what the alert says, but the two causes are not
  separated here. *Since answered by
  [drill 11](2026-09-24-drill-11-the-orchestrator-host-is-starved.md): the probe now reports the
  cluster's round trip and this host's share separately. A re-run of this drill's queueing on
  2026-09-24 put the round trip at 1.0–2.2s and the host's share at about 0.1s, so the slowness
  measured here was the server's.*
- The persistent cluster was untouched throughout; it is still unreachable behind a Windows port
  reservation, as [control plane unreachable](../runbooks/control-plane-unreachable.md) step 4
  describes.
