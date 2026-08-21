# ADR-0006: INSTANTIATED means the intent is satisfied, and "unreachable" is not a state

- **Status:** Accepted
- **Date:** 2026-08-21
- **Supersedes:** the derivation table and the two signal readers in
  [ADR-0005](0005-derive-lifecycle-state-from-cluster-signals.md). Everything else in ADR-0005 —
  derive on read, pure function, poll not watch, idempotent teardown — still stands.

## Context

The M4 failure drills on 2026-08-20 broke the derivation described in ADR-0005 twice, in ways that
were not close calls.

[Drill 1](../../operations/postmortems/2026-08-20-drill-1-pods-killed-mid-deploy.md): a
three-replica intent reported `INSTANTIATED` with six pods live mid-replacement, and — after the
Deployment was scaled to one outside Helm — reported `INSTANTIATED` with one pod running. It also
had no branch for the `Succeeded` phase, which is what a deleted nginx pod reports.

[Drill 2](../../operations/postmortems/2026-08-20-drill-2-control-plane-unreachable.md): with the
control plane stopped, `GET /deployments/{name}` returned **HTTP 200 with `NOT_INSTANTIATED`** for a
release that was running the whole time, in a response byte-identical to the one returned after a
real teardown. During recovery it reported `INSTANTIATED` for a pod whose container was in
`CreateContainerConfigError`.

Three assumptions in ADR-0005 caused all of it:

1. **The pods that exist are the pods that should exist.** `derive_state(helm_status, pod_phases)`
   was never given a desired count and could not ask for one, because `reconcile` takes only a
   release name.
2. **Pod phase describes container health.** `phase: Running` is true while a container inside the
   pod is in `waiting`, and failure was detected by matching three hardcoded waiting-reason strings
   — an allowlist that is open-ended by construction.
3. **A failed lookup means the release is absent.** `helm_release_status` caught every non-zero
   Helm exit and returned `None`, which `derive_state` maps to `NOT_INSTANTIATED`.

## Decision

**`INSTANTIATED` means the intent is satisfied, not that the pods present look well.** The
derivation now takes the desired replica count as a third argument:

```python
derive_state(helm_status: str | None, pods: list[dict], desired: int) -> State
```

`INSTANTIATED` requires `len(pods) == desired` and every pod ready. Anything else that is not a
failure is `INSTANTIATING`. A shortfall and an excess are both "not what was asked for".

**Desired count comes from the release's own values, not from the Deployment.**
`desired_replicas(name)` reads `helm get values <name> --all --output json`. `--all` includes chart
defaults, so it answers even when an intent did not set the field. This is deliberately not
`Deployment.spec.replicas`: if something scaled the Deployment outside Helm, the intent is still
the number to hold the cluster against, and reading `spec.replicas` would make that drift
invisible by definition.

**Readiness replaces the failure allowlist for deciding `INSTANTIATED`.** `pod_states(name)`
returns `{"phase", "reason", "ready"}` per pod, where `ready` is every container's `ready` flag.
The three known waiting-reasons are kept as a *fast path to `FAILED`*, because they are terminal
enough to report early, but they are no longer what stands between a pod and `INSTANTIATED`. A
reason nobody has seen yet now blocks `INSTANTIATED` instead of being ignored.

**Terminating pods are not counted.** `pod_states` skips any pod carrying a `deletionTimestamp`.
They are already leaving, and counting them is what doubled the pod count mid-replacement.

**A terminal pod phase is a failure.** `Succeeded` and `Failed` on a pod that is *not* terminating
mean the workload stopped and was not replaced, which under a Deployment is a failure, not a
rollout in progress. Previously both fell through to `INSTANTIATING`, which is what the function
returned whenever it did not recognise what it was looking at.

**`ClusterUnreachable` is a separate exception, and never a lifecycle state.** `classify_error`
in [`orchestrator/deploy.py`](../../../orchestrator/deploy.py) inspects the failure text from helm
or kubectl and raises `ClusterUnreachable` (a subclass of `DeployError`) when it matches a known
unreachable wording, POSIX or Windows. `helm_release_status` returns `None` **only** for
`release: not found`; `ClusterUnreachable` propagates, and so does any error it does not recognise
— an unexpected failure is no longer silently reported as an absent release.

**5xx split: 502 refused, 503 unreachable.** Every route maps `ClusterUnreachable` to `503` and
other `DeployError`s to `502`. "The cluster answered and said no" and "I could not ask" are
different answers, and neither is a deployment state.

**`/healthz` is liveness, `/readyz` is readiness.** `/healthz` stays green while the cluster is
down, which is now a documented choice with a test pinning it. The new `/readyz` runs
`kubectl get --raw=/readyz` and returns `503` when the cluster cannot be reached.

**The response carries the evidence.** `GET /deployments/{name}` now includes `desired_replicas`
and `ready_replicas` alongside the state, so the number the state depends on is visible to whoever
reads it. Drill 1's wrong answer would have been obvious in the payload.

### Revised derivation

| Condition on the live signals | Derived state |
|---|---|
| no Helm release (`release: not found`) | `NOT_INSTANTIATED` |
| cluster unreachable | no state — `ClusterUnreachable` → HTTP 503 |
| Helm release status is `failed` | `FAILED` |
| any pod waiting-reason in {`ImagePullBackOff`, `ErrImagePull`, `CrashLoopBackOff`} | `FAILED` |
| any non-terminating pod in phase `Succeeded` or `Failed` | `FAILED` |
| `desired > 0`, pod count == `desired`, every pod ready | `INSTANTIATED` |
| otherwise | `INSTANTIATING` |

Pods with a `deletionTimestamp` are excluded before any of these apply.

## Verified against the cluster on 2026-08-21

Shortfall, the deterministic case from drill 1 — release asks for 2, Deployment scaled to 1 outside
Helm:

```
$ helm get values probe-nf --kube-context kind-nf-orchestrator -o json
{"environment":"dev","replicaCount":2}

$ curl -s localhost:8000/deployments/probe-nf
{"release":"probe-nf","state":"INSTANTIATING","helm_status":"deployed",
 "desired_replicas":2,"ready_replicas":1,
 "pods":[{"phase":"Running","reason":null,"ready":true}]}
```

All pods killed mid-deploy on a three-replica intent. The pod count never exceeds three now, and
the drop in readiness is what moves the state:

```
16:13:44 poll3  INSTANTIATED   desired=3 ready=3 pods_counted=3
--- 16:13:45 KILL all pods ---
16:13:45 poll4  INSTANTIATING  desired=3 ready=0 pods_counted=3
16:13:46 poll5  INSTANTIATED   desired=3 ready=3 pods_counted=3
```

Control plane stopped, same release:

```
GET    /deployments/drill-one -> 503 {"detail":"Error: kubernetes cluster unreachable: ..."}
POST   /deployments           -> 503 {"detail":"Error: kubernetes cluster unreachable: ..."}
DELETE /deployments/drill-one -> 503 {"detail":"Error: kubernetes cluster unreachable: ..."}
GET    /healthz               -> 200 {"status":"ok"}
GET    /readyz                -> 503 {"detail":"Unable to connect to the server: ..."}
```

`FAILED` still works, unchanged from M3 — release upgraded to a nonexistent image tag:

```
{"release":"drill-one","state":"FAILED","helm_status":"deployed",
 "desired_replicas":3,"ready_replicas":3,
 "pods":[{"phase":"Pending","reason":"ErrImagePull","ready":false},
         {"phase":"Running","reason":null,"ready":true}, ...]}
```

Note `ready_replicas: 3` next to `FAILED`: the old ReplicaSet is still serving while the new pod
cannot pull. That is ADR-0005's rolling-upgrade caveat, now visible in the payload instead of only
in a document.

## Consequences

- **Good:** `INSTANTIATED` is now a claim about the intent, so drift introduced outside the
  orchestrator shows up instead of being absorbed.
- **Good:** an unreachable cluster can no longer be mistaken for a fleet-wide teardown, and an
  unrecognised helm error can no longer be mistaken for an absent release.
- **Good:** a waiting-reason nobody anticipated blocks `INSTANTIATED` rather than passing silently.
  The allowlist only accelerates `FAILED`; it no longer gates success.
- **Cost:** one more subprocess per status read (`helm get values`), on top of `helm status` and
  `kubectl get pods`. Three subprocesses per request is worse for a hot loop; still fine for a
  status endpoint, and the same trade-off ADR-0005 already accepted.
- **Cost:** `derive_state` gained a required argument, so every caller must decide what "desired"
  means. That is the point, but it makes the function harder to call casually.
- **Cost:** state still flaps across reads during a normal rollout, now honestly — `ready` genuinely
  drops when pods are replaced. Deciding what a *stable* state means is an alerting question and is
  still open (drill 1, action item 4).
- **Cost:** the unreachable check is substring matching on tool output. It is pinned by tests
  against the exact strings helm and kubectl produced on Windows and POSIX, but a future wording
  falls back to `DeployError`/502 — wrong status code, no longer a wrong deployment state.

## Alternatives considered

- **Read `Deployment.spec.replicas` as the desired count.** Rejected: it is whatever last wrote to
  the Deployment. Under it, drill 1's `kubectl scale` would have made the cluster agree with itself
  and the drift would be undetectable in principle.
- **Carry the intent in a store and compare against that.** Rejected for now: it reintroduces the
  persisted state ADR-0005 deliberately avoids. `helm get values` is already a durable record of
  the intent, written by the deploy path, and lives with the release.
- **Add an `UNKNOWN` fifth state for unreachable.** Rejected: the four states are a projection of
  ETSI SOL003's `instantiationState` (see [lifecycle-states.md](../lifecycle-states.md)), and
  "I cannot see the cluster" is not a property of the NF. HTTP already has a way to say it.
- **Return 200 with a `stale: true` flag during an outage.** Rejected: a 200 invites callers to
  read the body, and the body would have nothing true in it.
- **Drop the waiting-reason allowlist entirely and rely on readiness.** Rejected: an
  `ImagePullBackOff` would then read `INSTANTIATING` indefinitely rather than `FAILED`. The
  allowlist is worth keeping as a fast path, just not as the definition of failure.
