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

# Every helm and kubectl call is bounded. Drill 6 froze the control plane and found the
# only limit in play was Go's 10-second TLS handshake timeout inside kubectl and helm —
# an accident of that failure's shape, which a server that completes the handshake and
# then stalls would not trigger. Ten seconds was also twice Prometheus's scrape timeout,
# so the metric the ClusterUnreachable alert reads never reached Prometheus at all.
#
# Reads serve the status endpoint and the metrics collector, and Prometheus gives a scrape
# 5s, so a read has to fail well inside that. Writes can legitimately take longer.
READ_TIMEOUT = float(os.environ.get("NF_READ_TIMEOUT", "3"))
WRITE_TIMEOUT = float(os.environ.get("NF_WRITE_TIMEOUT", "60"))
WRITE_VERBS = frozenset({"upgrade", "install", "uninstall", "rollback"})

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


def run_bounded(command: list[str], args: list[str], timeout: float) -> str:
    """Run a cluster command, failing as unreachable rather than waiting indefinitely.

    `args` are the caller's arguments without the context flags, so a timeout names
    the verb that stalled — drill 7 got "kubectl --context did not answer".
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        verb = " ".join([command[0], *args[:1]])
        raise ClusterUnreachable(f"{verb} did not answer within {timeout:g}s") from exc
    if result.returncode != 0:
        raise classify_error(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def timeout_for(args: list[str]) -> float:
    return WRITE_TIMEOUT if args and args[0] in WRITE_VERBS else READ_TIMEOUT


def run_helm(args: list[str]) -> str:
    return run_bounded(["helm", *args], args, timeout_for(args))


def run_kubectl(args: list[str]) -> str:
    return run_bounded(
        ["kubectl", "--context", KUBE_CONTEXT, *args], args, timeout_for(args)
    )


def check_cluster() -> str:
    """Raise ClusterUnreachable unless the API server answers its own readyz probe."""
    return run_kubectl(["get", "--raw=/readyz"]).strip()


LIST_PAGE = 256


def list_releases(page: int = LIST_PAGE) -> list[str]:
    """Every Helm release name in the context, so something can iterate them.

    Paged, because `helm list` returns at most 256 releases unless told otherwise and
    says nothing when it stops. A release past the cut would have had no series and no
    `nf_release_reported` either — the one alert that names unmonitored releases can
    only name releases it was told exist. Found on day 26 reading `helm list --help`;
    `--max 0` does not mean "all", it means the server's default.

    Pages are read by offset over helm's alphabetical order, so a release installed or
    removed between two pages can shift the boundary; names are de-duplicated, and the
    next scrape lists again from the start.
    """
    names: dict[str, None] = {}
    offset = 0
    while True:
        batch = json.loads(
            run_helm(
                [
                    "list",
                    "--kube-context",
                    KUBE_CONTEXT,
                    "--max",
                    str(page),
                    "--offset",
                    str(offset),
                    "--output",
                    "json",
                ]
            )
        )
        names.update((release["name"], None) for release in batch)
        if len(batch) < page:
            return list(names)
        offset += page


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
