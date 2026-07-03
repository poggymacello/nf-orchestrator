from fastapi import FastAPI

app = FastAPI(title="nf-orchestrator")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
