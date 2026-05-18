"""Callable tool wrappers for the Orchestrator agent.

Each wrapper:
  - Runs the underlying agent
  - Captures its output artifact (JSON on disk)
  - Returns: { ok, artifact, status_line, cost_usd, latency_s, artifact_path }

These let the Orchestrator call agents like functions instead of via subprocess.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "outputs"
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────
# Cost estimates per call (rough — updated from real spend data later)
# ─────────────────────────────────────────────────────────
COST_PER_CALL = {
    "scout":           0.005,
    "strategist":      0.008,
    "writer":          0.006,
    "gate":            0.004,
    "asset_director":  0.012,
    "visual":          0.008,
    "manual_reel":     0.000,
    "manual_post":     0.000,
    "output_director": 0.010,
    "reel_director":   0.015,
    "image_director":  0.015,
}


def _today() -> str:
    return date.today().isoformat()


def _read_latest(prefix: str) -> tuple[Path | None, dict | list | None]:
    today = _today()
    direct = OUT_DIR / f"{prefix}_{today}.json"
    if direct.exists():
        try:
            return direct, json.loads(direct.read_text(encoding="utf-8"))
        except Exception:
            pass
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    if files:
        try:
            return files[0], json.loads(files[0].read_text(encoding="utf-8"))
        except Exception:
            return files[0], None
    return None, None


def _extract_status_line(artifact: Any) -> str:
    if isinstance(artifact, dict):
        sl = artifact.get("_status_line")
        if isinstance(sl, str) and sl.strip():
            return sl.strip()
        # Try common nested locations
        if "pieces" in artifact and isinstance(artifact["pieces"], list) and artifact["pieces"]:
            return _extract_status_line(artifact["pieces"][0]) or f"{len(artifact['pieces'])} pieces written"
        if "plans" in artifact and isinstance(artifact["plans"], list) and artifact["plans"]:
            return _extract_status_line(artifact["plans"][0]) or f"{len(artifact['plans'])} plans"
        if "ideas" in artifact and isinstance(artifact["ideas"], list):
            return f"{len(artifact['ideas'])} ideas generated"
    return "(no status line emitted)"


def _wrap(agent_key: str, runner_fn, output_prefix: str) -> dict:
    t0 = time.time()
    try:
        rc = runner_fn()
    except Exception as e:
        return {
            "ok": False,
            "agent": agent_key,
            "error": f"{type(e).__name__}: {e}",
            "status_line": f"FAILED — {type(e).__name__}",
            "cost_usd": 0,
            "latency_s": round(time.time() - t0, 1),
            "artifact_path": None,
            "artifact": None,
        }
    latency = round(time.time() - t0, 1)
    path, artifact = _read_latest(output_prefix)
    status_line = _extract_status_line(artifact) if artifact else f"completed in {latency}s"
    return {
        "ok": rc == 0,
        "agent": agent_key,
        "rc": rc,
        "status_line": status_line,
        "cost_usd": COST_PER_CALL.get(agent_key, 0),
        "latency_s": latency,
        "artifact_path": str(path.relative_to(ROOT)) if path else None,
        "artifact": artifact,
    }


# ─────────────────────────────────────────────────────────
# Public tool API — each function the Orchestrator can call
# ─────────────────────────────────────────────────────────
def scout(focus: str | None = None) -> dict:
    """Run Trend Scout. `focus` is currently informational only (Scout reads config/keywords.yaml)."""
    from run_scout import main as runner
    return _wrap("scout", runner, "trend_report")


def director(angle_hint: str | None = None, format: str | None = None) -> dict:
    """Run Marketing Director. Hints currently informational — passed via env hook later."""
    from run_strategist import main as runner
    return _wrap("strategist", runner, "content_brief")


def copy() -> dict:
    """Run Copy. Reads latest content_brief."""
    from run_writer import main as runner
    return _wrap("writer", runner, "copy")


def qa() -> dict:
    """Run Quality Gate. Reads latest copy."""
    from run_gate import main as runner
    return _wrap("gate", runner, "approved_copy")


def asset_director() -> dict:
    """Run Asset Director. Reads latest approved_copy + content_brief."""
    from run_asset_director import main as runner
    return _wrap("asset_director", runner, "asset_plan")


def visual_brief() -> dict:
    """Run Visual Brief. Reads latest approved_copy + asset_plan."""
    from run_visual import main as runner
    return _wrap("visual", runner, "visual_brief")


def manual_reel() -> dict:
    """Refresh manual_reel_state. Deterministic — no LLM."""
    from run_manual_reel import main as runner
    return _wrap("manual_reel", runner, "manual_reel_state")


def manual_post() -> dict:
    """Refresh manual_post_state. Deterministic — no LLM."""
    from run_manual_post import main as runner
    return _wrap("manual_post", runner, "manual_post_state")


def output_director() -> dict:
    """Run Output Director — synthesises all upstream into operator brief."""
    from run_output_director import main as runner
    return _wrap("output_director", runner, "operator_console")


def session_summary() -> dict:
    """Run Session Summary — final chronicler. Promotes self_learning_seeds to cloud."""
    from run_session_summary import main as runner
    return _wrap("session_summary", runner, "session_summary")


def reel_director() -> dict:
    """Run Reel Director — generates 5 ranked reel ideas."""
    from run_reel_director import main as runner
    return _wrap("reel_director", runner, "reel_ideas")


def image_director(refs: list[str] | None = None) -> dict:
    """Run Image Director — generates 5 ranked image ideas. Optional operator refs."""
    # Inject refs via sys.argv before calling main (run_image_director uses argparse)
    saved_argv = sys.argv
    sys.argv = ["run_image_director.py"]
    if refs:
        sys.argv += ["--refs"] + list(refs)
    try:
        from run_image_director import main as runner
        return _wrap("image_director", runner, "image_ideas")
    finally:
        sys.argv = saved_argv


# ─────────────────────────────────────────────────────────
# Catalog (for Orchestrator prompt to introspect)
# ─────────────────────────────────────────────────────────
TOOL_CATALOG = {
    "scout":            {"args": [], "produces": "trend_report",       "cost": 0.005, "layer": "discovery"},
    "director":         {"args": [], "produces": "content_brief",      "cost": 0.008, "layer": "discovery"},
    "copy":             {"args": [], "produces": "copy",               "cost": 0.006, "layer": "discovery"},
    "qa":               {"args": [], "produces": "approved_copy",      "cost": 0.004, "layer": "discovery"},
    "asset_director":   {"args": [], "produces": "asset_plan",         "cost": 0.012, "layer": "production"},
    "visual_brief":     {"args": [], "produces": "visual_brief",       "cost": 0.008, "layer": "production"},
    "manual_reel":      {"args": [], "produces": "manual_reel_state",  "cost": 0.000, "layer": "manual"},
    "manual_post":      {"args": [], "produces": "manual_post_state",  "cost": 0.000, "layer": "manual"},
    "reel_director":    {"args": [], "produces": "reel_ideas",         "cost": 0.015, "layer": "creative_council"},
    "image_director":   {"args": ["refs"], "produces": "image_ideas",  "cost": 0.015, "layer": "creative_council"},
    "output_director":  {"args": [], "produces": "operator_console",   "cost": 0.010, "layer": "synthesis"},
    "session_summary":  {"args": [], "produces": "session_summary",    "cost": 0.012, "layer": "synthesis"},
}


def dispatch(tool_name: str, args: dict | None = None) -> dict:
    """Generic dispatcher used by Orchestrator. Returns same shape as direct calls."""
    args = args or {}
    fn_map = {
        "scout": scout,
        "director": director,
        "copy": copy,
        "qa": qa,
        "asset_director": asset_director,
        "visual_brief": visual_brief,
        "manual_reel": manual_reel,
        "manual_post": manual_post,
        "reel_director": reel_director,
        "image_director": image_director,
        "output_director": output_director,
        "session_summary": session_summary,
    }
    fn = fn_map.get(tool_name)
    if not fn:
        return {"ok": False, "agent": tool_name, "error": f"unknown tool '{tool_name}'", "status_line": "BLOCKED — unknown tool"}
    try:
        return fn(**{k: v for k, v in args.items() if v is not None})
    except TypeError as e:
        return {"ok": False, "agent": tool_name, "error": f"bad args: {e}", "status_line": "BLOCKED — bad args"}
