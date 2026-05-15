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
    "scout": {
        "label": "Trend Scout",
        "stage": 1,
        "script": "run_scout.py",
        "kind": "llm",
        "outputs": ["raw_signals", "trend_report"],
        "prompt_file": "trend_scout.md",
    },
    "strategist": {
        "label": "Strategist",
        "stage": 2,
        "script": "run_strategist.py",
        "kind": "llm",
        "outputs": ["content_brief"],
        "prompt_file": "strategist.md",
    },
    "writer": {
        "label": "Copy Writer",
        "stage": 3,
        "script": "run_writer.py",
        "kind": "llm",
        "outputs": ["copy"],
        "prompt_file": "copy_writer.md",
    },
    "gate": {
        "label": "Quality Gate",
        "stage": 4,
        "script": "run_gate.py",
        "kind": "llm",
        "outputs": ["approved_copy"],
        "prompt_file": "quality_gate.md",
    },
    "visual": {
        "label": "Visual Brief",
        "stage": 5,
        "script": "run_visual.py",
        "kind": "llm",
        "outputs": ["visual_brief"],
        "prompt_file": "visual_brief.md",
    },
    "scheduler": {
        "label": "Scheduler",
        "stage": 6,
        "script": "run_scheduler.py",
        "kind": "det",
        "outputs": ["schedule", "notion_payload", "digest"],
        "prompt_file": None,
    },
    "render_image": {
        "label": "Image Render",
        "stage": 7,
        "script": "run_render_image.py",
        "kind": "render",
        "outputs": ["image_render"],
        "prompt_file": None,
    },
    "render_video": {
        "label": "Video Render",
        "stage": 8,
        "script": "run_render_video.py",
        "kind": "render",
        "outputs": ["video_render"],
        "prompt_file": None,
    },
}

JOBS: dict[str, dict[str, Any]] = {}


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

    return {
        "ok": True,
        "model": os.getenv("MODEL_CREATIVE", "qwen2.5:14b"),
        "ollama": _check(ollama_url, "/api/tags"),
        "comfyui": _check(comfy_url, "/system_stats"),
    }


@app.get("/api/agents")
def list_agents() -> dict:
    return {"agents": [{"key": k, **v} for k, v in AGENTS.items()]}


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
    }
    asyncio.create_task(_run_subprocess(job_id, cmd))
    return {"job_id": job_id, "cmd": JOBS[job_id]["cmd"]}


async def _run_subprocess(job_id: str, cmd: list[str]) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    queue: asyncio.Queue = job["queue"]

    # If running on host (no /app), proxy through docker exec into pws-agent.
    if not Path("/app").exists() and cmd and Path(cmd[0]).name.startswith("python"):
        script_args = cmd[1:]
        cmd = ["docker", "exec", "-i", "pws-agent", "python", *script_args]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                text = line.decode("utf-8", errors="replace").rstrip()
            except Exception:
                text = repr(line)
            await queue.put({"event": "log", "data": text})
        rc = await proc.wait()
        job["rc"] = rc
        job["status"] = "done" if rc == 0 else "failed"
        await queue.put({"event": "end", "data": json.dumps({"rc": rc, "status": job["status"]})})
    except Exception as e:
        job["status"] = "failed"
        await queue.put({"event": "end", "data": json.dumps({"rc": -1, "error": str(e)})})


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
    return {"job_id": job_id, "cmd": job["cmd"], "status": job["status"], "rc": job["rc"]}


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


@app.post("/api/chat")
def chat(req: ChatReq) -> dict:
    if req.agent not in AGENTS:
        raise HTTPException(400, "unknown agent")
    cfg = AGENTS[req.agent]
    if cfg["prompt_file"]:
        system_prompt = (PROMPTS / cfg["prompt_file"]).read_text(encoding="utf-8")
    else:
        system_prompt = META_PROMPTS.get(req.agent, "Generic assistant.")

    messages = [{"role": "system", "content": system_prompt}]
    for m in req.messages:
        messages.append({"role": m.role, "content": m.content})

    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    model = os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", "")
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.4, "num_ctx": 8192},
        "keep_alive": "10m",
    }
    if req.json_mode:
        body["format"] = "json"

    try:
        r = requests.post(f"{url}/api/chat", json=body, timeout=600)
        r.raise_for_status()
        reply = r.json()["message"]["content"]
    except Exception as e:
        raise HTTPException(500, f"ollama: {e}")
    return {"reply": reply, "agent": req.agent}


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

    # proxy via docker exec when running on host
    if not Path("/app").exists() and cmd and Path(cmd[0]).name.startswith("python"):
        script_args = cmd[1:]
        cmd = ["docker", "exec", "-i", "pws-agent", "python", *script_args]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=req.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return {"status": "timeout", "rc": -1, "log": f"killed after {req.timeout_s}s"}
        rc = proc.returncode or 0
        log = stdout.decode("utf-8", errors="replace")
        return {
            "status": "done" if rc == 0 else "failed",
            "rc": rc,
            "log_tail": log[-3500:],
            "cmd": " ".join(cmd),
        }
    except Exception as e:
        return {"status": "error", "rc": -1, "error": str(e)}


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
