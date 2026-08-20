# Runbook — deployment not reaching INSTANTIATED

**Use when:** a deployment sits in `INSTANTIATING`, flaps between states, or reports `INSTANTIATED`
you do not believe.

**Severity:** Sev-2 if the workload is genuinely still coming up. Sev-1 if the reported state and
the cluster disagree.

Every command here was run during
[drill 1](../postmortems/2026-08-20-drill-1-pods-killed-mid-deploy.md) on 2026-08-20.

## Warning: `INSTANTIATED` does not mean the intent was satisfied

The reconciler reports `INSTANTIATED` when every pod it finds is `Running`. It never reads how many
replicas the intent asked for. Confirmed on 2026-08-20: an intent for 3 replicas with 1 pod running
reported `INSTANTIATED`, and the same intent momentarily reported `INSTANTIATED` with 6 pods.
**Always check the count yourself (step 2).** Drill 1, action item 1, still open.

## 1. Read the state and the signals behind it

```bash
curl -s localhost:8000/deployments/<release>
```

The response carries the raw signals that produced the state, which is what you actually diagnose
from:

```
{"release":"drill-one","state":"INSTANTIATING","helm_status":"deployed",
 "pods":[{"phase":"Pending","reason":"ContainerCreating"}, ...]}
```

If `helm_status` is `null` and `pods` is empty, the release may not exist — or the cluster may be
unreachable. Those are indistinguishable in this response; go to
[control plane unreachable](control-plane-unreachable.md) before believing it.

## 2. Compare running pods against what the intent asked for

The orchestrator will not do this for you:

```bash
helm get values <release> --kube-context kind-nf-orchestrator -o json
kubectl --context kind-nf-orchestrator get deployment <release> \
  -o custom-columns=DESIRED:.spec.replicas,READY:.status.readyReplicas --no-headers
```

`replicaCount` from the first command is what was asked for. A `DESIRED` that differs from it means
something changed the Deployment outside Helm — a manual scale, or an autoscaler. A `READY` below
`DESIRED` means the rollout is incomplete regardless of what the state endpoint says.

## 3. Read container state, not pod phase

```bash
kubectl --context kind-nf-orchestrator get pods -l app=<release> \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[0].ready,REASON:.status.containerStatuses[0].state.waiting.reason
```

Pod `phase` is `Running` while a container inside it is stuck. Both drills produced a `Running` pod
whose container was not usable — `CreateContainerConfigError` in drill 2. `READY` is the column
that tells the truth.

Interpretation of common `REASON` values:

| Reason | Meaning | Reported state |
|---|---|---|
| `ContainerCreating` | Normal, expect seconds | `INSTANTIATING` |
| `ErrImagePull`, `ImagePullBackOff` | Bad image or tag; check the chart's `image.tag` | `FAILED` |
| `CrashLoopBackOff` | Container starts and exits; read logs | `FAILED` |
| `CreateContainerConfigError` | Config/env cannot be built; often transient after a node restart | `INSTANTIATED` — wrong, see drill 2 |
| anything else | Not in the reconciler's failure list | `INSTANTIATING` or `INSTANTIATED` — do not rely on it |

## 4. Check the events before changing anything

```bash
kubectl --context kind-nf-orchestrator get events \
  --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | tail
```

Events name the cause; pod status only shows the symptom. This is where
`Error: services have not yet been read at least once, cannot construct envvars` appeared in drill
2, which identified a self-healing condition and prevented a pointless redeploy.

## 5. Distinguish flapping from failing

State is derived per request, so a single read is an instant, not a state. During routine pod
replacement in drill 1 the endpoint went
`INSTANTIATING → INSTANTIATED → INSTANTIATING → INSTANTIATED` in three seconds with nothing wrong.

Poll before concluding anything:

```bash
for i in $(seq 1 10); do
  echo "$(date +%H:%M:%S) $(curl -s localhost:8000/deployments/<release>)"
done
```

A state that changes across reads is a rollout in progress. A state that is stable and wrong is an
incident.

## 6. Recovery

For a stuck rollout, resubmitting the intent is safe — the deploy engine runs
`helm upgrade --install`, which is idempotent for an unchanged intent:

```bash
curl -s -X POST localhost:8000/deployments -H 'Content-Type: application/json' \
  -d '{"name":"<release>","replicas":<n>,"environment":"<env>"}'
```

If the release must go, teardown is idempotent and leaves the cluster running:

```bash
curl -s -X DELETE localhost:8000/deployments/<release>
helm list --kube-context kind-nf-orchestrator
kubectl --context kind-nf-orchestrator get pods -l app=<release>
```

Expect `{"state":"NOT_INSTANTIATED","uninstalled":true}`, then no release and no pods. Called again
on a name with no release it returns `uninstalled: false` rather than erroring.

## What this runbook does not cover

- Rolling back to a previous revision (`helm rollback`). Not exposed by the orchestrator and not
  drilled.
- Anything requiring pod logs from a crashed container beyond `kubectl logs --previous`.
