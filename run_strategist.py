"""Content Strategist — reads latest trend report, outputs daily content brief.

Same pattern as Scout: deterministic input + single LLM synthesis call.
"""

from __future__ import annotations
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "strategist.md"
OUT_DIR = ROOT / "outputs"

# Project context paths (Royal Pop 7-folder structure)
PROJECT_ROOT = ROOT.parent.parent
CONTEXT_FILES = {
    "reel_hooks": PROJECT_ROOT / "03-content" / "reel-hooks.md",
    "calendar": PROJECT_ROOT / "03-content" / "7-day-calendar.md",
    "winning_hooks": PROJECT_ROOT / "03-content" / "winning-hooks.md",
    "objection_log": PROJECT_ROOT / "07-metrics" / "objection-log.md",
    "positioning": PROJECT_ROOT / "01-strategy" / "positioning.md",
}

ANTI_REPEAT_DAYS = 3  # look at last N days of briefs to avoid repeating hooks

def load_project_context() -> dict[str, str]:
    """Load all .md context files. Missing files return empty string (graceful)."""
    out: dict[str, str] = {}
    for key, path in CONTEXT_FILES.items():
        try:
            out[key] = path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            out[key] = ""
    return out

def recent_hooks(today: str, days: int = ANTI_REPEAT_DAYS) -> list[str]:
    """Return list of recent hooks from copy_*.json files (recommended_hook)."""
    files = sorted(OUT_DIR.glob("copy_*.json"), reverse=True)
    hooks: list[str] = []
    for path in files:
        if today in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("pieces", []) if isinstance(data, dict) and "pieces" in data else (data if isinstance(data, list) else [data])
        for piece in items:
            if not isinstance(piece, dict):
                continue
            h = (piece.get("recommended_hook") or "").strip()
            if h:
                hooks.append(h)
            for opt in piece.get("hook_options") or []:
                if isinstance(opt, str) and opt.strip():
                    hooks.append(opt.strip())
        if len(hooks) >= days * 5:
            break
    return hooks[: days * 5]

def recent_archetypes(today: str, days: int = 2) -> list[str]:
    """Return recent hook_archetypes (last N days) — Director should rotate, not repeat."""
    files = sorted(OUT_DIR.glob("copy_*.json"), reverse=True)
    out: list[str] = []
    for path in files:
        if today in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("pieces", []) if isinstance(data, dict) and "pieces" in data else (data if isinstance(data, list) else [data])
        for piece in items:
            arch = (piece.get("hook_archetype") or "").upper().strip()
            if arch:
                out.append(arch)
        if len(out) >= days * 2:
            break
    return out[: days * 2]

def sprint_day(today: str, sprint_start: str = "2026-05-14") -> int:
    """Day N of the 7-14 day validation sprint (1-indexed)."""
    try:
        from datetime import date as _date
        t = _date.fromisoformat(today)
        s = _date.fromisoformat(sprint_start)
        return max(1, (t - s).days + 1)
    except Exception:
        return 1

def latest_trend_report(today: str) -> dict:
    candidate = OUT_DIR / f"trend_report_{today}.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    # fall back to most recent
    reports = sorted(OUT_DIR.glob("trend_report_*.json"), reverse=True)
    if not reports:
        raise FileNotFoundError(
            "No trend_report_*.json in outputs/. Run run_scout.py first."
        )
    print(f"[strategist] using fallback report: {reports[0].name}")
    return json.loads(reports[0].read_text(encoding="utf-8"))

def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    return s.strip()

def synthesize(trend_report: dict, today: str) -> dict:
    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)
    ctx = load_project_context()
    prior_hooks = recent_hooks(today)
    prior_archetypes = recent_archetypes(today, days=2)
    day_n = sprint_day(today)

    # Calendar-day cue (cycles within 7 days)
    day_focus_map = {
        1: "Problem and concept",
        2: "Waitlist announcement",
        3: "Product variations",
        4: "Polls and comments",
        5: "Competitor contrast",
        6: "Price and bundle tests",
        7: "Decision and urgency",
    }
    focus = day_focus_map.get(((day_n - 1) % 7) + 1, "Mixed")

    user_prompt = f"""Today is {today}. Sprint day {day_n}. Calendar focus today: **{focus}**.

============ PROJECT CONTEXT ============

POSITIONING:
{ctx['positioning'][:1500]}

REEL HOOK BANK (patterns — do NOT duplicate verbatim):
{ctx['reel_hooks'][:2000]}

7-DAY CALENDAR:
{ctx['calendar'][:1200]}

OBJECTION CATEGORIES (address where you can):
{ctx['objection_log'][:1000]}

============ ANTI-REPEAT LIST ============
Hooks used in last {ANTI_REPEAT_DAYS} days — your `primary_angle.title` must NOT verbatim-overlap any of these:
{json.dumps(prior_hooks, indent=2) if prior_hooks else '(none yet — first run)'}

Hook archetypes used in last 2 days — `content_decision.hook_direction` must NOT reuse these archetypes:
{json.dumps(prior_archetypes) if prior_archetypes else '(none)'}

============ TREND SCOUT OUTPUT ============
```json
{json.dumps(trend_report, indent=2)[:6000]}
```

============ TASK ============
Make ONE clear campaign decision today. Choose the lead angle + supporting angle + content_decision.

RULES:
1. If Scout `opportunity_recommendation.confidence` = HIGH, your `primary_angle` must adopt that angle.
2. If a trend cluster has audience_fit_score >= 8, lead with it.
3. If today's calendar focus ("{focus}") is misaligned with Scout's top signal, choose Scout — note conflict in `supporting_angle.purpose`.
4. NEVER repeat a hook from the anti-repeat list.
5. Set `campaign_stage` to WAITLIST (current product stage).

Output valid JSON only. Schema in system prompt.
"""

    from providers import llm_json
    return llm_json(
        agent_name="strategist",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        num_ctx=8192,
    )

VALID_FORMATS = {"REEL", "CAROUSEL", "STORY", "STATIC"}
VALID_STAGES = {"AWARENESS", "INTEREST", "WAITLIST", "LAUNCH"}
BRAGGY_PHRASES = [
    "ours are better", "ours is better", "we're the best", "we are the best",
    "still the best", "we beat", "our kit beats", "nobody else",
    "better than anyone", "we win",
]

def _hook_overlap(a: str, b: str) -> float:
    aw = set(a.lower().split())
    bw = set(b.lower().split())
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)

def validate_brief(brief: dict, anti_repeat: list[str] | None = None) -> dict:
    """Shape + anti-repeat check for Marketing Director schema.

    New schema: primary_angle, supporting_angle, content_decision, creative_direction,
    success_metric, brief_for_copy. No more `ideas[]`.
    """
    notes: list[str] = []
    anti_repeat = anti_repeat or []

    if brief.get("status") == "BLOCKED":
        return brief

    pa = brief.get("primary_angle") or {}
    title = (pa.get("title") or "").strip()
    if not title:
        notes.append("primary_angle.title missing — Director must restate")
    else:
        for phrase in BRAGGY_PHRASES:
            if phrase in title.lower():
                notes.append(f"primary_angle.title contains braggy phrase '{phrase}'")
                break
        for prior in anti_repeat:
            overlap = _hook_overlap(title, prior)
            if overlap >= 0.7:
                notes.append(
                    f"primary_angle.title overlap {int(overlap*100)}% with recent '{prior[:60]}'"
                )
                break

    cd = brief.get("content_decision") or {}
    fmt = (cd.get("format") or "").upper()
    if fmt and fmt not in VALID_FORMATS:
        notes.append(f"content_decision.format '{fmt}' invalid (allowed: {sorted(VALID_FORMATS)})")

    stage = (brief.get("campaign_stage") or "").upper()
    if stage and stage not in VALID_STAGES:
        notes.append(f"campaign_stage '{stage}' invalid (allowed: {sorted(VALID_STAGES)})")

    if notes:
        brief["_validation_notes"] = notes
    return brief

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="custom trend_report JSON")
    parser.add_argument("--output", type=Path, help="custom output path")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    if args.input:
        trend_report = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[strategist] using custom input: {args.input}")
    else:
        trend_report = latest_trend_report(today)
    brief = synthesize(trend_report, today)
    brief = validate_brief(brief, anti_repeat=recent_hooks(today))

    OUT_DIR.mkdir(exist_ok=True)
    out_path = args.output or (OUT_DIR / f"content_brief_{today}.json")
    out_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"CONTENT BRIEF — {today}")
    print("=" * 60)
    print(json.dumps(brief, indent=2))
    print(f"\nSaved: {out_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
