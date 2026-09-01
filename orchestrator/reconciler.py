import json
from enum import Enum
from typing import Any

from orchestrator.deploy import (
    KUBE_CONTEXT,
    ClusterUnreachable,
    DeployError,
    run_helm,
    run_kubectl,
)

RELEASE_NOT_FOUND = "not found"


class State(str, Enum):
    """What is true of the network function itself."""

    NOT_INSTANTIATED = "NOT_INSTANTIATED"
    INSTANTIATING = "INSTANTIATING"
    INSTANTIATED = "INSTANTIATED"
    FAILED = "FAILED"


class OperationState(str, Enum):
    """What happened to the last lifecycle operation, separately from the NF.

    A subset of ETSI SOL003's LcmOperationStateType. UNKNOWN exists so that a Helm
    status nobody anticipated does not quietly land in a bucket that means something
    specific — the mistake drill 1 found when `Succeeded` fell through to
    INSTANTIATING.
    """

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


OPERATION_STATUS = {
    "deployed": OperationState.COMPLETED,
    "uninstalled": OperationState.COMPLETED,
    "superseded": OperationState.COMPLETED,
    "failed": OperationState.FAILED,
    "pending-install": OperationState.PROCESSING,
    "pending-upgrade": OperationState.PROCESSING,
    "pending-rollback": OperationState.PROCESSING,
    "uninstalling": OperationState.PROCESSING,
}


FAILED_POD_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"}
)

# A pod under a Deployment restarts forever, so reaching a terminal phase while it is
# not being deleted means the workload stopped and was not replaced.
TERMINAL_POD_PHASES = frozenset({"Succeeded", "Failed"})


def derive_state(
    helm_status: str | None,
    pods: list[dict[str, Any]],
    desired: int,
    operation: OperationState = OperationState.UNKNOWN,
    ever_deployed: bool = True,
) -> State:
    """Project the live signals onto one state describing the network function.

    helm_status is the release's `status` field, or None when no release exists. pods is
    one {"phase", "reason", "ready"} dict per non-terminating pod carrying the release's
    selector. desired is the replica count of the intent in effect. operation and
    ever_deployed describe the *last operation*, not the NF.

    INSTANTIATED means the intent is satisfied: exactly `desired` pods exist and every
    one of them is ready. It is not enough for the pods that happen to exist to look
    healthy — the M4 drill-1 defect, where one running pod of a three-replica intent,
    and six pods of the same intent, both reported INSTANTIATED.

    A failed operation is deliberately **not** a failed NF. Until M4 a `failed` release
    status alone returned FAILED, so a rejected upgrade made a workload that was still
    serving read FAILED with nothing about it having changed (drill 3, finding 2). The
    operation is reported separately; see ADR-0009.
    """
    if helm_status is None:
        return State.NOT_INSTANTIATED

    reasons = {pod.get("reason") for pod in pods}
    if reasons & FAILED_POD_REASONS:
        return State.FAILED
    if any(pod.get("phase") in TERMINAL_POD_PHASES for pod in pods):
        return State.FAILED

    if desired > 0 and len(pods) == desired and all(pod.get("ready") for pod in pods):
        return State.INSTANTIATED

    # A release that never deployed anything, whose attempt to do so failed, will not
    # progress on its own. Without this it would report INSTANTIATING indefinitely.
    if not ever_deployed and operation is OperationState.FAILED:
        return State.FAILED

    return State.INSTANTIATING


def helm_release_status(name: str) -> str | None:
    """Return the release status, or None only when the release genuinely does not exist.

    An unreachable cluster is not an answer about the release, so ClusterUnreachable
    propagates rather than being flattened into None. Collapsing the two was the M4
    drill-2 defect: a running NF read as NOT_INSTANTIATED at HTTP 200 during an outage.
    """
    try:
        output = run_helm(
            ["status", name, "--kube-context", KUBE_CONTEXT, "--output", "json"]
        )
    except ClusterUnreachable:
        raise
    except DeployError as exc:
        if RELEASE_NOT_FOUND in str(exc).lower():
            return None
        raise
    return json.loads(output)["info"]["status"]


def release_history(name: str) -> list[dict[str, Any]]:
    """Every revision Helm still holds for the release, newest last."""
    output = run_helm(
        ["history", name, "--kube-context", KUBE_CONTEXT, "--output", "json"]
    )
    return json.loads(output)


def deployed_revision(history: list[dict[str, Any]]) -> int | None:
    """The highest revision that actually reached `deployed`, or None if none did."""
    revisions = [
        entry["revision"] for entry in history if entry.get("status") == "deployed"
    ]
    return max(revisions) if revisions else None


def last_operation(history: list[dict[str, Any]]) -> dict[str, Any]:
    """What happened to the most recent lifecycle operation, whatever the NF is doing.

    Reported alongside the state rather than folded into it: a failed upgrade is a
    fact about an operation, not about the network function that is still serving.
    """
    if not history:
        return {
            "revision": None,
            "state": OperationState.UNKNOWN.value,
            "description": None,
        }
    latest = max(history, key=lambda entry: entry["revision"])
    state = OPERATION_STATUS.get(latest.get("status"), OperationState.UNKNOWN)
    return {
        "revision": latest["revision"],
        "state": state.value,
        "description": latest.get("description"),
    }


def last_deployed_revision(name: str) -> int | None:
    """The highest revision that actually reached `deployed`, or None if none did.

    `helm get values` without a revision returns the *latest* revision's values, which
    includes an upgrade that failed and never applied. Reading that would report a
    desired count from an intent the cluster rejected.
    """
    return deployed_revision(release_history(name))


def desired_replicas(name: str, history: list[dict[str, Any]] | None = None) -> int:
    """The replica count of the intent that is actually in effect.

    Read from the last *deployed* revision's values, not the latest revision's: a
    failed upgrade leaves its values on the release without ever having applied them.
    `--all` includes chart defaults, so this answers even for a release deployed
    without an explicit replicaCount.

    Deliberately not the Deployment's `spec.replicas`: if something scaled the
    Deployment outside Helm, the intent is still the number to hold the cluster
    against. Returns 0 when no revision ever deployed, because then no intent is in
    effect and there is nothing to hold it against.
    """
    revision = deployed_revision(
        release_history(name) if history is None else history
    )
    if revision is None:
        return 0
    output = run_helm(
        [
            "get",
            "values",
            name,
            "--kube-context",
            KUBE_CONTEXT,
            "--revision",
            str(revision),
            "--all",
            "--output",
            "json",
        ]
    )
    values = json.loads(output)
    replicas = values.get("replicaCount")
    if replicas is None:
        raise DeployError(f"release {name!r} has no replicaCount in its values")
    return int(replicas)


def pod_states(name: str) -> list[dict[str, Any]]:
    """Return phase, waiting-reason and readiness for each live pod of the release.

    Pods with a deletionTimestamp are excluded. They are already on their way out and
    counting them made a three-replica intent report six pods mid-replacement.
    """
    output = run_kubectl(["get", "pods", "-l", f"app={name}", "--output", "json"])
    states: list[dict[str, Any]] = []
    for item in json.loads(output)["items"]:
        if item["metadata"].get("deletionTimestamp"):
            continue
        containers = item["status"].get("containerStatuses", [])
        reason = None
        for container in containers:
            waiting = container.get("state", {}).get("waiting")
            if waiting:
                reason = waiting.get("reason")
                break
        states.append(
            {
                "phase": item["status"].get("phase"),
                "reason": reason,
                "ready": bool(containers) and all(c.get("ready") for c in containers),
            }
        )
    return states


def reconcile(name: str) -> dict[str, Any]:
    helm_status = helm_release_status(name)
    if helm_status is None:
        return {
            "release": name,
            "state": State.NOT_INSTANTIATED.value,
            "helm_status": None,
            "desired_replicas": 0,
            "ready_replicas": 0,
            "last_operation": last_operation([]),
            "pods": [],
        }
    history = release_history(name)
    operation = last_operation(history)
    desired = desired_replicas(name, history)
    pods = pod_states(name)
    state = derive_state(
        helm_status,
        pods,
        desired,
        operation=OperationState(operation["state"]),
        ever_deployed=deployed_revision(history) is not None,
    )
    return {
        "release": name,
        "state": state.value,
        "helm_status": helm_status,
        "desired_replicas": desired,
        "ready_replicas": sum(1 for pod in pods if pod.get("ready")),
        "last_operation": operation,
        "pods": pods,
    }


def teardown(name: str) -> dict[str, Any]:
    if helm_release_status(name) is None:
        return {"release": name, "state": State.NOT_INSTANTIATED.value, "uninstalled": False}
    run_helm(["uninstall", name, "--kube-context", KUBE_CONTEXT])
    return {"release": name, "state": State.NOT_INSTANTIATED.value, "uninstalled": True}
