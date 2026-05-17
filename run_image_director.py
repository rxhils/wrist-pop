"""Image Director — generates 5 ready-to-render still image ideas.

Mirror of Reel Director. Reads all upstream artifacts + optional operator
references + cloud winners → outputs image_ideas_<date>.json with 5 ideas
ranked by viral_score.

CLI:
  python run_image_director.py
  python run_image_director.py --refs path1.png path2.png
  python run_image_director.py --date 2026-05-16
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
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "image_director.md"
OUT_DIR = ROOT / "outputs"

ARTIFACT_PREFIXES = [
    "trend_report",
    "content_brief",
    "copy",
    "approved_copy",
    "asset_plan",
    "visual_brief",
    "reel_ideas",
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
    except json.JSONDecodeError:
        return p, {"_parse_error": str(p)}


def _truncate(obj, limit: int = 2500) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} chars]"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--refs", nargs="*", default=[],
                        help="Optional operator reference image paths (relative to outputs/ or absolute)")
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

    blocks = [f"Today: {today}", ""]
    if args.refs:
        blocks.append(f"[OPERATOR REFERENCE IMAGES]")
        blocks.append(f"The operator uploaded {len(args.refs)} reference images for this session:")
        for r in args.refs:
            blocks.append(f"  - {r}")
        blocks.append("")
        blocks.append("Use these as compositional / stylistic anchors. Match composition, lighting, mood.")
        blocks.append("Populate `inspired_by_ref` field on any idea drawing from them.")
        blocks.append("")
    if missing:
        blocks.append(f"MISSING ARTIFACTS (work around): {', '.join(missing)}")
        blocks.append("")
    for prefix, payload in artifacts.items():
        blocks.append(f"=== {prefix.upper()} ({payload['path']}) ===")
        blocks.append(_truncate(payload["data"]))
        blocks.append("")
    blocks.append(
        "Generate EXACTLY 5 image render ideas per your output schema. Rank by viral_score desc. "
        "Each idea must use a DIFFERENT image_archetype AND platform_role. "
        "Pick operators_best_bet. Be specific. No placeholders. ≤ 400 chars per render_prompt."
    )
    user_prompt = "\n".join(blocks)

    from providers import call_llm, llm_json, extract_json
    deck = None
    err_log = []
    try:
        deck = llm_json(
            agent_name="image_director",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=12288,
        )
    except Exception as e:
        err_log.append(f"primary: {type(e).__name__}: {e}")
        print(f"[image_director] primary failed ({type(e).__name__}). Trying mistral-large fallback…")
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
            deck = {"_errors": err_log, "status": "BLOCKED", "reason": "all providers failed", "_status_line": "BLOCKED — all providers failed"}

    if isinstance(deck, list):
        deck = {"ideas": deck}

    json_path = OUT_DIR / f"image_ideas_{today}.json"
    json_path.write_text(json.dumps(deck, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {json_path}")

    # Markdown summary
    md = [f"# Image Director — {today}", ""]
    bb = deck.get("operators_best_bet") if isinstance(deck, dict) else None
    if bb:
        md.append(f"**Operator's best bet:** {bb}")
        md.append("")
    for idea in (deck.get("ideas") or []) if isinstance(deck, dict) else []:
        md.append(f"## {idea.get('idea_id', '?')} — {idea.get('image_archetype', '?')}")
        md.append(
            f"_archetype: {idea.get('image_archetype')} · "
            f"platform: {idea.get('platform_role')} · "
            f"emotion: {idea.get('emotional_logic')} · "
            f"viral {idea.get('viral_score')}/10 · "
            f"effort {idea.get('production_effort_score')}/5_"
        )
        md.append("")
        md.append(f"**Prompt:** `{idea.get('render_prompt', '')}`")
        md.append("")
        md.append(f"_Model:_ {idea.get('preferred_model')} · _Aspect:_ {idea.get('aspect_ratio')} · _Colourway:_ {idea.get('colourway')}")
        if idea.get("viral_reasoning"):
            md.append(f"_Why viral:_ {idea['viral_reasoning']}")
        if idea.get("inspired_by_ref"):
            md.append(f"_Ref:_ ★ {idea['inspired_by_ref']}")
        md.append("")
    (OUT_DIR / f"image_ideas_{today}.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Saved: {OUT_DIR / f'image_ideas_{today}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
