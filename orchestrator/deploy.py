import json
import os
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.intent import Intent

CHART_PATH = Path(__file__).resolve().parent.parent / "charts" / "stand-in-nf"
# The cluster this orchestrator talks to. Overridable so a drill can be run against a
# throwaway cluster without touching the one every other milestone has reused, and so
# nothing has to edit a constant to point somewhere else.
KUBE_CONTEXT = os.environ.get("NF_KUBE_CONTEXT", "kind-nf-orchestrator")

# Substrings that mean "the cluster could not be reached", as opposed to "the cluster
# answered and said no". Collected from real helm and kubectl failures during the M4
# control-plane drill; the Windows wording differs from the POSIX one, so both are here.
UNREACHABLE_MARKERS = (
    "kubernetes cluster unreachable",
    "unable to connect to the server",
    "connection refused",
    "actively refused",
    "no such host",
    "i/o timeout",
)

# Server-side apply refusing to take a field from another manager. The wording comes
# from the M4 drill-3 conflict, where `kubectl scale` owned `.spec.replicas`.
CONFLICT_MARKERS = (
    "conflict occurred while applying",
    "apply failed with",
)


class DeployError(RuntimeError):
    pass


class ClusterUnreachable(DeployError):
    """The cluster could not be reached, so nothing is known about the release.

    Distinct from DeployError, which means the cluster answered and rejected the
    operation. Callers must not read "unreachable" as a fact about a deployment.
    """


class ClusterConflict(DeployError):
    """Another field manager owns a field this apply would change.

    Helm 4 applies server-side, so a field written by something else — `kubectl
    scale`, an autoscaler — is refused rather than silently overwritten. Resolving it
    means deciding who should own the field, which is a caller's decision and not
    one the deploy path makes on its own. See ADR-0008.
    """


def classify_error(message: str) -> DeployError:
    lowered = message.lower()
    if any(marker in lowered for marker in UNREACHABLE_MARKERS):
        return ClusterUnreachable(message)
    if any(marker in lowered for marker in CONFLICT_MARKERS):
        return ClusterConflict(message)
    return DeployError(message)


def render_values(intent: Intent) -> dict[str, Any]:
    return {
        "replicaCount": intent.replicas,
        "environment": intent.environment,
    }


def release_name(intent: Intent) -> str:
    return intent.name


def run_helm(args: list[str]) -> str:
    result = subprocess.run(
        ["helm", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise classify_error(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def run_kubectl(args: list[str]) -> str:
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise classify_error(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def check_cluster() -> str:
    """Raise ClusterUnreachable unless the API server answers its own readyz probe."""
    return run_kubectl(["get", "--raw=/readyz"]).strip()


def list_releases() -> list[str]:
    """Every Helm release name in the context, so something can iterate them."""
    output = run_helm(
        ["list", "--kube-context", KUBE_CONTEXT, "--output", "json"]
    )
    return [release["name"] for release in json.loads(output)]


def deploy(intent: Intent, force_conflicts: bool = False) -> dict[str, Any]:
    """Apply an intent. force_conflicts takes ownership of fields another manager holds.

    The default is False and the deploy path never sets it. Forcing is an explicit,
    separately named operation, because it overrides whatever else was writing to
    those fields — see ADR-0008.
    """
    values = render_values(intent)
    name = release_name(intent)
    args = [
        "upgrade",
        "--install",
        name,
        str(CHART_PATH),
        "--kube-context",
        KUBE_CONTEXT,
        "--set-json",
        json.dumps(values),
        "--output",
        "json",
    ]
    if force_conflicts:
        args.append("--force-conflicts")
    output = run_helm(args)
    info = json.loads(output)["info"]
    return {
        "release": name,
        "status": info["status"],
        "revision": json.loads(output)["version"],
        "values": values,
    }
