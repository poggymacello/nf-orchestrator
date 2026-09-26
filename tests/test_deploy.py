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

# What kubectl at -v=6 has logged by the time a call against a stalled or frozen server is
# killed: measured on day 30, 5 of 5 probes against drill 7's stall server had 11 lines.
# A timeout with nothing logged at all is a different incident — a host too starved to
# start kubectl (drill 11) — so fakes for a stalled server must carry this.
STARTED = b"I0924 10:30:01.000000   4242 loader.go:407] Config loaded from file\n"


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


def test_field_ownership_conflict_is_classified_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M4 drill 3: the exact wording Helm 4 produced when kubectl owned .spec.replicas."""
    message = (
        "conflict occurred while applying object default/stable-nf apps/v1, "
        'Kind=Deployment: Apply failed with 1 conflict: conflict with "kubectl.exe" '
        'with subresource "scale" using apps/v1: .spec.replicas'
    )
    assert isinstance(
        deploy_engine.classify_error(message), deploy_engine.ClusterConflict
    )


def test_conflict_surfaces_as_409_pointing_at_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_helm(args: list[str]) -> str:
        raise deploy_engine.ClusterConflict("conflict occurred while applying object")

    monkeypatch.setattr(deploy_engine, "run_helm", fake_run_helm)
    response = client.post("/deployments", json=VALID)
    assert response.status_code == 409
    assert "/repair" in response.json()["detail"]


def test_deploy_never_forces_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        deploy_engine, "run_helm", lambda args: captured.append(args) or HELM_OUTPUT
    )
    client.post("/deployments", json=VALID)
    assert "--force-conflicts" not in captured[0]


def test_repair_forces_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        deploy_engine, "run_helm", lambda args: captured.append(args) or HELM_OUTPUT
    )
    response = client.post("/deployments/sample-nf/repair", json=VALID)
    assert response.status_code == 200
    assert response.json()["forced"] is True
    assert "--force-conflicts" in captured[0]


def test_repair_rejects_a_name_that_does_not_match_the_path() -> None:
    response = client.post("/deployments/other-nf/repair", json=VALID)
    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]


def test_repair_records_its_own_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import metrics

    def value() -> float:
        return (
            metrics.REGISTRY.get_sample_value(
                "deployment_repairs_total", {"result": "success", "environment": "dev"}
            )
            or 0.0
        )

    monkeypatch.setattr(deploy_engine, "run_helm", lambda args: HELM_OUTPUT)
    before = value()
    client.post("/deployments/sample-nf/repair", json=VALID)
    assert value() == before + 1


# --- drill 6: a frozen control plane must fail fast, not after Go's TLS default ---


def test_a_command_that_never_answers_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd="kubectl", timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(deploy_engine.ClusterUnreachable, match="did not answer within"):
        deploy_engine.run_kubectl(["get", "pods"])


def test_a_timeout_names_the_verb_not_the_context_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 7: the message read "kubectl --context did not answer within 3s"."""
    import subprocess

    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd="kubectl", timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(deploy_engine.ClusterUnreachable) as caught:
        deploy_engine.run_kubectl(["get", "--raw=/readyz"])
    assert str(caught.value) == (
        f"kubectl get did not answer within {deploy_engine.READ_TIMEOUT:g}s"
    )


def test_every_call_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    seen: list[object] = []

    class Done:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def record(*args: object, **kwargs: object) -> Done:
        seen.append(kwargs.get("timeout"))
        return Done()

    monkeypatch.setattr(subprocess, "run", record)
    deploy_engine.run_helm(["status", "x"])
    deploy_engine.run_kubectl(["get", "pods"])
    assert None not in seen and all(isinstance(t, float) for t in seen)


def test_reads_fail_inside_the_prometheus_scrape_timeout() -> None:
    """Prometheus gives a scrape 5s. A read bounded any longer loses the metric the
    ClusterUnreachable alert reads — which is exactly what drill 6 found."""
    assert deploy_engine.READ_TIMEOUT < 5


def test_writes_get_the_longer_bound_and_reads_the_shorter() -> None:
    assert deploy_engine.timeout_for(["upgrade", "--install", "x"]) == deploy_engine.WRITE_TIMEOUT
    assert deploy_engine.timeout_for(["uninstall", "x"]) == deploy_engine.WRITE_TIMEOUT
    assert deploy_engine.timeout_for(["status", "x"]) == deploy_engine.READ_TIMEOUT
    assert deploy_engine.timeout_for(["get", "pods"]) == deploy_engine.READ_TIMEOUT


# --- day 26: helm list stops at 256 and says nothing ---


def paged_helm(names: list[str], calls: list[list[str]]):
    def fake(args: list[str]) -> str:
        calls.append(args)
        size = int(args[args.index("--max") + 1])
        offset = int(args[args.index("--offset") + 1])
        return json.dumps([{"name": n} for n in names[offset : offset + size]])

    return fake


def test_list_releases_reads_past_the_first_page(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [f"nf-{i:03d}" for i in range(600)]
    calls: list[list[str]] = []
    monkeypatch.setattr(deploy_engine, "run_helm", paged_helm(names, calls))
    assert deploy_engine.list_releases() == names
    assert len(calls) == 3


def test_list_releases_on_an_exact_page_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [f"nf-{i:03d}" for i in range(512)]
    calls: list[list[str]] = []
    monkeypatch.setattr(deploy_engine, "run_helm", paged_helm(names, calls))
    assert deploy_engine.list_releases() == names
    assert len(calls) == 3  # two full pages, then an empty one ends it


def test_a_release_seen_on_two_pages_is_listed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = iter([[{"name": "a"}, {"name": "b"}], [{"name": "b"}]])
    monkeypatch.setattr(deploy_engine, "run_helm", lambda args: json.dumps(next(pages)))
    assert deploy_engine.list_releases(page=2) == ["a", "b"]


# --- drill 9: a throttled cluster is not an unreachable one ---


def timeout_then(probe_answers: bool, calls: list[list[str]]):
    """subprocess.run that times out the real call and answers the readiness probe."""
    import subprocess

    class Done:
        returncode = 0 if probe_answers else 1
        stdout = "ok"
        stderr = ""

    def fake(command: list[str], *args: object, **kwargs: object) -> object:
        calls.append(command)
        if "--raw=/readyz" in command:
            return Done()
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED
        )

    return fake


def test_a_timeout_on_a_reachable_server_is_busy_not_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", timeout_then(True, calls))
    with pytest.raises(deploy_engine.ClusterBusy, match="reachable and not serving"):
        deploy_engine.run_helm(["history", "sample-nf"])
    assert "--raw=/readyz" in calls[-1]


def test_a_timeout_with_no_readiness_answer_stays_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drills 6 and 7: a frozen or stalled server fails the probe too."""
    import subprocess

    def hang(command: list[str], *args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(deploy_engine.ClusterUnreachable):
        deploy_engine.run_kubectl(["get", "pods"])
    with pytest.raises(deploy_engine.ClusterUnreachable):
        deploy_engine.run_helm(["history", "sample-nf"])


def test_a_timed_out_readiness_check_does_not_probe_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    calls: list[list[str]] = []

    def hang(command: list[str], *args: object, **kwargs: object) -> object:
        calls.append(command)
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(deploy_engine.ClusterUnreachable):
        deploy_engine.check_cluster()
    assert len(calls) == 1


def test_client_go_throttling_text_is_busy() -> None:
    message = (
        "Error from server (TooManyRequests): the server has received too many "
        "requests and has asked us to try again later"
    )
    assert isinstance(deploy_engine.classify_error(message), deploy_engine.ClusterBusy)


def test_a_busy_cluster_is_503_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(args: list[str]) -> str:
        raise deploy_engine.ClusterBusy("helm upgrade did not answer, API server is up")

    monkeypatch.setattr(deploy_engine, "run_helm", busy)
    response = client.post("/deployments", json=VALID)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "API server is up" in response.json()["detail"]


def test_the_probe_fits_inside_the_scrape_budget() -> None:
    from orchestrator import metrics

    assert deploy_engine.READ_TIMEOUT + deploy_engine.PROBE_TIMEOUT <= metrics.SCRAPE_BUDGET


# --- drill 10: a queueing API server is slow, not silent and not refusing ---


def test_a_timeout_after_a_recent_answer_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe is too slow to answer, and other calls are still completing.

    Drill 10 measured `/readyz` at up to 3.58s under queued back-pressure while helm
    calls were finishing in under a second. Reading the silent probe as "gone" put
    nf_cluster_reachable at 0 for a cluster that had just answered.
    """
    import subprocess

    class Done:
        returncode = 0
        stdout = "deployed"
        stderr = ""

    def nothing_answers_in_time(
        command: list[str], *args: object, **kwargs: object
    ) -> object:
        """Including the probe, which under queueing is just another slow call."""
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    deploy_engine.run_helm(["history", "sample-nf"])  # the cluster answers us

    monkeypatch.setattr(subprocess, "run", nothing_answers_in_time)
    with pytest.raises(deploy_engine.ClusterBusy, match="answered another call moments"):
        deploy_engine.run_helm(["history", "sample-nf"])


def test_an_old_answer_is_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence expires. A cluster that answered an hour ago proves nothing now."""
    import subprocess

    deploy_engine.note_success()
    monkeypatch.setattr(
        deploy_engine, "answered_recently", lambda *a, **k: False
    )

    def hang(command: list[str], *args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(deploy_engine.ClusterUnreachable):
        deploy_engine.run_helm(["history", "sample-nf"])


def test_a_refusal_still_counts_as_the_cluster_answering() -> None:
    """A conflict is a fact the cluster stated, so it is evidence the cluster is there."""
    import subprocess

    class Refused:
        returncode = 1
        stdout = ""
        stderr = "Apply failed with 1 conflict: conflict occurred while applying"

    real_run = subprocess.run
    try:
        subprocess.run = lambda *a, **k: Refused()  # type: ignore[assignment]
        with pytest.raises(deploy_engine.ClusterConflict):
            deploy_engine.run_helm(["upgrade", "sample-nf"])
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
    assert deploy_engine.answered_recently()


def test_an_unreachable_answer_is_not_evidence_of_reachability() -> None:
    import subprocess

    class Gone:
        returncode = 1
        stdout = ""
        stderr = "Kubernetes cluster unreachable: connection refused"

    real_run = subprocess.run
    try:
        subprocess.run = lambda *a, **k: Gone()  # type: ignore[assignment]
        with pytest.raises(deploy_engine.ClusterUnreachable):
            deploy_engine.run_helm(["history", "sample-nf"])
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
    assert not deploy_engine.answered_recently()


def test_calls_that_time_out_together_probe_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 10: one probe per timed-out call is a burst aimed at a struggling server."""
    import subprocess
    import threading

    probes: list[list[str]] = []
    lock = threading.Lock()

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake(command: list[str], *args: object, **kwargs: object) -> object:
        if "--raw=/readyz" in command:
            with lock:
                probes.append(command)
            return Done()
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED
        )

    monkeypatch.setattr(subprocess, "run", fake)

    def stalled_call() -> None:
        with pytest.raises(deploy_engine.ClusterBusy):
            deploy_engine.run_helm(["history", "sample-nf"])

    threads = [threading.Thread(target=stalled_call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(probes) == 1


def test_a_probe_that_does_not_answer_counts_as_slow() -> None:
    assert deploy_engine.Probe(answered=False, seconds=0.01).slow
    assert deploy_engine.Probe(
        answered=True, seconds=deploy_engine.SLOW_PROBE + 0.01
    ).slow
    assert not deploy_engine.Probe(answered=True, seconds=0.05).slow


# --- drill 11: separate the cluster's time from this host's ---

RESPONSE_LOG = (
    'I0924 10:14:50.104450   25748 round_trippers.go:632] "Response" verb="GET" '
    'url="https://127.0.0.1:45433/readyz" status="200 OK" milliseconds=41'
)


def test_the_probe_reads_client_gos_own_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The line is copied from kubectl v1.36.1 at -v=6 during the drill."""
    import subprocess

    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = "I0924 loader.go:407] Config loaded from file\n" + RESPONSE_LOG

    def fake(command: list[str], *args: object, **kwargs: object) -> object:
        seen.append(command)
        return Done()

    monkeypatch.setattr(subprocess, "run", fake)
    probe = deploy_engine.api_server_probe(max_age=0.0)
    assert "-v=6" in seen[0]
    assert probe.round_trip == 0.041
    assert probe.answered and probe.started


def test_a_fast_round_trip_is_not_slow_however_long_the_wall_time() -> None:
    probe = deploy_engine.Probe(answered=True, seconds=1.1, round_trip=0.041)
    assert not probe.slow
    assert probe.local_seconds is not None and abs(probe.local_seconds - 1.059) < 1e-9


def test_a_probe_killed_before_it_started_says_nothing_about_the_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 11: 7 of 8 probes held to 0.75s on a starved host had logged no line at all."""
    import subprocess

    def killed(command: list[str], *args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=command[0], timeout=kwargs["timeout"], stderr=b"")

    monkeypatch.setattr(subprocess, "run", killed)
    probe = deploy_engine.api_server_probe(max_age=0.0)
    assert not probe.answered and not probe.started
    assert not probe.slow
    assert probe.local_seconds == probe.seconds


def test_a_probe_killed_while_waiting_on_the_cluster_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 10's case: kubectl was running and asking; the server had not answered."""
    import subprocess

    def waiting(command: list[str], *args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd=command[0],
            timeout=kwargs["timeout"],
            stderr=b"I0923 loader.go:407] Config loaded from file\n",
        )

    monkeypatch.setattr(subprocess, "run", waiting)
    probe = deploy_engine.api_server_probe(max_age=0.0)
    assert probe.started and probe.slow
    assert probe.local_seconds is None


def test_an_unreadable_log_falls_back_to_wall_time() -> None:
    """If kubectl changes its log format, behave as on day 29 rather than going blind."""
    assert deploy_engine.Probe(answered=True, seconds=0.9).slow
    assert not deploy_engine.Probe(answered=True, seconds=0.1).slow


def test_a_host_too_slow_to_start_the_probe_is_its_own_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 11: nothing logged, so the host never asked the cluster anything."""
    import subprocess

    def starved(command: list[str], *args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=command[0], timeout=kwargs["timeout"], stderr=b"")

    monkeypatch.setattr(subprocess, "run", starved)
    with pytest.raises(deploy_engine.HostOverloaded, match="nothing can be said"):
        deploy_engine.run_helm(["history", "sample-nf"])


def test_a_prompt_cluster_behind_a_slow_host_is_the_hosts_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing that set nf_cluster_busy on the starved host: probe fine, call late."""
    import subprocess

    monkeypatch.setattr(
        deploy_engine,
        "api_server_probe",
        lambda *a, **k: deploy_engine.Probe(answered=True, seconds=1.2, round_trip=0.04),
    )

    def late(command: list[str], *args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=command[0], timeout=kwargs["timeout"], stderr=STARTED)

    monkeypatch.setattr(subprocess, "run", late)
    with pytest.raises(deploy_engine.HostOverloaded, match="answered its readiness probe in 40ms"):
        deploy_engine.run_kubectl(["get", "pods"])


def test_host_overload_is_503_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    def overloaded(args: list[str]) -> str:
        raise deploy_engine.HostOverloaded("helm upgrade did not answer; this host is slow")

    monkeypatch.setattr(deploy_engine, "run_helm", overloaded)
    response = client.post("/deployments", json=VALID)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "this host is slow" in response.json()["detail"]


# --- drill 12: a starved host and a frozen cluster at the same time ---


def local_time(hour: int, minute: int, second: int, fraction: float) -> float:
    import time

    return time.mktime((2026, 9, 26, hour, minute, second, 0, 0, -1)) + fraction


def test_the_host_share_comes_from_kubectls_first_line() -> None:
    """kubectl logged at 15:47:11.25 for a process spawned at 15:47:10.25."""
    spawned = local_time(15, 47, 10, 0.25)
    log = "I0926 15:47:11.250000   4242 loader.go:407] Config loaded from file\n"
    delay = deploy_engine.startup_delay(log, spawned)
    assert delay is not None and abs(delay - 1.0) < 1e-6


def test_the_host_share_survives_midnight() -> None:
    spawned = local_time(23, 59, 59, 0.9)
    log = "I0927 00:00:00.100000   4242 loader.go:407] Config loaded from file\n"
    delay = deploy_engine.startup_delay(log, spawned)
    assert delay is not None and abs(delay - 0.2) < 1e-6


def test_no_log_line_means_no_host_share_from_it() -> None:
    assert deploy_engine.startup_delay("", 0.0) is None


def test_a_frozen_cluster_does_not_hide_the_hosts_share() -> None:
    """Drill 12: killed while waiting on a frozen server, so no round trip — and the
    host's series vanished, clearing OrchestratorHostOverloaded on a starved host."""
    probe = deploy_engine.Probe(answered=False, seconds=3.06, startup=1.04)
    assert probe.slow
    assert probe.local_seconds == 1.04


def test_a_killed_probe_records_when_kubectl_got_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import time

    def frozen(command: list[str], *args: object, **kwargs: object) -> object:
        stamp = time.strftime("%H:%M:%S", time.localtime()) + ".000000"
        line = f"I0926 {stamp}   4242 loader.go:407] Config loaded from file\n"
        raise subprocess.TimeoutExpired(
            cmd=command[0], timeout=kwargs["timeout"], stderr=line.encode()
        )

    monkeypatch.setattr(subprocess, "run", frozen)
    probe = deploy_engine.api_server_probe(max_age=0.0)
    assert probe.started and not probe.answered
    assert probe.startup is not None and probe.startup < 1.0
