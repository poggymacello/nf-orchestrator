import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from orchestrator import reconciler
from orchestrator.reconciler import State, derive_state

client = TestClient(app)


def test_no_release_is_not_instantiated() -> None:
    assert derive_state(None, []) is State.NOT_INSTANTIATED


def test_deployed_with_no_pods_yet_is_instantiating() -> None:
    assert derive_state("deployed", []) is State.INSTANTIATING


def test_deployed_while_pod_pending_is_instantiating() -> None:
    pods = [{"phase": "Pending", "reason": "ContainerCreating"}]
    assert derive_state("deployed", pods) is State.INSTANTIATING


def test_deployed_and_all_running_is_instantiated() -> None:
    pods = [{"phase": "Running", "reason": None}, {"phase": "Running", "reason": None}]
    assert derive_state("deployed", pods) is State.INSTANTIATED


def test_one_pod_still_pending_holds_instantiating() -> None:
    pods = [{"phase": "Running", "reason": None}, {"phase": "Pending", "reason": None}]
    assert derive_state("deployed", pods) is State.INSTANTIATING


def test_image_pull_backoff_is_failed() -> None:
    pods = [{"phase": "Pending", "reason": "ImagePullBackOff"}]
    assert derive_state("deployed", pods) is State.FAILED


def test_crash_loop_is_failed() -> None:
    pods = [{"phase": "Running", "reason": "CrashLoopBackOff"}]
    assert derive_state("deployed", pods) is State.FAILED


def test_failed_helm_release_is_failed() -> None:
    assert derive_state("failed", [{"phase": "Running", "reason": None}]) is State.FAILED


def test_status_endpoint_reports_derived_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "helm_release_status", lambda name: "deployed")
    monkeypatch.setattr(
        reconciler, "pod_phases", lambda name: [{"phase": "Running", "reason": None}]
    )
    response = client.get("/deployments/sample-nf")
    assert response.status_code == 200
    assert response.json()["state"] == "INSTANTIATED"


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
