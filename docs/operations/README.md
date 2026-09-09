# Operations

How this project is operated when it breaks: the drills that break it deliberately, the runbooks
written from those drills, and the blameless postmortems that record what the failures revealed.

This is a **self-directed learning project** on a single-node kind cluster. There is no on-call
rota and no user traffic. "Incident" here means a deliberate failure drill, run against the real
system, with the response written down as if it were real.

## Why drills come before runbooks

A runbook written from imagination documents the system you think you built. Every runbook in this
directory was written **after** a drill, and every command in it was run against the cluster during
that drill. Where a drill showed the orchestrator reporting something misleading, the runbook says
so instead of pretending the tool is trustworthy.

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
| 2 | Control plane unreachable | 2026-08-20 | 4 findings — a running NF was reported as `NOT_INSTANTIATED` with HTTP 200 while the cluster was unreachable. 1-3 fixed 2026-08-21; 4 is the disk-drill prerequisite, still open | [drill-2](postmortems/2026-08-20-drill-2-control-plane-unreachable.md) |
| 3 | Repairing drift the orchestrator did not cause | 2026-08-26 | 3 findings — resubmitting the intent to repair a Deployment scaled outside Helm fails on a server-side apply conflict, and the failed upgrade makes a running workload read `FAILED`. All findings fixed by 2026-09-01 | [drill-3](postmortems/2026-08-26-drill-3-repairing-drift-outside-helm.md) |
| 4 | Steering the translator with its own input | 2026-09-09 | 3 findings — every injection was refused, but by the order of a tuple in the source rather than by design; the same accident made ordinary text silently wrong ("from staging to prod" → staging). 1 and 2 fixed, 3 accepted as a limit of validation | [drill-4](postmortems/2026-09-09-drill-4-steering-the-translator-with-text.md) |
| — | Node disk full | — | Abandoned before running; kind disables kubelet disk eviction, so the drill cannot produce the failure it is meant to produce. Reasoning in [drill-2](postmortems/2026-08-20-drill-2-control-plane-unreachable.md#why-this-drill-replaced-the-disk-full-drill) | — |

## Runbooks

| Runbook | Use when |
|---|---|
| [Deployment not reaching INSTANTIATED](runbooks/deployment-not-reaching-instantiated.md) | A deployment is stuck in `INSTANTIATING`, flapping, or reporting `INSTANTIATED` you do not believe |
| [Control plane unreachable](runbooks/control-plane-unreachable.md) | Deploys return `502 kubernetes cluster unreachable`, or every deployment suddenly reads `NOT_INSTANTIATED` |

## Severity

Two levels are enough for a project this size.

| Severity | Meaning | Response |
|---|---|---|
| **Sev-1** | The orchestrator reports state that is wrong, not just unavailable | Stop, capture the raw signals, write a postmortem. A monitoring system that lies is worse than one that is down |
| **Sev-2** | The orchestrator is unavailable or refuses work, and says so | Follow the runbook, note the duration |

All four drills turned up Sev-1 behaviour, which is the point of running them. Every **defect**
they found is now fixed, across ADRs [0006](../design/adr/0006-instantiated-means-the-intent-is-satisfied.md),
[0007](../design/adr/0007-stability-is-an-alerting-concern.md),
[0008](../design/adr/0008-forcing-field-ownership-is-an-explicit-operation.md) and
[0009](../design/adr/0009-the-operation-is-not-the-network-function.md) for drills 1-3, and in the
translator itself for drill 4. Each drill's action-item table says which change closed which
finding.

Two items stay open and neither is a defect: drill 2's action item 4, the eviction threshold a real
disk drill would need before it has any failure to observe; and drill 4's finding 3, that a
well-formed but wrong candidate passes every validation check — a limit of validation itself, which
only the separation between translating and deploying addresses.

The drills kept finding one mistake in different places: two things that are usually equal,
collapsed into a single value, diverging only during an incident. Pod phase versus container
readiness; release-absent versus cluster-unreachable; intent submitted versus intent applied;
operation failed versus network function failed. Four instances across three drills, each invisible
until the system was under stress, which is the argument for running drills at all.

Drill 4 added a different lesson: three probes came back green and nearly became a claim that the
translator resists injection. It did not — it had an arbitrary preference that happened to be safe
that day, and the same arbitrariness was answering ordinary sentences wrongly. Ask why a gate held,
not just whether it held.

Alerting rules built from these drills live in
[`monitoring/nf-lifecycle.rules.yml`](../../monitoring/nf-lifecycle.rules.yml). Every one carries a
`for:` window, which is where "stable" is defined — see ADR-0007.

## Conventions

- Timestamps in drill output are local time (UTC+7) unless the output itself carries a `Z`.
- Postmortems are blameless: they describe what the system did, not who typed what.
- Findings are numbered per drill and carried into the postmortem's action-item table. Nothing is
  marked fixed here until the fix is committed and linked.
