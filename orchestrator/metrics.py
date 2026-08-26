from collections.abc import Iterator
from typing import Any

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.core import GaugeMetricFamily

from orchestrator import reconciler
from orchestrator.deploy import DeployError, list_releases
from orchestrator.reconciler import State

REGISTRY = CollectorRegistry()

deployments_total = Counter(
    "deployments_total",
    "Intents submitted to the deploy engine, by outcome",
    ["result", "environment"],
    registry=REGISTRY,
)

# helm and kubectl are subprocesses: a missing binary raises OSError rather than
# DeployError, and a scrape must survive that the same way it survives an outage.
UNAVAILABLE = (DeployError, OSError)


def record_deploy(result: str, environment: str) -> None:
    deployments_total.labels(result=result, environment=environment).inc()


def release_names() -> list[str]:
    return list_releases()


def reconcile_release(name: str) -> dict[str, Any]:
    return reconciler.reconcile(name)


def snapshot(name: str) -> dict[str, Any] | None:
    """Reconcile one release, or None if it cannot be read this scrape."""
    try:
        return reconcile_release(name)
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

    The cost is real and deliberate: one `helm list` plus three subprocesses per
    release per scrape. At a 15s interval and a handful of releases that is fine, and
    it is the same trade-off ADR-0005 already accepted for the status endpoint.
    """

    def collect(self) -> Iterator[GaugeMetricFamily]:
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

        try:
            names = release_names()
        except UNAVAILABLE:
            # No cluster, no answer. Report that plainly and emit no release series
            # at all: a scrape that cannot see the cluster must not be indistinguish-
            # able from a cluster with nothing deployed in it.
            reachable.add_metric([], 0.0)
            yield reachable
            return

        reachable.add_metric([], 1.0)
        for name in names:
            current = snapshot(name)
            if current is None:
                continue
            for member in State:
                state.add_metric(
                    [name, member.value],
                    1.0 if current["state"] == member.value else 0.0,
                )
            desired.add_metric([name], float(current.get("desired_replicas", 0)))
            ready.add_metric([name], float(current.get("ready_replicas", 0)))

        yield reachable
        yield state
        yield desired
        yield ready


REGISTRY.register(LifecycleCollector())


def render() -> bytes:
    return generate_latest(REGISTRY)
