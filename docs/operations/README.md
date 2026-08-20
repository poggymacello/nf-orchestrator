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

## Drills run so far

| # | Drill | Date | Outcome | Postmortem |
|---|---|---|---|---|
| 1 | Pods killed mid-deploy | 2026-08-20 | 3 findings — the reconciler reports `INSTANTIATED` without ever checking how many replicas the intent asked for | [drill-1](postmortems/2026-08-20-drill-1-pods-killed-mid-deploy.md) |
| 2 | Control plane unreachable | 2026-08-20 | 3 findings — a running NF is reported as `NOT_INSTANTIATED` with HTTP 200 while the cluster is unreachable | [drill-2](postmortems/2026-08-20-drill-2-control-plane-unreachable.md) |
| 3 | Node disk full | — | Abandoned before running; kind disables kubelet disk eviction, so the drill cannot produce the failure it is meant to produce. Reasoning in [drill-2](postmortems/2026-08-20-drill-2-control-plane-unreachable.md#why-this-drill-replaced-the-disk-full-drill) | — |

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

Both drills so far turned up Sev-1 behaviour, which is the point of running them.

## Conventions

- Timestamps in drill output are local time (UTC+7) unless the output itself carries a `Z`.
- Postmortems are blameless: they describe what the system did, not who typed what.
- Findings are numbered per drill and carried into the postmortem's action-item table. Nothing is
  marked fixed here until the fix is committed and linked.
