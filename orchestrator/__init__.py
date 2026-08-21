from typing import Any

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST

from orchestrator import deploy as deploy_engine
from orchestrator import metrics, reconciler
from orchestrator.intent import Intent

app = FastAPI(title="nf-orchestrator")


def http_error(exc: deploy_engine.DeployError) -> HTTPException:
    """502 when the cluster answered and refused, 503 when it could not be reached."""
    unreachable = isinstance(exc, deploy_engine.ClusterUnreachable)
    return HTTPException(status_code=503 if unreachable else 502, detail=str(exc))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only: this process is running. Says nothing about the cluster."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness: this process can reach the cluster it orchestrates."""
    try:
        deploy_engine.check_cluster()
    except deploy_engine.DeployError as exc:
        raise http_error(exc) from exc
    return {"status": "ready", "cluster": "reachable"}


@app.get("/intents/schema")
def intent_schema() -> dict[str, Any]:
    return Intent.model_json_schema()


@app.post("/intents/validate")
def validate_intent(intent: Intent) -> dict[str, Any]:
    return {"valid": True, "intent": intent.model_dump()}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)


@app.post("/deployments", status_code=201)
def create_deployment(intent: Intent) -> dict[str, Any]:
    try:
        result = deploy_engine.deploy(intent)
    except deploy_engine.DeployError as exc:
        metrics.record_deploy("failed", intent.environment)
        raise http_error(exc) from exc
    metrics.record_deploy("success", intent.environment)
    return result


@app.get("/deployments/{name}")
def get_deployment(name: str) -> dict[str, Any]:
    try:
        return reconciler.reconcile(name)
    except deploy_engine.DeployError as exc:
        raise http_error(exc) from exc


@app.delete("/deployments/{name}")
def delete_deployment(name: str) -> dict[str, Any]:
    try:
        return reconciler.teardown(name)
    except deploy_engine.DeployError as exc:
        raise http_error(exc) from exc
