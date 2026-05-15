"""Royal Pop Content Pipeline — Open-WebUI Tool.

Install:
  Open-WebUI → Workspace → Tools → "+" → paste this file → Save.
  Then in any chat: tool icon → enable "Royal Pop Content Pipeline".

Methods become callable by the LLM. Docstrings = how LLM picks which to call.
"""
from __future__ import annotations

import json
from typing import Optional

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        base_url: str = Field(
            default="http://host.docker.internal:8000",
            description="FastAPI backend (FastAPI for Royal Pop pipeline).",
        )
        timeout_s: int = Field(
            default=1500,
            description="Max seconds for a stage to finish before kill.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── helpers ────────────────────────────────────────────────
    def _post(self, path: str, body: dict, timeout: Optional[int] = None) -> dict:
        try:
            r = requests.post(
                f"{self.valves.base_url}{path}",
                json=body,
                timeout=timeout or self.valves.timeout_s,
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            return {"status": "error", "error": str(e)}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            r = requests.get(
                f"{self.valves.base_url}{path}",
                params=params or {},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            return {"status": "error", "error": str(e)}

    def _summarise(self, result: dict, stage: str) -> str:
        if result.get("status") == "error":
            return f"FAIL {stage}: {result.get('error')}"
        if result.get("status") == "failed":
            return f"FAIL {stage} (rc={result.get('rc')}). Log tail:\n{result.get('log_tail', '')[-1500:]}"
        if result.get("status") == "timeout":
            return f"TIMEOUT {stage}: {result.get('log')}"
        return f"OK {stage} (rc={result.get('rc', 0)}). Log tail:\n{result.get('log_tail', '')[-1500:]}"

    # ── health / metadata ──────────────────────────────────────
    def health_check(self) -> str:
        """Check that the Royal Pop FastAPI backend, Ollama, and ComfyUI are reachable.
        Use this first if anything seems wrong, or at the start of a session.
        """
        r = self._get("/api/health")
        return f"ROYAL POP STACK STATUS:\n{json.dumps(r, indent=2)}"

    def list_outputs(self, date: Optional[str] = None) -> str:
        """List all artefacts produced today (or for a given YYYY-MM-DD date)."""
        r = self._get("/api/outputs", {"d": date} if date else None)
        return json.dumps(r, indent=2)

    # ── stage runs ─────────────────────────────────────────────
    def run_scout(self) -> str:
        """STAGE 1 — TREND SCOUT. Scrape Google Trends + DuckDuckGo for Royal Pop signals.
        Output: 3–5 urgency-ranked trends with real source URLs. Run this first each morning.
        """
        return self._summarise(
            self._post("/api/owui/run-stage", {"stage": "scout"}),
            "scout",
        )

    def run_strategist(self) -> str:
        """STAGE 2 — STRATEGIST. Take latest trend report, output 3–5 prioritised reel ideas
        with hook + platform + pillar + CTA. Requires Scout to have run today first.
        """
        return self._summarise(
            self._post("/api/owui/run-stage", {"stage": "strategist"}),
            "strategist",
        )

    def run_writer(self, pick: Optional[int] = None) -> str:
        """STAGE 3 — COPY WRITER. Turn each Strategist idea into a full script + B-roll +
        caption + hashtags. Pass `pick=N` to write only the idea with that priority (1–5).
        Requires Strategist to have run.
        """
        body: dict = {"stage": "writer"}
        if pick is not None:
            body["pick"] = pick
        return self._summarise(self._post("/api/owui/run-stage", body), "writer")

    def run_gate(self) -> str:
        """STAGE 4 — QUALITY GATE. Brand-safety review with auto-retry. Hard rules block
        (banned phrases, missing disclaimer, hashtag count). Soft brand-voice notes attached
        as advisory. Requires Writer to have run.
        """
        return self._summarise(
            self._post("/api/owui/run-stage", {"stage": "gate"}),
            "gate",
        )

    def run_visual(self, pick: Optional[int] = None) -> str:
        """STAGE 5 — VISUAL BRIEF. Per approved piece, generate shot list + FLUX/LTX-Video
        prompts + aspect ratio. Pass `pick=N` for one specific priority.
        Requires Gate to have run.
        """
        body: dict = {"stage": "visual"}
        if pick is not None:
            body["pick"] = pick
        return self._summarise(self._post("/api/owui/run-stage", body), "visual")

    def run_scheduler(self) -> str:
        """STAGE 6 — SCHEDULER. Time-slot the approved pieces per UK posting windows.
        Emit schedule.json + notion_payload.json + digest.md. No LLM call.
        Requires Visual to have run.
        """
        return self._summarise(
            self._post("/api/owui/run-stage", {"stage": "scheduler"}),
            "scheduler",
        )

    def run_image_render(
        self, pick: Optional[int] = None, shot: Optional[int] = None
    ) -> str:
        """STAGE 7 — IMAGE RENDER. Send FLUX.1 schnell prompts to ComfyUI :8188.
        Will first evict Ollama qwen2.5:14b from VRAM to free space.
        Pass `pick=N` and optionally `shot=M` for fine control. Auto-skips if ComfyUI down.
        """
        self.evict_ollama()
        body: dict = {"stage": "render_image"}
        if pick is not None:
            body["pick"] = pick
        if shot is not None:
            body["shot"] = shot
        return self._summarise(
            self._post("/api/owui/run-stage", body),
            "render_image",
        )

    def run_video_render(
        self, pick: Optional[int] = None, shot: Optional[int] = None
    ) -> str:
        """STAGE 8 — VIDEO RENDER. Send LTX-Video 2.3 prompts to ComfyUI :8188.
        Slow — minutes per clip. Will evict Ollama from VRAM first.
        Pass `pick=N` and optionally `shot=M`. Auto-skips if ComfyUI down.
        """
        self.evict_ollama()
        body: dict = {"stage": "render_video"}
        if pick is not None:
            body["pick"] = pick
        if shot is not None:
            body["shot"] = shot
        return self._summarise(
            self._post("/api/owui/run-stage", body),
            "render_video",
        )

    def run_pipeline(
        self, from_stage: Optional[str] = None, only_stage: Optional[str] = None
    ) -> str:
        """RUN ALL 8 STAGES end-to-end. Optional `from_stage` skips earlier ones
        (one of scout/strategist/writer/gate/visual/scheduler/render_image/render_video).
        Optional `only_stage` runs only that one. ~10–15 min full run.
        """
        body: dict = {"stage": "pipeline"}
        if from_stage:
            body["from_stage"] = from_stage
        if only_stage:
            body["only"] = only_stage
        return self._summarise(self._post("/api/owui/run-stage", body), "pipeline")

    # ── outputs / utilities ────────────────────────────────────
    def get_latest_output(self, stage: str) -> str:
        """Return the latest JSON output for a stage. Use stage names: scout, strategist,
        writer, gate, visual, scheduler, render_image, render_video.
        """
        r = self._get("/api/owui/latest", {"stage": stage})
        if r.get("status") != "ok":
            return f"no output yet for {stage}"
        return f"file: {r['file']}\n\n{json.dumps(r['data'], indent=2)[:6000]}"

    def evict_ollama(self) -> str:
        """Force Ollama to unload qwen2.5:14b. Use before any render stage (7 or 8)
        to free ~8GB VRAM for ComfyUI. Stage 7 and 8 call this automatically.
        """
        r = self._post("/api/owui/evict-ollama", {}, timeout=15)
        return f"evict: {json.dumps(r)}"
