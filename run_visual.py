"""Visual Brief — per approved piece, generate shot list + AI image/video prompts.

Same pattern as Writer: loop one LLM call per piece. Output saved to
visual_brief_<date>.json with one entry per approved piece.
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


def brief_one(piece: dict, today: str) -> dict:
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    meta = piece.get("_meta", {})
    plat = meta.get("platform")
    fmt = meta.get("format")
    aspect = ASPECT_BY_PLATFORM_FORMAT.get((plat, fmt), "9:16")

    public_piece = {k: v for k, v in piece.items() if not k.startswith("_")}

    user_prompt = f"""Today: {today}

APPROVED CONTENT PIECE:
Platform: {plat}
Format: {fmt}
Required aspect ratio: {aspect}
Priority: {meta.get("priority")}
Pillar: {meta.get("pillar")}
Informed by trend: {meta.get("informed_by_trend")}

Content body:
```json
{json.dumps(public_piece, indent=2, ensure_ascii=False)}
```

Produce the visual brief. Use `aspect_ratio` = `{aspect}`. Make every AI prompt complete (no placeholders). Output VALID JSON only.
"""

    from providers import llm_call
    content = llm_call(
        agent_name="visual",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=True,
        num_ctx=6144,
    )
    brief = json.loads(_strip_fences(content))
    brief["_meta"] = meta
    return brief


def validate_brief(brief: dict) -> dict:
    notes: list[str] = []
    shot_ids = set()
    shots = brief.get("shot_list") or []
    prompts = brief.get("ai_prompts") or []

    if not isinstance(shots, list) or not shots:
        notes.append("shot_list empty")
    else:
        for i, s in enumerate(shots):
            sid = s.get("id")
            if sid in shot_ids:
                notes.append(f"shot[{i}] duplicate id {sid}")
            shot_ids.add(sid)
            if s.get("type") not in {"live_phone", "ai_image", "ai_video", "reuse_existing"}:
                notes.append(f"shot[{i}] invalid type {s.get('type')!r}")

    for i, p in enumerate(prompts):
        if p.get("for_shot_id") not in shot_ids:
            notes.append(f"ai_prompt[{i}] references unknown shot id {p.get('for_shot_id')}")
        neg = (p.get("negative_prompt") or "").lower()
        missing = [t for t in REQUIRED_NEGATIVE_TOKENS if t.lower() not in neg]
        if missing:
            notes.append(f"ai_prompt[{i}] negative_prompt missing tokens: {missing}")

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
    pieces = approved.get("pieces", [])
    if args.pick is not None:
        pieces = [p for p in pieces if p.get("_meta", {}).get("priority") == args.pick]
    if not pieces:
        print("[visual] no pieces to brief.")
        return 1

    import time as _time
    briefs: list[dict] = []
    first = True
    for piece in pieces:
        if piece.get("_gate_status") != "PASS":
            print(
                f"[visual] skipping P{piece.get('_meta', {}).get('priority')} — gate FAIL"
            )
            continue
        if not first:
            _time.sleep(8)
        first = False
        meta = piece.get("_meta", {})
        prio = meta.get("priority")
        print(f"[visual] briefing P{prio} {meta.get('platform')}/{meta.get('format')}...")
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
