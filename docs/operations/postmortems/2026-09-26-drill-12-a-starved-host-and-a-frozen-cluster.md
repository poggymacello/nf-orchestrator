# Postmortem — drill 12: a starved host and a frozen cluster at once

**Date:** 2026-09-26 · **Duration of the staged incident:** two freezes, ~4 minutes each, on a host starved throughout · **Severity:** Sev-1 (staged: a real outage)

**Summary:** Drill 11 ended with a limit I accepted: when the orchestrator's host is too starved to
start kubectl, "nothing can be said about the cluster". That would include a cluster that is actually
down. This drill combined the two failures — drill 11's starved host and drill 6's frozen control
plane — to see whether the outage page would fire.

It did. The accepted limit was wrong. But the two alerts **around** the outage both misbehaved:

- The alert for the starved host **cleared in the middle of the outage**, with the host as starved
  as before.
- The throttling alert went pending during a real outage. It **missed paging "throttling" by about
  two seconds**, and no test would have caught it: the alert rules had never been evaluated against
  any series in any test.

## What was staged

A throwaway kind cluster (`nf-drill12`, v1.35.0, port 45434) with ten releases. The orchestrator and
eight busy loops were pinned to one core, as in drill 11. Once `OrchestratorHostOverloaded` was
firing, the control-plane container was frozen with `docker pause`. About four minutes later it was
unfrozen, with the host still starved. Then the load stopped.

The starvation was heavier than on day 30. Some scrapes took up to 7.2s against Prometheus's 5s
timeout, and one scrape probe didn't start within its 3s. The drill also taught a method lesson:
manually `curl`ing `/metrics` runs a second collection beside Prometheus's on the same starved core,
so it makes the incident being measured worse. After the freeze, everything was read from
Prometheus.

## Hypotheses, and what happened

| | Predicted | Observed |
| --- | --- | --- |
| H1: status route | `HostOverloaded` for as long as the host is starved, because its 0.75s probe can't start kubectl | **Wrong, in the good direction.** 503 with the "moments ago" busy wording for the first 30s, then `helm history did not answer within 3s` — unreachable. The scrape's longer probe fills the shared cache, so the route reuses a probe that started. |
| H2: scrape | `nf_cluster_reachable 0` once the 30s recent-success window expires, contradicting day 30's finding 8 | **Held.** 0 from 32s after the first freeze and 24s after the second; `ClusterUnreachable` fired both times |
| H3: probe ordering | The listing's evidence check waits for the scrape's probe rather than starting its own | Consistent with every observation; not isolated separately |
| H4: both alerts at once | Correct reading: two things are wrong | **Failed before the fix** (finding 1) |
| H5: recovery | Unfreezing while starved brings back reachable 1, busy 0 | **Held**, within 8–10s both times |

## Findings

**1. The host alert cleared in the middle of the outage.** `nf_orchestrator_probe_local_seconds`
stopped at 15:47:05 and didn't return until the node was unfrozen. Day 30 could only compute the
host's share by subtracting the round trip from the wall time. A frozen server never answers, so there
is no round trip, and the series disappeared. `OrchestratorHostOverloaded` then resolved with the
host still starved. **Fixed.**

**2. The throttling alert almost paged during a real outage.** From 15:47:10 to 15:47:35 the
orchestrator still had recent evidence that the cluster had answered. Every timed-out call therefore
read as busy, so there were six `nf_cluster_busy 1` samples before reachability went to 0. After
that, no busy series at all. Day 29's rule was `avg_over_time(nf_cluster_busy[2m]) >= 0.5`, and
`avg_over_time` averages only the samples that exist. As the zeros from before the freeze aged out
of the window, the sample count fell from 17 to 1, and the six 1s became 100% of it:

```
avg_over_time(nf_cluster_busy[2m]): 47:05=0.06 47:35=0.41 48:35=0.5 48:55=0.75 49:05=1 ... 49:30=1
count_over_time(nf_cluster_busy[2m]): 47:05=17 ... 48:35=12 ... 49:05=6 ... 49:30=1
```

The alert went pending at 15:48:33 and needed one minute. The last busy sample aged out at 15:49:35.
It was two seconds short, not correct. promtool reproduces the firing with seven busy samples instead
of six. **Fixed.**

**3. No alert rule had ever been executed by a test.** Drills checked alerts by watching Prometheus
during an incident, which only covers the incident each drill happened to stage. Finding 2 was a
property of the expression that no drill had provoked, and reading the rule didn't reveal it. The
rules are now unit-tested with promtool in CI, fed the series the drills produced. **Fixed.**

**4. Day 30's finding 8 was wrong. Retired.** "A starved host hides the cluster" held for a single
probe held to 0.75s. It did not hold for the system: the scrape's 3s probe starts even on a starved
host, a started probe that gets no answer counts as slow rather than inconclusive, and the outage was
reported 22–32 seconds after each freeze. The accepted-limits count goes down by one.

## The fix

- **The host's share survives a silent server.** kubectl stamps its first log line with the local
  time, to the microsecond, as soon as it has loaded its config. That stamp minus the spawn time is
  how long the host took to get kubectl going. Measured before relying on it: **0.05–0.08s** idle,
  **1.07–1.50s** starved with a healthy server, and **0.97–1.20s** starved with the server frozen.
  It is now the host's share whenever there is no round trip, and the unreachable path publishes it
  instead of returning early with nothing. Midnight wrap-around is handled and tested.
- **The throttling share is taken over every scrape attempted:**

  ```promql
  (sum_over_time(nf_cluster_busy[2m]) / count_over_time(up{job="nf-orchestrator"}[2m]))
    >= 0.5 unless on(instance, job) nf_cluster_reachable == 0
  ```

  `up` has a sample for every scrape, whether it succeeded or not, so a missing busy sample counts
  as not busy. An unreachable cluster is not a throttling one.
- **`monitoring/tests/`** replays three series in promtool:
  - Drill 12's outage: must not page throttling. The old rule fails this.
  - Drill 10's sustained throttling: reaches 1.
  - Drill 10's flapping throttling: reaches 2/3.

  CI runs these, together with `promtool check rules`, in `lint-test`. A pytest keeps the expressions
  the tests copy identical to the rules they came from.
  [ADR-0019](../../design/adr/0019-alert-rules-are-tested-against-drill-series.md).

## Verification

The same combined incident on the fixed build. Alert states were recorded every 15 seconds for 4½
minutes after the freeze at 16:00:36:

| | Before | After |
| --- | --- | --- |
| `ClusterUnreachable` | pending 15:47:53 → firing | pending 16:00:58 → firing by 16:02:15 |
| `OrchestratorHostOverloaded` | resolved during the freeze | **firing throughout** (from 15:59:23) |
| `nf_orchestrator_probe_local_seconds` during the freeze | absent | 0.98–1.41s every scrape |
| `ClusterThrottlingOrchestrator` | pending 15:48:33, missed firing by ~2s | **inactive throughout** |

An honest caveat: the second run didn't reproduce the six busy samples, so the old rule might have
stayed quiet on it too. The rule fix's evidence is the promtool replay, where the old expression
fires and the new one doesn't. The live run shows the new expression doing no harm.

After unfreezing (host still starved): reachable 1, busy 0, round trip 54ms, host share 1.26s,
within 8 seconds.

## Still true, re-measured

Day 30's finding 7 recurred, heavier: 4 failed scrapes in six minutes. `OrchestratorScrapeFailing`
went pending twice and never fired. It remains accepted — the orchestrator can't make its own host
faster — and the host alert is the page that names the cause.

## What this drill did not cover

- The request path when no scrape has warmed the probe cache recently, for example with Prometheus
  down. H1's prediction would then apply: `HostOverloaded` until the host recovers.
- A cluster that is stopped (connection refused) rather than frozen, on a starved host.
- Memory or I/O pressure. Still only CPU.
