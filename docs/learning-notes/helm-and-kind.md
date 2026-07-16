# Helm + kind

kind runs a real Kubernetes control plane and node inside a Docker container. Helm templates a
chart's manifests with a values file and tracks the result as a named, upgradeable release.

## Mental model

kind is not a simulator — `kubectl get nodes` talks to an actual API server, just one running in
Docker instead of on bare metal or a cloud VM. Helm sits above `kubectl apply`: a chart is a
template plus a `values.yaml`, `helm install` renders the template with those values into plain
Kubernetes manifests and applies them, and the running result is tracked as a numbered release
(`helm upgrade` produces revision 2, 3, and so on, each one diffable).

The intent → Helm-values mapping described in the project design does not exist yet — this is
`(planned, M2)`. What follows uses a minimal scratch chart written just to exercise the mechanics,
not the real stand-in NF chart.

## Reproducible steps

Cluster already existed from the M0 milestone (`kind-config.yaml`, single control-plane node) and
was reused rather than recreated:

```
$ kubectl --context kind-nf-orchestrator get nodes
NAME                            STATUS   ROLES           AGE   VERSION
nf-orchestrator-control-plane   Ready    control-plane   12d   v1.36.1
```

Minimal chart (`Chart.yaml`, `values.yaml`, `templates/deployment.yaml`) with two values,
`replicaCount` and `image.repository`/`image.tag`, deploying a public `nginx` image as a stand-in
workload.

Install:
```
$ helm install stand-in-nf ./stand-in-nf-chart --kube-context kind-nf-orchestrator
NAME: stand-in-nf
STATUS: deployed
REVISION: 1
```

Pod reaching Running:
```
$ kubectl --context kind-nf-orchestrator get pods -l app=stand-in-nf -w
NAME                           READY   STATUS              RESTARTS   AGE
stand-in-nf-56b5f48675-bqn5z   0/1     ContainerCreating   0          11s
stand-in-nf-56b5f48675-bqn5z   1/1     Running             0          13s
```

Change a value and upgrade (`replicaCount` 1 → 2):
```
$ helm upgrade stand-in-nf ./stand-in-nf-chart --kube-context kind-nf-orchestrator --set replicaCount=2
REVISION: 2

$ kubectl --context kind-nf-orchestrator get pods -l app=stand-in-nf
NAME                           READY   STATUS    RESTARTS   AGE
stand-in-nf-56b5f48675-bqn5z   1/1     Running   0          5m48s
stand-in-nf-56b5f48675-sxrs4   1/1     Running   0          6s
```
Second pod appeared without touching any manifest by hand — the value change alone drove it.

## The gotcha: a nonexistent image doesn't fail at `helm upgrade`

Pointed the chart at an image that doesn't exist anywhere (`stand-in-nf-app:local`, meant to
simulate a locally-built image not yet loaded into the cluster):

```
$ helm upgrade stand-in-nf ./stand-in-nf-chart --kube-context kind-nf-orchestrator \
    --set image.repository=stand-in-nf-app --set image.tag=local
REVISION: 3
DESCRIPTION: Upgrade complete
```
`helm upgrade` reports success — it only confirms the manifest was applied, not that the workload
is actually healthy. The pod told the real story:
```
NAME                           READY   STATUS         RESTARTS   AGE
stand-in-nf-58bbf4fd65-gkfrt   0/1     ErrImagePull   0          8s
```
```
$ kubectl describe pod stand-in-nf-58bbf4fd65-gkfrt
Warning  Failed   kubelet  Failed to pull image "stand-in-nf-app:local": failed to resolve
reference "docker.io/library/stand-in-nf-app:local": pull access denied, repository does not
exist or may require authorization
Warning  Failed   kubelet  Error: ImagePullBackOff
```
kind nodes don't share the host's local Docker image cache. A locally-built image has to be loaded
into the cluster explicitly with `kind load docker-image <image> --name <cluster>` before a pod
can use it — without that, kubelet tries to pull it from a public registry where it doesn't exist.
Fixed here by reverting to the known-good `nginx` image; the real fix for an actual local image is
the `kind load` step, not yet exercised since there's no built image for this project at M0.

## Teardown

```
$ helm uninstall stand-in-nf --kube-context kind-nf-orchestrator
release "stand-in-nf" uninstalled
```
Cluster itself was left running — it's the persistent M0 cluster reused across days, not scratch
infrastructure torn down after each exercise.
