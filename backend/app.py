"""ProofCrawl HTTP API + static chat UI."""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config, tools
from .intent import parse
from .runner import RunManager

app = FastAPI(title="ProofCrawl", version="1.0.0")
manager = RunManager()
DOWNLOADABLE = {"result.json": "application/json", "receipt.json": "application/json",
                "stdout.ndjson": "application/x-ndjson", "stderr.log": "text/plain"}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def plan_of(run) -> dict:
    t = run.task
    return {
        "intent": t.intent, "interpreted_by": t.interpreted_by, "reasoning": t.reasoning,
        "url": t.url, "outputs": t.outputs, "limits": {"max_pages": t.max_pages, "max_depth": t.max_depth,
        "crawl_budget_s": config.CRAWL_BUDGET_S, "hard_timeout_s": config.HARD_TIMEOUT_S},
        "tool": run.tool.name, "tool_why": run.tool.why, "argv": run.display_argv(),
        "steps": ["Validate URL & limits", "Spawn sitecrawl (no shell, scrubbed env, own process group)",
                  "Check robots.txt", "Crawl relevance-ranked pages", "Scan & quarantine injected instructions",
                  "Build evidence-linked summary", "Verify output hash & schema", "Respond"],
    }


@app.get("/")
def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "llm_enabled": config.LLM_ENABLED, "llm_model": config.LLM_MODEL if config.LLM_ENABLED else None,
            "allow_private_hosts": config.ALLOW_PRIVATE_HOSTS, "tools": [t["name"] for t in tools.describe()],
            "limits": {"max_pages": config.MAX_PAGES_CAP, "max_depth": config.MAX_DEPTH_CAP, "hard_timeout_s": config.HARD_TIMEOUT_S}}


@app.get("/api/tools")
def list_tools():
    return tools.describe()


@app.post("/api/chat")
def chat(body: ChatIn):
    spec = parse(body.message)
    if spec.intent != "crawl_site":
        return {"type": "message", "intent": spec.intent, "text": spec.reply, "interpreted_by": spec.interpreted_by,
                "reasoning": spec.reasoning}
    run = manager.start(spec)
    if run is None:
        return JSONResponse({"type": "message", "intent": "busy",
                             "text": f"Too many tasks running (limit {config.MAX_CONCURRENT_RUNS}). Please wait for one to finish."}, status_code=429)
    return {"type": "run", "run_id": run.id, "plan": plan_of(run)}


def _get(run_id: str):
    run = manager.get(run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    return run


@app.get("/api/runs/{run_id}/events")
async def stream(run_id: str, after: int = 0):
    run = _get(run_id)

    async def gen():
        n = after
        idle = 0
        while True:
            evs = run.events_since(n)
            for ev in evs:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            n += len(evs)
            if run.done.is_set() and not run.events_since(n):
                yield f"event: final\ndata: {json.dumps(run.final, ensure_ascii=False)}\n\n"
                return
            idle = 0 if evs else idle + 1
            if idle and idle % 100 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.12)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = _get(run_id)
    return {"run_id": run.id, "state": run.state, "plan": plan_of(run), "final": run.final}


@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str):
    run = _get(run_id)
    return {"cancelled": run.cancel(), "state": run.state}


@app.post("/api/runs/{run_id}/replay")
def replay(run_id: str):
    old = _get(run_id)
    if not old.done.is_set():
        raise HTTPException(409, "original run still in progress")
    run = manager.start(old.task.model_copy(), replay_of=old.id)
    if run is None:
        raise HTTPException(429, "too many runs")
    return {"type": "run", "run_id": run.id, "plan": plan_of(run), "replay_of": old.id}


@app.get("/api/runs/{run_id}/files/{name}")
def download(run_id: str, name: str):
    run = _get(run_id)
    if name not in DOWNLOADABLE:  # whitelist: no path traversal possible
        raise HTTPException(404, "not available")
    path = run.dir / name
    if not path.exists():
        raise HTTPException(404, "file not produced")
    return FileResponse(path, media_type=DOWNLOADABLE[name], filename=f"{run.id}-{name}")
