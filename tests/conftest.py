"""Make `scripts/` importable, so the standalone scanners can be unit tested."""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

CLUSTER_TOOLS = frozenset({"helm", "kubectl"})


@pytest.fixture(autouse=True)
def unit_tests_never_reach_a_cluster(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail any unit test that runs helm or kubectl for real.

    Day 26: a test faked `helm_release_status` to raise ClusterUnreachable and asserted a
    503. After a refactor stopped calling that function, the fake never ran — the test
    shelled out to real helm against a stopped local cluster, got a genuine 503, and kept
    passing. It would have kept passing on any machine without a cluster, which is every
    CI runner. A test that only passes because nothing is there proves nothing.

    Tests that replace `subprocess.run` themselves override this, which is what they
    are asking for. End-to-end tests are exempt: reaching the cluster is their job.
    """
    if "e2e" in Path(str(request.node.fspath)).parts:
        yield
        return
    real_run = subprocess.run

    def guarded(command: object, *args: object, **kwargs: object) -> object:
        if isinstance(command, list | tuple) and command and command[0] in CLUSTER_TOOLS:
            raise AssertionError(
                f"unit test {request.node.name} ran `{' '.join(map(str, command[:4]))}` "
                "for real — fake the function the code under test actually calls"
            )
        return real_run(command, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", guarded)
    yield
