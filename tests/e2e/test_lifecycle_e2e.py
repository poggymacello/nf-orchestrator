"""End-to-end lifecycle against a real kind cluster.

Skipped unless NF_E2E=1, so `pytest` stays a unit-test run with no cluster. The CI
`e2e` job sets it after creating the cluster.

What this covers is the M4 drills' ground: that a deploy reaches INSTANTIATED, that
drift applied outside Helm shows up as a shortfall, that resubmitting an intent over a
contested field is refused with 409 rather than silently winning, that repair takes the
field back, and that teardown is idempotent. Those were all verified by hand on one
laptop; this is them running somewhere else.
"""

import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from orchestrator.deploy import CHART_PATH, KUBE_CONTEXT

pytestmark = pytest.mark.skipif(
    os.getenv("NF_E2E") != "1",
    reason="end-to-end test needs a kind cluster; set NF_E2E=1 to run it",
)

client = TestClient(app)

RELEASE = "e2e-nf"
INTENT = {"name": RELEASE, "replicas": 2, "environment": "dev"}

# kind in CI pulls nginx and schedules on a cold node, which is slower than a laptop
# with a warm image cache.
TIMEOUT = 180.0
INTERVAL = 2.0


def status() -> dict[str, Any]:
    response = client.get(f"/deployments/{RELEASE}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for(predicate: Callable[[dict[str, Any]], bool], what: str) -> dict[str, Any]:
    """Poll the status endpoint until predicate holds, or fail saying what was seen."""
    deadline = time.monotonic() + TIMEOUT
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = status()
        if predicate(last):
            return last
        time.sleep(INTERVAL)
    pytest.fail(f"timed out after {TIMEOUT}s waiting for {what}; last read: {last}")


@pytest.fixture(autouse=True)
def clean_release() -> Any:
    client.delete(f"/deployments/{RELEASE}")
    yield
    client.delete(f"/deployments/{RELEASE}")


def test_nothing_deployed_is_not_instantiated() -> None:
    body = status()
    assert body["state"] == "NOT_INSTANTIATED"
    assert body["pods"] == []
    assert body["last_operation"]["state"] == "UNKNOWN"


def test_an_intent_reaches_instantiated() -> None:
    created = client.post("/deployments", json=INTENT)
    assert created.status_code == 201, created.text

    body = wait_for(lambda b: b["state"] == "INSTANTIATED", "the deploy to be satisfied")
    assert body["desired_replicas"] == 2
    assert body["ready_replicas"] == 2
    assert body["helm_status"] == "deployed"
    assert body["last_operation"]["state"] == "COMPLETED"
    assert len(body["pods"]) == 2
    assert all(pod["ready"] for pod in body["pods"])


def test_drift_outside_helm_shows_as_a_shortfall_and_is_repairable() -> None:
    """M4 drills 1 and 3, end to end: see the drift, refuse to guess, then repair."""
    assert client.post("/deployments", json=INTENT).status_code == 201
    wait_for(lambda b: b["state"] == "INSTANTIATED", "the initial deploy")

    subprocess.run(
        [
            "kubectl",
            "--context",
            KUBE_CONTEXT,
            "scale",
            f"deployment/{RELEASE}",
            "--replicas=1",
        ],
        check=True,
        capture_output=True,
    )

    drifted = wait_for(
        lambda b: b["ready_replicas"] == 1, "the shortfall to become visible"
    )
    assert drifted["state"] == "INSTANTIATING"
    assert drifted["desired_replicas"] == 2

    # Resubmitting must not silently win the field back.
    conflict = client.post("/deployments", json=INTENT)
    assert conflict.status_code == 409, conflict.text
    assert "/repair" in conflict.json()["detail"]

    # The failed operation must not be reported as a failed network function.
    after = status()
    assert after["state"] == "INSTANTIATING"
    assert after["last_operation"]["state"] == "FAILED"

    repaired = client.post(f"/deployments/{RELEASE}/repair", json=INTENT)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["forced"] is True

    body = wait_for(lambda b: b["state"] == "INSTANTIATED", "the repair to take effect")
    assert body["ready_replicas"] == 2
    assert body["last_operation"]["state"] == "COMPLETED"


def test_a_bad_image_tag_is_failed() -> None:
    assert client.post("/deployments", json=INTENT).status_code == 201
    wait_for(lambda b: b["state"] == "INSTANTIATED", "the initial deploy")

    subprocess.run(
        [
            "helm",
            "upgrade",
            RELEASE,
            str(CHART_PATH),
            "--kube-context",
            KUBE_CONTEXT,
            "--set-json",
            '{"replicaCount":2,"environment":"dev"}',
            "--set",
            "image.tag=does-not-exist-e2e",
        ],
        check=True,
        capture_output=True,
    )

    body = wait_for(lambda b: b["state"] == "FAILED", "the bad image to be detected")
    assert any(
        pod["reason"] in {"ErrImagePull", "ImagePullBackOff"} for pod in body["pods"]
    )


def test_teardown_is_idempotent() -> None:
    assert client.post("/deployments", json=INTENT).status_code == 201
    wait_for(lambda b: b["state"] == "INSTANTIATED", "the initial deploy")

    first = client.delete(f"/deployments/{RELEASE}")
    assert first.status_code == 200
    assert first.json()["uninstalled"] is True

    second = client.delete(f"/deployments/{RELEASE}")
    assert second.json()["uninstalled"] is False
    assert status()["state"] == "NOT_INSTANTIATED"


def test_readyz_answers_against_a_real_cluster() -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "cluster": "reachable"}
