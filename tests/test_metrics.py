import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from orchestrator import deploy as deploy_engine
from orchestrator import metrics

client = TestClient(app)

VALID = {"name": "sample-nf", "replicas": 2, "environment": "dev"}


def counter_value(result: str, environment: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "deployments_total", {"result": result, "environment": environment}
        )
        or 0.0
    )


def test_metrics_endpoint_serves_the_registry() -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "deployments_total" in response.text


def test_successful_deploy_increments_the_success_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy_engine,
        "run_helm",
        lambda args: '{"name": "sample-nf", "version": 1, "info": {"status": "deployed"}}',
    )
    before = counter_value("success", "dev")
    assert client.post("/deployments", json=VALID).status_code == 201
    assert counter_value("success", "dev") == before + 1


def test_failed_deploy_increments_the_failed_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(args: list[str]) -> str:
        raise deploy_engine.DeployError("release name is invalid")

    monkeypatch.setattr(deploy_engine, "run_helm", boom)
    before = counter_value("failed", "dev")
    assert client.post("/deployments", json=VALID).status_code == 502
    assert counter_value("failed", "dev") == before + 1


def test_rejected_intent_records_no_sample() -> None:
    before = counter_value("failed", "dev")
    response = client.post("/deployments", json={**VALID, "environment": "production"})
    assert response.status_code == 422
    assert counter_value("failed", "dev") == before
