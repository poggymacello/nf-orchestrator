import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.intent import Intent

CHART_PATH = Path(__file__).resolve().parent.parent / "charts" / "stand-in-nf"
# The cluster this orchestrator talks to. Overridable so a drill can be run against a
# throwaway cluster without touching the one every other milestone has reused, and so
# nothing has to edit a constant to point somewhere else.
KUBE_CONTEXT = os.environ.get("NF_KUBE_CONTEXT", "kind-nf-orchestrator")

# Every helm and kubectl call is bounded. Drill 6 froze the control plane and found the
# only limit in play was Go's 10-second TLS handshake timeout inside kubectl and helm —
# an accident of that failure's shape, which a server that completes the handshake and
# then stalls would not trigger. Ten seconds was also twice Prometheus's scrape timeout,
# so the metric the ClusterUnreachable alert reads never reached Prometheus at all.
#
# Reads serve the status endpoint and the metrics collector, and Prometheus gives a scrape
# 5s, so a read has to fail well inside that. Writes can legitimately take longer.
READ_TIMEOUT = float(os.environ.get("NF_READ_TIMEOUT", "3"))
WRITE_TIMEOUT = float(os.environ.get("NF_WRITE_TIMEOUT", "60"))
WRITE_VERBS = frozenset({"upgrade", "install", "uninstall", "rollback"})

# Substrings that mean "the cluster could not be reached", as opposed to "the cluster
# answered and said no". Collected from real helm and kubectl failures during the M4
# control-plane drill; the Windows wording differs from the POSIX one, so both are here.
UNREACHABLE_MARKERS = (
    "kubernetes cluster unreachable",
    "unable to connect to the server",
    "connection refused",
    "actively refused",
    "no such host",
    "i/o timeout",
)

# The API server answered and said "not now". client-go normally retries these silently
# (drill 9 saw `Retry-After: 32`), so the text only surfaces once its retries run out.
BUSY_MARKERS = (
    "too many requests",
    "has asked us to try again later",
)

# How long the readiness probe after a timed-out call may take. It must fit, together with
# the read timeout, inside the scrape budget — a test pins that.
PROBE_TIMEOUT = float(os.environ.get("NF_PROBE_TIMEOUT", "0.75"))
READYZ = ["get", "--raw=/readyz"]

# A probe answer is reused for this long. Drill 10 fired one probe per timed-out call: a
# wave of eight stalled releases asked the same question eight times in the same instant,
# of a server that was already over capacity. Long enough to cover one scrape, short
# enough that the answer still describes now.
PROBE_TTL = float(os.environ.get("NF_PROBE_TTL", "2"))

# A probe that answers this slowly says the server is being served slowly. `/readyz` is the
# cheapest thing the API server does and runs on a flow nobody throttles; drill 10 measured
# it at 0.45-3.58s (mean 1.65s) under queued back-pressure against ~0.25s idle.
SLOW_PROBE = float(os.environ.get("NF_SLOW_PROBE", "0.5"))

# client-go at -v=6 logs every response with the round trip it measured itself, which
# leaves out starting the kubectl process. Drill 11 starved the orchestrator's CPU with the
# cluster healthy: the probe's wall time was ~1.1s and its round trip 31-80ms.
PROBE_VERBOSITY = "-v=6"
ROUND_TRIP = re.compile(
    r'"Response" verb="GET" url="[^"]*/readyz" status="[^"]*" milliseconds=(\d+)'
)

# How long a successful call keeps counting as evidence that the cluster is there. Under
# queueing the probe is often too slow to answer inside PROBE_TIMEOUT, which on its own
# looked exactly like drill 7's stalled server — while other calls were still completing.
RECENT_SUCCESS = float(os.environ.get("NF_RECENT_SUCCESS", "30"))

# Server-side apply refusing to take a field from another manager. The wording comes
# from the M4 drill-3 conflict, where `kubectl scale` owned `.spec.replicas`.
CONFLICT_MARKERS = (
    "conflict occurred while applying",
    "apply failed with",
)


class DeployError(RuntimeError):
    pass


class ClusterUnreachable(DeployError):
    """The cluster could not be reached, so nothing is known about the release.

    Distinct from DeployError, which means the cluster answered and rejected the
    operation. Callers must not read "unreachable" as a fact about a deployment.
    """


class ClusterBusy(DeployError):
    """The API server is up but is not serving this client: throttled or overloaded.

    Not ClusterUnreachable, which says nothing can be known — the cluster can be reached
    and is choosing not to answer yet. Drill 9 saw API Priority and Fairness reject the
    orchestrator's service account with 429 and `Retry-After` up to 32s while `/readyz`
    answered instantly, and every one of those was reported as "unreachable".
    """


class HostOverloaded(DeployError):
    """This call was slow because of the orchestrator's own host, not the cluster.

    Drill 11 starved the orchestrator's CPU with the cluster healthy. Calls ran past their
    bound because starting helm or kubectl took over a second, the readiness probe
    answered, and day 28's rule reported the cluster as throttling this client. The probe's
    round trip was 31-80ms — the cluster was serving it promptly.
    """


class ClusterConflict(DeployError):
    """Another field manager owns a field this apply would change.

    Helm 4 applies server-side, so a field written by something else — `kubectl
    scale`, an autoscaler — is refused rather than silently overwritten. Resolving it
    means deciding who should own the field, which is a caller's decision and not
    one the deploy path makes on its own. See ADR-0008.
    """


def classify_error(message: str) -> DeployError:
    lowered = message.lower()
    if any(marker in lowered for marker in BUSY_MARKERS):
        return ClusterBusy(message)
    if any(marker in lowered for marker in UNREACHABLE_MARKERS):
        return ClusterUnreachable(message)
    if any(marker in lowered for marker in CONFLICT_MARKERS):
        return ClusterConflict(message)
    return DeployError(message)


def render_values(intent: Intent) -> dict[str, Any]:
    return {
        "replicaCount": intent.replicas,
        "environment": intent.environment,
    }


def release_name(intent: Intent) -> str:
    return intent.name


@dataclass(frozen=True)
class Probe:
    """What asking the API server for `/readyz` cost, and whether it answered at all.

    `seconds` is wall time, including starting kubectl on this host. `round_trip` is the
    part client-go measured itself, request to response, when it logged one. `started`
    says whether kubectl logged anything at all before it finished or was killed — a probe
    that never started never asked the cluster anything.
    """

    answered: bool
    seconds: float
    round_trip: float | None = None
    started: bool = True

    @property
    def slow(self) -> bool:
        """Whether the cluster, rather than this host, was slow to answer the probe.

        Judged on the round trip when there is one. A probe killed before kubectl logged
        a line says nothing about the cluster (drill 11: 7 of 8 probes on a starved host).
        One killed after starting, with no response yet, counts as slow, as on day 29:
        drill 10's queueing server took up to 3.58s to say `ok`. A response whose timing
        could not be read falls back to wall time, so a kubectl that changes its log
        format degrades to day 29's behaviour rather than to silence.
        """
        if self.round_trip is not None:
            return self.round_trip >= SLOW_PROBE
        if not self.started:
            return False
        return not self.answered or self.seconds >= SLOW_PROBE

    @property
    def local_seconds(self) -> float | None:
        """How much of the probe was spent on this host, when that can be said."""
        if self.round_trip is not None:
            return max(0.0, self.seconds - self.round_trip)
        if not self.started:
            return self.seconds
        return None


_probe_lock = threading.Lock()
_probe: tuple[float, Probe] | None = None
_last_success: float | None = None


def reset_probe_state() -> None:
    """Forget the cached probe and the last successful call. For tests."""
    global _probe, _last_success
    with _probe_lock:
        _probe = None
        _last_success = None


def note_success() -> None:
    """Record that the cluster answered us just now, whatever it answered."""
    global _last_success
    _last_success = time.monotonic()


def answered_recently(window: float = RECENT_SUCCESS) -> bool:
    return _last_success is not None and time.monotonic() - _last_success <= window


def api_server_probe(max_age: float = PROBE_TTL, timeout: float | None = None) -> Probe:
    """Ask `/readyz`, or reuse an answer no older than `max_age` seconds.

    The lock also means that when several calls time out together, the first one probes
    and the rest wait for its answer instead of each starting a kubectl of their own.
    """
    global _probe
    with _probe_lock:
        cached = _probe
        if cached is not None and time.monotonic() - cached[0] <= max_age:
            return cached[1]
        measured = _measure_probe(PROBE_TIMEOUT if timeout is None else timeout)
        _probe = (time.monotonic(), measured)
        return measured


def _measure_probe(timeout: float) -> Probe:
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, *READYZ, PROBE_VERBOSITY],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stderr or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        return Probe(
            answered=False,
            seconds=time.monotonic() - started,
            round_trip=_round_trip(partial),
            started=bool(partial.strip()),
        )
    except OSError:
        return Probe(answered=False, seconds=time.monotonic() - started, started=False)
    answered = result.returncode == 0
    if answered:
        note_success()
    return Probe(
        answered=answered,
        seconds=time.monotonic() - started,
        round_trip=_round_trip(result.stderr or ""),
    )


def _round_trip(log: str) -> float | None:
    match = ROUND_TRIP.search(log)
    return int(match.group(1)) / 1000 if match else None


def run_bounded(command: list[str], args: list[str], timeout: float) -> str:
    """Run a cluster command, failing as unreachable rather than waiting indefinitely.

    `args` are the caller's arguments without the context flags, so a timeout names
    the verb that stalled — drill 7 got "kubectl --context did not answer".
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        verb = " ".join([command[0], *args[:1]])
        if args != READYZ:
            local = local_delay()
            if local:
                raise HostOverloaded(
                    f"{verb} did not answer within {timeout:g}s, and {local}: the delay "
                    "is the orchestrator's own host, not the cluster"
                ) from exc
            evidence = reachability_evidence()
            if evidence:
                raise ClusterBusy(
                    f"{verb} did not answer within {timeout:g}s, but {evidence}: it is "
                    "reachable and not serving this client (throttled or overloaded)"
                ) from exc
        raise ClusterUnreachable(f"{verb} did not answer within {timeout:g}s") from exc
    if result.returncode != 0:
        error = classify_error(result.stderr.strip() or result.stdout.strip())
        if not isinstance(error, ClusterUnreachable):
            # It refused us, conflicted with us, or told us to come back later. Whatever
            # it said, it was there to say it.
            note_success()
        raise error
    note_success()
    return result.stdout


def reachability_evidence() -> str:
    """Why we can say the cluster is there, after a call of ours was killed for silence.

    Two independent sources, because drill 10 broke the first one. The probe is the
    direct answer. A call that completed moments ago is the indirect one, and it is what
    covers a server too loaded to serve even `/readyz` inside its bound while still
    serving everything else — the shape queueing takes. Neither is true of drills 2, 6
    and 7, where nothing answered at all.
    """
    if api_server_probe().answered:
        return "the API server answers its readiness probe"
    if answered_recently():
        return "the cluster answered another call moments ago"
    return ""


def local_delay() -> str:
    """Why this host, rather than the cluster, is the likely reason a call was slow.

    Asked first, because both other answers blame the cluster. Two cases, both from
    drill 11: the probe came back promptly from the server but took long on this host, or
    this host could not even start kubectl inside the probe's bound. The second says
    nothing about the cluster at all — which is exactly why it must not be called busy or
    unreachable; OrchestratorHostOverloaded is what should be paging.
    """
    probe = api_server_probe()
    if not probe.started:
        return (
            f"this host could not start its readiness probe within {probe.seconds:.2f}s, "
            "so nothing can be said about the cluster"
        )
    local = probe.local_seconds
    if probe.answered and not probe.slow and local is not None and local >= SLOW_PROBE:
        assert probe.round_trip is not None
        return (
            f"the API server answered its readiness probe in "
            f"{probe.round_trip * 1000:.0f}ms while this host took {local:.2f}s to run it"
        )
    return ""


def api_server_answers() -> bool:
    """Whether the API server answers `/readyz` inside its bound, cached for PROBE_TTL.

    A timeout alone cannot tell a server that is gone from one that is refusing this
    client. Kubernetes serves probe endpoints outside the flow-control levels that
    throttle ordinary requests, so during drill 9's load shedding `/readyz` answered in
    ~0.2s while every list and helm call waited on `Retry-After`. When the server is
    frozen or stalled (drills 6 and 7) the probe times out too, and the answer stays
    "unreachable" — but so does a server that is merely overloaded, which is why this is
    no longer the only evidence considered. See reachability_evidence.
    """
    return api_server_probe().answered


def timeout_for(args: list[str]) -> float:
    return WRITE_TIMEOUT if args and args[0] in WRITE_VERBS else READ_TIMEOUT


def run_helm(args: list[str]) -> str:
    return run_bounded(["helm", *args], args, timeout_for(args))


def run_kubectl(args: list[str]) -> str:
    return run_bounded(
        ["kubectl", "--context", KUBE_CONTEXT, *args], args, timeout_for(args)
    )


def check_cluster() -> str:
    """Raise ClusterUnreachable unless the API server answers its own readyz probe."""
    return run_kubectl(READYZ).strip()


LIST_PAGE = 256


def list_releases(page: int = LIST_PAGE) -> list[str]:
    """Every Helm release name in the context, so something can iterate them.

    Paged, because `helm list` returns at most 256 releases unless told otherwise and
    says nothing when it stops. A release past the cut would have had no series and no
    `nf_release_reported` either — the one alert that names unmonitored releases can
    only name releases it was told exist. Found on day 26 reading `helm list --help`;
    `--max 0` does not mean "all", it means the server's default.

    Pages are read by offset over helm's alphabetical order, so a release installed or
    removed between two pages can shift the boundary; names are de-duplicated, and the
    next scrape lists again from the start.
    """
    names: dict[str, None] = {}
    offset = 0
    while True:
        batch = json.loads(
            run_helm(
                [
                    "list",
                    "--kube-context",
                    KUBE_CONTEXT,
                    "--max",
                    str(page),
                    "--offset",
                    str(offset),
                    "--output",
                    "json",
                ]
            )
        )
        names.update((release["name"], None) for release in batch)
        if len(batch) < page:
            return list(names)
        offset += page


def deploy(intent: Intent, force_conflicts: bool = False) -> dict[str, Any]:
    """Apply an intent. force_conflicts takes ownership of fields another manager holds.

    The default is False and the deploy path never sets it. Forcing is an explicit,
    separately named operation, because it overrides whatever else was writing to
    those fields — see ADR-0008.
    """
    values = render_values(intent)
    name = release_name(intent)
    args = [
        "upgrade",
        "--install",
        name,
        str(CHART_PATH),
        "--kube-context",
        KUBE_CONTEXT,
        "--set-json",
        json.dumps(values),
        "--output",
        "json",
    ]
    if force_conflicts:
        args.append("--force-conflicts")
    output = run_helm(args)
    info = json.loads(output)["info"]
    return {
        "release": name,
        "status": info["status"],
        "revision": json.loads(output)["version"],
        "values": values,
    }
