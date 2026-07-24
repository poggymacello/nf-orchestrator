import json
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.intent import Intent

CHART_PATH = Path(__file__).resolve().parent.parent / "charts" / "stand-in-nf"
KUBE_CONTEXT = "kind-nf-orchestrator"


class DeployError(RuntimeError):
    pass


def render_values(intent: Intent) -> dict[str, Any]:
    return {
        "replicaCount": intent.replicas,
        "environment": intent.environment,
    }


def release_name(intent: Intent) -> str:
    return intent.name


def run_helm(args: list[str]) -> str:
    result = subprocess.run(
        ["helm", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DeployError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def deploy(intent: Intent) -> dict[str, Any]:
    values = render_values(intent)
    name = release_name(intent)
    output = run_helm(
        [
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
    )
    info = json.loads(output)["info"]
    return {
        "release": name,
        "status": info["status"],
        "revision": json.loads(output)["version"],
        "values": values,
    }
