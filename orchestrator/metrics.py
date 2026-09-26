import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.core import GaugeMetricFamily

from orchestrator import reconciler
from orchestrator.deploy import (
    ClusterBusy,
    DeployError,
    HostOverloaded,
    Probe,
    api_server_probe,
    list_releases,
)
from orchestrator.reconciler import OperationState, State

REGISTRY = CollectorRegistry()

# The whole scrape gets a deadline, not just each call. Drill 7 found the day-24 bound was
# per subprocess while a scrape makes four per release in series: twenty healthy releases
# took 8.4s against Prometheus's 5s, so the target went down and OrchestratorScrapeFailing
# paged for an orchestrator and a cluster that were both fine.
#
# Releases are reconciled concurrently, and whatever has not finished by the deadline is
# counted in nf_releases_unreported instead of holding the scrape past the point where
# Prometheus abandons it and keeps nothing.
SCRAPE_BUDGET = float(os.environ.get("NF_SCRAPE_BUDGET", "4"))
SCRAPE_WORKERS = int(os.environ.get("NF_SCRAPE_WORKERS", "8"))

# The scrape's own probe runs beside the work rather than after a failed call, so it can be
# given most of the budget instead of the 0.75s a timed-out call can spare. Drill 11: on a
# starved host kubectl took ~1.1s just to start, and every probe held to 0.75s was killed
# before it asked the cluster anything.
SCRAPE_PROBE_TIMEOUT = float(os.environ.get("NF_SCRAPE_PROBE_TIMEOUT", "3"))

deployments_total = Counter(
    "deployments_total",
    "Intents submitted to the deploy engine, by outcome",
    ["result", "environment"],
    registry=REGISTRY,
)

# helm and kubectl are subprocesses: a missing binary raises OSError rather than
# DeployError, and a scrape must survive that the same way it survives an outage.
UNAVAILABLE = (DeployError, OSError)


repairs_total = Counter(
    "deployment_repairs_total",
    "Repairs that forced ownership of a contested field, by outcome",
    ["result", "environment"],
    registry=REGISTRY,
)


def record_deploy(result: str, environment: str) -> None:
    deployments_total.labels(result=result, environment=environment).inc()


def record_repair(result: str, environment: str) -> None:
    repairs_total.labels(result=result, environment=environment).inc()


def release_names() -> list[str]:
    return list_releases()


def release_pods() -> dict[str, list[dict[str, Any]]]:
    return reconciler.pods_by_release()


def reconcile_release(
    name: str, pods: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return reconciler.reconcile(name, pods)


# What a scrape learned about one release: its reconciled state, BUSY when the API server
# was reachable but not serving us (drill 9), HOST when the delay was this host's own
# (drill 11), or None when reading it failed otherwise.
BUSY = "busy"
HOST = "host"
Snapshot = dict[str, Any] | str | None


def snapshot(name: str, pods: list[dict[str, Any]] | None = None) -> Snapshot:
    """Reconcile one release, or say why it could not be read this scrape."""
    try:
        return reconcile_release(name, pods)
    except HostOverloaded:
        return HOST
    except ClusterBusy:
        return BUSY
    except UNAVAILABLE:
        return None


def reconcile_within(
    names: list[str],
    pods: dict[str, list[dict[str, Any]]] | None,
    deadline: float,
) -> dict[str, Future[Snapshot]]:
    """Reconcile as many releases as the budget allows, in waves that grow while the
    cluster keeps up.

    Drill 8: against an API server that was slow *and* limited in how much it served at
    once, throughput was fixed, so every extra worker bought a share of the same total.
    Sixteen workers left all thirty releases half-read and **none** finished inside the
    budget; two workers finished six. Submitting every release at once is a bet that the
    cluster can serve them all, and that bet is lost exactly when monitoring matters.

    So work is admitted a wave at a time: start small, double while a wave completes,
    and stop admitting once the remaining budget is shorter than the last wave took.
    Releases never admitted, and those still running at the deadline, are reported as
    unreported — the same way, for the same reason, as before.
    """
    first_wave = min(2, SCRAPE_WORKERS)
    pool = ThreadPoolExecutor(max_workers=SCRAPE_WORKERS)
    futures: dict[str, Future[Snapshot]] = {}
    queue = list(names)
    wave = first_wave
    previous_cost = 0.0
    try:
        while queue:
            remaining = deadline - time.monotonic()
            # Do not start work that the last wave's evidence says cannot finish.
            if remaining <= 0 or (previous_cost and remaining < previous_cost):
                break
            batch, queue = queue[:wave], queue[wave:]
            started = time.monotonic()
            batch_futures = {
                name: pool.submit(
                    snapshot, name, None if pods is None else pods.get(name, [])
                )
                for name in batch
            }
            futures.update(batch_futures)
            wait(batch_futures.values(), timeout=max(0.0, deadline - time.monotonic()))
            previous_cost = time.monotonic() - started
            if not all(future.done() for future in batch_futures.values()):
                break
            wave = min(wave * 2, SCRAPE_WORKERS)
    finally:
        # Queued work is dropped; anything already running is itself bounded by the
        # per-call timeout, so abandoning it cannot leave a subprocess behind for long.
        pool.shutdown(wait=False, cancel_futures=True)
    for name in queue:
        futures.setdefault(name, _never_started())
    return futures


def _never_started() -> Future[Snapshot]:
    """A future for a release the budget never allowed us to start."""
    return Future()


class LifecycleCollector:
    """Reconciles every release at scrape time and emits its state as gauges.

    The reconciler derives state on read (ADR-0005), which until now meant it could
    not report a transition nobody polled for. Registering this collector makes
    Prometheus the poller: each scrape reconciles every release, so the state series
    exists on a fixed interval instead of only when someone calls the API.

    Cardinality is four series per release for `nf_deployment_state` plus two more,
    bounded by the number of releases rather than by anything unbounded.

    The cost is real and deliberate: `helm list`, one pod listing for every release, and
    two helm calls per release (history, values) — `2 + 2N` subprocesses per scrape, down
    from `1 + 4N` before day 26. What the original trade-off did not survive is running
    them in series inside a fixed scrape timeout; see ADR-0014.
    """

    def collect(self) -> Iterator[GaugeMetricFamily]:
        deadline = time.monotonic() + SCRAPE_BUDGET
        # Timed alongside the scrape, not in front of it, so measuring costs the scrape
        # nothing; the answer is also what any call that times out during this scrape
        # will reuse instead of probing again.
        measured: list[Probe] = []
        prober = threading.Thread(
            target=lambda: measured.append(
                api_server_probe(max_age=0.0, timeout=SCRAPE_PROBE_TIMEOUT)
            ),
            daemon=True,
        )
        prober.start()
        reachable = GaugeMetricFamily(
            "nf_cluster_reachable",
            "1 when the orchestrator could reach the cluster during this scrape",
        )
        state = GaugeMetricFamily(
            "nf_deployment_state",
            "1 for the release's current lifecycle state, 0 for the others",
            labels=["release", "state"],
        )
        desired = GaugeMetricFamily(
            "nf_deployment_desired_replicas",
            "Replicas the intent asked for, read back from the release values",
            labels=["release"],
        )
        ready = GaugeMetricFamily(
            "nf_deployment_ready_replicas",
            "Pods of the release whose containers all report ready",
            labels=["release"],
        )
        operation = GaugeMetricFamily(
            "nf_last_operation_state",
            "1 for the state of the release's last lifecycle operation, 0 for the others",
            labels=["release", "state"],
        )

        busy = GaugeMetricFamily(
            "nf_cluster_busy",
            "1 when the API server was reachable but did not serve the orchestrator this "
            "scrape (throttled or overloaded)",
        )
        probe_seconds = GaugeMetricFamily(
            "nf_cluster_probe_seconds",
            "How long the API server took to answer its readiness probe this scrape, "
            "request to response as client-go measured it",
        )
        probe_local = GaugeMetricFamily(
            "nf_orchestrator_probe_local_seconds",
            "How much of this scrape's readiness probe was spent on the orchestrator's "
            "own host, mostly starting kubectl",
        )

        try:
            names = release_names()
        except HostOverloaded:
            # Nothing is known about the cluster, so no claim about it either way: no
            # reachable, no busy. What is known is how slow this host is, and that is the
            # series OrchestratorHostOverloaded pages on.
            record_probe(prober, measured, deadline, probe_seconds, probe_local)
            yield probe_seconds
            yield probe_local
            return
        except ClusterBusy:
            # Reachable, and refusing us. Not the drill-2 case — the cluster is there — but
            # no release could be listed either, so no release series: nf_cluster_busy is
            # what says why they are missing.
            reachable.add_metric([], 1.0)
            busy.add_metric([], 1.0)
            # Drill 11's regression run: half the queueing scrapes took this path and
            # carried no probe numbers, which is where the runbook sends you to look.
            record_probe(prober, measured, deadline, probe_seconds, probe_local)
            yield reachable
            yield busy
            yield probe_seconds
            yield probe_local
            return
        except UNAVAILABLE:
            # No cluster, no answer. Report that plainly and emit no release series
            # at all: a scrape that cannot see the cluster must not be indistinguish-
            # able from a cluster with nothing deployed in it.
            reachable.add_metric([], 0.0)
            # Drill 12: an outage and a starved host at once. The host's measurement is
            # still true and still needed; without it OrchestratorHostOverloaded cleared in
            # the middle of both. The probe is bounded, so waiting for it cannot outlast
            # the scrape it started with.
            record_probe(prober, measured, deadline, probe_seconds, probe_local)
            yield reachable
            yield probe_local
            return

        unreported = GaugeMetricFamily(
            "nf_releases_unreported",
            "Releases listed this scrape that emitted no lifecycle series, by reason",
            labels=["reason"],
        )
        # One series per listed release, present even when its reconcile did not finish.
        # Drill 7 found the releases that miss the deadline are the same ones every scrape
        # — the tail of helm's order — so a count alone hides which NFs have no alerting.
        reported = GaugeMetricFamily(
            "nf_release_reported",
            "1 if the release's lifecycle series were emitted this scrape, 0 if not",
            labels=["release"],
        )
        reachable.add_metric([], 1.0)

        # Every release's pods in one call. If that call fails, each reconcile falls back
        # to reading its own pods, so a failure lands in the per-release accounting below
        # instead of needing a path of its own.
        throttled = False
        try:
            pods: dict[str, list[dict[str, Any]]] | None = release_pods()
        except HostOverloaded:
            # Drill 11: this listing was the call that ran past its bound on a starved
            # host, and it used to set nf_cluster_busy for a cluster answering in 40ms.
            pods = None
        except ClusterBusy:
            pods, throttled = None, True
        except UNAVAILABLE:
            pods = None

        futures = reconcile_within(names, pods, deadline)

        late = errored = refused = ours = 0
        for name, future in futures.items():
            if not future.done() or future.cancelled():
                late += 1
                reported.add_metric([name], 0.0)
                continue
            current = future.result()
            if current is HOST:
                ours += 1
                reported.add_metric([name], 0.0)
                continue
            if current is BUSY:
                refused += 1
                reported.add_metric([name], 0.0)
                continue
            if not isinstance(current, dict):
                errored += 1
                reported.add_metric([name], 0.0)
                continue
            reported.add_metric([name], 1.0)
            for member in State:
                state.add_metric(
                    [name, member.value],
                    1.0 if current["state"] == member.value else 0.0,
                )
            desired.add_metric([name], float(current.get("desired_replicas", 0)))
            ready.add_metric([name], float(current.get("ready_replicas", 0)))
            last = current.get("last_operation", {}).get("state")
            for member in OperationState:
                operation.add_metric(
                    [name, member.value], 1.0 if last == member.value else 0.0
                )

        unreported.add_metric(["deadline"], float(late))
        unreported.add_metric(["error"], float(errored))
        unreported.add_metric(["busy"], float(refused))
        unreported.add_metric(["host"], float(ours))

        # Drill 10: under queueing nothing fails. Calls just take longer than the budget,
        # every release goes unreported for reason="deadline", and a count of late
        # releases reads as "the orchestrator is short of time" — drill 7's incident,
        # whose runbook fixes nothing here. The probe is the one call whose duration means
        # something on its own, so a slow probe is what says the cause is the cluster.
        #
        # Drill 11: a slow probe is not always a slow cluster. With the orchestrator's CPU
        # starved and the cluster healthy, the wall time was ~1.1s and the round trip
        # 31-80ms, and day 29 paged ClusterThrottlingOrchestrator for a cluster with
        # nothing wrong. So the cluster is judged on the round trip, and the rest is
        # reported as the host's.
        #
        # The probe started with the scrape and is bounded below the budget, so by the
        # deadline it has finished; the grace only covers a thread scheduled late.
        probe = record_probe(prober, measured, deadline, probe_seconds, probe_local)
        # No probe result at all says nothing about the cluster. Day 29 counted it as busy;
        # drill 11 showed the likeliest reason is the host being too slow to run it.
        busy.add_metric(
            [], 1.0 if throttled or refused or (probe is not None and probe.slow) else 0.0
        )

        yield reachable
        yield busy
        yield probe_seconds
        yield probe_local
        yield unreported
        yield reported
        yield state
        yield desired
        yield ready
        yield operation


def record_probe(
    prober: threading.Thread,
    measured: list[Probe],
    deadline: float,
    cluster_seconds: GaugeMetricFamily,
    local_seconds: GaugeMetricFamily,
) -> Probe | None:
    """Wait for the scrape's probe and record what it says about each end.

    The probe started with the scrape and is bounded below the budget, so by the deadline
    it has finished; the grace only covers a thread scheduled late.
    """
    prober.join(timeout=max(0.0, deadline - time.monotonic()) + 0.25)
    probe = measured[0] if measured else None
    if probe is None:
        return None
    if probe.round_trip is not None:
        cluster_seconds.add_metric([], probe.round_trip)
    elif probe.started:
        cluster_seconds.add_metric([], probe.seconds)
    if probe.local_seconds is not None:
        local_seconds.add_metric([], probe.local_seconds)
    return probe


REGISTRY.register(LifecycleCollector())


def render() -> bytes:
    return generate_latest(REGISTRY)
