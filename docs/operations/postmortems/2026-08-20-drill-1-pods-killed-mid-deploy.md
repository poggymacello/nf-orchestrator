# Postmortem — Drill 1: pods killed mid-deploy

**Date:** 2026-08-20 · **Severity:** Sev-1 · **Duration of the drill:** ~4 minutes ·
**Status:** findings 1-3 fixed 2026-08-21, finding 4 answered 2026-08-26

**Summary:** Deleted all three pods of a deployment while it was still coming up. The workload
recovered on its own in about two seconds, as Kubernetes is supposed to. The orchestrator's
reported lifecycle state did not survive the drill: it reported `INSTANTIATED` while six pods
existed for a three-replica intent, and — in a follow-up check — reported `INSTANTIATED` with one
pod running for the same three-replica intent. The reconciler never compares what is running
against what the intent asked for.

## Environment

Single-node kind cluster `nf-orchestrator`, reused from M0. Helm v4.2.2, kubectl v1.36.1,
kind v0.32.0, Kubernetes v1.36.1. Orchestrator run locally with
`uvicorn orchestrator:app --host 127.0.0.1 --port 8000`. Chart is `charts/stand-in-nf`
(nginx:1.27, image already cached on the node).

## Hypothesis before the drill

Killing every pod of a healthy deployment should move the reported state
`INSTANTIATED → INSTANTIATING → INSTANTIATED` as the ReplicaSet replaces them, and should never
report `FAILED`, because nothing is actually wrong.

## Timeline

Deploy, then delete all pods three polls in, polling `GET /deployments/drill-one` continuously.

```
20:48:13  POST /deployments {"name":"drill-one","replicas":3,"environment":"dev"}
          -> 201 {"release":"drill-one","status":"deployed","revision":1,
                  "values":{"replicaCount":3,"environment":"dev"}}

20:48:13  poll1  INSTANTIATING  helm=deployed  pods=3  [Pending/ContainerCreating x3]
20:48:14  poll3  INSTANTIATING  helm=deployed  pods=3  [Pending/ContainerCreating x3]

20:48:14  kubectl delete pods -l app=drill-one --wait=false
          pod "drill-one-c77b67dcd-4wmmm" deleted from default namespace
          pod "drill-one-c77b67dcd-ksdx5" deleted from default namespace
          pod "drill-one-c77b67dcd-ldh5t" deleted from default namespace

20:48:14  poll4  INSTANTIATING  helm=deployed  pods=6  [Running x3, Pending/ContainerCreating x3]
20:48:15  poll6  INSTANTIATED   helm=deployed  pods=6  [Running x6]          <-- finding 1
20:48:15  poll8  INSTANTIATING  helm=deployed  pods=6  [Succeeded x3, Running x3]
20:48:16  poll11 INSTANTIATED   helm=deployed  pods=3  [Running x3]
```

The workload itself behaved correctly throughout. From delete to three fresh `Running` pods took
about two seconds, and `helm status` read `deployed` the whole time.

## Finding 1 — `INSTANTIATED` is reported without ever checking the replica count

At 20:48:15 the reconciler reported `INSTANTIATED` for an intent that asked for three replicas
while six pods carried the release's label: three dying originals and three replacements, all
momentarily `Running`.

The mirror image is worse, and is deterministic rather than a one-second race. Scaling the
Deployment down outside Helm, as an operator or an autoscaler might:

```
$ kubectl --context kind-nf-orchestrator scale deployment/drill-one --replicas=1
deployment.apps/drill-one scaled

$ helm get values drill-one --kube-context kind-nf-orchestrator -o json
{"environment":"dev","replicaCount":3}

$ kubectl --context kind-nf-orchestrator get deployment drill-one \
    -o custom-columns=DESIRED:.spec.replicas,READY:.status.readyReplicas --no-headers
1     1

$ curl -s localhost:8000/deployments/drill-one
{"release":"drill-one","state":"INSTANTIATED","helm_status":"deployed",
 "pods":[{"phase":"Running","reason":null}]}
```

The intent asked for three. One is running. The orchestrator says `INSTANTIATED`.

**Root cause.** `derive_state(helm_status, pod_phases)` answers "are all the pods I found
`Running`?" — a question about a set it did not choose. It has no notion of a desired count, and
`reconcile(name)` cannot supply one because it takes only a release name. The desired count is
sitting in `helm get values` and is never read. The condition `phases == {"Running"}` is also
vacuously true for an empty set; it is saved only by the separate `if not pod_phases` guard above
it, which reports `INSTANTIATING`, not `FAILED`.

## Finding 2 — the `Succeeded` phase falls through to `INSTANTIATING`

Deleted nginx pods exit 0 on SIGTERM, so a terminating pod reports `phase: Succeeded`, not
`Running` and not `Terminating`:

```
$ kubectl get pods -l app=drill-one \
    -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,DELETING:.metadata.deletionTimestamp
drill-one-c77b67dcd-b4bnb   Succeeded   2026-08-20T12:49:10Z
drill-one-c77b67dcd-m9psn   Running     <none>
drill-one-c77b67dcd-mknlq   Running     <none>
drill-one-c77b67dcd-zq9b5   Running     <none>
```

`derive_state` has no branch for `Succeeded`. It is not in `FAILED_POD_REASONS` (which matches
container waiting-reasons, not phases) and it fails the `phases == {"Running"}` test, so it lands
in the final `return State.INSTANTIATING`. During this drill that produced the right answer for
the wrong reason: the pods really were mid-replacement. But a deployment whose containers have all
exited 0 and are not being replaced would report `INSTANTIATING` indefinitely rather than `FAILED`,
because `INSTANTIATING` is what the function returns whenever it does not recognise what it is
looking at.

Worth recording because it contradicts the assumption I went in with: a pod being deleted did
**not** report `Running` with a `deletionTimestamp` set. It had already reached `Succeeded` by the
first poll after the delete. The trap I expected was not the trap that was there.

## Finding 3 — the state flaps during a normal, self-healing event

`INSTANTIATING → INSTANTIATED → INSTANTIATING → INSTANTIATED` inside three seconds, with nothing
wrong. State is derived per read (ADR-0005), so a poll landing at 20:48:15 sees `INSTANTIATED` with
six pods and one half a second later sees `INSTANTIATING`. Any alert built on a single read of this
endpoint would fire on routine pod replacement. This is the same shape as the Day 10 finding about
absent series versus zero values: the endpoint reports an instant, and an instant is not a state.

## What went well

- The lifecycle derivation from live signals held up as a design: every reported state traced back
  to a signal carried in the same response, which is what made the wrong answers visible at all.
- `helm status` read `deployed` throughout, confirming the M3 conclusion that release status says
  nothing about workload health.
- Teardown was clean and idempotent:
  `DELETE /deployments/drill-one` → `{"state":"NOT_INSTANTIATED","uninstalled":true}`, no pods left.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | `INSTANTIATED` reported without a replica check | Read the desired replica count (`helm get values`, or carry the intent) and require ready pods == desired before reporting `INSTANTIATED` | **fixed 2026-08-21** — `derive_state` takes `desired`; terminating pods excluded ([ADR-0006](../../design/adr/0006-instantiated-means-the-intent-is-satisfied.md)) |
| 2 | `Succeeded` falls through to `INSTANTIATING` | Handle terminal phases explicitly; make the fall-through case say "unknown" rather than "instantiating" | **fixed 2026-08-21** — a non-terminating pod in `Succeeded`/`Failed` is now `FAILED` |
| 3 | Failure detection is an allowlist of three waiting-reasons | Derive health from container `ready` and pod conditions instead of matching reason strings | **fixed 2026-08-21** — readiness gates `INSTANTIATED`; the allowlist only accelerates `FAILED` |
| 4 | State flaps on routine replacement | Decide what a stable state means before alerting on it — a sustained-read rule, or a rollout-complete signal | **answered 2026-08-26** — stability is defined in the alerting rule's `for:` window, not in the application ([ADR-0007](../../design/adr/0007-stability-is-an-alerting-concern.md)); a 57s shortfall was shown to stay `pending` and never fire |

Findings 1-3 were fixed on Day 12 and finding 4 was answered on Day 13, each verified against a
running cluster, in [ADR-0006](../../design/adr/0006-instantiated-means-the-intent-is-satisfied.md)
and [ADR-0007](../../design/adr/0007-stability-is-an-alerting-concern.md). The body of this
postmortem is left as written on the day, describing the system as it was.

## Reproduce

```bash
uvicorn orchestrator:app --host 127.0.0.1 --port 8000 &
curl -s -X POST localhost:8000/deployments -H 'Content-Type: application/json' \
  -d '{"name":"drill-one","replicas":3,"environment":"dev"}'
kubectl --context kind-nf-orchestrator delete pods -l app=drill-one --wait=false
# poll GET /deployments/drill-one in a tight loop while the above runs
kubectl --context kind-nf-orchestrator scale deployment/drill-one --replicas=1
curl -s localhost:8000/deployments/drill-one
curl -s -X DELETE localhost:8000/deployments/drill-one
```
