import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from orchestrator import deploy as deploy_engine

client = TestClient(app)


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_ready_when_the_cluster_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_engine, "check_cluster", lambda: "ok")
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "cluster": "reachable"}


def test_readyz_is_503_when_the_cluster_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable() -> str:
        raise deploy_engine.ClusterUnreachable("Kubernetes cluster unreachable")

    monkeypatch.setattr(deploy_engine, "check_cluster", unreachable)
    assert client.get("/readyz").status_code == 503


def test_healthz_stays_green_while_the_cluster_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M4 drill 2, finding 3: /healthz is liveness only, and that is now deliberate."""

    def unreachable() -> str:
        raise deploy_engine.ClusterUnreachable("Kubernetes cluster unreachable")

    monkeypatch.setattr(deploy_engine, "check_cluster", unreachable)
    assert client.get("/healthz").json() == {"status": "ok"}
