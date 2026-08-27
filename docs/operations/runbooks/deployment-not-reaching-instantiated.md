# Runbook — deployment not reaching INSTANTIATED

**Use when:** a deployment sits in `INSTANTIATING`, flaps between states, or reports `INSTANTIATED`
you do not believe.

**Severity:** Sev-2 if the workload is genuinely still coming up. Sev-1 if the reported state and
the cluster disagree.

Every command here was run during
[drill 1](../postmortems/2026-08-20-drill-1-pods-killed-mid-deploy.md) on 2026-08-20, and re-run
against the fixed build on 2026-08-21.

## 1. Read the state and the numbers behind it

```bash
curl -s localhost:8000/deployments/<release>
```

The response carries the signals that produced the state, which is what you actually diagnose from:

```
{"release":"drill-one","state":"INSTANTIATING","helm_status":"deployed",
 "desired_replicas":3,"ready_replicas":1,
 "pods":[{"phase":"Running","reason":null,"ready":true}]}
```

`INSTANTIATED` is only reported when `ready_replicas` equals `desired_replicas` and every pod is
ready, so `desired_replicas` versus `ready_replicas` is the first thing to read. A gap between them
is the whole diagnosis in one line.

> **Before 2026-08-21** neither field existed and `INSTANTIATED` meant only "every pod I happened
> to find is `Running`" — it reported `INSTANTIATED` for one pod of a three-replica intent. Fixed in
> [ADR-0006](../../design/adr/0006-instantiated-means-the-intent-is-satisfied.md).

If the request returns **503**, the cluster is unreachable and this response is not about your
deployment — go to [control plane unreachable](control-plane-unreachable.md).

## 2. Find out whether something changed the Deployment outside Helm

`desired_replicas` comes from the release's values, which is what the intent asked for. The
Deployment may say something else:

```bash
helm get values <release> --kube-context kind-nf-orchestrator --all -o json
```

```bash
kubectl --context kind-nf-orchestrator get deployment <release> -o custom-columns=DESIRED:.spec.replicas,READY:.status.readyReplicas --no-headers
```

If the Deployment's `DESIRED` differs from the release's `replicaCount`, something scaled it outside
the orchestrator — a manual `kubectl scale`, or an autoscaler. The orchestrator deliberately holds
the cluster against the intent, so it will keep reporting `INSTANTIATING` until they agree.
Resubmitting the intent (step 6) puts it back.

## 3. Read container state, not pod phase

```bash
kubectl --context kind-nf-orchestrator get pods -l app=<release> -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[0].ready,REASON:.status.containerStatuses[0].state.waiting.reason
```

Pod `phase` reads `Running` while a container inside it is stuck. `READY` is the column that tells
the truth, and it is what the reconciler now uses.

Interpretation of common `REASON` values:

| Reason | Meaning | Reported state |
|---|---|---|
| `ContainerCreating` | Normal, expect seconds | `INSTANTIATING` |
| `ErrImagePull`, `ImagePullBackOff` | Bad image or tag; check the chart's `image.tag` | `FAILED` |
| `CrashLoopBackOff` | Container starts and exits; read logs | `FAILED` |
| `CreateContainerConfigError` | Config/env cannot be built; often transient after a node restart | `INSTANTIATING` |
| anything else | Not a recognised failure, but an unready pod either way | `INSTANTIATING` |

The last row is the point of the M4 change: an unrecognised reason no longer passes as success. It
holds the deployment at `INSTANTIATING` rather than being ignored.

## 4. Check the events before changing anything

```bash
kubectl --context kind-nf-orchestrator get events --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | tail
```

Events name the cause; pod status only shows the symptom. This is where
`Error: services have not yet been read at least once, cannot construct envvars` appeared in drill
2, which identified a self-healing condition and prevented a pointless redeploy.

## 5. Distinguish flapping from failing

State is derived per request, so a single read is an instant, not a state. During routine pod
replacement the endpoint moves between `INSTANTIATING` and `INSTANTIATED` within seconds, because
readiness genuinely drops while pods are replaced:

```
16:13:44 poll3  INSTANTIATED   desired=3 ready=3
16:13:45 KILL all pods
16:13:45 poll4  INSTANTIATING  desired=3 ready=0
16:13:46 poll5  INSTANTIATED   desired=3 ready=3
```

Poll before concluding anything:

```bash
for i in $(seq 1 10); do echo "$(date +%H:%M:%S) $(curl -s localhost:8000/deployments/<release>)"; done
```

A state that changes across reads is a rollout in progress. A state that is stable and wrong is an
incident. What counts as a *stable* state for alerting purposes is still an open question
(drill 1, action item 4).

## 6. Recovery

**For a stuck rollout** — nothing outside Helm has touched the Deployment — resubmitting the intent
is safe. The deploy engine runs `helm upgrade --install`, which is idempotent for an unchanged
intent:

```bash
curl -s -X POST localhost:8000/deployments -H 'Content-Type: application/json' -d '{"name":"<release>","replicas":3,"environment":"dev"}'
```

**For drift found in step 2, use the repair route.** A plain resubmit returns `409`, because
`kubectl scale` took ownership of `.spec.replicas` as a server-side apply field manager and Helm
will not take it back without being told to:

```
conflict occurred while applying object default/<release> apps/v1, Kind=Deployment: Apply
failed with 1 conflict: conflict with "kubectl.exe" with subresource "scale" using apps/v1:
.spec.replicas -- another field manager owns a field this intent would change. Resubmitting
will not help. To take ownership, POST the same intent to /deployments/{name}/repair.
```

Repair takes the intent you want in effect and applies it with `--force-conflicts`:

```bash
curl -s -X POST localhost:8000/deployments/<release>/repair -H 'Content-Type: application/json' -d '{"name":"<release>","replicas":3,"environment":"dev"}'
```

Expect `"forced": true` and a new revision. That returns the release to `deployed` and clears a
`FAILED` reading in the same step. Retrying the plain deploy does **not** clear it — each attempt
appends another failed revision.

**Repair is an override.** It takes ownership of the contested field from whatever held it. If the
other manager was an autoscaler doing its job, repairing makes the orchestrator win and the
autoscaler lose. Check what wrote the field before forcing it back:

```bash
kubectl --context kind-nf-orchestrator get deployment <release> --show-managed-fields -o yaml | grep -A3 'manager:'
```

> Until 2026-08-26 this step said resubmitting the intent would undo drift. It does not. Until
> 2026-08-27 the repair had to be run as a raw `helm upgrade --force-conflicts` by hand. See
> [drill 3](../postmortems/2026-08-26-drill-3-repairing-drift-outside-helm.md) and
> [ADR-0008](../../design/adr/0008-forcing-field-ownership-is-an-explicit-operation.md).

If the release must go, teardown is idempotent and leaves the cluster running:

```bash
curl -s -X DELETE localhost:8000/deployments/<release>
```

Expect `{"state":"NOT_INSTANTIATED","uninstalled":true}`, then no release and no pods. Called again
on a name with no release it returns `uninstalled: false` rather than erroring.

```bash
helm list --kube-context kind-nf-orchestrator
```

## What this runbook does not cover

- Rolling back to a previous revision (`helm rollback`). Not exposed by the orchestrator and not
  drilled.
- Anything requiring pod logs from a crashed container beyond `kubectl logs --previous`.
