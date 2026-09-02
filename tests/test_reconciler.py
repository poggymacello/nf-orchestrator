import json

import pytest
from fastapi.testclient import TestClient

from orchestrator import app, reconciler
from orchestrator.deploy import ClusterUnreachable, DeployError
from orchestrator.reconciler import State, derive_state

client = TestClient(app)


def ready(count: int) -> list[dict[str, object]]:
    return [{"phase": "Running", "reason": None, "ready": True}] * count


def test_no_release_is_not_instantiated() -> None:
    assert derive_state(None, [], 2) is State.NOT_INSTANTIATED


def test_deployed_with_no_pods_yet_is_instantiating() -> None:
    assert derive_state("deployed", [], 2) is State.INSTANTIATING


def test_deployed_while_pod_pending_is_instantiating() -> None:
    pods = [{"phase": "Pending", "reason": "ContainerCreating", "ready": False}]
    assert derive_state("deployed", pods, 1) is State.INSTANTIATING


def test_all_desired_pods_ready_is_instantiated() -> None:
    assert derive_state("deployed", ready(2), 2) is State.INSTANTIATED


def test_one_pod_still_pending_holds_instantiating() -> None:
    pods = ready(1) + [{"phase": "Pending", "reason": None, "ready": False}]
    assert derive_state("deployed", pods, 2) is State.INSTANTIATING


def test_image_pull_backoff_is_failed() -> None:
    pods = [{"phase": "Pending", "reason": "ImagePullBackOff", "ready": False}]
    assert derive_state("deployed", pods, 1) is State.FAILED


def test_crash_loop_is_failed() -> None:
    pods = [{"phase": "Running", "reason": "CrashLoopBackOff", "ready": False}]
    assert derive_state("deployed", pods, 1) is State.FAILED


def test_failed_helm_release_is_failed() -> None:
    assert derive_state("failed", ready(1), 1) is State.FAILED


# --- M4 drill 1: INSTANTIATED must mean the intent is satisfied ---


def test_fewer_ready_pods_than_desired_is_not_instantiated() -> None:
    """Drill 1, finding 1: one running pod of a three-replica intent read INSTANTIATED."""
    assert derive_state("deployed", ready(1), 3) is State.INSTANTIATING


def test_more_pods_than_desired_is_not_instantiated() -> None:
    """Drill 1, finding 1: six pods of a three-replica intent read INSTANTIATED."""
    assert derive_state("deployed", ready(6), 3) is State.INSTANTIATING


def test_running_but_unready_pod_is_not_instantiated() -> None:
    """Drill 2, finding 2: CreateContainerConfigError under a Running phase read
    INSTANTIATED because the reason was not in the failure allowlist."""
    pods = [{"phase": "Running", "reason": "CreateContainerConfigError", "ready": False}]
    assert derive_state("deployed", pods, 1) is State.INSTANTIATING


def test_unknown_waiting_reason_still_blocks_instantiated() -> None:
    pods = [{"phase": "Running", "reason": "SomeReasonInventedTomorrow", "ready": False}]
    assert derive_state("deployed", pods, 1) is State.INSTANTIATING


def test_terminal_pod_phase_is_failed() -> None:
    """Drill 1, finding 2: Succeeded had no branch and fell through to INSTANTIATING."""
    pods = [{"phase": "Succeeded", "reason": None, "ready": False}]
    assert derive_state("deployed", pods, 1) is State.FAILED


def test_zero_desired_is_never_instantiated() -> None:
    assert derive_state("deployed", [], 0) is State.INSTANTIATING


def test_terminating_pods_are_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drill 1: mid-replacement, dying pods doubled the count for a 3-replica intent."""
    payload = {
        "items": [
            {
                "metadata": {"deletionTimestamp": "2026-08-20T12:49:10Z"},
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [{"ready": False, "state": {}}],
                },
            },
            {
                "metadata": {},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True, "state": {}}],
                },
            },
        ]
    }
    monkeypatch.setattr(reconciler, "run_kubectl", lambda args: json.dumps(payload))
    assert reconciler.pod_states("sample-nf") == [
        {"phase": "Running", "reason": None, "ready": True}
    ]


# --- M4 drill 2: unreachable is not a lifecycle state ---


def test_release_not_found_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_helm(args: list[str]) -> str:
        raise DeployError("Error: release: not found")

    monkeypatch.setattr(reconciler, "run_helm", fake_run_helm)
    assert reconciler.helm_release_status("ghost") is None


def test_unreachable_cluster_is_not_reported_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_helm(args: list[str]) -> str:
        raise ClusterUnreachable("Error: Kubernetes cluster unreachable")

    monkeypatch.setattr(reconciler, "run_helm", fake_run_helm)
    with pytest.raises(ClusterUnreachable):
        reconciler.helm_release_status("sample-nf")


def test_unexpected_helm_error_is_not_reported_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_helm(args: list[str]) -> str:
        raise DeployError("Error: forbidden: user cannot list releases")

    monkeypatch.setattr(reconciler, "run_helm", fake_run_helm)
    with pytest.raises(DeployError):
        reconciler.helm_release_status("sample-nf")


def test_status_endpoint_returns_503_when_cluster_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(name: str) -> str:
        raise ClusterUnreachable("Error: Kubernetes cluster unreachable")

    monkeypatch.setattr(reconciler, "helm_release_status", unreachable)
    response = client.get("/deployments/sample-nf")
    assert response.status_code == 503
    assert "unreachable" in response.json()["detail"].lower()


# --- endpoint behaviour ---


def test_status_endpoint_reports_derived_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "helm_release_status", lambda name: "deployed")
    monkeypatch.setattr(reconciler, "desired_replicas", lambda name: 1)
    monkeypatch.setattr(reconciler, "pod_states", lambda name: ready(1))
    response = client.get("/deployments/sample-nf")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "INSTANTIATED"
    assert body["desired_replicas"] == 1
    assert body["ready_replicas"] == 1


def test_status_endpoint_shows_the_shortfall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "helm_release_status", lambda name: "deployed")
    monkeypatch.setattr(reconciler, "desired_replicas", lambda name: 3)
    monkeypatch.setattr(reconciler, "pod_states", lambda name: ready(1))
    body = client.get("/deployments/sample-nf").json()
    assert body["state"] == "INSTANTIATING"
    assert (body["desired_replicas"], body["ready_replicas"]) == (3, 1)


def test_status_endpoint_reports_not_instantiated_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciler, "helm_release_status", lambda name: None)
    response = client.get("/deployments/ghost")
    assert response.status_code == 200
    assert response.json()["state"] == "NOT_INSTANTIATED"
    assert response.json()["pods"] == []


def test_teardown_uninstalls_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(reconciler, "helm_release_status", lambda name: "deployed")
    monkeypatch.setattr(reconciler, "run_helm", lambda args: calls.append(args) or "")
    response = client.delete("/deployments/sample-nf")
    assert response.status_code == 200
    assert response.json() == {
        "release": "sample-nf",
        "state": "NOT_INSTANTIATED",
        "uninstalled": True,
    }
    assert calls[0][:2] == ["uninstall", "sample-nf"]


def test_teardown_is_a_noop_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "helm_release_status", lambda name: None)
    response = client.delete("/deployments/ghost")
    assert response.status_code == 200
    assert response.json()["uninstalled"] is False
