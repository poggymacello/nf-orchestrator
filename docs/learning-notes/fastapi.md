# FastAPI

FastAPI turns a Python function with type hints into a documented HTTP endpoint, using Starlette
for the ASGI layer and Pydantic for request/response validation.

## Mental model

A request comes in, uvicorn (the ASGI server) hands it to Starlette's router, the router matches
the path to the Python function decorated with `@app.get(...)` or similar, and the return value
gets serialized to JSON. No manual route registration file, no separate serializer class — the
function signature and its type hints are the contract.

## Three things that confused me

**1. Where the app object actually lives.** I expected an `app.py` at the top level. This repo's
`app = FastAPI(...)` lives in `orchestrator/__init__.py`, so `uvicorn orchestrator:app` is the
correct invocation, not `uvicorn orchestrator.app:app`. The module path in the uvicorn command has
to match wherever the `FastAPI()` instance is actually assigned, and a package's `__init__.py`
counts as the package's own module.

**2. What ASGI buys you over WSGI.** Uvicorn is an ASGI server, meaning it can handle async
request handlers natively (`async def` endpoints), unlike WSGI servers which are strictly
synchronous per worker. The current `/healthz` handler is a plain `def`, not `async def` — FastAPI
runs sync handlers in a thread pool automatically, so both styles work, but async only helps if the
handler is actually doing I/O it can await.

**3. `--reload` is not guaranteed to work.** I assumed file-watching plus auto-restart was a solved
problem. It is not, at least not on this setup. See the gotcha below.

## The gotcha: `--reload` silently served stale code

Ran `uvicorn orchestrator:app --reload --port 8000`, then edited the `/healthz` return value while
the server was running.

Symptom: the log printed `WatchFiles detected changes in 'orchestrator\__init__.py'. Reloading...`,
but no new `Started server process [PID]` line followed, and curl kept returning the old response
body for several seconds after the edit.

Fix: killed the process tree and started a plain `uvicorn orchestrator:app --port 8000`, no
`--reload`. That served the new code immediately, confirming the code change itself was correct —
the reload mechanism was the failure, not the handler.

## Commands run and real output

```
$ uvicorn orchestrator:app --reload --port 8000
INFO:     Will watch for changes in these directories: ['C:\\Users\\POGGY\\nf-orchestrator']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [1908] using WatchFiles
INFO:     Started server process [37408]
INFO:     Waiting for application startup.
INFO:     Application startup complete.

$ curl -i http://127.0.0.1:8000/
HTTP/1.1 404 Not Found
{"detail":"Not Found"}

$ curl -i http://127.0.0.1:8000/healthz
HTTP/1.1 200 OK
{"status":"ok"}
```

Only `/healthz` exists in the code today. There is no root route and no request-body endpoint yet,
so testing Pydantic validation errors (422 responses) is `(planned, M1)` — the intent-submission
endpoint that will actually accept a body doesn't exist until then.

## Minimal runnable snippet

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

Run with `uvicorn <module>:app --port 8000`, where `<module>` is wherever this `app` object is
assigned.
