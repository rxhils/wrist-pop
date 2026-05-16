"""FastAPI backend wrapping all 8 agents + chat + pipeline + outputs.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000

Endpoints:
  GET  /                          static index.html
  GET  /api/agents                list 8 agents + metadata
  GET  /api/outputs               list output files for today
  GET  /api/outputs/{name}        fetch one output file (json/text)
  POST /api/run                   start a stage or pipeline; returns job_id
  GET  /api/job/{id}/stream       SSE stream of stdout + final status
  POST /api/chat                  single-turn chat with an agent's system prompt
  GET  /api/health                liveness + Ollama/ComfyUI status
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
OUT_DIR = ROOT / "outputs"
PROMPTS = ROOT / "prompts"

load_dotenv(ROOT / ".env")

app = FastAPI(title="Royal Pop Content System")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AGENTS: dict[str, dict[str, Any]] = {
    # ─── MARKETING CHAIN ───
    "scout": {
        "label": "Trend Scout",
        "tier": "marketing",
        "stage": 1,
        "script": "run_scout.py",
        "kind": "llm",
        "outputs": ["raw_signals", "trend_report"],
        "prompt_file": "trend_scout.md",
    },
    "strategist": {
        "label": "Marketing Director",
        "tier": "marketing",
        "stage": 2,
        "script": "run_strategist.py",
        "kind": "llm",
        "outputs": ["content_brief"],
        "prompt_file": "strategist.md",
    },
    "writer": {
        "label": "Copy",
        "tier": "marketing",
        "stage": 3,
        "script": "run_writer.py",
        "kind": "llm",
        "outputs": ["copy"],
        "prompt_file": "copy_writer.md",
    },
    "gate": {
        "label": "QA",
        "tier": "marketing",
        "stage": 4,
        "script": "run_gate.py",
        "kind": "llm",
        "outputs": ["approved_copy"],
        "prompt_file": "quality_gate.md",
    },
    "visual": {
        "label": "Visual Brief",
        "tier": "marketing",
        "stage": 5,
        "script": "run_visual.py",
        "kind": "llm",
        "outputs": ["visual_brief"],
        "prompt_file": "visual_brief.md",
    },
    "manual_reel": {
        "label": "Manual Reel",
        "tier": "marketing",
        "stage": 6,
        "script": "run_manual_reel.py",
        "kind": "manual",
        "outputs": ["manual_reel_state"],
        "prompt_file": "manual_reel.md",
    },
    "manual_post": {
        "label": "Manual Post",
        "tier": "marketing",
        "stage": 7,
        "script": "run_manual_post.py",
        "kind": "manual",
        "outputs": ["manual_post_state"],
        "prompt_file": "manual_post.md",
    },
    "output_director": {
        "label": "Output Director",
        "tier": "marketing",
        "stage": 8,
        "script": "run_output_director.py",
        "kind": "llm",
        "outputs": ["operator_console"],
        "prompt_file": "output_director.md",
    },
    # ─── TOOLS (optional, off-chain) ───
    "scheduler": {
        "label": "Scheduler",
        "tier": "tools",
        "stage": 9,
        "script": "run_scheduler.py",
        "kind": "det",
        "outputs": ["schedule", "notion_payload", "digest"],
        "prompt_file": "scheduler.md",
    },
    "render_image": {
        "label": "Image Render",
        "tier": "tools",
        "stage": 10,
        "script": "run_render_image.py",
        "kind": "render",
        "outputs": ["image_render"],
        "prompt_file": None,
    },
    "render_video": {
        "label": "Video Render",
        "tier": "tools",
        "stage": 11,
        "script": "run_render_video.py",
        "kind": "render",
        "outputs": ["video_render"],
        "prompt_file": None,
    },
    # ─── REELS (post-pipeline creative engine) ───
    "reel_director": {
        "label": "Reel Director",
        "tier": "reels",
        "stage": 12,
        "script": "run_reel_director.py",
        "kind": "llm",
        "outputs": ["reel_ideas"],
        "prompt_file": "reel_director.md",
    },
}

JOBS: dict[str, dict[str, Any]] = {}


def _autosave_cloud(agent: str) -> None:
    """After a successful agent run, push today's matching artifact files to Supabase.
    Silent no-op when cloud not configured. Best-effort; never raises upstream.
    """
    try:
        import cloud_store as _cs
        if not _cs.is_configured():
            return
    except ImportError:
        return

    today = date.today().isoformat()
    if agent == "pipeline":
        agents_to_save = [k for k, v in AGENTS.items() if v.get("tier") == "marketing"]
    elif agent in AGENTS:
        agents_to_save = [agent]
    else:
        return

    for ag in agents_to_save:
        cfg = AGENTS.get(ag)
        if not cfg:
            continue
        for prefix in cfg.get("outputs") or []:
            path = OUT_DIR / f"{prefix}_{today}.json"
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            try:
                _cs.save_run(ag, prefix, payload, run_date=today)
            except Exception as e:
                print(f"[cloud autosave] {ag}/{prefix} failed: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# Health / metadata
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    comfy_url = os.getenv("COMFYUI_URL", "http://host.docker.internal:8188")

    def _check(url: str, path: str = "/") -> bool:
        try:
            return requests.get(url + path, timeout=3).ok
        except Exception:
            return False

    try:
        import cloud_store as _cs
        cloud = _cs.health()
    except Exception as e:
        cloud = {"configured": False, "ok": False, "reason": str(e)[:200]}

    return {
        "ok": True,
        "model": os.getenv("MODEL_CREATIVE", "qwen2.5:14b"),
        "ollama": _check(ollama_url, "/api/tags"),
        "comfyui": _check(comfy_url, "/system_stats"),
        "cloud": cloud,
    }


@app.get("/api/agents")
def list_agents() -> dict:
    return {"agents": [{"key": k, **v} for k, v in AGENTS.items()]}


@app.get("/api/cloud/health")
def cloud_health() -> dict:
    try:
        import cloud_store as _cs
        return _cs.health()
    except Exception as e:
        return {"configured": False, "ok": False, "reason": str(e)[:200]}


@app.get("/api/cloud/runs")
def cloud_runs(agent: str | None = None, limit: int = 30) -> dict:
    try:
        import cloud_store as _cs
        rows = _cs.recent_runs(agent_id=agent, limit=min(limit, 100))
        return {"rows": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/cloud/winners")
def cloud_winners(limit: int = 10, days: int = 30) -> dict:
    try:
        import cloud_store as _cs
        rows = _cs.recent_winners(limit=min(limit, 100), days=days)
        return {"rows": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(500, str(e))


class PromoteWinnerReq(BaseModel):
    hook_text: str
    format: str | None = None
    engagement_score: int | None = None
    source_run_id: int | None = None


@app.post("/api/cloud/winners")
def cloud_promote_winner(req: PromoteWinnerReq) -> dict:
    import cloud_store as _cs
    wid = _cs.promote_winner(
        req.hook_text,
        format=req.format,
        engagement_score=req.engagement_score,
        source_run_id=req.source_run_id,
    )
    return {"id": wid}


class RecordMetricsReq(BaseModel):
    post_id: str
    run_id: int | None = None
    platform: str | None = None
    saves: int | None = None
    shares: int | None = None
    comments: int | None = None
    waitlist_signups: int | None = None
    posted_at: str | None = None


@app.post("/api/cloud/metrics")
def cloud_record_metrics(req: RecordMetricsReq) -> dict:
    import cloud_store as _cs
    mid = _cs.record_metrics(
        req.post_id, run_id=req.run_id, platform=req.platform,
        saves=req.saves, shares=req.shares, comments=req.comments,
        waitlist_signups=req.waitlist_signups, posted_at=req.posted_at,
    )
    return {"id": mid}


# ─────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/outputs")
def list_outputs(d: str | None = None) -> dict:
    today = d or date.today().isoformat()
    OUT_DIR.mkdir(exist_ok=True)
    files = []
    for p in sorted(OUT_DIR.glob(f"*{today}*")):
        files.append({
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
        })
    return {"date": today, "files": files}


@app.get("/api/prompts/{agent}")
def get_agent_prompt(agent: str, inlined: bool = True) -> dict:
    """Return agent's system prompt.

    inlined=True (default): @include directives resolved (what the LLM actually sees).
    inlined=False: raw file as-is on disk.
    """
    if agent not in AGENTS:
        raise HTTPException(404, f"unknown agent '{agent}'")
    cfg = AGENTS[agent]
    pf = cfg.get("prompt_file")
    if not pf:
        return {"agent": agent, "prompt": "(no prompt file — deterministic agent)", "path": None}
    path = PROMPTS / pf
    if not path.exists():
        raise HTTPException(404, f"prompt file missing: {pf}")
    from prompts import load_prompt
    text = load_prompt(pf) if inlined else path.read_text(encoding="utf-8")
    return {
        "agent": agent,
        "prompt": text,
        "path": str(path.relative_to(ROOT)),
        "inlined": inlined,
    }


@app.get("/api/outputs/{name}")
def get_output(name: str) -> Any:
    safe = (OUT_DIR / name).resolve()
    if not str(safe).startswith(str(OUT_DIR.resolve())):
        raise HTTPException(400, "bad path")
    if not safe.exists():
        raise HTTPException(404, "not found")
    if safe.suffix == ".json":
        try:
            return JSONResponse(json.loads(safe.read_text(encoding="utf-8")))
        except Exception as e:
            raise HTTPException(500, f"parse: {e}")
    return FileResponse(safe, media_type="text/markdown" if safe.suffix == ".md" else "text/plain")


# ─────────────────────────────────────────────────────────────────────────
# Run jobs (single stage or full pipeline) — SSE for live output
# ─────────────────────────────────────────────────────────────────────────
class RunReq(BaseModel):
    agent: str | None = None        # one of AGENTS keys, OR "pipeline"
    from_stage: str | None = None   # for pipeline only
    only: str | None = None         # for pipeline only
    pick: int | None = None         # for writer/visual/render
    shot: int | None = None         # for render
    input_path: str | None = None   # for any agent that supports --input


@app.post("/api/run")
async def start_job(req: RunReq) -> dict:
    if req.agent == "pipeline" or req.agent is None and (req.from_stage or req.only):
        cmd = [sys.executable, "run_pipeline.py"]
        if req.from_stage:
            cmd += ["--from", req.from_stage]
        if req.only:
            cmd += ["--only", req.only]
    elif req.agent in AGENTS:
        script = AGENTS[req.agent]["script"]
        cmd = [sys.executable, script]
        if req.input_path:
            cmd += ["--input", req.input_path]
        if req.pick is not None:
            cmd += ["--pick", str(req.pick)]
        if req.shot is not None:
            cmd += ["--shot", str(req.shot)]
    else:
        raise HTTPException(400, "unknown agent")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "cmd": " ".join(shlex.quote(c) for c in cmd),
        "status": "queued",
        "queue": asyncio.Queue(),
        "rc": None,
        "agent": req.agent,
    }
    asyncio.create_task(_run_subprocess(job_id, cmd, req.agent))
    return {"job_id": job_id, "cmd": JOBS[job_id]["cmd"]}


def _maybe_proxy_through_docker(cmd: list[str]) -> list[str]:
    """Only proxy through `docker exec pws-agent` if running on host AND docker container is up.
    Otherwise run subprocess directly (works fine on host with deps installed)."""
    if Path("/app").exists():
        return cmd  # already in container
    if not cmd or not Path(cmd[0]).name.startswith("python"):
        return cmd
    # Check docker availability quickly
    try:
        import shutil, subprocess as _sp
        if not shutil.which("docker"):
            return cmd  # docker CLI not installed
        r = _sp.run(
            ["docker", "ps", "--filter", "name=pws-agent", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=3,
        )
        if "pws-agent" not in r.stdout:
            return cmd  # container not running
    except Exception:
        return cmd  # docker hung or missing
    # Container present + running — proxy through it
    return ["docker", "exec", "-i", "pws-agent", "python", *cmd[1:]]


async def _run_subprocess(job_id: str, cmd: list[str], agent: str | None = None) -> None:
    """Run subprocess in a thread (asyncio.create_subprocess_exec broken on Windows uvicorn SelectorEventLoop)."""
    import subprocess as _sp
    import threading
    job = JOBS[job_id]
    job["status"] = "running"
    queue: asyncio.Queue = job["queue"]
    cmd = _maybe_proxy_through_docker(cmd)
    loop = asyncio.get_event_loop()

    def _put(evt: dict) -> None:
        asyncio.run_coroutine_threadsafe(queue.put(evt), loop)

    def _runner():
        try:
            proc = _sp.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=_sp.PIPE,
                stderr=_sp.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            assert proc.stdout
            for line in proc.stdout:
                _put({"event": "log", "data": line.rstrip()})
            rc = proc.wait()
            job["rc"] = rc
            job["status"] = "done" if rc == 0 else "failed"
            # Auto-save artifacts to cloud (best-effort; never blocks UI)
            if rc == 0 and agent:
                try:
                    _autosave_cloud(agent)
                    _put({"event": "log", "data": "[cloud] artifacts saved"})
                except Exception as e:
                    _put({"event": "log", "data": f"[cloud] autosave failed: {e}"})
            _put({"event": "end", "data": json.dumps({"rc": rc, "status": job["status"]})})
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}\n{tb}"
            _put({"event": "log", "data": f"[error] {type(e).__name__}: {e}"})
            _put({"event": "log", "data": tb})
            _put({"event": "end", "data": json.dumps({"rc": -1, "error": str(e)})})

    threading.Thread(target=_runner, daemon=True).start()


@app.get("/api/job/{job_id}/stream")
async def job_stream(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no job")
    queue: asyncio.Queue = job["queue"]

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}
                continue
            yield evt
            if evt.get("event") == "end":
                break

    return EventSourceResponse(gen())


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no job")
    return {
        "job_id": job_id,
        "cmd": job["cmd"],
        "status": job["status"],
        "rc": job["rc"],
        "error": job.get("error"),
    }


# ─────────────────────────────────────────────────────────────────────────
# Chat
# ─────────────────────────────────────────────────────────────────────────
META_PROMPTS = {
    "scheduler": "You are a meta-assistant for the Scheduler stage. Deterministic Python — assigns posting times per platform (UK windows), emits Notion payload + digest. Help the user understand or modify it.",
    "render_image": "You are a meta-assistant for the Image Render stage. Deterministic Python — sends FLUX.1 schnell prompts to ComfyUI :8188. Help the user understand or modify it.",
    "render_video": "You are a meta-assistant for the Video Render stage. Deterministic Python — sends LTX-Video 2.3 prompts to ComfyUI :8188. Help the user understand or modify it.",
}


class ChatMsg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    agent: str
    messages: list[ChatMsg]
    json_mode: bool = False


CHAT_MODE_OVERRIDE = """

## CHAT MODE OVERRIDE
The user is chatting with you directly, not triggering a pipeline run.

IGNORE the output schema / JSON-only / "valid JSON" rules above. Those apply ONLY
when the pipeline invokes you with real data.

In chat mode:
- Reply in natural English. Conversational tone.
- Answer questions about your role, your prompts, your outputs.
- If asked to draft, rewrite, or critique something — do it inline.
- If asked to produce structured output (JSON, table, list), then do — but only when explicitly asked.
- You may reference Pop Wrist Studio facts, but you can also discuss your own behaviour.
"""


@app.post("/api/chat")
def chat(req: ChatReq) -> dict:
    if req.agent not in AGENTS:
        raise HTTPException(400, "unknown agent")
    cfg = AGENTS[req.agent]
    if cfg["prompt_file"]:
        from prompts import load_prompt
        system_prompt = load_prompt(cfg["prompt_file"])
    else:
        system_prompt = META_PROMPTS.get(req.agent, "Generic assistant.")

    # If user did NOT explicitly ask for JSON mode, append chat override
    if not req.json_mode:
        system_prompt = system_prompt + CHAT_MODE_OVERRIDE

    # Map agent key → pipeline_config name. Both use same keys.
    from pipeline_config import AGENT_CONFIG
    from providers import call_llm
    pcfg = AGENT_CONFIG.get(req.agent, {})
    provider = pcfg.get("provider", "ollama")
    model = pcfg.get("model")
    temperature = pcfg.get("temperature", 0.4) or 0.4
    max_tokens = pcfg.get("max_tokens", 2048) or 2048

    # Build prompt — last user message + history above as part of user msg
    history_text = ""
    for m in req.messages[:-1]:
        history_text += f"[{m.role}] {m.content}\n\n"
    last_user = req.messages[-1].content if req.messages else ""
    user_prompt = (history_text + last_user) if history_text else last_user

    try:
        reply = call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider=provider,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=req.json_mode,
            num_ctx=8192,
        )
    except Exception as e:
        raise HTTPException(500, f"{provider} error: {e}")
    return {
        "reply": reply,
        "agent": req.agent,
        "provider_used": provider,
        "model_used": model,
    }


# ─────────────────────────────────────────────────────────────────────────
# Open-WebUI integration endpoints
# Synchronous wrappers so Open-WebUI Tools can call + get a complete result.
# ─────────────────────────────────────────────────────────────────────────
class OWUIRunReq(BaseModel):
    stage: str  # agent key OR "pipeline"
    input_path: str | None = None
    pick: int | None = None
    shot: int | None = None
    from_stage: str | None = None
    only: str | None = None
    timeout_s: int = 1500


@app.post("/api/owui/run-stage")
async def owui_run_stage(req: OWUIRunReq) -> dict:
    """Sync run — block until stage finishes or timeout. Return final log + status.

    For Open-WebUI Tools that prefer single-shot calls instead of SSE.
    """
    if req.stage == "pipeline":
        cmd = [sys.executable, "run_pipeline.py"]
        if req.from_stage:
            cmd += ["--from", req.from_stage]
        if req.only:
            cmd += ["--only", req.only]
    elif req.stage in AGENTS:
        script = AGENTS[req.stage]["script"]
        cmd = [sys.executable, script]
        if req.input_path:
            cmd += ["--input", req.input_path]
        if req.pick is not None:
            cmd += ["--pick", str(req.pick)]
        if req.shot is not None:
            cmd += ["--shot", str(req.shot)]
    else:
        raise HTTPException(400, f"unknown stage: {req.stage}")

    cmd = _maybe_proxy_through_docker(cmd)

    # Use sync subprocess in a thread (asyncio subprocess broken on Windows uvicorn).
    import subprocess as _sp
    import concurrent.futures

    def _run():
        try:
            res = _sp.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=req.timeout_s,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            log = (res.stdout or "") + (res.stderr or "")
            rc = res.returncode
            return {
                "status": "done" if rc == 0 else "failed",
                "rc": rc,
                "log_tail": log[-3500:],
                "cmd": " ".join(cmd),
            }
        except _sp.TimeoutExpired:
            return {"status": "timeout", "rc": -1, "log": f"killed after {req.timeout_s}s"}
        except Exception as e:
            return {"status": "error", "rc": -1, "error": str(e)}

    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, _run)


@app.get("/api/owui/latest")
def owui_latest(stage: str) -> dict:
    """Return the latest output JSON for a stage."""
    pattern_map = {
        "scout": "trend_report",
        "strategist": "content_brief",
        "writer": "copy",
        "gate": "approved_copy",
        "visual": "visual_brief",
        "scheduler": "schedule",
        "render_image": "image_render",
        "render_video": "video_render",
    }
    prefix = pattern_map.get(stage)
    if not prefix:
        raise HTTPException(400, "unknown stage")
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    if not files:
        return {"status": "no_output", "stage": stage}
    data = json.loads(files[0].read_text(encoding="utf-8"))
    return {"status": "ok", "stage": stage, "file": files[0].name, "data": data}


@app.post("/api/owui/upload")
async def owui_upload(file_b64: str, filename: str) -> dict:
    """Accept a base64-encoded file from Open-WebUI tool. Save to outputs/uploads/."""
    import base64
    safe_name = Path(filename).name
    target_dir = OUT_DIR / "uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name
    try:
        target.write_bytes(base64.b64decode(file_b64))
    except Exception as e:
        raise HTTPException(400, f"decode failed: {e}")
    return {"status": "ok", "path": str(target)}


@app.post("/api/owui/evict-ollama")
def owui_evict_ollama() -> dict:
    """Force Ollama to unload the model — frees VRAM before ComfyUI render."""
    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    model = os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", "")
    try:
        requests.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=10,
        )
        return {"status": "ok", "evicted": model}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# Agent provider/model config — get + set per-agent assignments
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/agents-config")
def get_agents_config() -> dict:
    """Return current per-agent provider/model assignments + provider catalog + key status."""
    from pipeline_config import AGENT_CONFIG
    from providers import PROVIDERS, has_key
    return {
        "agents": {
            name: {
                "provider": cfg.get("provider"),
                "model": cfg.get("model"),
                "temperature": cfg.get("temperature"),
                "max_tokens": cfg.get("max_tokens"),
                "description": cfg.get("description", ""),
            }
            for name, cfg in AGENT_CONFIG.items()
        },
        "providers": {
            p: {
                "models": cfg.get("models", {}),
                "default_model": cfg.get("default_model"),
                "key_present": has_key(p),
            }
            for p, cfg in PROVIDERS.items()
        },
    }


class AgentConfigUpdate(BaseModel):
    agent: str
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@app.post("/api/agents-config")
def set_agent_config(req: AgentConfigUpdate) -> dict:
    """Update one agent's provider/model live (no restart needed). In-memory only."""
    from pipeline_config import AGENT_CONFIG
    if req.agent not in AGENT_CONFIG:
        raise HTTPException(404, f"unknown agent '{req.agent}'")
    if req.provider is not None:
        AGENT_CONFIG[req.agent]["provider"] = req.provider
    if req.model is not None:
        AGENT_CONFIG[req.agent]["model"] = req.model
    if req.temperature is not None:
        AGENT_CONFIG[req.agent]["temperature"] = req.temperature
    if req.max_tokens is not None:
        AGENT_CONFIG[req.agent]["max_tokens"] = req.max_tokens
    return {"status": "ok", "agent": req.agent, "config": AGENT_CONFIG[req.agent]}


class GlobalOverride(BaseModel):
    provider: str
    model: str | None = None


@app.post("/api/agents-config/all")
def set_all_agents_endpoint(req: GlobalOverride) -> dict:
    """Override all agents to a single provider (e.g. 'mistral' globally)."""
    from pipeline_config import AGENT_CONFIG, set_all_agents
    set_all_agents(req.provider, req.model)
    return {"status": "ok", "config": {n: c for n, c in AGENT_CONFIG.items()}}


# ─────────────────────────────────────────────────────────────────────────
# Vision Analyst — analyse uploaded images via Ollama vision model
# ─────────────────────────────────────────────────────────────────────────
class VisionAnalyseReq(BaseModel):
    images_b64: list[str]
    model: str | None = None


@app.get("/api/vision/models")
def vision_models_endpoint() -> dict:
    from vision import get_available_vision_models, pick_default_model, VISION_MODEL_NAMES, OLLAMA_URL
    import requests as _rq
    raw_models = []
    err = None
    try:
        r = _rq.get(f"{OLLAMA_URL}/api/tags", timeout=20)
        r.raise_for_status()
        raw_models = [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        err = str(e)
    return {
        "available": get_available_vision_models(),
        "default": pick_default_model(),
        "_debug": {
            "ollama_url_used": OLLAMA_URL,
            "all_models_from_ollama": raw_models,
            "filter_keywords": list(VISION_MODEL_NAMES),
            "error": err,
        },
    }


@app.post("/api/vision/analyse")
def vision_analyse_endpoint(req: VisionAnalyseReq) -> dict:
    from vision import analyse_batch, build_context_summary, pick_default_model
    model = req.model or pick_default_model()
    if not model:
        return {
            "status": "error",
            "error": "No vision model found. Run: ollama pull qwen2.5vl",
        }
    analyses = analyse_batch(req.images_b64, model)
    return {
        "status": "ok",
        "model_used": model,
        "analyses": analyses,
        "context_summary": build_context_summary(analyses),
        "count": len(analyses),
    }


# ─────────────────────────────────────────────────────────────────────────
# Manual image render — one-shot from UI (separate from batch render_image stage)
# ─────────────────────────────────────────────────────────────────────────
class RenderImageReq(BaseModel):
    model: str = "SDXL"               # "FLUX.1 schnell" | "SDXL"
    mode: str = "Text to image"       # "Text to image" | "Image to image"
    colourway: str = "Monochrome"
    shot_type: str = "Hero shot"
    user_text: str = ""
    ref_b64: str | None = None
    strength: float = 0.65


@app.get("/api/render/options")
def render_options() -> dict:
    from render import COLOURWAY, SHOT
    return {
        "models": ["FLUX.1 schnell", "SDXL", "SDXL Turbo"],
        "modes": ["Text to image", "Image to image"],
        "colourways": list(COLOURWAY.keys()),
        "shots": list(SHOT.keys()),
    }


@app.post("/api/render/image")
def render_image_endpoint(req: RenderImageReq) -> dict:
    from render import render_image
    return render_image(
        model=req.model,
        mode=req.mode,
        colourway=req.colourway,
        shot_type=req.shot_type,
        user_text=req.user_text,
        ref_b64=req.ref_b64,
        strength=req.strength,
    )


class RenderVideoReq(BaseModel):
    prompt: str
    refs_b64: list[str]


@app.post("/api/render/video")
def render_video_endpoint(req: RenderVideoReq) -> dict:
    from render import render_video_multi
    return render_video_multi(prompt=req.prompt, refs_b64=req.refs_b64)


@app.get("/api/render/video/status")
def render_video_status() -> dict:
    from render import ltx_checkpoint_present
    return {"ltx_available": ltx_checkpoint_present()}


# ─────────────────────────────────────────────────────────────────────────
# Static
# ─────────────────────────────────────────────────────────────────────────
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "index.html missing"}, status_code=500)
    return FileResponse(idx)
