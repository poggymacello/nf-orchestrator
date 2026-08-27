# ADR-0008: Forcing field ownership is an explicit operation

- **Status:** Accepted
- **Date:** 2026-08-27
- **Closes:** action item 1 of
  [drill 3](../../operations/postmortems/2026-08-26-drill-3-repairing-drift-outside-helm.md) —
  "decide whether the deploy engine should pass `--force-conflicts`".
- **Amends:** [ADR-0006](0006-instantiated-means-the-intent-is-satisfied.md)'s definition of the
  desired replica count, which read the wrong revision.

## Context

Drill 3 found that an operator scaling a Deployment outside Helm leaves the orchestrator able to
*see* drift and unable to *correct* it. `kubectl scale` writes `.spec.replicas` through the `scale`
subresource, which makes `kubectl` the server-side-apply field manager for that field; Helm 4 then
applies as a different manager and the API server refuses:

```
conflict occurred while applying object default/repair-nf apps/v1, Kind=Deployment:
Apply failed with 1 conflict: conflict with "kubectl.exe" with subresource "scale"
using apps/v1: .spec.replicas
```

`helm upgrade --force-conflicts` resolves it by transferring ownership of the contested field back
to Helm. The question the drill deferred was whether the deploy engine should just pass that flag.

While implementing this, a second problem surfaced from the same root. `helm get values <name>`
returns the values of the **latest** revision, including an upgrade that failed and never applied.
ADR-0006 built `desired_replicas` on exactly that call, so after a failed upgrade the orchestrator
reported a desired count from an intent the cluster had rejected. Observed directly: a release
running revision 1 (`replicaCount: 2, environment: dev`) with a failed revision 2
(`replicaCount: 5, environment: staging`) reported `desired_replicas: 5`.

Both problems are the same confusion in two places: **what was submitted is not what is in effect.**

## Decision

**The deploy path never forces.** `POST /deployments` runs `helm upgrade --install` without
`--force-conflicts`, always. Forcing means overriding whatever else was writing to that field, and
the deploy path cannot know whether that "whatever else" is a mistake or an autoscaler doing its
job. Silently winning that race is exactly the class of behaviour the M4 drills kept punishing.

**A conflict is a 409, not a 502, and it names the way out.** `ClusterConflict` is a new
`DeployError` subclass, classified from the apply-conflict wording, and mapped to
`409 Conflict` with a detail that says resubmitting will not help and points at the repair route.
The status code carries the meaning: this is not a broken cluster and not a broken intent, it is two
managers claiming one field.

**Repair is a separate, named operation that takes the intent it should apply.**
`POST /deployments/{name}/repair` accepts the same `Intent` body as a deploy and applies it with
`--force-conflicts`, returning `"forced": true`. The name in the body must match the path.

Taking a body rather than restoring "the intent on file" is the deliberate part. After a failed
upgrade there are two recorded intents — the last one that deployed and the last one submitted — and
the drill showed how easy it is to read the wrong one. Rather than pick for the caller, the caller
states what it wants in effect. It covers both real cases with one route: restoring a drifted
replica count (resend the old intent) and pushing through a new intent that conflicts (send the new
one).

**Repairs are counted separately.** `deployment_repairs_total{result,environment}`. A repair is an
override, and how often overriding is necessary is worth knowing on its own rather than blended into
the deploy counter.

**`desired_replicas` reads the last revision that actually deployed.**
`last_deployed_revision(name)` takes the highest revision from `helm history --output json` whose
status is `deployed`, and the values are read with `--revision N --all`. A release where nothing ever
deployed has no intent in effect, so the desired count is `0`.

## Verified against the cluster on 2026-08-27

Drift, then the conflict, then repair:

```
1. POST /deployments {"name":"repair-nf","replicas":2,"environment":"dev"}   -> 201 revision 1
   GET  -> INSTANTIATED  desired=2 ready=2

2. kubectl scale deployment/repair-nf --replicas=1
   GET  -> INSTANTIATING desired=2 ready=1

3. POST /deployments (same intent)                                          -> 409
   "...conflict with \"kubectl.exe\" with subresource \"scale\" using apps/v1:
    .spec.replicas -- another field manager owns a field this intent would change.
    Resubmitting will not help. To take ownership, POST the same intent to
    /deployments/{name}/repair."

4. POST /deployments/repair-nf/repair {same intent}                         -> 200
   {"release":"repair-nf","status":"deployed","revision":3,"forced":true}
   GET  -> INSTANTIATED  desired=2 ready=2

5. POST /deployments/other-nf/repair {name: repair-nf}                      -> 400
```

The revision fix, shown where the two recorded intents differ. Same release, drifted, with a failed
attempt to move it to 5 replicas in `staging`:

```
$ helm get values repair-nf --kube-context kind-nf-orchestrator -o json
{"environment":"staging","replicaCount":5}          <- the failed revision, never applied

$ curl -s localhost:8000/deployments/repair-nf
{"state":"FAILED","helm_status":"failed","desired_replicas":2,"ready_replicas":1,...}
```

Helm's own view says 5; the orchestrator reports 2, which is what is running. Before this change it
reported 5.

Repairing with the intent that was actually wanted applies it:

```
$ curl -s -X POST localhost:8000/deployments/repair-nf/repair \
    -d '{"name":"repair-nf","replicas":5,"environment":"staging"}'
{"release":"repair-nf","status":"deployed","revision":5,
 "values":{"replicaCount":5,"environment":"staging"},"forced":true}

GET -> INSTANTIATED  desired=5 ready=5
```

## Consequences

- **Good:** drift is now repairable through the API, and repairing is a thing someone chose to do
  rather than something the deploy path did quietly.
- **Good:** `409` distinguishes a field-ownership conflict from a broken cluster (`503`) and from a
  rejected operation (`502`). Three different problems, three different codes.
- **Good:** the desired count now describes what is in effect, so `INSTANTIATED` keeps the meaning
  ADR-0006 gave it even after a failed upgrade.
- **Good:** repairs are counted, so "how often is something else fighting us for these fields" is
  answerable rather than a hunch.
- **Cost:** a status read now costs four subprocesses per release instead of three —
  `helm status`, `helm history`, `helm get values --revision`, `kubectl get pods` — and the lifecycle
  collector pays that per release per scrape. Still fine at this scale, and still the wrong shape for
  a hot loop.
- **Cost:** repair is a real override. Pointed at a Deployment an autoscaler owns, it takes the field
  and the autoscaler loses, with nothing but the counter to show for it.
- **Cost:** conflict detection is substring matching on Helm's message, like the unreachable check
  before it. Pinned by a test against the exact wording from the drill; a future rewording degrades
  to `502`, which is a wrong status code rather than a wrong action.
- **Cost:** a failed upgrade still leaves `helm_status: failed`, so the release still reads `FAILED`
  until something succeeds. That is drill 3's action item 2 and is untouched here.

## Alternatives considered

- **Always pass `--force-conflicts` in the deploy path.** Rejected: it makes every deploy an
  override, so the orchestrator silently wins against any other controller touching its Deployments.
  The conflict is information, and discarding it by default throws away the only signal that
  something else is managing the same field.
- **Never force, and document the manual `helm` command.** That was day 13's stopgap, and it leaves
  the API unable to fix a problem the API can see. Rejected once there was a way to make forcing
  explicit.
- **A `force` field on the intent.** Rejected: the intent describes a desired network function, not
  how to argue with the API server about it. It would also break the M1 schema contract, which
  forbids extra fields.
- **`?force=true` on `POST /deployments`.** Rejected in favour of a distinct route: a query
  parameter is easy to add to a script and forget, and this operation should read differently in a
  log and in a runbook than a normal deploy does.
- **Repair with no body, restoring the last deployed intent.** Rejected: it handles the drift case
  and not the conflicting-new-intent case, and it makes the orchestrator choose between two recorded
  intents — the exact ambiguity that produced the `desired_replicas` bug.
- **Fix `desired_replicas` by reading `Deployment.spec.replicas` instead.** Rejected again, for
  ADR-0006's reason: that is whatever last wrote to the Deployment, which under drift is the thing
  being detected.
