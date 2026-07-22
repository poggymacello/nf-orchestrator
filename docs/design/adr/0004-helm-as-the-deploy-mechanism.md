# ADR-0004: Helm is the deploy mechanism, the release is the unit of lifecycle

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

M1 ends with a validated intent and nothing else. Turning that intent into a running network
function means producing Kubernetes manifests from it and applying them, then being able to change
them later when the intent changes, and remove them when the intent goes away.

Three fields have to reach the cluster: how many instances to run, which environment the workload
belongs to, and what to call the thing. The last one matters more than it looks, because whatever
identifies the deployed workload is what M3 (planned) will reconcile status against.

## Decision

**Helm renders intent-derived values into manifests.** The orchestrator does not build Kubernetes
objects in Python and does not shell out to `kubectl apply`. It renders a values dictionary from
the intent and hands it to `helm upgrade --install`, against the chart at
[`charts/stand-in-nf/`](../../../charts/stand-in-nf).

**The mapping is a pure function.** `render_values` in
[`orchestrator/deploy.py`](../../../orchestrator/deploy.py) takes an `Intent` and returns values,
with no cluster involved:

| Intent field (M1 schema) | Helm value | Where it lands |
|---|---|---|
| `name` | release name | `metadata.name`, the `app` label, the pod name prefix |
| `replicas` | `replicaCount` | `spec.replicas` on the Deployment |
| `environment` | `environment` | `environment` label and the `NF_ENVIRONMENT` env var |

Nothing else is read from the intent, and nothing in the values is invented by the engine. The
image is a chart default, not an intent field, so it is not caller-controlled at M2.

**`upgrade --install`, not `install`.** The same call handles the first deploy and every later one.
Submitting a changed intent for a name that already exists produces a new revision rather than a
name-collision error, which is the behaviour an intent-driven system needs: the caller declares the
desired state and does not track whether it has declared it before.

**The release is the unit of lifecycle.** One intent maps to one Helm release, named after the
intent. That gives a single handle for status (`helm status`), for history (`helm history`, one
revision per accepted intent), and for teardown (`helm uninstall`). M3 (planned) reconciles against
that release rather than against loose Kubernetes objects.

**kind is the target.** The engine passes `--kube-context kind-nf-orchestrator`. A local
single-node cluster is enough to exercise the real API server, and it costs nothing to recreate.

**Helm failures surface as HTTP 502.** `POST /deployments` returns `201` with the release name,
status, revision, and the rendered values. If Helm exits non-zero, its stderr is returned as a
`502` body. A validation failure is still `422` from the M1 model, so a caller can tell a bad
intent from a working intent that the cluster refused.

## Verified against a real deploy on 2026-07-22

`POST /deployments` with `{"name": "sample-nf", "replicas": 2, "environment": "dev"}` returned
`201`:

```json
{"release":"sample-nf","status":"deployed","revision":1,"values":{"replicaCount":2,"environment":"dev"}}
```

```
$ helm get values sample-nf --kube-context kind-nf-orchestrator
USER-SUPPLIED VALUES:
environment: dev
replicaCount: 2

$ kubectl --context kind-nf-orchestrator get pods -l app=sample-nf -L environment
NAME                         READY   STATUS    RESTARTS   AGE   ENVIRONMENT
sample-nf-5f964655b9-7q8kl   1/1     Running   0          57s   dev
sample-nf-5f964655b9-tqf95   1/1     Running   0          57s   dev
```

Resubmitting the same intent with `replicas: 3` produced revision 2 and a third pod. The full run,
including the failure that changed the M1 schema, is in
[the M2 build log](../../build-log/m2-deploy-engine.md).

## Consequences

- **Good:** rendering, diffing, and rollback come from Helm rather than from code in this repo.
  `helm history` is an audit trail of accepted intents at no extra cost.
- **Good:** the mapping is a pure function, so CI covers it without a cluster. The Helm call is the
  only part that needs one.
- **Cost:** the engine shells out to a binary. `helm` has to be on PATH wherever the API runs, and
  errors arrive as stderr text rather than structured data.
- **Cost:** Helm reports success when the manifest is accepted, not when the workload is healthy. A
  deploy can return `201` while the pod never reaches Running. Closing that gap is M3 (planned).
- **Constraint:** the intent contract inherits Helm's limits. Release names cap at 53 characters,
  which is stricter than the RFC 1123 limit the schema first used. ADR-0003 was corrected after a
  real deploy hit it.

## Alternatives considered

- **Build manifests in Python and apply them with the Kubernetes client.** Rejected: templating,
  revision history, and uninstall would all have to be written and maintained here, and Helm charts
  are what an operator would expect to receive.
- **`kubectl apply -f` on a rendered template.** Rejected: it applies manifests but keeps no
  release identity, so there is nothing to ask for status or history later.
- **`helm install` with a generated unique release name per submission.** Rejected: a second
  submission of the same intent would create a second workload instead of converging the first.
- **Running Helm's Go SDK in-process.** Rejected for M2: it removes the PATH dependency but pulls
  in a large dependency surface for a call this repo makes in one place.
