"""Content Strategist — reads latest trend report, outputs daily content brief.

Same pattern as Scout: deterministic input + single LLM synthesis call.
"""
from __future__ import annotations

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
    """Return list of hooks from last N days of content_brief_*.json (excluding today)."""
    briefs = sorted(OUT_DIR.glob("content_brief_*.json"), reverse=True)
    hooks: list[str] = []
    for path in briefs:
        if today in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for idea in data.get("ideas", []):
                h = (idea.get("hook") or "").strip()
                if h:
                    hooks.append(h)
        except Exception:
            continue
        if len(hooks) >= days * 5:  # ~5 ideas per day
            break
    return hooks[: days * 5]


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
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    ctx = load_project_context()
    prior_hooks = recent_hooks(today)
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

    user_prompt = f"""Today is {today}. **Sprint day {day_n} of 7-14**. Calendar focus today: **{focus}**.

============ PROJECT CONTEXT ============

POSITIONING (01-strategy/positioning.md):
{ctx['positioning'][:1500]}

REEL HOOK BANK (03-content/reel-hooks.md — use these patterns, do NOT duplicate verbatim):
{ctx['reel_hooks'][:2500]}

7-DAY CALENDAR (03-content/7-day-calendar.md):
{ctx['calendar'][:1500]}

OBJECTION CATEGORIES (07-metrics/objection-log.md — address these in content where you can):
{ctx['objection_log'][:1000]}

WINNING HOOKS so far (03-content/winning-hooks.md — if any, use as gold standard):
{ctx['winning_hooks'][:1000]}

============ ANTI-REPEAT LIST ============
Hooks used in last {ANTI_REPEAT_DAYS} days — DO NOT repeat verbatim or near-verbatim:
{json.dumps(prior_hooks, indent=2) if prior_hooks else '(none yet — first run)'}

============ TODAY'S TREND REPORT ============
```json
{json.dumps(trend_report, indent=2)[:5000]}
```

============ TASK ============
Generate today's content brief — 3–5 ideas, ranked by priority. Each idea must be executable as-is (no placeholders in the hook).

PRIORITY RULES:
1. If any trend has urgency >= 8, priority 1 MUST be informed by that trend.
2. At least ONE idea must address the day's calendar focus: "{focus}".
3. At least ONE idea must address an objection from the categories above.
4. NEVER repeat a hook from the anti-repeat list.
5. Rotate pillars — never same pillar twice in one brief.

Output valid JSON only. Schema in system prompt.
"""

    from providers import llm_call
    content = llm_call(
        agent_name="strategist",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=True,
        num_ctx=8192,
    )
    return json.loads(_strip_fences(content))


VALID_COMBOS = {
    "TikTok": {"Video script"},
    "Instagram Reel": {"Video script"},
    "Instagram Feed": {"Carousel", "Caption"},
    "Instagram Story": {"Poll", "Caption"},
    "Email": {"Email copy"},
}
VALID_PILLARS = {"Problem", "Build journey", "Poll", "Premium", "Competitor"}
BRAGGY_PHRASES = [
    "ours are better",
    "ours is better",
    "we're the best",
    "we are the best",
    "still the best",
    "we beat",
    "our kit beats",
    "nobody else",
    "better than anyone",
    "we win",
]


def _hook_overlap(a: str, b: str) -> float:
    """Cheap Jaccard overlap on lowercase words."""
    aw = set(a.lower().split())
    bw = set(b.lower().split())
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)


def validate_brief(brief: dict, anti_repeat: list[str] | None = None) -> dict:
    ideas = brief.get("ideas", [])
    anti_repeat = anti_repeat or []
    cleaned: list[dict] = []
    notes: list[str] = []
    seen_pillars: set[str] = set()

    for i, idea in enumerate(ideas):
        plat = idea.get("platform")
        fmt = idea.get("format")
        pill = idea.get("pillar")
        hook = (idea.get("hook") or "").strip()

        if plat not in VALID_COMBOS:
            notes.append(f"idea[{i}]: platform '{plat}' invalid — dropped")
            continue
        if fmt not in VALID_COMBOS[plat]:
            notes.append(
                f"idea[{i}]: format '{fmt}' invalid for {plat} (allowed: {sorted(VALID_COMBOS[plat])}) — dropped"
            )
            continue
        if pill not in VALID_PILLARS:
            notes.append(f"idea[{i}]: pillar '{pill}' invalid")
            idea["pillar"] = "Problem"

        if not hook:
            notes.append(f"idea[{i}]: hook missing — dropped")
            continue
        word_count = len(hook.split())
        if word_count > 12:
            notes.append(f"idea[{i}]: hook {word_count} words (>12) — flagged for rewrite")

        hook_lc = hook.lower()
        for phrase in BRAGGY_PHRASES:
            if phrase in hook_lc:
                notes.append(f"idea[{i}]: braggy phrase '{phrase}' in hook — needs rewrite")
                idea["_needs_rewrite"] = True
                break

        # anti-repeat check vs last N days
        for prior in anti_repeat:
            overlap = _hook_overlap(hook, prior)
            if overlap >= 0.7:
                notes.append(
                    f"idea[{i}]: hook overlap {int(overlap*100)}% with recent '{prior[:60]}' — flag rewrite"
                )
                idea["_needs_rewrite"] = True
                break

        # pillar deduplication
        pill = idea.get("pillar")
        if pill in seen_pillars:
            notes.append(f"idea[{i}]: pillar '{pill}' duplicated within brief — flag rotation")
        seen_pillars.add(pill)

        cleaned.append(idea)

    cleaned.sort(key=lambda x: x.get("priority", 99))
    brief["ideas"] = cleaned[:5]
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
