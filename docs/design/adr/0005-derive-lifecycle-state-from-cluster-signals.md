# ADR-0005: Derive lifecycle state from live cluster signals, don't store it

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

M2 deploys an intent and returns Helm's view, "manifest accepted". That is not the same as "the
workload is running", and it says nothing once the deploy call has returned. Anything watching a
deployment (a dashboard, an alarm, a rollback controller) needs a single small state, not a choice
between reading raw pod phases and reading Helm release status and reconciling the two by hand.

The lifecycle model was fixed on Day 5 in
[lifecycle-states.md](../../design/lifecycle-states.md): four states,
`NOT_INSTANTIATED → INSTANTIATING → INSTANTIATED → FAILED`, a simplified projection of the ETSI
NFV SOL003 `instantiationState` plus its operation state. What was missing was the code that emits
them. This ADR is about how that code derives the state.

## Decision

**State is derived on read, not stored.** There is no state column, no state machine persisted
anywhere. `GET /deployments/{name}` calls `reconciler.reconcile`, which reads two live signals at
request time and returns the state it computes from them. The cluster is the source of truth; the
reconciler is a pure projection over it. Restarting the API loses nothing, because it held nothing.

**The projection is a pure function.** `derive_state(helm_status, pod_phases)` in
[`orchestrator/reconciler.py`](../../../orchestrator/reconciler.py) takes a Helm release status and
a list of pod phase/reason pairs and returns one of the four states. It touches no cluster, so CI
tests it directly with mocked signals. The two functions that fetch the signals (`helm status
--output json` and `kubectl get pods -o json`) are the only parts that need a cluster.

The derivation, in order:

| Condition on the live signals | Derived state |
|---|---|
| no Helm release | `NOT_INSTANTIATED` |
| Helm release status is `failed` | `FAILED` |
| any pod waiting-reason in {`ImagePullBackOff`, `ErrImagePull`, `CrashLoopBackOff`} | `FAILED` |
| release exists but no pods yet | `INSTANTIATING` |
| every pod phase is `Running` | `INSTANTIATED` |
| otherwise (some pod still `Pending`/creating) | `INSTANTIATING` |

**Pod health outranks release status for `FAILED`.** A release can read `deployed` while a pod
under it cannot pull its image. The Day-5 note recorded this; the reconciler acts on it by checking
pod reasons even when Helm says `deployed`. `FAILED` therefore means "the current rollout has a pod
that cannot become healthy", not "Helm reported a failure".

**Teardown is uninstall, and it is idempotent.** `DELETE /deployments/{name}` uninstalls the Helm
release and reports `NOT_INSTANTIATED`. Called on a name with no release, it returns
`NOT_INSTANTIATED` with `uninstalled: false` rather than erroring, so a caller can converge to
"gone" without first checking existence.

**Poll, not watch.** The reconciler reads on demand. It does not hold a Kubernetes watch or run a
background loop. For a synchronous status endpoint this is enough and has no lifecycle of its own to
manage. A watch-based controller is out of scope until something needs push updates.

## Verified against a real deploy/teardown on 2026-07-23

Success path, one intent, polled sub-second while pods came up:

```
08:11:38 NOT_INSTANTIATED helm=None pods=[]
08:11:43 INSTANTIATING     helm=deployed pods=[('Pending','ContainerCreating'),('Pending','ContainerCreating')]
08:11:44 INSTANTIATED      helm=deployed pods=[('Running',None),('Running',None)]
```

Failure path, same release upgraded to a nonexistent image tag:

```
08:14:16 FAILED helm=deployed pods=[('Pending','ErrImagePull'),('Running',None),('Running',None)]
```

`helm status` stayed `deployed` throughout; the `FAILED` came from the pod reason alone.

Teardown:

```
$ curl -s -X DELETE localhost:8000/deployments/sample-nf
{"release":"sample-nf","state":"NOT_INSTANTIATED","uninstalled":true}
$ helm list --kube-context kind-nf-orchestrator | grep sample-nf   # (release gone)
```

Full run in [the M3 build log](../../build-log/m3-reconciler.md).

## Consequences

- **Good:** no persisted state to fall out of sync with the cluster. Every read reflects the
  cluster as it is now.
- **Good:** the derivation is a pure function, unit-tested without a cluster; CI covers the logic
  that matters.
- **Good:** `FAILED` is honest about rollout health because it reads pods, not just Helm.
- **Cost:** every status read costs a `helm status` and a `kubectl get` subprocess. Fine for a
  status endpoint, wrong for a hot loop.
- **Cost:** a mixed rollout where old pods still serve while a new pod fails reports `FAILED`, even
  though the service is still up. The state describes the latest rollout, not user-visible
  availability. A caller that needs both has to look further.
- **Cost:** polling cannot report a transition the caller did not poll for. There is no event
  stream. Metrics and push are M4 (planned).

## Alternatives considered

- **Store state and update it on transitions.** Rejected: it can disagree with the cluster the
  moment anything changes state outside our code (a pod crash, a manual `helm rollback`), and it
  needs a writer to keep it fresh.
- **Report Helm release status directly as the state.** Rejected: `deployed` does not mean healthy,
  as the failure run shows. It would report success over a pod stuck in `ImagePullBackOff`.
- **A background watch controller reconciling continuously.** Rejected for M3: it adds a process
  lifecycle, leader concerns, and resync logic for no gain over on-demand read at this scale.
- **Derive from pod phase only, ignoring Helm.** Rejected: with no release there are no pods, and
  `NOT_INSTANTIATED` versus `INSTANTIATING`-with-no-pods-yet needs the release signal to tell apart.
