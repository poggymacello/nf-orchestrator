import json

import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from orchestrator import deploy as deploy_engine
from orchestrator.intent import Intent

client = TestClient(app)

VALID = {"name": "sample-nf", "replicas": 2, "environment": "dev"}

HELM_OUTPUT = json.dumps(
    {"name": "sample-nf", "version": 1, "info": {"status": "deployed"}}
)


def test_values_come_from_the_intent() -> None:
    values = deploy_engine.render_values(Intent.model_validate(VALID))
    assert values == {"replicaCount": 2, "environment": "dev"}


def test_changing_replicas_changes_rendered_values() -> None:
    two = deploy_engine.render_values(Intent.model_validate(VALID))
    three = deploy_engine.render_values(Intent.model_validate({**VALID, "replicas": 3}))
    assert two["replicaCount"] == 2
    assert three["replicaCount"] == 3


def test_release_name_is_the_intent_name() -> None:
    assert deploy_engine.release_name(Intent.model_validate(VALID)) == "sample-nf"


def test_deploy_passes_rendered_values_to_helm(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run_helm(args: list[str]) -> str:
        captured.append(args)
        return HELM_OUTPUT

    monkeypatch.setattr(deploy_engine, "run_helm", fake_run_helm)

    result = deploy_engine.deploy(Intent.model_validate(VALID))

    assert result == {
        "release": "sample-nf",
        "status": "deployed",
        "revision": 1,
        "values": {"replicaCount": 2, "environment": "dev"},
    }
    args = captured[0]
    assert args[:3] == ["upgrade", "--install", "sample-nf"]
    assert json.loads(args[args.index("--set-json") + 1]) == {
        "replicaCount": 2,
        "environment": "dev",
    }


def test_deployment_endpoint_returns_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_engine, "run_helm", lambda args: HELM_OUTPUT)
    response = client.post("/deployments", json=VALID)
    assert response.status_code == 201
    assert response.json()["release"] == "sample-nf"
    assert response.json()["values"] == {"replicaCount": 2, "environment": "dev"}


def test_deployment_endpoint_rejects_invalid_intent() -> None:
    response = client.post("/deployments", json={**VALID, "environment": "production"})
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["literal_error"]


def test_helm_failure_surfaces_as_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_helm(args: list[str]) -> str:
        raise deploy_engine.DeployError("chart not found")

    monkeypatch.setattr(deploy_engine, "run_helm", fake_run_helm)
    response = client.post("/deployments", json=VALID)
    assert response.status_code == 502
    assert response.json()["detail"] == "chart not found"


def test_unreachable_cluster_is_classified_apart_from_a_refusal() -> None:
    """M4 drill 2: helm's and kubectl's unreachable wordings, POSIX and Windows."""
    unreachable = [
        'Error: Kubernetes cluster unreachable: Get "https://127.0.0.1:64114/version"',
        "Unable to connect to the server: dial tcp 127.0.0.1:64114: connection refused",
        (
            "connectex: No connection could be made because the target machine "
            "actively refused it."
        ),
    ]
    for message in unreachable:
        assert isinstance(
            deploy_engine.classify_error(message), deploy_engine.ClusterUnreachable
        ), message

    refusal = deploy_engine.classify_error("Error: release: not found")
    assert isinstance(refusal, deploy_engine.DeployError)
    assert not isinstance(refusal, deploy_engine.ClusterUnreachable)


def test_unreachable_cluster_surfaces_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_helm(args: list[str]) -> str:
        raise deploy_engine.ClusterUnreachable("Kubernetes cluster unreachable")

    monkeypatch.setattr(deploy_engine, "run_helm", fake_run_helm)
    response = client.post("/deployments", json=VALID)
    assert response.status_code == 503
    assert response.json()["detail"] == "Kubernetes cluster unreachable"
