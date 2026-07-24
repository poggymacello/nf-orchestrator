from typing import Any

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST

from orchestrator import deploy as deploy_engine
from orchestrator import metrics, reconciler
from orchestrator.intent import Intent

app = FastAPI(title="nf-orchestrator")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    metrics.record_deploy("success", intent.environment)
    return result


@app.get("/deployments/{name}")
def get_deployment(name: str) -> dict[str, Any]:
    try:
        return reconciler.reconcile(name)
    except deploy_engine.DeployError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete("/deployments/{name}")
def delete_deployment(name: str) -> dict[str, Any]:
    try:
        return reconciler.teardown(name)
    except deploy_engine.DeployError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
