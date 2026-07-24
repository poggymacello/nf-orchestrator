import json
import subprocess
from enum import Enum
from typing import Any

from orchestrator.deploy import KUBE_CONTEXT, DeployError, run_helm

NOT_FOUND = "not found"


class State(str, Enum):
    NOT_INSTANTIATED = "NOT_INSTANTIATED"
    INSTANTIATING = "INSTANTIATING"
    INSTANTIATED = "INSTANTIATED"
    FAILED = "FAILED"


FAILED_POD_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"}
)


def derive_state(helm_status: str | None, pod_phases: list[dict[str, Any]]) -> State:
    """Project a Helm release status and the pods' phases onto one lifecycle state.

    helm_status is the release's `status` field (`deployed`, `failed`, ...) or None
    when no release exists. pod_phases is a list of {"phase", "reason"} dicts, one
    per pod carrying the release's selector.
    """
    if helm_status is None:
        return State.NOT_INSTANTIATED
    if helm_status == "failed":
        return State.FAILED

    reasons = {p.get("reason") for p in pod_phases}
    if reasons & FAILED_POD_REASONS:
        return State.FAILED

    phases = {p.get("phase") for p in pod_phases}
    if not pod_phases:
        return State.INSTANTIATING
    if phases == {"Running"}:
        return State.INSTANTIATED
    return State.INSTANTIATING


def helm_release_status(name: str) -> str | None:
    try:
        output = run_helm(
            ["status", name, "--kube-context", KUBE_CONTEXT, "--output", "json"]
        )
    except DeployError:
        return None
    return json.loads(output)["info"]["status"]


def pod_phases(name: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            KUBE_CONTEXT,
            "get",
            "pods",
            "-l",
            f"app={name}",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DeployError(result.stderr.strip() or result.stdout.strip())
    items = json.loads(result.stdout)["items"]
    phases: list[dict[str, Any]] = []
    for item in items:
        phase = item["status"].get("phase")
        reason = None
        for container in item["status"].get("containerStatuses", []):
            waiting = container.get("state", {}).get("waiting")
            if waiting:
                reason = waiting.get("reason")
                break
        phases.append({"phase": phase, "reason": reason})
    return phases


def reconcile(name: str) -> dict[str, Any]:
    helm_status = helm_release_status(name)
    pods = [] if helm_status is None else pod_phases(name)
    state = derive_state(helm_status, pods)
    return {
        "release": name,
        "state": state.value,
        "helm_status": helm_status,
        "pods": pods,
    }


def teardown(name: str) -> dict[str, Any]:
    if helm_release_status(name) is None:
        return {"release": name, "state": State.NOT_INSTANTIATED.value, "uninstalled": False}
    run_helm(["uninstall", name, "--kube-context", KUBE_CONTEXT])
    return {"release": name, "state": State.NOT_INSTANTIATED.value, "uninstalled": True}
