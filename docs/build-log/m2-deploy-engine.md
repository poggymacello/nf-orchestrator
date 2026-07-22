# M2 — Deploy engine

**Goal:** turn a validated intent into a running workload, so the contract from M1 produces
something on a cluster instead of a `200`.

Rationale for using Helm is in [ADR-0004](../design/adr/0004-helm-as-the-deploy-mechanism.md). The
Helm and kind mechanics themselves were worked out by hand on Day 4 and are in
[learning-notes/helm-and-kind.md](../learning-notes/helm-and-kind.md). This page is the record of
building and running the engine on 2026-07-22.

## What exists now

- `charts/stand-in-nf/`, a chart with three values (`replicaCount`, `environment`, `image`) that
  deploys a public nginx image as the stand-in network function.
- `orchestrator/deploy.py`, which renders values from an `Intent` and calls
  `helm upgrade --install`.
- `POST /deployments`, which validates the body against the M1 model and then deploys it.

The cluster is the single-node kind cluster from M0, reused rather than recreated:

```
$ kubectl --context kind-nf-orchestrator get nodes
NAME                            STATUS   ROLES           AGE   VERSION
nf-orchestrator-control-plane   Ready    control-plane   18d   v1.36.1
```

## Intent to release

```
$ curl -s -X POST localhost:8000/deployments \
    -H 'content-type: application/json' \
    -d @examples/intent-valid.json
```

Intent in:

```json
{
  "name": "sample-nf",
  "replicas": 2,
  "environment": "dev"
}
```

`HTTP 201` out:

```json
{"release":"sample-nf","status":"deployed","revision":1,"values":{"replicaCount":2,"environment":"dev"}}
```

The mapping is three fields wide. `name` becomes the release name, `replicas` becomes
`replicaCount`, `environment` becomes both a label and an env var. Helm's own view of what it was
given:

```
$ helm get values sample-nf --kube-context kind-nf-orchestrator
USER-SUPPLIED VALUES:
environment: dev
replicaCount: 2
```

```
$ helm status sample-nf --kube-context kind-nf-orchestrator
NAME: sample-nf
LAST DEPLOYED: Wed Jul 22 20:59:05 2026
NAMESPACE: default
STATUS: deployed
REVISION: 1
DESCRIPTION: Install complete
RESOURCES:
==> v1/Deployment
NAME        READY   UP-TO-DATE   AVAILABLE   AGE
sample-nf   2/2     2            2           57s
```

Two pods, carrying the environment from the intent as a label:

```
$ kubectl --context kind-nf-orchestrator get pods -l app=sample-nf -L environment
NAME                         READY   STATUS    RESTARTS   AGE   ENVIRONMENT
sample-nf-5f964655b9-7q8kl   1/1     Running   0          57s   dev
sample-nf-5f964655b9-tqf95   1/1     Running   0          57s   dev
```

## The mapping is real, not hardcoded

Same intent, one field changed, `replicas` 2 to 3. Nothing else touched, no manifest edited:

```
$ curl -s -X POST localhost:8000/deployments \
    -H 'content-type: application/json' \
    -d '{"name":"sample-nf","replicas":3,"environment":"dev"}'
{"release":"sample-nf","status":"deployed","revision":2,"values":{"replicaCount":3,"environment":"dev"}}
```

Revision went to 2 rather than failing on a name collision, because the engine calls
`upgrade --install`. Helm's stored values followed:

```
$ helm get values sample-nf --kube-context kind-nf-orchestrator
USER-SUPPLIED VALUES:
environment: dev
replicaCount: 3
```

And so did the cluster:

```
$ kubectl --context kind-nf-orchestrator get deploy sample-nf
NAME        READY   UP-TO-DATE   AVAILABLE   AGE
sample-nf   2/3     3            2           67s

$ kubectl --context kind-nf-orchestrator rollout status deploy/sample-nf --timeout=90s
deployment "sample-nf" successfully rolled out

$ kubectl --context kind-nf-orchestrator get pods -l app=sample-nf
NAME                         READY   STATUS    RESTARTS   AGE
sample-nf-5f964655b9-5r5kb   1/1     Running   0          27s
sample-nf-5f964655b9-7q8kl   1/1     Running   0          94s
sample-nf-5f964655b9-tqf95   1/1     Running   0          94s
```

## What broke: a name M1 accepts and Helm refuses

The M1 schema capped `name` at 63 characters, the RFC 1123 label limit, on the reasoning that the
name becomes a Kubernetes object name. Helm release names cap at 53. Nothing in M1 knew that,
because M1 had no deploy to fail against.

A 60-character name passes validation:

```
$ curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/intents/validate \
    -H 'content-type: application/json' \
    -d '{"name":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","replicas":1,"environment":"dev"}'
200
```

The same intent sent to the deploy engine gets as far as Helm and dies there:

```
$ curl -s -X POST localhost:8000/deployments -H 'content-type: application/json' \
    -d '{"name":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","replicas":1,"environment":"dev"}'
{"detail":"Error: release name is invalid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

The `502` is the engine doing the right thing with a Helm failure, but the rejection is in the
wrong place. The caller learns their name is unusable only after a request has crossed the API,
been accepted, and reached a subprocess.

Fixed in the M1 model, since that is where the contract lives: `max_length` on `name` lowered from
63 to 53. Because `/intents/schema` is generated from the same model, the published contract moved
with it, and no second definition had to be edited:

The same 60-character intent, resent to `/deployments` after the fix, is now `422` and never
reaches Helm:

```json
{"type":"string_too_long","loc":["body","name"],"msg":"String should have at most 53 characters","ctx":{"max_length":53}}
```

and the published schema reports the new limit:

```json
{"maxLength": 53, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", "title": "Name", "type": "string"}
```

Two tests now pin the boundary, one at 53 characters accepted and one at 54 rejected.
[ADR-0003](../design/adr/0003-intent-schema-contract.md) was corrected, since it stated 63.

The general point is the same one Day 7 produced from a different angle: a contract written ahead
of the thing that consumes it will contain limits that nobody has tested. The deploy is what turned
an assumption about names into a fact.

## Teardown

```
$ helm uninstall sample-nf --kube-context kind-nf-orchestrator
release "sample-nf" uninstalled
```

The kind cluster itself was left running. It is the persistent M0 cluster reused across days, not
scratch infrastructure created for this exercise.

## Not here yet

- **Status reconciliation (planned, M3).** `POST /deployments` returns Helm's view, which is
  "manifest accepted", not "workload healthy". A deploy can return `201` seconds before a pod
  crashes, and nothing here would notice. Lifecycle states and teardown-by-intent are M3.
- **Persistence.** Accepted intents are still not stored. `helm history` is the only record that a
  given intent was ever submitted.
- **Image selection.** The image is a chart default. The intent cannot choose it, so nothing here
  exercises `kind load docker-image`.
- **Observability (planned, M4).** No metrics on deploy success, duration, or failure.
- **CI does not deploy.** `lint-test` has no cluster. The mapping is unit-tested as a pure function
  and the Helm call is monkeypatched; e2e against kind is `(planned, M5)`. The deploys above were
  run by hand.
- `make demo` still prints its placeholder rather than driving the engine.

## Checks

`ruff check .` clean and `pytest` green locally (18 tests), then the same two commands on the pull
request through the repo's one CI job, `lint-test`.
