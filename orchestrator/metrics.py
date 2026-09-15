import os
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.core import GaugeMetricFamily

from orchestrator import reconciler
from orchestrator.deploy import DeployError, list_releases
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


def snapshot(name: str, pods: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Reconcile one release, or None if it cannot be read this scrape."""
    try:
        return reconcile_release(name, pods)
    except UNAVAILABLE:
        return None


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

        try:
            names = release_names()
        except UNAVAILABLE:
            # No cluster, no answer. Report that plainly and emit no release series
            # at all: a scrape that cannot see the cluster must not be indistinguish-
            # able from a cluster with nothing deployed in it.
            reachable.add_metric([], 0.0)
            yield reachable
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
        try:
            pods: dict[str, list[dict[str, Any]]] | None = release_pods()
        except UNAVAILABLE:
            pods = None

        pool = ThreadPoolExecutor(max_workers=SCRAPE_WORKERS)
        futures: dict[str, Future[dict[str, Any] | None]] = {
            name: pool.submit(snapshot, name, None if pods is None else pods.get(name, []))
            for name in names
        }
        wait(futures.values(), timeout=max(0.0, deadline - time.monotonic()))
        # Queued work is dropped; anything already running is itself bounded by the
        # per-call timeout, so abandoning it cannot leave a subprocess behind for long.
        pool.shutdown(wait=False, cancel_futures=True)

        late = errored = 0
        for name, future in futures.items():
            if not future.done() or future.cancelled():
                late += 1
                reported.add_metric([name], 0.0)
                continue
            current = future.result()
            if current is None:
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

        yield reachable
        yield unreported
        yield reported
        yield state
        yield desired
        yield ready
        yield operation


REGISTRY.register(LifecycleCollector())


def render() -> bytes:
    return generate_latest(REGISTRY)
