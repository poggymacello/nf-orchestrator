from typing import Any

from fastapi import FastAPI, HTTPException

from orchestrator import deploy as deploy_engine
from orchestrator import reconciler
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


@app.post("/deployments", status_code=201)
def create_deployment(intent: Intent) -> dict[str, Any]:
    try:
        return deploy_engine.deploy(intent)
    except deploy_engine.DeployError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
