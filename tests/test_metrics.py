import re

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
    monkeypatch.setattr(metrics, "release_pods", dict)
    # The scrape measures the readiness probe itself now; no test asks for a real one.
    monkeypatch.setattr(
        metrics,
        "api_server_probe",
        lambda **kwargs: deploy_engine.Probe(answered=True, seconds=0.05),
    )


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
    monkeypatch.setattr(metrics, "reconcile_release", lambda name, pods=None: SNAPSHOT)

    assert gauge("nf_cluster_reachable", {}) == 1.0
    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "INSTANTIATED"}) == 1.0
    for other in ("NOT_INSTANTIATED", "INSTANTIATING", "FAILED"):
        assert gauge("nf_deployment_state", {"release": "sample-nf", "state": other}) == 0.0
    assert gauge("nf_deployment_desired_replicas", {"release": "sample-nf"}) == 2.0
    assert gauge("nf_deployment_ready_replicas", {"release": "sample-nf"}) == 2.0


def test_shortfall_is_visible_in_the_gauges(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {**SNAPSHOT, "state": "INSTANTIATING", "ready_replicas": 1}
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", lambda name, pods=None: snapshot)

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
    def reconcile(name: str, pods: object = None) -> dict[str, object]:
        if name == "broken-nf":
            raise deploy_engine.DeployError("release has no replicaCount in its values")
        return SNAPSHOT

    monkeypatch.setattr(metrics, "release_names", lambda: ["broken-nf", "sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", reconcile)

    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "INSTANTIATED"}) == 1.0
    assert gauge("nf_deployment_state", {"release": "broken-nf", "state": "FAILED"}) is None
    assert gauge("nf_releases_unreported", {"reason": "error"}) == 1.0
    assert gauge("nf_releases_unreported", {"reason": "deadline"}) == 0.0


# --- drill 7: the scrape has one budget, however many releases there are ---


def test_the_scrape_budget_fits_inside_the_prometheus_timeout() -> None:
    """Prometheus gives a scrape 5s. The budget has to leave room to render and send,
    and a single read must be able to fail inside it."""
    assert metrics.SCRAPE_BUDGET < 5
    assert deploy_engine.READ_TIMEOUT <= metrics.SCRAPE_BUDGET


def test_releases_are_reconciled_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    names = [f"nf-{i}" for i in range(8)]
    monkeypatch.setattr(metrics, "release_names", lambda: names)
    monkeypatch.setattr(metrics, "SCRAPE_WORKERS", 8)

    def slow(name: str, pods: object = None) -> dict[str, object]:
        time.sleep(0.3)
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "reconcile_release", slow)
    started = time.monotonic()
    body = metrics.render().decode()
    assert time.monotonic() - started < 1.5  # in series this would be 2.4s
    for name in names:
        assert f'release="{name}"' in body


def test_a_release_past_the_deadline_is_counted_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(metrics, "release_names", lambda: ["fast-nf", "stuck-nf"])
    monkeypatch.setattr(metrics, "SCRAPE_BUDGET", 0.3)

    def reconcile(name: str, pods: object = None) -> dict[str, object]:
        if name == "stuck-nf":
            release.wait(5)
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "reconcile_release", reconcile)
    started = time.monotonic()
    try:
        body = metrics.render().decode()
    finally:
        release.set()
    assert time.monotonic() - started < 1.0
    assert 'nf_deployment_state{release="fast-nf"' in body
    assert 'nf_deployment_state{release="stuck-nf"' not in body
    assert 'nf_release_reported{release="stuck-nf"} 0.0' in body
    assert 'nf_releases_unreported{reason="deadline"} 1.0' in body


def test_every_listed_release_says_whether_it_was_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 7: past the deadline the same releases miss out every scrape, so the
    alert has to be able to name them, not just count them."""

    def reconcile(name: str, pods: object = None) -> dict[str, object]:
        if name == "broken-nf":
            raise deploy_engine.DeployError("values unreadable")
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "release_names", lambda: ["broken-nf", "sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", reconcile)

    assert gauge("nf_release_reported", {"release": "sample-nf"}) == 1.0
    assert gauge("nf_release_reported", {"release": "broken-nf"}) == 0.0


# --- day 26: one pod listing per scrape ---


def test_the_scrape_lists_pods_once_and_hands_each_release_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listings: list[int] = []
    received: dict[str, object] = {}
    by_release = {"a-nf": [{"phase": "Running", "reason": None, "ready": True}]}

    def release_pods() -> dict[str, object]:
        listings.append(1)
        return by_release

    def reconcile(name: str, pods: object = None) -> dict[str, object]:
        received[name] = pods
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "release_names", lambda: ["a-nf", "b-nf"])
    monkeypatch.setattr(metrics, "release_pods", release_pods)
    monkeypatch.setattr(metrics, "reconcile_release", reconcile)

    metrics.render()
    assert listings == [1]
    assert received == {"a-nf": by_release["a-nf"], "b-nf": []}


def test_a_failed_pod_listing_falls_back_to_per_release_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def listing_fails() -> dict[str, object]:
        raise deploy_engine.DeployError("forbidden: cannot list pods cluster-wide")

    def reconcile(name: str, pods: object = "unset") -> dict[str, object]:
        received[name] = pods
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "release_names", lambda: ["a-nf"])
    monkeypatch.setattr(metrics, "release_pods", listing_fails)
    monkeypatch.setattr(metrics, "reconcile_release", reconcile)

    body = metrics.render().decode()
    assert received == {"a-nf": None}
    assert 'nf_release_reported{release="a-nf"} 1.0' in body


# --- drill 8: a slow cluster must not leave every release half-read ---


def test_a_slow_cluster_does_not_get_every_release_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 8: against an API server that was slow and served a limited number of calls
    at a time, submitting all thirty releases at once finished none of them inside the
    budget — every release got a share of a fixed throughput and none got enough. Two at
    a time finished six. So the first wave is small, and it grows only on evidence that
    the cluster kept up.

    Asserted on how much work is put in flight, which is the thing this code decides.
    How many releases then complete belongs to the cluster, and is in the drill's
    postmortem with real numbers rather than in a test that would race.
    """
    import threading
    import time

    names = [f"nf-{i}" for i in range(30)]
    in_flight = live = 0
    peak = 0
    lock = threading.Lock()
    monkeypatch.setattr(metrics, "release_names", lambda: names)
    monkeypatch.setattr(metrics, "SCRAPE_BUDGET", 0.5)
    monkeypatch.setattr(metrics, "SCRAPE_WORKERS", 16)

    def slow(name: str, pods: object = None) -> dict[str, object]:
        nonlocal in_flight, live, peak
        with lock:
            in_flight += 1
            live += 1
            peak = max(peak, live)
        time.sleep(0.4)                  # one wave costs most of the budget
        with lock:
            live -= 1
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "reconcile_release", slow)
    metrics.render()

    assert peak <= 2, f"{peak} releases were read at once against a slow cluster"
    assert in_flight == 2, f"{in_flight} releases were started; the budget allowed one wave"


def test_no_wave_is_started_that_the_budget_cannot_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    started: list[str] = []
    monkeypatch.setattr(metrics, "release_names", lambda: [f"nf-{i}" for i in range(20)])
    monkeypatch.setattr(metrics, "SCRAPE_BUDGET", 0.8)
    monkeypatch.setattr(metrics, "SCRAPE_WORKERS", 8)

    def slow(name: str, pods: object = None) -> dict[str, object]:
        started.append(name)
        time.sleep(0.3)
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "reconcile_release", slow)
    began = time.monotonic()
    metrics.render()
    elapsed = time.monotonic() - began
    # It stops admitting rather than spending the whole budget on work it cannot finish.
    assert len(started) < 20
    assert elapsed < 0.8 + 0.3


def test_a_healthy_cluster_still_reports_every_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The waves must grow: fast reconciles mean nothing is left unreported."""
    names = [f"nf-{i}" for i in range(40)]
    monkeypatch.setattr(metrics, "release_names", lambda: names)
    monkeypatch.setattr(metrics, "reconcile_release", lambda name, pods=None: {**SNAPSHOT, "release": name})
    body = metrics.render().decode()
    assert len(re.findall(r'^nf_release_reported\{.*\} 1\.0$', body, re.MULTILINE)) == 40
    assert 'nf_releases_unreported{reason="deadline"} 0.0' in body


# --- drill 9: throttled is its own answer ---


def test_a_throttled_listing_is_reachable_and_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    def throttled() -> list[str]:
        raise deploy_engine.ClusterBusy("helm list did not answer; API server is up")

    monkeypatch.setattr(metrics, "release_names", throttled)
    assert gauge("nf_cluster_reachable", {}) == 1.0
    assert gauge("nf_cluster_busy", {}) == 1.0
    assert gauge("nf_deployment_state", {"release": "sample-nf", "state": "FAILED"}) is None


def test_an_unreachable_cluster_is_not_reported_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gauge("nf_cluster_reachable", {}) == 0.0
    assert gauge("nf_cluster_busy", {}) is None


def test_a_throttled_release_is_counted_as_busy_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reconcile(name: str, pods: object = None) -> dict[str, object]:
        if name == "throttled-nf":
            raise deploy_engine.ClusterBusy("history did not answer; API server is up")
        return {**SNAPSHOT, "release": name}

    monkeypatch.setattr(metrics, "release_names", lambda: ["throttled-nf", "sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", reconcile)
    assert gauge("nf_releases_unreported", {"reason": "busy"}) == 1.0
    assert gauge("nf_releases_unreported", {"reason": "error"}) == 0.0
    assert gauge("nf_cluster_busy", {}) == 1.0
    assert gauge("nf_release_reported", {"release": "sample-nf"}) == 1.0


def test_a_healthy_scrape_is_not_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(metrics, "reconcile_release", lambda name, pods=None: SNAPSHOT)
    assert gauge("nf_cluster_busy", {}) == 0.0


# --- drill 10: queued back-pressure fails nothing, so nothing said it was happening ---


def fast_probe(seconds: float = 0.05) -> object:
    return lambda **kwargs: deploy_engine.Probe(answered=True, seconds=seconds)


def test_a_slow_probe_reports_the_cluster_as_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every release readable, every call successful, and the cluster still throttling us.

    Under APF queueing nothing is refused and nothing times out — calls simply take
    longer. Before this, `nf_cluster_busy` stayed 0 and the late releases showed up as
    reason="deadline", which is the orchestrator's own over-budget incident (drill 7) and
    sends whoever is paged to the wrong runbook.
    """
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(
        metrics,
        "api_server_probe",
        lambda **kwargs: deploy_engine.Probe(
            answered=True, seconds=deploy_engine.SLOW_PROBE + 0.5
        ),
    )
    body = client.get("/metrics").text
    assert "nf_cluster_reachable 1.0" in body
    assert "nf_cluster_busy 1.0" in body
    assert 'nf_release_reported{release="sample-nf"} 1.0' in body


def test_a_prompt_probe_leaves_the_cluster_unthrottled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(metrics, "api_server_probe", fast_probe())
    body = client.get("/metrics").text
    assert "nf_cluster_busy 0.0" in body


def test_the_probe_duration_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The number the busy decision is made from, so nobody has to take it on trust."""
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(metrics, "api_server_probe", fast_probe(0.25))
    body = client.get("/metrics").text
    assert "nf_cluster_probe_seconds 0.25" in body


def test_the_scrape_probes_once_no_matter_how_many_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[float] = []

    def counted(**kwargs: object) -> object:
        probes.append(0.0)
        return deploy_engine.Probe(answered=True, seconds=0.05)

    monkeypatch.setattr(metrics, "release_names", lambda: [f"nf-{i}" for i in range(12)])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(metrics, "api_server_probe", counted)
    client.get("/metrics")
    assert len(probes) == 1


def test_a_probe_that_never_comes_back_is_not_blamed_on_the_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day 29 read a missing probe as a busy cluster. Drill 11 found the likeliest reason
    for a missing probe is a host too slow to run it, which says nothing about the cluster.
    """
    import time

    monkeypatch.setattr(metrics, "SCRAPE_BUDGET", 0.05)
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )

    def never(**kwargs: object) -> object:
        time.sleep(1.0)
        return deploy_engine.Probe(answered=True, seconds=1.0)

    monkeypatch.setattr(metrics, "api_server_probe", never)
    body = client.get("/metrics").text
    assert "nf_cluster_busy 0.0" in body
    samples = [line for line in body.splitlines() if not line.startswith("#")]
    assert not any(line.startswith("nf_cluster_probe_seconds ") for line in samples)


# --- drill 11: the orchestrator's own host is starved, and the cluster is fine ---


def test_a_starved_host_is_not_a_throttling_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 11's numbers: ~1.1s of wall time, of which the cluster took 41ms."""
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(
        metrics,
        "api_server_probe",
        lambda **kwargs: deploy_engine.Probe(answered=True, seconds=1.1, round_trip=0.041),
    )
    body = client.get("/metrics").text
    assert "nf_cluster_busy 0.0" in body
    assert "nf_cluster_probe_seconds 0.041" in body
    local = re.search(r"^nf_orchestrator_probe_local_seconds (\S+)$", body, re.MULTILINE)
    assert local and abs(float(local.group(1)) - 1.059) < 1e-9


def test_a_slow_round_trip_is_still_a_busy_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 10 must keep reading as throttling once the round trip is the measure."""
    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(
        metrics,
        "api_server_probe",
        lambda **kwargs: deploy_engine.Probe(answered=True, seconds=1.7, round_trip=1.65),
    )
    body = client.get("/metrics").text
    assert "nf_cluster_busy 1.0" in body


def test_the_scrape_probe_gets_most_of_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs beside the work, so it is not held to the 0.75s a failed call can spare."""
    asked: list[object] = []

    def record(**kwargs: object) -> object:
        asked.append(kwargs.get("timeout"))
        return deploy_engine.Probe(answered=True, seconds=0.05, round_trip=0.005)

    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(
        metrics, "reconcile_release", lambda name, pods=None: {"state": "INSTANTIATED"}
    )
    monkeypatch.setattr(metrics, "api_server_probe", record)
    client.get("/metrics")
    assert asked == [metrics.SCRAPE_PROBE_TIMEOUT]
    assert deploy_engine.PROBE_TIMEOUT < metrics.SCRAPE_PROBE_TIMEOUT < metrics.SCRAPE_BUDGET


def test_a_listing_late_because_of_this_host_does_not_blame_the_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill 11, after the probe was split: the pod listing ran past its bound on the
    starved host, the probe answered, and the scrape still set nf_cluster_busy."""

    def late_pods() -> dict[str, list[dict[str, object]]]:
        raise deploy_engine.HostOverloaded("kubectl get did not answer; this host is slow")

    def late_release(name: str, pods: object = None) -> dict[str, object]:
        raise deploy_engine.HostOverloaded("helm history did not answer; this host is slow")

    monkeypatch.setattr(metrics, "release_names", lambda: ["sample-nf"])
    monkeypatch.setattr(metrics, "release_pods", late_pods)
    monkeypatch.setattr(metrics, "reconcile_release", late_release)
    body = client.get("/metrics").text
    assert "nf_cluster_busy 0.0" in body
    assert 'nf_releases_unreported{reason="host"} 1.0' in body
    assert 'nf_releases_unreported{reason="busy"} 0.0' in body


def test_a_listing_this_host_could_not_run_claims_nothing_about_the_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def late_names() -> list[str]:
        raise deploy_engine.HostOverloaded("helm list did not answer; this host is slow")

    monkeypatch.setattr(metrics, "release_names", late_names)
    monkeypatch.setattr(
        metrics,
        "api_server_probe",
        lambda **kwargs: deploy_engine.Probe(answered=True, seconds=1.2, round_trip=0.04),
    )
    samples = [
        line for line in client.get("/metrics").text.splitlines() if not line.startswith("#")
    ]
    assert not any(line.startswith(("nf_cluster_reachable", "nf_cluster_busy")) for line in samples)
    assert any(line.startswith("nf_orchestrator_probe_local_seconds ") for line in samples)


def test_a_throttled_listing_still_reports_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused() -> list[str]:
        raise deploy_engine.ClusterBusy("helm list did not answer; the API server is up")

    monkeypatch.setattr(metrics, "release_names", refused)
    monkeypatch.setattr(
        metrics,
        "api_server_probe",
        lambda **kwargs: deploy_engine.Probe(answered=True, seconds=1.6, round_trip=1.5),
    )
    body = client.get("/metrics").text
    assert "nf_cluster_busy 1.0" in body
    assert "nf_cluster_probe_seconds 1.5" in body
