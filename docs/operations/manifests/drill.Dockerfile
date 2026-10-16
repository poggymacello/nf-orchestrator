# The orchestrator in a Linux container, for drills that need Linux resource limits.
#
# Drill 14 (pids limit) and drill 15 (missing tools, failed spawns) both ran the orchestrator
# here, on the kind Docker network, so a real cgroup limit could be applied to it with
# `docker update --pids-limit N`. This image is a drill fixture: not hardened, not scanned,
# not published. Build it from the repository root:
#
#   docker build -f docs/operations/manifests/drill.Dockerfile -t nf-orchestrator-drill .
#
# and run it against a kind cluster's internal kubeconfig (`kind get kubeconfig --internal`):
#
#   docker run -d --network kind -p 8014:8000 -v <kubeconfig>:/kube/config:ro \
#     -e KUBECONFIG=/kube/config -e NF_KUBE_CONTEXT=kind-<cluster> nf-orchestrator-drill
FROM debian:trixie-slim

ARG HELM_VERSION=v4.2.2
ARG KUBECTL_VERSION=v1.35.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Both tools come from their projects' own release sites, each checked against the
# checksum published beside it.
RUN set -eu; cd /tmp; \
    curl -fsSLO "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz"; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" | sha256sum -c -; \
    tar -xzf "helm-${HELM_VERSION}-linux-amd64.tar.gz"; \
    install -m 0755 linux-amd64/helm /usr/local/bin/helm; \
    curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; \
    echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256")  kubectl" | sha256sum -c -; \
    install -m 0755 kubectl /usr/local/bin/kubectl; \
    rm -rf /tmp/*

COPY . /app
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir /app
WORKDIR /app
ENV PATH=/venv/bin:$PATH
EXPOSE 8000
CMD ["uvicorn", "orchestrator:app", "--host", "0.0.0.0", "--port", "8000"]
