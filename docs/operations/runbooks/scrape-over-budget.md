# Runbook — scrape over budget

**Use when:** `NFReleaseUnreported` is firing, `nf_releases_unreported` is above zero, or the
orchestrator's Prometheus target shows a `lastScrapeDuration` close to 4 seconds.

**Severity:** Sev-1 for the releases named, Sev-2 overall. The orchestrator and the cluster may both
be healthy — but a release with no lifecycle series has **no alerting at all**: if it fails,
`NFFailed` cannot fire for it.

Every command here was run during
[drill 7](../postmortems/2026-09-14-drill-7-the-scrape-outgrows-its-budget.md) on 2026-09-14 against a
throwaway cluster with twenty releases, and during
[drill 8](../postmortems/2026-09-16-drill-8-a-slow-api-server.md) on 2026-09-16 against the same
cluster behind a proxy that made the API server slow. Steps not exercised there are marked
**(not drilled)**.

## How this incident presents

The scrape stops at its budget (4s by default) and reports what it finished. The target stays `up`,
so `OrchestratorScrapeFailing` does **not** fire. What you see instead:

```bash
curl -s localhost:8000/metrics | grep -E '^nf_releases_unreported|^nf_release_reported.* 0\.0'
```

```
nf_releases_unreported{reason="deadline"} 7.0
nf_releases_unreported{reason="error"} 0.0
nf_release_reported{release="load-nf-5"} 0.0
...
```

**Expect it to be the same releases every time.** Helm lists releases alphabetically, so the scrape
runs out of time at the same point on every pass. In the drill, `load-nf-5` to `load-nf-9` never had
a single series in six minutes. Do not wait for them to "come round".

## 1. Which reason?

- `reason="deadline"` — the scrape ran out of time. **Check `nf_cluster_busy` before going on.**
  If it is 1, the cluster is holding the orchestrator's requests and the deadline is a symptom, not
  the cause — go to [cluster throttling](cluster-throttling.md). Drill 10 saw every release miss the
  deadline with nothing failing, because APF was queueing rather than rejecting; before 2026-09-23
  that incident arrived here, where none of the steps below would have helped. Otherwise continue
  with step 2.
- `reason="busy"` — the API server was reachable and not serving the orchestrator. Go to
  [cluster throttling](cluster-throttling.md).
- `reason="host"` — the call ran long on the orchestrator's own machine while the cluster answered
  promptly. Go to [the orchestrator's host is overloaded](#the-orchestrators-host-is-overloaded).
  `reason="deadline"` alongside `OrchestratorHostOverloaded` means the same thing.
- `reason="error"` — reading that release failed while the others succeeded. The drill produced no
  errors, so this branch is covered by a unit test only **(not drilled)**. Read the release directly
  and the error comes back in the response:

```bash
curl -s -w ' http=%{http_code}' localhost:8000/deployments/<release>
```

A `503` there means the cluster is the problem: go to
[control plane unreachable](control-plane-unreachable.md).

## 2. Is it the release count, or the cluster?

```bash
curl -s -o /dev/null -w '%{time_total}s\n' localhost:8000/metrics
```

```bash
helm list --kube-context kind-nf-orchestrator -o json | python -c "import json,sys; print(len(json.load(sys.stdin)))"
```

That count stops at 256: `helm list` returns at most 256 releases and does not say so. If it prints
exactly 256, there are probably more — add `--max 1000`, or page with `--offset`. The orchestrator
pages since 2026-09-15.

Reference numbers from the development host, eight workers, measured 2026-09-15 after the scrape
went from four calls per release to two: 22 releases in 0.83s, 40 in 1.39s, 60 in 2.05s. (Drill 7's
build, before that change: 20 releases in 1.3-2.2s, and 60 in 4.28s — past the budget.) A scrape
pinned at the budget with far fewer releases than that means each call is slow, not that there are
too many of them — check the API server before adding workers.

## 3. Find out which releases have had no coverage, and for how long

In Prometheus:

```
count_over_time(nf_deployment_state{state="INSTANTIATED"}[6m])
```

Any listed release missing from that result has had no lifecycle series for six minutes. **Check
those releases by hand now**, before changing anything — nothing has been watching them:

```bash
curl -s localhost:8000/deployments/<release>
```

## 4. Do not reach for more workers first

`NF_SCRAPE_WORKERS` (default 8) is a **ceiling** on how many releases are read at once, not a target:
since 2026-09-16 the scrape starts with two and grows the wave only while the cluster keeps up
([ADR-0015](../../design/adr/0015-admit-scrape-work-in-waves.md)).

Raising it helps only when the cluster has capacity to spare and the release count is the problem.
When the cluster is the problem it makes things worse — drill 8 measured 16 workers reporting **zero**
of 30 releases against a slow API server where 2 workers reported six. Step 2 tells you which case
you are in: a scrape pinned at the budget with few releases means slow calls, not too many of them.

If the calls are slow, the fix is on the cluster's side, not here. Check the API server's own health
and latency before changing any setting in this project.

## 5. Do not raise the budget past the scrape timeout

`NF_SCRAPE_BUDGET` is 4s because Prometheus abandons a scrape at 5s, and a test fails if it is set at
or above that. A scrape that runs past the timeout is thrown away whole: every release loses its
series and `OrchestratorScrapeFailing` pages — which is exactly what drill 7 found before the budget
existed.

## The orchestrator's host is overloaded

**Use when:** `OrchestratorHostOverloaded` is firing, `reason="host"` is non-zero, or a `503` detail
says *the delay is the orchestrator's own host, not the cluster*.

Every step here was run during
[drill 11](../postmortems/2026-09-24-drill-11-the-orchestrator-host-is-starved.md) on 2026-09-24,
with the orchestrator's CPU starved and the cluster healthy.

**First, check which end is slow.** The probe reports both:

```bash
curl -s localhost:8000/metrics | grep -E '^nf_(cluster_probe_seconds|orchestrator_probe_local_seconds|cluster_busy)'
```

```
nf_cluster_busy 0.0
nf_cluster_probe_seconds 0.039
nf_orchestrator_probe_local_seconds 1.305
```

`nf_cluster_probe_seconds` is the cluster's round trip, as client-go measured it. The drill saw
34–53ms here, and 5–8ms idle. `nf_orchestrator_probe_local_seconds` is everything else, mostly
starting kubectl: 1.0–2.3s in the drill, and about 0.1s idle. **If the first number is low and the
second is high, leave the cluster alone.** If both are high, both ends are struggling. Deal with the
host first, because while it is starved the cluster's number is the less reliable of the two.

**Then find what is using the machine.** On the Windows host that ran the drill:

```powershell
Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,Id,CPU
```

In the drill, this was eight `python3.11` processes, each a busy loop pinned to the orchestrator's
core. Anything pinned to the same core as the orchestrator hurts more than its share: the kubectl
and helm processes the orchestrator starts inherit its CPU affinity. Check the orchestrator's own
affinity too:

```powershell
(Get-Process -Id (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess).ProcessorAffinity
```

The drill's value was `1` (core 0 only). An unpinned process on this 16-thread machine shows
`65535`.

**Expect the alert to clear about a minute after the load goes.** The metrics were back to normal
in the first scrape, about 10 seconds after the burners stopped. The alert averages over two
minutes. **(the alert clearing was not watched to the end)**

**What does not help:** more workers (the host is already short of CPU), a bigger budget (the
scrape timeout is fixed at 5s), or anything on the cluster.

## What this runbook does not cover

- **A release on the boundary.** One that is reported on some scrapes and not others holds no alert
  window, including `NFReleaseUnreported`. It shows in the `deadline` count. Drill 7, finding 4.
- **A cache.** The structural fix when concurrency and two calls per release stop being enough —
  [ADR-0014](../../design/adr/0014-the-scrape-has-one-budget.md). Not built. (Reducing calls per
  release, the other one listed there, was built on 2026-09-15.)
- **An API server saturated by real traffic.** Drill 8 imposed latency and a queue with a proxy,
  which reproduces the timing but not the causes — etcd contention or a slow admission webhook. The
  API server's own load shedding was drilled separately and has its own runbook:
  [cluster throttling](cluster-throttling.md).
