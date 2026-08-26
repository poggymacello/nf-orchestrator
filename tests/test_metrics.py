import pytest
from fastapi.testclient import TestClient

from orchestrator import app, metrics
from orchestrator import deploy as deploy_engine

client = TestClient(app)

VALID = {"name": "sample-nf", "replicas": 2, "environment": "dev"}


@pytest.fixture(autouse=True)
def no_cluster_during_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the lifecycle collector off the cluster unless a test asks for it."""

    def unreachable() -> list[str]:
        raise deploy_engine.ClusterUnreachable("no cluster in tests")

    monkeypatch.setattr(metrics, "release_names", unreachable)


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


SNAPSHOT = {
    "release": "sample-nf",
    "state": "INSTANTIATED",
    "helm_status": "deployed",
    "desired_replicas": 2,
    "ready_replicas": 2,
    "pods": [],
}


def gauge(name: str, labels: dict[str, str]) -> float | None:
    return metrics.REGISTRY.get_sample_value(name, labels)


def test_collector_emits_one_state_gauge_per_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", lambda name: SNAPSHOT)

    assert gauge("nf_cluster_reachable", {}) == 1.0
    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "INSTANTIATED"}) == 1.0
    for other in ("NOT_INSTANTIATED", "INSTANTIATING", "FAILED"):
        assert gauge("nf_deployment_state", {"release": "sample-nf", "state": other}) == 0.0
    assert gauge("nf_deployment_desired_replicas", {"release": "sample-nf"}) == 2.0
    assert gauge("nf_deployment_ready_replicas", {"release": "sample-nf"}) == 2.0


def test_shortfall_is_visible_in_the_gauges(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {**SNAPSHOT, "state": "INSTANTIATING", "ready_replicas": 1}
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", lambda name: snapshot)

    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "INSTANTIATED"}) == 0.0
    assert gauge("nf_deployment_desired_replicas", {"release": "sample-nf"}) == 2.0
    assert gauge("nf_deployment_ready_replicas", {"release": "sample-nf"}) == 1.0


def test_unreachable_cluster_emits_no_release_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scrape that cannot see the cluster must not look like an empty cluster."""

    def unreachable() -> list[str]:
        raise deploy_engine.ClusterUnreachable("Kubernetes cluster unreachable")

    monkeypatch.setattr(metrics, "release_names", unreachable)

    assert gauge("nf_cluster_reachable", {}) == 0.0
    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "FAILED"}) is None


def test_a_broken_release_does_not_hide_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reconcile(name: str) -> dict[str, object]:
        if name == "broken-nf":
            raise deploy_engine.DeployError("release has no replicaCount in its values")
        return SNAPSHOT

    monkeypatch.setattr(metrics, "release_names", lambda: ["broken-nf", "sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", reconcile)

    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "INSTANTIATED"}) == 1.0
    assert gauge("nf_deployment_state", {"release": "broken-nf", "state": "FAILED"}) is None
