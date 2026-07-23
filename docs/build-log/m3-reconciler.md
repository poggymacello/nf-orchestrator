# M3 — Status reconciler, lifecycle, teardown

**Goal:** make the four lifecycle states real, so a deployment reports
`NOT_INSTANTIATED / INSTANTIATING / INSTANTIATED / FAILED` derived from live cluster signals, and
can be torn down.

Design rationale is in
[ADR-0005](../design/adr/0005-derive-lifecycle-state-from-cluster-signals.md). The state model and
its public grounding are in [lifecycle-states.md](../design/lifecycle-states.md), fixed on Day 5.
This page records building and running the reconciler on 2026-07-23.

## What exists now

- `orchestrator/reconciler.py`: `derive_state`, a pure function from
  (Helm release status, pod phases) to one of the four states, plus the two readers that fetch
  those signals with `helm status --output json` and `kubectl get pods -o json`.
- `GET /deployments/{name}`: returns the derived state and the raw signals behind it.
- `DELETE /deployments/{name}`: uninstalls the Helm release, returns `NOT_INSTANTIATED`.

The cluster is the persistent single-node kind cluster from M0, reused:

```
$ kubectl --context kind-nf-orchestrator get nodes
NAME                            STATUS   ROLES           AGE   VERSION
nf-orchestrator-control-plane   Ready    control-plane   19d   v1.36.1
```

## Success path

Before any deploy, the reconciler finds no release:

```
$ curl -s localhost:8000/deployments/sample-nf
{"release":"sample-nf","state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}
```

Deployed a valid intent through the M2 engine and polled the status route sub-second while pods
came up, printing the state next to the signals that drove it:

```
08:11:38 NOT_INSTANTIATED helm=None     pods=[]
08:11:43 INSTANTIATING     helm=deployed pods=[('Pending','ContainerCreating'),('Pending','ContainerCreating')]
08:11:44 INSTANTIATED      helm=deployed pods=[('Running',None),('Running',None)]
```

Each transition ties to a signal: the release appearing as `deployed` with pods not yet `Running`
gives `INSTANTIATING`; every pod phase reaching `Running` gives `INSTANTIATED`. The state is
computed from those two signals on each read, not stored.

## Failure path

Upgraded the same release to a nonexistent image tag and polled until the reconciler reported
`FAILED`:

```
08:14:16 FAILED helm=deployed pods=[('Pending','ErrImagePull'),('Running',None),('Running',None)]
```

Settled state a few seconds later:

```
$ curl -s localhost:8000/deployments/sample-nf
state: FAILED | helm: deployed
 pod: Pending ImagePullBackOff
 pod: Running None
 pod: Running None

$ kubectl --context kind-nf-orchestrator get pods -l app=sample-nf
NAME                         READY   STATUS             RESTARTS   AGE
sample-nf-585d7b986f-kk9sh   0/1     ImagePullBackOff   0          110s
sample-nf-5f964655b9-gzm6b   1/1     Running            0          4m15s
sample-nf-5f964655b9-qgqrq   1/1     Running            0          4m15s
```

`helm status` read `deployed` the entire time. The `FAILED` came from the pod waiting-reason
`ErrImagePull` / `ImagePullBackOff`, not from Helm. This is the Day-5 observation turned into code:
release status and pod health are separate signals, and only the second tells you the workload
failed. The reconciler checks pod reasons even when Helm says `deployed`.

This run also shows what `FAILED` does and does not mean. Two old pods stayed `Running` and kept
serving while the new pod failed to pull, because a rolling upgrade holds the old ReplicaSet up.
`FAILED` describes the current rollout, not user-visible availability. That trade-off is recorded
in ADR-0005.

## Teardown

```
$ helm list --kube-context kind-nf-orchestrator | grep sample-nf
sample-nf  default  2  ...  deployed  stand-in-nf-0.1.0  1.27

$ curl -s -X DELETE localhost:8000/deployments/sample-nf
{"release":"sample-nf","state":"NOT_INSTANTIATED","uninstalled":true}

$ helm list --kube-context kind-nf-orchestrator | grep sample-nf
(release gone)

$ curl -s localhost:8000/deployments/sample-nf
{"release":"sample-nf","state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}

$ kubectl --context kind-nf-orchestrator get pods -l app=sample-nf
No resources found in default namespace.
```

Teardown means `helm uninstall`, not deleting the cluster. The kind cluster is the persistent M0
one and was left running. Called again on a name with no release, `DELETE` returns
`NOT_INSTANTIATED` with `uninstalled: false` rather than erroring, so teardown is idempotent.

## What broke: the success transition was invisible on the first try

The first attempt polled once per second, starting after the deploy call returned. It only ever
caught `INSTANTIATED`:

```
08:11:20 INSTANTIATED | helm=deployed | pods=[('Running',None),('Running',None)]
```

The nginx image was already cached on the kind node from earlier days, so
`ContainerCreating → Running` took under a second and the whole `INSTANTIATING` window fell between
two polls. Nothing was wrong with the derivation; the observation was too slow to see it.

Fixed by tearing down, starting a sub-second poller before submitting the intent, then deploying.
That caught the `Pending/ContainerCreating → INSTANTIATING` frame at 08:11:43 shown above. The
lesson is about proving derived state, not about the code: a fast transition needs an observer
already running before the event, or it looks like it never happened.

## What is still planned

- **Metrics and events (planned, M4).** The reconciler polls on request. It emits no metrics and
  has no push or event stream, so it cannot report a transition nobody polled for. Instrumenting
  these transitions is M4.
- **Persistence.** Still none. State is derived on read; there is no record that an intent was ever
  submitted beyond `helm history`.
- **Watch-based reconciliation.** On-demand read only. No background controller.
- **CI does not reconcile against a cluster.** `lint-test` has no kind. `derive_state` is unit
  tested with mocked signals and the endpoints are tested with the readers monkeypatched; e2e
  against kind is `(planned, M5)`. The runs above were by hand.

## Checks

`ruff check .` clean and `pytest` green locally (30 tests), then the same two commands on the pull
request through the repo's one CI job, `lint-test`.
