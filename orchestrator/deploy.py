import json
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.intent import Intent

CHART_PATH = Path(__file__).resolve().parent.parent / "charts" / "stand-in-nf"
KUBE_CONTEXT = "kind-nf-orchestrator"

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


class DeployError(RuntimeError):
    pass


class ClusterUnreachable(DeployError):
    """The cluster could not be reached, so nothing is known about the release.

    Distinct from DeployError, which means the cluster answered and rejected the
    operation. Callers must not read "unreachable" as a fact about a deployment.
    """


def classify_error(message: str) -> DeployError:
    lowered = message.lower()
    if any(marker in lowered for marker in UNREACHABLE_MARKERS):
        return ClusterUnreachable(message)
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


def deploy(intent: Intent) -> dict[str, Any]:
    values = render_values(intent)
    name = release_name(intent)
    output = run_helm(
        [
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
    )
    info = json.loads(output)["info"]
    return {
        "release": name,
        "status": info["status"],
        "revision": json.loads(output)["version"],
        "values": values,
    }
