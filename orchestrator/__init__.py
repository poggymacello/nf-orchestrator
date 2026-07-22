from typing import Any

from fastapi import FastAPI

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
