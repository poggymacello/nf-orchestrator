import json

import pytest
from fastapi.testclient import TestClient

from orchestrator import app, reconciler
from orchestrator.deploy import ClusterUnreachable, DeployError
from orchestrator.reconciler import OperationState, State, derive_state

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


def test_a_failed_operation_is_not_a_failed_nf() -> None:
    """Drill 3, finding 2: a rejected upgrade made a serving workload read FAILED."""
    assert derive_state("failed", ready(1), 1) is State.INSTANTIATED


def test_a_failed_first_install_that_deployed_nothing_is_failed() -> None:
    """Without this it would report INSTANTIATING forever: nothing will progress."""
    state = derive_state(
        "failed", [], 0, operation=OperationState.FAILED, ever_deployed=False
    )
    assert state is State.FAILED


def test_a_failed_operation_over_an_unhealthy_workload_is_still_failed() -> None:
    pods = [{"phase": "Pending", "reason": "ImagePullBackOff", "ready": False}]
    assert derive_state("failed", pods, 1) is State.FAILED


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

    monkeypatch.setattr(reconciler, "release_history", unreachable)
    response = client.get("/deployments/sample-nf")
    assert response.status_code == 503
    assert "unreachable" in response.json()["detail"].lower()


# --- endpoint behaviour ---


def test_status_endpoint_reports_derived_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reconciler, "release_history", lambda name: [{"revision": 1, "status": "deployed"}]
    )
    monkeypatch.setattr(reconciler, "desired_replicas", lambda name, history=None: 1)
    monkeypatch.setattr(reconciler, "pod_states", lambda name: ready(1))
    response = client.get("/deployments/sample-nf")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "INSTANTIATED"
    assert body["desired_replicas"] == 1
    assert body["ready_replicas"] == 1


def test_status_endpoint_shows_the_shortfall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reconciler, "release_history", lambda name: [{"revision": 1, "status": "deployed"}]
    )
    monkeypatch.setattr(reconciler, "desired_replicas", lambda name, history=None: 3)
    monkeypatch.setattr(reconciler, "pod_states", lambda name: ready(1))
    body = client.get("/deployments/sample-nf").json()
    assert body["state"] == "INSTANTIATING"
    assert (body["desired_replicas"], body["ready_replicas"]) == (3, 1)


def test_status_endpoint_reports_not_instantiated_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_found(name: str) -> str:
        raise DeployError("Error: release: not found")

    monkeypatch.setattr(reconciler, "release_history", not_found)
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


# --- M4 drill 3: the intent in effect is the last one that deployed ---


def history(*entries: tuple[int, str]) -> str:
    return json.dumps([{"revision": r, "status": s} for r, s in entries])


def test_last_deployed_revision_ignores_failed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconciler, "run_helm", lambda args: history((1, "deployed"), (2, "failed"))
    )
    assert reconciler.last_deployed_revision("sample-nf") == 1


def test_last_deployed_revision_is_the_highest_not_the_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconciler,
        "run_helm",
        lambda args: history((1, "deployed"), (2, "deployed"), (3, "failed")),
    )
    assert reconciler.last_deployed_revision("sample-nf") == 2


def test_no_deployed_revision_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "run_helm", lambda args: history((1, "failed")))
    assert reconciler.last_deployed_revision("sample-nf") is None


def test_desired_replicas_reads_the_deployed_revision_not_the_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 3: a failed upgrade leaves its values on the release without applying them."""
    calls: list[list[str]] = []

    def fake_run_helm(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "history":
            return history((1, "deployed"), (2, "failed"))
        return json.dumps({"replicaCount": 2, "environment": "dev"})

    monkeypatch.setattr(reconciler, "run_helm", fake_run_helm)

    assert reconciler.desired_replicas("sample-nf") == 2
    values_call = calls[1]
    assert values_call[values_call.index("--revision") + 1] == "1"


def test_desired_replicas_is_zero_when_nothing_ever_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciler, "run_helm", lambda args: history((1, "failed")))
    assert reconciler.desired_replicas("sample-nf") == 0


# --- M4 drill 3, finding 2: the operation is reported apart from the NF ---


def op(*entries: tuple[int, str]) -> list[dict[str, object]]:
    return [
        {"revision": r, "status": s, "description": f"revision {r} {s}"}
        for r, s in entries
    ]


def test_last_operation_reads_the_newest_revision() -> None:
    result = reconciler.last_operation(op((1, "deployed"), (2, "failed")))
    assert result["revision"] == 2
    assert result["state"] == "FAILED"
    assert result["description"] == "revision 2 failed"


def test_a_completed_operation_is_reported_as_completed() -> None:
    assert reconciler.last_operation(op((1, "deployed")))["state"] == "COMPLETED"


def test_an_in_flight_operation_is_processing() -> None:
    assert reconciler.last_operation(op((1, "pending-upgrade")))["state"] == "PROCESSING"


def test_an_unrecognised_helm_status_is_unknown_not_something_specific() -> None:
    """Drill 1's lesson: a fall-through must not mean something in particular."""
    assert reconciler.last_operation(op((1, "invented-tomorrow")))["state"] == "UNKNOWN"


def test_no_history_is_unknown() -> None:
    result = reconciler.last_operation([])
    assert result == {"revision": None, "state": "UNKNOWN", "description": None}


def test_status_endpoint_reports_the_operation_beside_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drill-3 case: a failed upgrade over a workload that is still serving."""
    monkeypatch.setattr(
        reconciler, "release_history", lambda name: op((1, "deployed"), (2, "failed"))
    )
    monkeypatch.setattr(reconciler, "desired_replicas", lambda name, history=None: 2)
    monkeypatch.setattr(reconciler, "pod_states", lambda name: ready(1))

    body = client.get("/deployments/sample-nf").json()
    assert body["state"] == "INSTANTIATING"
    assert body["helm_status"] == "failed"
    assert body["last_operation"]["state"] == "FAILED"
    assert body["last_operation"]["revision"] == 2


# --- drill 5: an evicted pod carries its reason on the pod, not on a container ---


def test_a_pod_level_reason_is_used_when_no_container_is_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 5: evicted pods reported reason null, discarding the only useful word."""
    payload = {
        "items": [
            {
                "metadata": {},
                "status": {"phase": "Failed", "reason": "Evicted"},
            }
        ]
    }
    monkeypatch.setattr(reconciler, "run_kubectl", lambda args: json.dumps(payload))
    assert reconciler.pod_states("disk-nf") == [
        {"phase": "Failed", "reason": "Evicted", "ready": False}
    ]


def test_a_container_waiting_reason_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "items": [
            {
                "metadata": {},
                "status": {
                    "phase": "Pending",
                    "reason": "SomethingPodLevel",
                    "containerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "ImagePullBackOff"}}}
                    ],
                },
            }
        ]
    }
    monkeypatch.setattr(reconciler, "run_kubectl", lambda args: json.dumps(payload))
    assert reconciler.pod_states("disk-nf")[0]["reason"] == "ImagePullBackOff"


def test_an_evicted_pod_makes_the_nf_failed() -> None:
    pods = [{"phase": "Failed", "reason": "Evicted", "ready": False}]
    assert derive_state("deployed", pods, 2) is State.FAILED


def test_evicted_corpses_beside_a_satisfied_intent_are_not_a_failure() -> None:
    """Drill 5, finding 2: after recovery the NF reported FAILED forever, because
    Kubernetes never deletes an evicted pod and the whole list was being judged."""
    pods = [{"phase": "Failed", "reason": "Evicted", "ready": False}] * 8 + ready(2)
    assert derive_state("deployed", pods, 2) is State.INSTANTIATED


def test_evicted_corpses_with_the_intent_unsatisfied_are_still_a_failure() -> None:
    pods = [{"phase": "Failed", "reason": "Evicted", "ready": False}] * 8 + [
        {"phase": "Pending", "reason": None, "ready": False}
    ] * 2
    assert derive_state("deployed", pods, 2) is State.FAILED


def test_a_live_failure_reason_still_wins_over_a_satisfied_count() -> None:
    pods = ready(1) + [{"phase": "Running", "reason": "CrashLoopBackOff", "ready": True}]
    assert derive_state("deployed", pods, 2) is State.FAILED


# --- day 26: fewer calls per release, same answers ---


def pod(app: str, phase: str = "Running", ready: bool = True, deleting: bool = False) -> dict:
    metadata: dict[str, object] = {"labels": {"app": app}}
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-15T02:00:00Z"
    return {
        "metadata": metadata,
        "status": {"phase": phase, "containerStatuses": [{"ready": ready, "state": {}}]},
    }


def test_reconcile_reads_the_status_from_history_not_helm_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verbs: list[str] = []

    def fake_run_helm(args: list[str]) -> str:
        verbs.append(args[0])
        if args[0] == "history":
            return json.dumps(op((1, "deployed"), (2, "failed")))
        if args[0] == "get":
            return json.dumps({"replicaCount": 1})
        raise AssertionError(f"reconcile should not call helm {args[0]}")

    monkeypatch.setattr(reconciler, "run_helm", fake_run_helm)
    monkeypatch.setattr(
        reconciler, "run_kubectl", lambda args: json.dumps({"items": [pod("sample-nf")]})
    )
    result = reconciler.reconcile("sample-nf")
    assert verbs == ["history", "get"]
    assert result["helm_status"] == "failed"
    assert result["state"] == "INSTANTIATED"


def test_reconcile_with_pods_given_makes_no_kubectl_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_helm(args: list[str]) -> str:
        if args[0] == "history":
            return history((1, "deployed"))
        return json.dumps({"replicaCount": 2})

    def no_kubectl(args: list[str]) -> str:
        raise AssertionError("pods were supplied; kubectl must not be called")

    monkeypatch.setattr(reconciler, "run_helm", fake_run_helm)
    monkeypatch.setattr(reconciler, "run_kubectl", no_kubectl)
    result = reconciler.reconcile("sample-nf", pods=ready(2))
    assert (result["state"], result["ready_replicas"]) == ("INSTANTIATED", 2)


def test_absent_history_is_none_and_unreachable_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_found(args: list[str]) -> str:
        raise DeployError("Error: release: not found")

    def unreachable(args: list[str]) -> str:
        raise ClusterUnreachable("Error: Kubernetes cluster unreachable")

    def forbidden(args: list[str]) -> str:
        raise DeployError("Error: forbidden")

    monkeypatch.setattr(reconciler, "run_helm", not_found)
    assert reconciler.existing_history("ghost") is None
    monkeypatch.setattr(reconciler, "run_helm", unreachable)
    with pytest.raises(ClusterUnreachable):
        reconciler.existing_history("sample-nf")
    monkeypatch.setattr(reconciler, "run_helm", forbidden)
    with pytest.raises(DeployError):
        reconciler.existing_history("sample-nf")


def test_pods_by_release_matches_the_per_release_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grouping one `-l app` listing by label must give each release exactly what
    `-l app=<name>` gives it, terminating pods excluded the same way."""
    everything = [
        pod("a-nf"),
        pod("a-nf", phase="Pending", ready=False),
        pod("b-nf", deleting=True),
        pod("b-nf"),
    ]

    def fake_kubectl(args: list[str]) -> str:
        selector = args[args.index("-l") + 1]
        if selector == "app":
            items = everything
        else:
            items = [p for p in everything if p["metadata"]["labels"]["app"] == selector[4:]]
        return json.dumps({"items": items})

    monkeypatch.setattr(reconciler, "run_kubectl", fake_kubectl)
    grouped = reconciler.pods_by_release()
    for name in ("a-nf", "b-nf"):
        assert grouped[name] == reconciler.pod_states(name)
    assert len(grouped["b-nf"]) == 1
