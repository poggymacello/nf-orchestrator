from typing import Any

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict

from orchestrator import deploy as deploy_engine
from orchestrator import metrics, reconciler
from orchestrator.intent import Intent
from orchestrator.translate import RuleTranslator, TranslationError, translate

app = FastAPI(title="nf-orchestrator")


class TranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str


BUSY_RETRY_AFTER = "5"


def http_error(exc: deploy_engine.DeployError) -> HTTPException:
    """503 unreachable or busy, 409 a field-ownership conflict, 502 any other refusal."""
    if isinstance(exc, deploy_engine.HostOverloaded):
        # The orchestrator cannot answer right now and expects to again, which is 503 with
        # Retry-After whoever is to blame — the detail is what says it is this host.
        return HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": BUSY_RETRY_AFTER}
        )
    if isinstance(exc, deploy_engine.ClusterBusy):
        # Still 503 — the orchestrator cannot answer right now — but with Retry-After,
        # because unlike an outage this one is expected to clear on its own. Not 502:
        # that means the cluster refused the operation, and a caller would not retry it.
        return HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": BUSY_RETRY_AFTER}
        )
    if isinstance(exc, deploy_engine.ClusterUnreachable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, deploy_engine.ClusterConflict):
        return HTTPException(
            status_code=409,
            detail=(
                f"{exc} -- another field manager owns a field this intent would "
                "change. Resubmitting will not help. To take ownership, POST the "
                "same intent to /deployments/{name}/repair."
            ),
        )
    return HTTPException(status_code=502, detail=str(exc))


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


@app.post("/intents/translate")
def translate_intent(request: TranslationRequest) -> dict[str, Any]:
    """Free text to a candidate intent. Translating is not deploying.

    The candidate goes through the same Intent validation as any hand-written body, and
    the caller still has to POST it to /deployments on purpose. See ADR-0012.
    """
    try:
        return translate(request.text, RuleTranslator())
    except TranslationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@app.post("/deployments/{name}/repair", status_code=200)
def repair_deployment(name: str, intent: Intent) -> dict[str, Any]:
    """Apply an intent, taking ownership of fields another manager holds.

    Separate from POST /deployments because forcing overrides whatever else was
    writing to those fields. The caller states the intent it wants in effect rather
    than the orchestrator guessing which of several recorded intents to restore.
    """
    if intent.name != name:
        raise HTTPException(
            status_code=400,
            detail=f"intent name {intent.name!r} does not match path {name!r}",
        )
    try:
        result = deploy_engine.deploy(intent, force_conflicts=True)
    except deploy_engine.DeployError as exc:
        metrics.record_repair("failed", intent.environment)
        raise http_error(exc) from exc
    metrics.record_repair("success", intent.environment)
    return {**result, "forced": True}


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
