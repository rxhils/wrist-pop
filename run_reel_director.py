"""Reel Director — generates 5 ready-to-shoot Reel/TikTok ideas.

Reads ALL upstream pipeline artifacts for today (plus optional historical
winners block from cloud_store) and outputs reel_ideas_<date>.json
+ reel_ideas_<date>.md (operator brief).

Sits SEPARATE from main marketing chain. Triggered manually after pipeline
completes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "reel_director.md"
OUT_DIR = ROOT / "outputs"

ARTIFACT_PREFIXES = [
    "trend_report",
    "content_brief",
    "copy",
    "approved_copy",
    "visual_brief",
    "manual_reel_state",
    "manual_post_state",
    "operator_console",
]


def _latest(prefix: str, today: str) -> Path | None:
    direct = OUT_DIR / f"{prefix}_{today}.json"
    if direct.exists():
        return direct
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    return files[0] if files else None


def _load_artifact(prefix: str, today: str):
    p = _latest(prefix, today)
    if not p:
        return None, None
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return p, {"_parse_error": str(e)}


def _truncate(obj, limit: int = 2500) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} chars]"


def _load_historical_winners() -> str:
    """Optional self-learning hook. Returns markdown block of past winners
    if cloud_store is wired, else empty string."""
    try:
        from cloud_store import recent_winners
        winners = recent_winners(limit=10, days=30)
        if not winners:
            return ""
        lines = ["[KNOWN WINNERS LAST 30 DAYS]"]
        for w in winners:
            lines.append(f"- hook: {w.get('hook_text')!r}  engagement={w.get('engagement_score')}  format={w.get('format')}")
        return "\n".join(lines) + "\n"
    except ImportError:
        return ""  # cloud not configured yet
    except Exception as e:
        print(f"[reel_director] cloud lookup failed (continuing without): {e}")
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    args, _ = parser.parse_known_args()
    today = args.date

    OUT_DIR.mkdir(exist_ok=True)

    artifacts = {}
    missing = []
    for prefix in ARTIFACT_PREFIXES:
        path, data = _load_artifact(prefix, today)
        if data is None:
            missing.append(prefix)
        else:
            artifacts[prefix] = {"path": str(path.relative_to(ROOT)) if path else None, "data": data}

    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)

    winners_block = _load_historical_winners()

    blocks = [f"Today: {today}", ""]
    if winners_block:
        blocks.append(winners_block)
        blocks.append("")
    if missing:
        blocks.append(f"MISSING ARTIFACTS (work around them): {', '.join(missing)}")
        blocks.append("")
    for prefix, payload in artifacts.items():
        blocks.append(f"=== {prefix.upper()} ({payload['path']}) ===")
        blocks.append(_truncate(payload["data"]))
        blocks.append("")
    blocks.append(
        "Generate EXACTLY 5 reel ideas per your output schema. Rank by expected_engagement_score "
        "descending. Pick operators_best_bet. Be specific. No placeholders."
    )
    user_prompt = "\n".join(blocks)

    from providers import call_llm, llm_json, extract_json
    deck = None
    err_log = []
    try:
        deck = llm_json(
            agent_name="reel_director",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=12288,
        )
    except Exception as e:
        err_log.append(f"primary: {type(e).__name__}: {e}")
        print(f"[reel_director] primary failed ({type(e).__name__}). Trying mistral-large fallback…")
        try:
            text = call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider="mistral",
                model="mistral-large-latest",
                json_mode=True,
                max_tokens=12288,
                temperature=0.5,
            )
            deck = extract_json(text)
        except Exception as e2:
            err_log.append(f"mistral: {type(e2).__name__}: {e2}")
            print(f"[reel_director] mistral fallback failed ({type(e2).__name__}). Trying openrouter gpt-oss-20b text-only…")
            try:
                text = call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    provider="openrouter",
                    model="openai/gpt-oss-20b:free",
                    json_mode=False,
                    max_tokens=8192,
                    temperature=0.5,
                )
                deck = {"_text_only": text, "_errors": err_log}
            except Exception as e3:
                err_log.append(f"text_fallback: {type(e3).__name__}: {e3}")
                deck = {"_errors": err_log, "status": "BLOCKED", "reason": "all providers failed"}

    if isinstance(deck, list):
        # If model returned bare array, wrap
        deck = {"ideas": deck}

    json_path = OUT_DIR / f"reel_ideas_{today}.json"
    json_path.write_text(json.dumps(deck, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {json_path}")

    # Human-readable markdown
    md_lines = [f"# Reel Director — {today}", ""]
    bb = deck.get("operators_best_bet") if isinstance(deck, dict) else None
    if bb:
        md_lines.append(f"**Operator's best bet:** {bb}")
        md_lines.append("")
    for idea in (deck.get("ideas") or []) if isinstance(deck, dict) else []:
        md_lines.append(f"## {idea.get('idea_id', '?')} — {idea.get('hook', '')}")
        md_lines.append(
            f"_archetype: {idea.get('hook_archetype')} · "
            f"opening: {idea.get('opening_shot_type')} · "
            f"effort {idea.get('production_effort_score')}/5 · "
            f"engagement {idea.get('expected_engagement_score')}/10_"
        )
        md_lines.append("")
        md_lines.append("### Beats")
        for b in idea.get("beats") or []:
            md_lines.append(f"- **{b.get('time')}** [{b.get('purpose')}] {b.get('shot')} — _{b.get('voiceover_or_caption_line')}_")
            if b.get("on_screen_text"):
                md_lines.append(f"   OST: \"{b['on_screen_text']}\"")
        md_lines.append("")
        for c in idea.get("caption_options") or []:
            tag = "★ " if c.get("label") == idea.get("recommended_caption") else ""
            md_lines.append(f"**Caption {tag}{c.get('label')}:** {c.get('caption')}")
        md_lines.append("")
        if idea.get("why_this_wins"):
            md_lines.append(f"_Why wins:_ {idea['why_this_wins']}")
        md_lines.append("")
    md_path = OUT_DIR / f"reel_ideas_{today}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Saved: {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
