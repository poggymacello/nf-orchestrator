# Runbook — scrape over budget

**Use when:** `NFReleaseUnreported` is firing, `nf_releases_unreported` is above zero, or the
orchestrator's Prometheus target shows a `lastScrapeDuration` close to 4 seconds.

**Severity:** Sev-1 for the releases named, Sev-2 overall. The orchestrator and the cluster may both
be healthy — but a release with no lifecycle series has **no alerting at all**: if it fails,
`NFFailed` cannot fire for it.

Every command here was run during
[drill 7](../postmortems/2026-09-14-drill-7-the-scrape-outgrows-its-budget.md) on 2026-09-14, against
a throwaway cluster with twenty releases. Steps not exercised there are marked **(not drilled)**.

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

- `reason="deadline"` — the scrape ran out of time. Continue with step 2.
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

On the drill host, with eight workers, twenty releases scraped in 1.3-2.2s. A scrape pinned at the
budget with a similar count means each call is slow, not that there are too many of them — check
the API server before adding workers. With one worker, twenty releases took 8.4s unbounded.

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

## 4. Give the scrape more concurrency (not drilled beyond 1 and 8)

`NF_SCRAPE_WORKERS` sets how many releases are reconciled at once. The drill ran 1 (serial, 7 of 20
unreported) and 8 (the default, 0 unreported). Raising it further was not tried. Each worker is a
kubectl or helm process against the API server, so on a cluster that is already slow, more workers
is more load on the thing that is slow.

Restart the orchestrator with the new value and repeat step 1.

## 5. Do not raise the budget past the scrape timeout

`NF_SCRAPE_BUDGET` is 4s because Prometheus abandons a scrape at 5s, and a test fails if it is set at
or above that. A scrape that runs past the timeout is thrown away whole: every release loses its
series and `OrchestratorScrapeFailing` pages — which is exactly what drill 7 found before the budget
existed.

## What this runbook does not cover

- **A release on the boundary.** One that is reported on some scrapes and not others holds no alert
  window, including `NFReleaseUnreported`. It shows in the `deadline` count. Drill 7, finding 4.
- **Reducing calls per release, or a cache.** The structural fixes when concurrency stops being
  enough — [ADR-0014](../../design/adr/0014-the-scrape-has-one-budget.md). Not built.
- **An API server slow under real load.** The drill simulated growth and a silent connection, not a
  busy control plane. **(not drilled)**
