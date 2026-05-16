"""Visual Brief — per approved piece, generate shot list + AI image/video prompts.

Same pattern as Writer: loop one LLM call per piece. Output saved to
visual_brief_<date>.json with one entry per approved piece.
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
PROMPT_PATH = ROOT / "prompts" / "visual_brief.md"
OUT_DIR = ROOT / "outputs"

ASPECT_BY_PLATFORM_FORMAT = {
    ("TikTok", "Video script"): "9:16",
    ("Instagram Reel", "Video script"): "9:16",
    ("Instagram Feed", "Carousel"): "1:1",
    ("Instagram Feed", "Caption"): "4:5",
    ("Instagram Story", "Poll"): "9:16",
    ("Instagram Story", "Caption"): "9:16",
    ("Email", "Email copy"): "16:9",
}

REQUIRED_NEGATIVE_TOKENS = [
    "text overlay",
    "watermark",
    "low quality",
    "AP logo",
    "Swatch logo",
]

def latest_approved(today: str) -> dict:
    candidate = OUT_DIR / f"approved_copy_{today}.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    files = sorted(OUT_DIR.glob("approved_copy_*.json"), reverse=True)
    if not files:
        raise FileNotFoundError("No approved_copy_*.json. Run run_gate.py first.")
    print(f"[visual] fallback: {files[0].name}")
    return json.loads(files[0].read_text(encoding="utf-8"))

def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    return s.strip()

def _load_asset_plan_for(post_id: str, today: str) -> dict | None:
    """Look up Asset Director plan for this post_id (if Asset Director ran upstream)."""
    direct = OUT_DIR / f"asset_plan_{today}.json"
    candidates = [direct] if direct.exists() else sorted(OUT_DIR.glob("asset_plan_*.json"), reverse=True)[:1]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for plan in data.get("plans") or []:
            if plan.get("post_id") == post_id and plan.get("status") == "OK":
                return plan
    return None

def brief_one(piece: dict, today: str) -> dict:
    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)
    meta = piece.get("_meta", {})
    fmt = (meta.get("format") or "REEL").upper()
    aspect = {"REEL": "9:16", "STORY": "9:16", "CAROUSEL": "1:1", "STATIC": "4:5"}.get(fmt, "9:16")

    public_piece = {k: v for k, v in piece.items() if not k.startswith("_")}
    post_id = piece.get("post_id") or "unknown"

    asset_plan = _load_asset_plan_for(post_id, today)
    asset_block = ""
    if asset_plan:
        asset_block = (
            "ASSET PLAN (from Asset Director — translate this into shot_list + edit_plan, "
            "DO NOT invent extra assets):\n```json\n"
            + json.dumps({
                "image_blueprint": asset_plan.get("image_blueprint"),
                "motion_blueprint": asset_plan.get("motion_blueprint"),
                "build_order": asset_plan.get("build_order"),
                "minimum_viable_asset_set": asset_plan.get("minimum_viable_asset_set"),
                "handoff_for_visual_brief": asset_plan.get("handoff_for_visual_brief"),
            }, indent=2, ensure_ascii=False)[:4000]
            + "\n```\n\n"
        )

    user_prompt = f"""Today: {today}
post_id: {post_id}
Format: {fmt}
Required aspect ratio: {aspect}

{asset_block}APPROVED COPY (from QA):
```json
{json.dumps(public_piece, indent=2, ensure_ascii=False)}
```

QA visual instruction: {piece.get('_qa_visual_instruction') or '(none)'}

Produce the visual brief using your output schema. shot_list must have 3–8 shots. edit_plan steps must align with reel_script.beats. {('Reuse asset IDs from the Asset Plan above where applicable (set must_capture to reference image_id/video_id). ' if asset_plan else '')}Output VALID JSON only.
"""

    from providers import llm_json
    brief = llm_json(
        agent_name="visual",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        num_ctx=6144,
    )
    brief["_meta"] = meta
    brief["post_id"] = brief.get("post_id") or post_id
    return brief

def validate_brief(brief: dict) -> dict:
    """New schema: visual_direction + shot_list + edit_plan."""
    notes: list[str] = []
    shots = brief.get("shot_list") or []
    edits = brief.get("edit_plan") or []
    vd = brief.get("visual_direction") or {}

    valid_styles = {"HERO", "TECHNICAL BOARD", "COLOURWAY CARD", "WRIST SHOT", "DETAIL MACRO"}
    style = (vd.get("style") or "").upper()
    if style and style not in valid_styles:
        notes.append(f"visual_direction.style '{style}' not in {sorted(valid_styles)}")

    if not isinstance(shots, list) or not shots:
        notes.append("shot_list empty")
    elif len(shots) < 3 or len(shots) > 8:
        notes.append(f"shot_list has {len(shots)} shots — should be 3–8 for video formats")
    else:
        shot_nos = set()
        valid_shot_types = {"CLOSE", "MEDIUM", "WIDE", "MACRO", "OVER-SHOULDER"}
        for i, s in enumerate(shots):
            n = s.get("shot_no")
            if n in shot_nos:
                notes.append(f"shot[{i}] duplicate shot_no {n}")
            shot_nos.add(n)
            st = (s.get("shot_type") or "").upper()
            if st and st not in valid_shot_types:
                notes.append(f"shot[{i}].shot_type '{st}' not in {sorted(valid_shot_types)}")

    if isinstance(edits, list) and edits:
        valid_trans = {"CUT", "DISSOLVE", "WHIP", "NONE"}
        for i, e in enumerate(edits):
            tr = (e.get("transition") or "").upper()
            if tr and tr not in valid_trans:
                notes.append(f"edit_plan[{i}].transition '{tr}' not in {sorted(valid_trans)}")

    if notes:
        brief["_validation_notes"] = notes
        brief["_visual_status"] = "FLAG"
    else:
        brief["_visual_status"] = "PASS"
    return brief

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="custom approved_copy JSON")
    parser.add_argument("--output", type=Path, help="custom output path")
    parser.add_argument("--pick", type=int, help="only brief piece with this priority")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    if args.input:
        approved = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[visual] using custom input: {args.input}")
    else:
        approved = latest_approved(today)
    pieces = approved.get("pieces", []) if isinstance(approved, dict) else (approved if isinstance(approved, list) else [])
    if not pieces:
        print("[visual] no pieces to brief.")
        return 1

    import time as _time
    briefs: list[dict] = []
    first = True
    for piece in pieces:
        if piece.get("_gate_status") != "PASS":
            print(f"[visual] skipping {piece.get('post_id')} — gate {piece.get('_gate_status')}")
            continue
        if not first:
            _time.sleep(8)
        first = False
        post_id = piece.get("post_id") or "unknown"
        print(f"[visual] briefing {post_id}...")
        b = brief_one(piece, today)
        b = validate_brief(b)
        briefs.append(b)

    output = {
        "date": today,
        "briefs": briefs,
        "_stats": {
            "total": len(briefs),
            "pass": sum(1 for b in briefs if b.get("_visual_status") == "PASS"),
            "flag": sum(1 for b in briefs if b.get("_visual_status") == "FLAG"),
        },
    }

    out_path = args.output or (OUT_DIR / f"visual_brief_{today}.json")
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"VISUAL BRIEF — {today}")
    print(f"Pass: {output['_stats']['pass']}  Flag: {output['_stats']['flag']}")
    print("=" * 60)
    for b in briefs:
        m = b.get("_meta", {})
        print(
            f"  P{m.get('priority')} {m.get('platform')}/{m.get('format')} "
            f"({b.get('aspect_ratio')}): {len(b.get('shot_list') or [])} shots, "
            f"{len(b.get('ai_prompts') or [])} ai prompts — {b.get('_visual_status')}"
        )
    print(f"\nSaved: {out_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
