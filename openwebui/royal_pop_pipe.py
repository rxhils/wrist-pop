"""Royal Pop — Open-WebUI Pipe (Manifold).

Install:
  Open-WebUI → Workspace → Functions → "+" → paste this file → Save → enable.
  New chat → model dropdown → 9 entries appear under "Royal Pop":
    • Pipeline (auto)         ← runs all 8 stages
    • Scout only
    • Strategist only
    • Writer only
    • Gate only
    • Visual only
    • Scheduler only
    • Image Render only
    • Video Render only

The "Pipeline (auto)" entry routes every chat message through orchestrator
that hits the FastAPI backend. Per-stage entries map 1:1 to a stage.
"""
from __future__ import annotations

import json
from typing import Generator, Iterator, Union

import requests
from pydantic import BaseModel, Field


ORCHESTRATOR_SYSTEM = """# Royal Pop Studio Orchestrator

You bridge user chat commands to a local 8-stage Dockerized content pipeline
at http://host.docker.internal:8000.

## Pipeline stages
1. SCOUT          — pytrends + ddgs → trend report
2. STRATEGIST     — trends → reel ideas brief
3. WRITER         — ideas → full scripts + captions + hashtags
4. GATE           — brand-safety check + auto-retry
5. VISUAL BRIEF   — shot lists + FLUX / LTX-Video prompts
6. SCHEDULER      — time slots + Notion payload + markdown digest
7. IMAGE RENDER   — FLUX.1 schnell via ComfyUI
8. VIDEO RENDER   — LTX-Video 2.3 via ComfyUI

## Command protocol
- "Run pipeline" / "run all" → POST /api/owui/run-stage stage=pipeline
- "Scout" / "run scout" / "trend report" → stage=scout
- "Strategist" / "ideas" / "brief" → stage=strategist
- (etc. per stage)
- "Show latest X" → GET /api/owui/latest?stage=X

## Brand truths (enforce strictly)
- Independent brand. NO affiliation with Audemars Piguet or Swatch.
- Product = full Wrist Conversion Kit (adapter + silicone strap + pouch + tool).
- Tiers: Core £59 / Premium £79 / Collector £99.
- Phase = validation sprint. CTA always waitlist or comment. NEVER "Buy Now".
- No braggy phrasing ("we're best", "ours is better").

## VRAM rule
Before any render stage (7, 8): Ollama qwen2.5:14b must be evicted
(call /api/owui/evict-ollama). The render endpoints do this automatically.
"""


class Pipe:
    class Valves(BaseModel):
        base_url: str = Field(
            default="http://host.docker.internal:8000",
            description="Royal Pop FastAPI base URL.",
        )
        ollama_url: str = Field(
            default="http://host.docker.internal:11434",
            description="Ollama base URL (for orchestrator chat).",
        )
        orchestrator_model: str = Field(
            default="qwen2.5:14b",
            description="Ollama model used to interpret user messages in 'Pipeline (auto)' mode.",
        )

    STAGE_MAP = {
        "pipeline": "pipeline",
        "scout": "scout",
        "strategist": "strategist",
        "writer": "writer",
        "gate": "gate",
        "visual": "visual",
        "scheduler": "scheduler",
        "image_render": "render_image",
        "video_render": "render_video",
    }

    def __init__(self):
        self.type = "manifold"
        self.id = "royal_pop"
        self.name = "Royal Pop"
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        """Each entry shows up as a selectable model in the dropdown."""
        return [
            {"id": "pipeline", "name": "Pipeline (auto)"},
            {"id": "scout", "name": "Scout"},
            {"id": "strategist", "name": "Strategist"},
            {"id": "writer", "name": "Writer"},
            {"id": "gate", "name": "Gate"},
            {"id": "visual", "name": "Visual Brief"},
            {"id": "scheduler", "name": "Scheduler"},
            {"id": "image_render", "name": "Image Render"},
            {"id": "video_render", "name": "Video Render"},
        ]

    def _selected_model_id(self, body: dict) -> str:
        full = body.get("model", "")
        # body model id format: "<pipe-prefix>.<entry-id>"
        return full.split(".")[-1]

    def _last_user_msg(self, body: dict) -> str:
        msgs = body.get("messages", [])
        for m in reversed(msgs):
            if m.get("role") == "user":
                return m.get("content", "")
        return ""

    def _run_stage_sync(self, stage_key: str, user_msg: str) -> str:
        """Direct stage call — no orchestrator LLM in the middle."""
        body = {"stage": stage_key, "timeout_s": 1500}
        # quick "pick N" or "shot M" parse from user text
        for tok in user_msg.lower().split():
            if tok.startswith("pick=") and tok[5:].isdigit():
                body["pick"] = int(tok[5:])
            elif tok.startswith("shot=") and tok[5:].isdigit():
                body["shot"] = int(tok[5:])
            elif tok.startswith("from=") and tok[5:]:
                body["from_stage"] = tok[5:]
            elif tok.startswith("only=") and tok[5:]:
                body["only"] = tok[5:]
        try:
            r = requests.post(
                f"{self.valves.base_url}/api/owui/run-stage",
                json=body,
                timeout=body["timeout_s"],
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            return f"❌ FastAPI unreachable: {e}\n\nCheck: docker compose ps"

        status = data.get("status")
        rc = data.get("rc")
        log = data.get("log_tail", "")
        header = {
            "done": f"✓ {stage_key} OK (rc={rc})",
            "failed": f"✗ {stage_key} FAILED (rc={rc})",
            "timeout": f"⏱ {stage_key} timed out",
            "error": f"✗ {stage_key} error",
        }.get(status, f"{status} {stage_key}")
        return f"**{header}**\n\n```\n{log[-3000:]}\n```"

    def _orchestrator_chat(self, body: dict) -> str:
        """For 'Pipeline (auto)' — let the LLM decide which stages to call.

        Cheap approach: prepend orchestrator system prompt, pass to Ollama, return reply.
        Real tool-calling happens via the separate Tool (Royal Pop Content Pipeline) which
        the user should enable on this chat.
        """
        msgs = [{"role": "system", "content": ORCHESTRATOR_SYSTEM}]
        for m in body.get("messages", []):
            if m.get("role") in {"user", "assistant"}:
                msgs.append({"role": m["role"], "content": m.get("content", "")})
        payload = {
            "model": self.valves.orchestrator_model,
            "messages": msgs,
            "stream": False,
            "options": {"temperature": 0.3, "num_ctx": 8192},
            "keep_alive": "10m",
        }
        try:
            r = requests.post(
                f"{self.valves.ollama_url}/api/chat",
                json=payload,
                timeout=600,
            )
            r.raise_for_status()
            return r.json()["message"]["content"]
        except requests.RequestException as e:
            return f"orchestrator error: {e}"

    def pipe(
        self, body: dict, __user__: dict | None = None
    ) -> Union[str, Generator, Iterator]:
        sel = self._selected_model_id(body)
        user_msg = self._last_user_msg(body)

        if sel == "pipeline":
            # Auto mode → orchestrator chat (combine with Tool for actual stage calls)
            return self._orchestrator_chat(body)

        stage_key = self.STAGE_MAP.get(sel)
        if not stage_key:
            return f"unknown model: {sel}"
        if stage_key == "pipeline":
            return self._run_stage_sync("pipeline", user_msg)
        return self._run_stage_sync(stage_key, user_msg)
