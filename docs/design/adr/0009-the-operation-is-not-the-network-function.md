# ADR-0009: The operation is not the network function

- **Status:** Accepted
- **Date:** 2026-09-01
- **Closes:** action item 2 of
  [drill 3](../../operations/postmortems/2026-08-26-drill-3-repairing-drift-outside-helm.md) — the
  last unfixed *defect* across all three M4 drills. Drill 2's action item 4 stays open, but it is a
  prerequisite for a drill that was never run, not a fault in the system.
- **Supersedes:** the `Helm release status is failed → FAILED` row of
  [ADR-0006](0006-instantiated-means-the-intent-is-satisfied.md)'s derivation table, inherited
  unchanged from [ADR-0005](0005-derive-lifecycle-state-from-cluster-signals.md).

## Context

Drill 3 produced two readings of the same running workload, seconds apart:

```
13:48:35  {"state":"INSTANTIATING","helm_status":"deployed","desired_replicas":2,"ready_replicas":1}
13:48:39  {"state":"FAILED",       "helm_status":"failed",  "desired_replicas":2,"ready_replicas":1}
```

Nothing about the network function changed between them. One pod was serving before and the same
one pod was serving after. What changed is that a `helm upgrade` was rejected, so Helm recorded a
failed revision, and `derive_state` mapped `helm_status == "failed"` straight to `FAILED`.

The reconciler was answering two different questions with one word. "Is the NF healthy?" and "did
the last thing I tried work?" are independent: an operation can fail against a perfectly healthy NF
(drill 3), and an operation can succeed onto an NF that is broken (a completed upgrade to an image
tag that does not exist — the M3 failure path, where `helm status` read `deployed` throughout).

ETSI SOL003 already separates these. `instantiationState` describes the VNF; `LcmOperationStateType`
— `STARTING`, `PROCESSING`, `COMPLETED`, `FAILED_TEMP`, `FAILED`, `ROLLING_BACK`, `ROLLED_BACK` —
describes the operation. M3 collapsed both into one four-value enum deliberately, and
[lifecycle-states.md](../lifecycle-states.md) recorded that as a known simplification. Drill 3 is
what the simplification cost.

## Decision

**The four states describe the network function only.** `derive_state` no longer returns `FAILED`
because a Helm operation failed. `FAILED` now comes from workload signals — a failure waiting-reason,
or a non-terminating pod in a terminal phase — exactly as ADR-0006 defined them.

**The last operation is reported beside the state, not folded into it.**
`GET /deployments/{name}` gains a `last_operation` object:

```json
"last_operation": {
  "revision": 2,
  "state": "FAILED",
  "description": "Upgrade \"opstate-nf\" failed: conflict occurred while applying object ..."
}
```

`OperationState` is a subset of SOL003's type — `PROCESSING`, `COMPLETED`, `FAILED`, plus `UNKNOWN`
— mapped from the newest revision's Helm status via `OPERATION_STATUS`. It is read from
`helm history --output json`, which the reconciler already called for `desired_replicas`, so this
costs no extra subprocess.

**`UNKNOWN` exists so a fall-through means nothing in particular.** A Helm status not in the map
becomes `UNKNOWN` rather than the nearest plausible value. This is drill 1's finding 2 applied
before it can happen again: there, `Succeeded` had no branch and fell into `INSTANTIATING`, which is
what the function returned whenever it did not recognise what it was looking at.

**One exception keeps a failed install honest.** A release where *no* revision ever deployed, whose
last operation `FAILED`, reports `FAILED`. Such a release will not progress on its own, and without
this it would sit at `INSTANTIATING` forever. It needs both conditions: `ever_deployed=False` and a
failed operation.

**The operation is also a metric.** `nf_last_operation_state{release,state}`, 0/1 per state, beside
`nf_deployment_state`. A new rule, `NFLastOperationFailed`, fires after `for: 5m` at `severity: warn`
— longer and softer than the NF alerts, because an unresolved failed operation is a thing to go and
clean up, not an outage.

### Considered and rejected: report a failed install as NOT_INSTANTIATED

The strict SOL003 reading is that instantiation either completed or it did not, so a release whose
first install failed is `NOT_INSTANTIATED` with the operation `FAILED`. Checked against a real failed
install before deciding:

```
$ curl -s localhost:8000/deployments/badfirst
{"state":"FAILED","helm_status":"failed","desired_replicas":0,"ready_replicas":0,
 "pods":[{"phase":"Pending","reason":"ImagePullBackOff","ready":false}]}
```

There is a release, and there is a pod sitting in `ImagePullBackOff`. Reporting `NOT_INSTANTIATED`
would put it in the same bucket as a name that was never deployed and a release that was torn
down — and `NOT_INSTANTIATED` is also what `DELETE` returns and what the teardown path checks.
Collapsing "there is nothing here" with "there is something here and it is broken" is precisely the
mistake drill 2 found, in a new place.

So `NOT_INSTANTIATED` keeps meaning exactly one thing: no release exists. A release that exists and
cannot become healthy is `FAILED`, whatever the reason.

## Verified against the cluster on 2026-09-01

Drill 3's case, rebuilt. Healthy, then drifted by `kubectl scale`, then a resubmit that conflicts:

```
healthy      {"state":"INSTANTIATED", "helm_status":"deployed",
              "desired_replicas":2,"ready_replicas":2,
              "last_operation":{"revision":1,"state":"COMPLETED","description":"Install complete"}}

drifted      {"state":"INSTANTIATING","helm_status":"deployed","ready_replicas":1,
              "last_operation":{"revision":1,"state":"COMPLETED",...}}

POST /deployments -> 409

after        {"state":"INSTANTIATING","helm_status":"failed","ready_replicas":1,
              "last_operation":{"revision":2,"state":"FAILED",
                                "description":"Upgrade ... conflict occurred ..."}}
```

That last line is the finding closed: on day 14 the same situation read `FAILED`. The NF is
described accurately — one of two ready, rolling — and the failed operation is reported next to it
rather than instead of it.

A failed first install still reads `FAILED`, now with the operation visible:

```
{"release":"badfirst","state":"FAILED","helm_status":"failed",
 "desired_replicas":0,"ready_replicas":0,
 "last_operation":{"revision":1,"state":"FAILED",
                   "description":"Release \"badfirst\" failed: resource Deployment/default/
                                  badfirst not ready ... context deadline exceeded"},
 "pods":[{"phase":"Pending","reason":"ImagePullBackOff","ready":false}]}
```

The two axes moving independently on `/metrics`:

```
nf_deployment_state{release="badfirst",  state="FAILED"}        1.0
nf_last_operation_state{release="badfirst",  state="FAILED"}    1.0

nf_deployment_state{release="opstate-nf",state="INSTANTIATING"} 1.0
nf_last_operation_state{release="opstate-nf",state="FAILED"}    1.0
```

And a repair clearing the operation without the NF ever having been `FAILED`:

```
POST /deployments/opstate-nf/repair -> 200
{"state":"INSTANTIATED","last_operation":{"revision":3,"state":"COMPLETED",
                                          "description":"Upgrade complete"}}
nf_last_operation_state{release="opstate-nf",state="COMPLETED"} 1.0
```

## Consequences

- **Good:** a failed operation no longer makes a serving NF look broken, which was the finding.
- **Good:** the two questions have two answers, so "the NF is fine but somebody's upgrade keeps
  failing" is now expressible — and alertable, at a lower severity than an outage.
- **Good:** the `description` carries Helm's own error text, so the reason a deploy was rejected
  survives in the status endpoint rather than only in the 4xx/5xx the caller saw at the time.
- **Good:** no extra subprocess. `helm history` was already being read for `desired_replicas`.
- **Cost:** `derive_state` now takes five arguments. Two of them describe the operation and exist
  only for the failed-install case, which is a wart on an otherwise clean projection.
- **Cost:** `helm_status` is now in the response without driving the state, which invites the reader
  to wonder what it is for. It is kept as a raw signal, and `last_operation.state` is the derived
  one.
- **Cost:** the operation state is only as good as Helm's revision statuses. A `pending-upgrade` left
  behind by a killed Helm process reads `PROCESSING` forever, and nothing here times that out.
- **Cost:** two alerts can now describe one incident from different angles. `NFFailed` and
  `NFLastOperationFailed` firing together is normal for a failed install, and whoever reads the
  alerts has to know that.

## Alternatives considered

- **Keep one enum and simply stop mapping `failed` to `FAILED`.** Rejected on its own: it fixes
  drill 3's case and leaves a failed first install reporting `INSTANTIATING` forever, since no pods
  and no desired count means nothing else fires. The `ever_deployed` exception exists for exactly
  that, and once it exists the operation state is worth reporting properly.
- **Add `FAILED_TEMP` and the rollback states from SOL003.** Rejected: Helm has no rollback in
  progress in this project (`helm rollback` is not exposed), so those values could never be produced.
  Adding enum members nothing can emit would be documentation pretending to be code.
- **Report the operation only in the error response, not in the status.** Rejected: the failure is
  most interesting to whoever finds the release later, not to the caller who already saw the 409.
- **Derive the operation from `helm status` rather than `helm history`.** Rejected: `helm status`
  gives the current release status but not the revision or the description, and history was already
  being fetched.
