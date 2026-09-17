# Operations

How this project is operated when it breaks: the drills that break it deliberately, the runbooks
written from those drills, and the blameless postmortems that record what the failures revealed.

This is a **self-directed learning project** on a single-node kind cluster. There is no on-call
rota and no user traffic. "Incident" here means a deliberate failure drill, run against the real
system, with the response written down as if it were real.

## Why drills come before runbooks

A runbook written from imagination documents the system you think you built. Every runbook in this
directory was written **after** a drill, from the commands run during it. Where a step could not be
drilled but the runbook would be incomplete without it, the step is marked **(not drilled)** rather
than presented as tested. Where a drill showed the orchestrator reporting something misleading, the
runbook says so instead of pretending the tool is trustworthy.

## The drill process

1. **Pick one failure mode** and write down the expected behaviour before touching anything.
2. **Run it against the real cluster**, capturing timestamped output rather than summarising.
3. **Compare** what the orchestrator reported against what the cluster was actually doing.
4. **Write the postmortem** from the captured output, blameless, with the surprises kept in.
5. **Write or update a runbook** only for the steps that were actually used.
6. **Record action items** as findings. Fixes land in their own commits, not in the drill.
7. **Re-run the drill after the fix**, and update the runbook so it stops warning about behaviour
   that no longer exists. A runbook describing a fixed bug is as misleading as one describing an
   imagined system.

Drill 3 added a corollary to step 5. A command can be genuinely executed during a drill and still be
documented for the wrong situation: the "resubmit the intent" step was run for real, but in a drill
where nothing had touched the Deployment, so it succeeded for reasons unrelated to the repair it was
later recommended for. Ask what the step is being claimed to fix, not just whether it ran.

## Drills run so far

| # | Drill | Date | Outcome | Postmortem |
|---|---|---|---|---|
| 1 | Pods killed mid-deploy | 2026-08-20 | 4 findings — the reconciler reported `INSTANTIATED` without ever checking how many replicas the intent asked for. 1-3 fixed 2026-08-21, 4 answered 2026-08-26 | [drill-1](postmortems/2026-08-20-drill-1-pods-killed-mid-deploy.md) |
| 2 | Control plane unreachable | 2026-08-20 | 4 findings — a running NF was reported as `NOT_INSTANTIATED` with HTTP 200 while the cluster was unreachable. 1-3 fixed 2026-08-21; 4, the disk-drill prerequisite, closed 2026-09-12 by drill 5 | [drill-2](postmortems/2026-08-20-drill-2-control-plane-unreachable.md) |
| 3 | Repairing drift the orchestrator did not cause | 2026-08-26 | 3 findings — resubmitting the intent to repair a Deployment scaled outside Helm fails on a server-side apply conflict, and the failed upgrade makes a running workload read `FAILED`. All findings fixed by 2026-09-01 | [drill-3](postmortems/2026-08-26-drill-3-repairing-drift-outside-helm.md) |
| 4 | Steering the translator with its own input | 2026-09-09 | 3 findings — every injection was refused, but by the order of a tuple in the source rather than by design; the same accident made ordinary text silently wrong ("from staging to prod" → staging). 1 and 2 fixed, 3 accepted as a limit of validation | [drill-4](postmortems/2026-09-09-drill-4-steering-the-translator-with-text.md) |
| 5 | Disk pressure and eviction | 2026-09-12 | 3 findings — under real eviction the state was right and the reason discarded, and a **recovered** NF reported `FAILED` forever because Kubernetes never deletes an evicted pod. 1 and 2 fixed, 3 accepted | [drill-5](postmortems/2026-09-12-drill-5-disk-pressure-and-eviction.md) |
| 6 | A frozen control plane | 2026-09-13 | 3 findings — cluster calls were bounded only by Go's 10s TLS handshake default, twice Prometheus's scrape timeout, so the `ClusterUnreachable` alert had **no data** during the outage it exists for; and no rule covered a failed scrape. All fixed, and both alerts seen firing | [drill-6](postmortems/2026-09-13-drill-6-frozen-control-plane.md) |
| 7 | A stalled API server, and a scrape that outgrew its budget | 2026-09-14 | 4 findings — drill 6's bound held against a post-handshake stall, but it was per call: twenty **healthy** releases took the scrape to 8.4s and `OrchestratorScrapeFailing` paged for an orchestrator that was up; and past a deadline the same releases went unreported every time, with nothing naming them. 1-3 fixed, 4 accepted | [drill-7](postmortems/2026-09-14-drill-7-the-scrape-outgrows-its-budget.md) |
| 8 | An API server that is slow, not silent | 2026-09-16 | 3 findings — the scrape budget held, but day 25's concurrency **made things worse** under load: against a slow, queue-limited API server, 16 workers left all 30 releases half-read and **none** reported, while 2 workers reported six. 1 fixed by admitting work in waves, 2 and 3 accepted | [drill-8](postmortems/2026-09-16-drill-8-a-slow-api-server.md) |
| 9 | The API server sheds load | 2026-09-17 | 3 findings — under real API Priority and Fairness shedding (429, `Retry-After` up to 32s, `/readyz` still instant) every call was reported as **unreachable**, the page for a dead control plane; and the orchestrator's own concurrency throttled itself against a 3-seat allowance. 1 and 2 fixed, 3 accepted with a verified setting | [drill-9](postmortems/2026-09-17-drill-9-the-api-server-sheds-load.md) |
| — | Node disk full | — | Abandoned on 2026-08-20 and superseded by drill 5, which turned eviction back on and calibrated the threshold to the host's free space instead of filling 760 GiB to reach a conventional one | — |

## Runbooks

| Runbook | Use when |
|---|---|
| [Deployment not reaching INSTANTIATED](runbooks/deployment-not-reaching-instantiated.md) | A deployment is stuck in `INSTANTIATING`, flapping, or reporting `INSTANTIATED` you do not believe |
| [Control plane unreachable](runbooks/control-plane-unreachable.md) | Requests that need the cluster return `503`, or `/readyz` is failing |
| [Node under disk pressure](runbooks/node-under-disk-pressure.md) | A deployment reads `FAILED` with pods showing `reason: Evicted`, or the node reports `DiskPressure=True` |
| [Cluster throttling the orchestrator](runbooks/cluster-throttling.md) | `ClusterThrottlingOrchestrator` is firing, or a `503` carries `Retry-After` and says the API server is reachable |
| [Scrape over budget](runbooks/scrape-over-budget.md) | `NFReleaseUnreported` is firing, or `nf_releases_unreported` is above zero |

## Severity

Two levels are enough for a project this size.

| Severity | Meaning | Response |
|---|---|---|
| **Sev-1** | The orchestrator reports state that is wrong, not just unavailable | Stop, capture the raw signals, write a postmortem. A monitoring system that lies is worse than one that is down |
| **Sev-2** | The orchestrator is unavailable or refuses work, and says so | Follow the runbook, note the duration |

All nine drills turned up Sev-1 behaviour, which is the point of running them. Every **defect**
they found is now fixed, across ADRs [0006](../design/adr/0006-instantiated-means-the-intent-is-satisfied.md),
[0007](../design/adr/0007-stability-is-an-alerting-concern.md),
[0008](../design/adr/0008-forcing-field-ownership-is-an-explicit-operation.md) and
[0009](../design/adr/0009-the-operation-is-not-the-network-function.md) for drills 1-3, in the
translator for drill 4, in the reconciler's derivation for drill 5, in the subprocess bounds and
alert rules for drill 6, in the scrape budget of
[ADR-0014](../design/adr/0014-the-scrape-has-one-budget.md) for drill 7, in the wave admission of
[ADR-0015](../design/adr/0015-admit-scrape-work-in-waves.md) for drill 8, and in the busy
classification of [ADR-0016](../design/adr/0016-busy-is-not-unreachable.md) for drill 9. Each drill's action-item
table says which change closed which finding.

Five items stay open and none is a defect. Drill 9's finding 3: a scrape concurrency ceiling above
the orchestrator's APF allowance throttles it by itself, so the ceiling is a setting to match to the
cluster, verified in the drill and written into the runbook. Drill 8's findings 2 and 3 are one item: under sustained
overload most releases stay unreported, which the per-release alert reports truthfully, and the
concurrency that suits a cluster is measured per scrape rather than configured. Drill 7's finding 4: a release on the scrape deadline's
boundary is reported on some scrapes and not others, so no alert window holds for it; it still shows
in the `deadline` count. Drill 4's finding 3: a well-formed but wrong candidate
passes every validation check — a limit of validation itself, which only the separation between
translating and deploying addresses. Drill 5's finding 3: the pods array carries the whole eviction
history, which is the evidence of what happened and is kept on purpose.

The drills kept finding one mistake in different places: two things that are usually equal,
collapsed into a single value, diverging only during an incident. Pod phase versus container
readiness; release-absent versus cluster-unreachable; intent submitted versus intent applied;
operation failed versus network function failed. Four instances across three drills, each invisible
until the system was under stress, which is the argument for running drills at all.

Drill 4 added a different lesson: three probes came back green and nearly became a claim that the
translator resists injection. It did not — it had an arbitrary preference that happened to be safe
that day, and the same arbitrariness was answering ordinary sentences wrongly. Ask why a gate held,
not just whether it held.

Drill 5 added a third: a fix can be correct for the case it was written for and wrong for the next
one. The rule that a terminal pod means failure was added on day 15 to close drill 1's finding 2,
and on day 22 it made a fully recovered network function report `FAILED` forever. Nothing between
those two drills exercised the difference, which is the argument for running drills that are not
re-runs of the last one.

Drills 6 and 7 added a fourth: a timeout is a budget shared with everything downstream that is also
counting. Drill 6 bounded each call inside Prometheus's five seconds and was right to; drill 7 found
nothing bounded the sum, and a cluster with nothing wrong except twenty releases paged as an outage.
The alert added on day 24 to catch a failing scrape was, on day 25, the alert giving the wrong
diagnosis — correct about the symptom, wrong about the cause.

Drill 8 sharpened drill 7's lesson into its opposite case. Drill 7 said a scrape must not do less
work than the releases need; drill 8 said it must not do more work than the cluster can serve. Both
are the same rule from different sides: the budget belongs to whoever owns the total, and spending it
on work that cannot finish buys nothing. It also made a fix from eleven days earlier into a defect
without changing a line of it — concurrency was right for a cluster with spare capacity and wrong for
one without, and only a drill on the second kind could tell them apart.

Alerting rules built from these drills live in
[`monitoring/nf-lifecycle.rules.yml`](../../monitoring/nf-lifecycle.rules.yml). Every one carries a
`for:` window, which is where "stable" is defined — see ADR-0007.

## Conventions

- Timestamps in drill output are local time (UTC+7) unless the output itself carries a `Z`.
- Postmortems are blameless: they describe what the system did, not who typed what.
- Findings are numbered per drill and carried into the postmortem's action-item table. Nothing is
  marked fixed here until the fix is committed and linked.
