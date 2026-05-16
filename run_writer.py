"""Copy Writer — turn brief's ideas into production-ready content.

One LLM call per idea (loop). Format-specific output schema per platform/format.
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
PROMPT_PATH = ROOT / "prompts" / "copy_writer.md"
OUT_DIR = ROOT / "outputs"

from tools.safety import hard_check  # noqa: E402

# per-platform character limits
LENGTH_LIMITS = {
    "TikTok": {"caption": 150},
    "Instagram Reel": {"caption_first_line": 125, "caption": 2200},
    "Instagram Feed": {"caption_first_line": 220, "caption": 2200},
    "Instagram Story": {"caption": 100, "question": 80},
    "Email": {"subject": 50, "preview_text": 90, "body": 5000},
}


def _flatten_text(piece: dict) -> str:
    parts: list[str] = []

    def walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, dict):
            for k, item in v.items():
                if not str(k).startswith("_"):
                    walk(item)

    walk(piece)
    return " ".join(parts)


def recent_captions(today: str, days: int = 3) -> list[str]:
    """Pull recommended captions from last N days of copy_*.json (new schema)."""
    files = sorted(OUT_DIR.glob("copy_*.json"), reverse=True)
    caps: list[str] = []
    for f in files:
        if today in f.name:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("pieces", []) if isinstance(data, dict) and "pieces" in data else (data if isinstance(data, list) else [data])
        for piece in items:
            if not isinstance(piece, dict):
                continue
            rec = piece.get("recommended_caption")
            opts = {o.get("label"): o.get("caption") for o in piece.get("caption_options") or [] if isinstance(o, dict)}
            cap = opts.get(rec) if rec in opts else None
            if cap and cap.strip():
                caps.append(cap.strip())
        if len(caps) >= days * 5:
            break
    return caps[: days * 5]


def check_lengths(piece: dict, director_brief: dict) -> list[str]:
    """Length checks for new Copy schema (hook_options + reel_script.beats + caption_options)."""
    issues: list[str] = []

    # Recommended hook ≤ 8 words for strong first-3-seconds
    rec_hook = (piece.get("recommended_hook") or "").strip()
    if rec_hook and len(rec_hook.split()) > 8:
        issues.append(f"recommended_hook {len(rec_hook.split())} words > 8 (hook should land fast)")

    # Each beat.line ≤ 15 words
    beats = (piece.get("reel_script") or {}).get("beats") or []
    for i, b in enumerate(beats):
        line = (b.get("line") or "").strip()
        if line and len(line.split()) > 15:
            issues.append(f"reel_script.beats[{i}].line {len(line.split())} words > 15")

    # Caption A/B length cap 2200 (IG hard limit)
    for opt in piece.get("caption_options") or []:
        cap = (opt.get("caption") or "").strip()
        if len(cap) > 2200:
            issues.append(f"caption_options[{opt.get('label')}] {len(cap)} chars > 2200 (IG limit)")
        first_line = cap.split("\n", 1)[0]
        if len(first_line) > 125:
            issues.append(
                f"caption_options[{opt.get('label')}] first line {len(first_line)} chars > 125 (above IG fold)"
            )

    return issues


def latest_brief(today: str) -> dict:
    candidate = OUT_DIR / f"content_brief_{today}.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    briefs = sorted(OUT_DIR.glob("content_brief_*.json"), reverse=True)
    if not briefs:
        raise FileNotFoundError("No content_brief_*.json. Run run_strategist.py first.")
    print(f"[writer] using fallback brief: {briefs[0].name}")
    return json.loads(briefs[0].read_text(encoding="utf-8"))


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    return s.strip()


def write_one(director_brief: dict, post_id: str, today: str) -> dict:
    """Generate one Copy piece from a Marketing Director brief."""
    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)

    user_prompt = f"""Today: {today}
post_id: {post_id}

MARKETING DIRECTOR BRIEF:
```json
{json.dumps(director_brief, indent=2, ensure_ascii=False)}
```

Convert the `primary_angle` + `brief_for_copy` into publishable words using your output schema:
- 3 distinct `hook_options`, pick `recommended_hook`
- `reel_script.beats[]` covering HOOK → BODY → PROOF → CTA, each line ≤ 15 words
- 2 `caption_options` (A and B), pick `recommended_caption` with reason in `handoff_note_for_qa`
- `cta` matching brief_for_copy.cta_language

Format target: `{(director_brief.get("content_decision") or {}).get("format", "REEL")}`.
Output VALID JSON only. No preamble.
"""

    from providers import llm_json
    piece = llm_json(
        agent_name="writer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        num_ctx=6144,
    )
    piece["post_id"] = piece.get("post_id") or post_id
    return piece


def validate_piece(piece: dict, director_brief: dict, anti_repeat: list[str] | None = None) -> dict:
    """New-schema validation. hard_check kept for banned-phrase scan across all text."""
    issues = hard_check(piece, director_brief)
    issues.extend(check_lengths(piece, director_brief))

    # caption anti-repeat on recommended_caption
    if anti_repeat:
        rec = piece.get("recommended_caption")
        opts = {o.get("label"): o.get("caption") for o in piece.get("caption_options") or [] if isinstance(o, dict)}
        cap = (opts.get(rec) or "").strip()
        if cap:
            for prior in anti_repeat:
                aw, bw = set(cap.lower().split()), set(prior.lower().split())
                if aw and bw and len(aw & bw) / len(aw | bw) >= 0.7:
                    issues.append("recommended_caption near-duplicate of recent post (overlap >=70%)")
                    break

    if issues:
        piece["_validation_notes"] = issues
        piece["_gate_status"] = "FAIL"
    else:
        piece["_gate_status"] = "PASS"
    return piece


def rewrite_one(director_brief: dict, prior: dict, revision_notes: list[str], today: str) -> dict:
    """Re-run writer with explicit revision notes."""
    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)

    user_prompt = f"""Today: {today}

MARKETING DIRECTOR BRIEF:
```json
{json.dumps(director_brief, indent=2, ensure_ascii=False)}
```

YOUR PRIOR OUTPUT (failed validation):
```json
{json.dumps({k: v for k, v in prior.items() if not k.startswith('_')}, indent=2, ensure_ascii=False)}
```

REVISION NOTES — fix EVERY one:
- {chr(10) + "- ".join(revision_notes)}

Return the FULL revised JSON only. Same schema. No preamble.
"""

    from providers import llm_json
    return llm_json(
        agent_name="writer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        num_ctx=6144,
        temperature=0.4,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="custom content_brief JSON")
    parser.add_argument("--output", type=Path, help="custom output path")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    if args.input:
        director_brief = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[writer] using custom input: {args.input}")
    else:
        director_brief = latest_brief(today)

    if director_brief.get("status") == "BLOCKED":
        print(f"[writer] Director brief BLOCKED: {director_brief.get('reason')}")
        return 1

    campaign_id = director_brief.get("campaign_id") or today
    fmt = (director_brief.get("content_decision") or {}).get("format", "REEL")
    post_id = f"{today}-{fmt}-{campaign_id[-6:]}"

    prior_caps = recent_captions(today)

    print(f"[writer] generating Copy for post_id={post_id} format={fmt}")
    piece = write_one(director_brief, post_id, today)
    piece["_meta"] = {
        "campaign_id": campaign_id,
        "format": fmt,
        "campaign_stage": director_brief.get("campaign_stage"),
        "primary_angle_title": (director_brief.get("primary_angle") or {}).get("title"),
    }
    piece = validate_piece(piece, director_brief, anti_repeat=prior_caps)

    output = {
        "date": today,
        "pieces": [piece],
    }

    OUT_DIR.mkdir(exist_ok=True)
    out_path = args.output or (OUT_DIR / f"copy_{today}.json")
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    status = piece.get("_gate_status") or "?"
    print("\n" + "=" * 60)
    print(f"COPY OUTPUT — {today} (status: {status})")
    print("=" * 60)
    print(json.dumps(piece, indent=2, ensure_ascii=False)[:4000])
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
