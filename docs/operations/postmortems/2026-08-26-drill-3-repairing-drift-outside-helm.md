# Postmortem — Drill 3: repairing drift the orchestrator did not cause

**Date:** 2026-08-26 · **Severity:** Sev-1 · **Status:** all findings fixed ·
**Found:** accidentally, then reproduced deliberately

**Summary:** An operator scales a Deployment outside Helm. The orchestrator correctly reports the
shortfall. Resubmitting the unchanged intent — the repair the day-12 runbook told you to
perform — **fails with a server-side apply conflict**, and the failed upgrade leaves the Helm
release in `status: failed`, which the reconciler reports as `FAILED` even though the workload is
running. The documented repair path made the incident worse, and the recovery needs a flag the
orchestrator never passes.

## How this was found

Not planned. While verifying the alerting rules for
[ADR-0007](../../design/adr/0007-stability-is-an-alerting-concern.md), the transient-shortfall
experiment ended by resubmitting the intent to restore the replica count. It returned `502`, and the
alert set changed shape in a way the experiment did not predict — `NFFailed` appeared alongside the
shortfall alerts. That was the real bug surfacing inside an unrelated test.

Reproduced deliberately afterwards to record it properly. The reproduction is deterministic.

## Environment

Single-node kind cluster `nf-orchestrator`, Kubernetes v1.36.1, **Helm v4.2.2**, kubectl v1.36.1.
The Helm version matters: Helm 4 applies resources with server-side apply, which is what produces
the conflict below. Chart `charts/stand-in-nf`, release `stable-nf`, intent `replicas: 2`.

## Timeline

```
13:48:22  stable-nf healthy, revision 6
          {"state":"INSTANTIATED","desired_replicas":2,"ready_replicas":2}

13:48:30  kubectl --context kind-nf-orchestrator scale deployment/stable-nf --replicas=1
          deployment.apps/stable-nf scaled

13:48:35  GET /deployments/stable-nf
          {"state":"INSTANTIATING","helm_status":"deployed",
           "desired_replicas":2,"ready_replicas":1}          <-- correct: the shortfall is visible

13:48:35  POST /deployments {"name":"stable-nf","replicas":2,"environment":"dev"}
          -> 502
          Error: UPGRADE FAILED: conflict occurred while applying object
          default/stable-nf apps/v1, Kind=Deployment: Apply failed with 1 conflict:
          conflict with "kubectl.exe" with subresource "scale" using apps/v1: .spec.replicas

13:48:39  GET /deployments/stable-nf
          {"state":"FAILED","helm_status":"failed",
           "desired_replicas":2,"ready_replicas":1}          <-- finding 2
```

`helm history` after the failed repair:

```
6   Wed Aug 26 13:48:22 2026   deployed   stand-in-nf-0.1.0   Upgrade complete
7   Wed Aug 26 13:48:35 2026   failed     stand-in-nf-0.1.0   Upgrade "stable-nf" failed: conflict occurred while app...
```

## Finding 1 — the orchestrator cannot repair drift it can see

`kubectl scale` writes `.spec.replicas` through the `scale` subresource, which makes `kubectl` the
server-side-apply **field manager** for that field. Helm 4 then applies the same field as a
different manager, and the API server refuses rather than silently picking a winner. That refusal is
Kubernetes working correctly: two controllers claiming one field is exactly what SSA exists to
surface.

The consequence for this project is specific and bad. `GET` correctly reports the shortfall — that
is the day-12 fix working — but `POST` cannot act on it. The orchestrator can *see* drift and
cannot *correct* it, which is the least useful combination available.

The repair that does work, tested twice:

```
$ helm upgrade stable-nf ./charts/stand-in-nf --kube-context kind-nf-orchestrator \
    --set-json '{"replicaCount":2,"environment":"dev"}' --force-conflicts
STATUS: deployed
REVISION: 8

$ curl -s localhost:8000/deployments/stable-nf
{"release":"stable-nf","state":"INSTANTIATED","helm_status":"deployed",
 "desired_replicas":2,"ready_replicas":2,"pods":[...]}
```

`--force-conflicts` tells the API server to transfer ownership of the contested field back to Helm.
The deploy engine does not pass it, so this repair is only available by hand, outside the API.

Whether the orchestrator *should* always pass `--force-conflicts` is a real design question, not an
oversight to patch quietly. Passing it always means the orchestrator silently overrides any other
controller that touches its Deployments — including, one day, an autoscaler doing its job. Not
passing it means drift is unrepairable through the API. That decision is deferred to its own ADR
rather than made in a postmortem.

## Finding 2 — a failed repair poisons the reported state

Before the failed upgrade, the release read `INSTANTIATING` with 1 of 2 ready: accurate and mild.
After it, the same running workload read `FAILED`, because `derive_state` maps
`helm_status == "failed"` straight to `FAILED` (ADR-0005, unchanged by ADR-0006).

Nothing about the workload changed between those two reads. One pod was serving before, and the same
one pod was serving after. What changed is that Helm recorded a failed *operation*, and the
reconciler reads a failed operation as a failed *deployment*.

The state stays `FAILED` until someone runs a successful upgrade — a retry of the same failing
command does not clear it, it appends another failed revision. Two attempts produced revisions 2 and
3, both `failed`, before `--force-conflicts` produced revision 4 and cleared it.

This is arguably the ETSI distinction the lifecycle model already acknowledges:
`instantiationState` describes the NF, `LcmOperationStateType` describes the operation, and this
project collapsed both into one enum on purpose
([lifecycle-states.md](../../design/lifecycle-states.md)). The collapse has a cost, and this is what
the cost looks like.

## Finding 3 — the day-12 runbook gave the wrong instruction

[Runbook step 6](../runbooks/deployment-not-reaching-instantiated.md) said, in as many words, that
resubmitting the intent is safe and idempotent and would undo drift found in step 2. It was written
from the M2 build log's characterisation of `helm upgrade --install`, and it was never tested
against a Deployment that another manager had written to.

It is wrong in the one situation step 2 exists to detect. Corrected in the same commit as this
postmortem.

The lesson generalises past this bug. Day 11's rule was "only put steps in a runbook that were
actually run during the drill". This step *was* run — but during a drill where nothing had touched
the Deployment, so the command succeeded for a reason that had nothing to do with drift. A step can
be genuinely executed and still be documented for the wrong situation.

## What went well

- The shortfall detection from day 12 worked exactly as designed and gave an accurate reading right
  up to the moment the repair was attempted.
- The alerting rules surfaced the anomaly during an unrelated experiment; `NFFailed` appearing where
  only shortfall alerts were expected is what prompted the investigation.
- Recovery was complete and quick once the right flag was known: one command, back to
  `INSTANTIATED` with 2 of 2 ready.
- Helm's error message named the conflicting manager and the exact field. Nothing had to be guessed.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | Drift is visible but unrepairable through the API | Decide in an ADR whether the deploy engine should pass `--force-conflicts`, always or on an explicit repair path. Do not patch it silently | **fixed 2026-08-27** — the deploy path never forces; conflicts return `409` and `POST /deployments/{name}/repair` forces explicitly ([ADR-0008](../../design/adr/0008-forcing-field-ownership-is-an-explicit-operation.md)) |
| 2 | A failed Helm operation is reported as a failed deployment | Consider separating operation state from instantiation state, as ETSI SOL003 does, rather than mapping `helm_status == "failed"` to `FAILED` | **fixed 2026-09-01** — the four states describe the NF only; the operation is reported as `last_operation` and as `nf_last_operation_state` ([ADR-0009](../../design/adr/0009-the-operation-is-not-the-network-function.md)) |
| 3 | Runbook step 6 told operators to do the thing that breaks | Corrected, with the tested `--force-conflicts` command and a warning about the poisoned state | **fixed 2026-08-26** |

## Reproduce

```bash
curl -s -X POST localhost:8000/deployments -H 'Content-Type: application/json' -d '{"name":"stable-nf","replicas":2,"environment":"dev"}'
```

```bash
kubectl --context kind-nf-orchestrator scale deployment/stable-nf --replicas=1
```

```bash
curl -s -X POST localhost:8000/deployments -H 'Content-Type: application/json' -d '{"name":"stable-nf","replicas":2,"environment":"dev"}'
```

The third command returns `502` with the conflict, and `GET /deployments/stable-nf` then reads
`FAILED`. Recover with `helm upgrade ... --force-conflicts`.

## Follow-up, 2026-08-27

Finding 1 is fixed, and fixing it surfaced a second bug with the same root.

`desired_replicas` was reading `helm get values <name>`, which returns the **latest** revision's
values — including an upgrade that failed and never applied. So after the conflict above, the
orchestrator reported a desired count taken from an intent the cluster had rejected. With the two
revisions carrying different values it is unmistakable:

```
$ helm get values repair-nf --kube-context kind-nf-orchestrator -o json
{"environment":"staging","replicaCount":5}       <- failed revision 2, never applied

$ curl -s localhost:8000/deployments/repair-nf   # before the fix
{"state":"FAILED","desired_replicas":5,"ready_replicas":1,...}
```

`desired_replicas` now reads the last revision whose status is `deployed`, and reports 2 — what is
actually running. This drill's finding 1 and this bug are the same confusion in two places: what was
submitted is not what is in effect.

## Follow-up, 2026-09-01

Finding 2 is fixed. `derive_state` no longer maps a failed Helm operation to a failed NF, and the
operation is reported beside the state:

```
after the same conflicting upgrade, rebuilt on 2026-09-01:
{"state":"INSTANTIATING","helm_status":"failed","desired_replicas":2,"ready_replicas":1,
 "last_operation":{"revision":2,"state":"FAILED","description":"Upgrade ... conflict occurred ..."}}
```

On day 14 that same situation read `FAILED`. The NF is now described by the four states and the
operation by `last_operation`, which is the ETSI SOL003 split this project had collapsed since M3.
Reasoning, and the one case where a failed operation still produces `FAILED`, in
[ADR-0009](../../design/adr/0009-the-operation-is-not-the-network-function.md).

That closes every finding from all three M4 drills.
