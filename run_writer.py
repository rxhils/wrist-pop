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
    """Pull captions from last N days of copy_*.json for anti-repeat."""
    files = sorted(OUT_DIR.glob("copy_*.json"), reverse=True)
    caps: list[str] = []
    for f in files:
        if today in f.name:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for piece in data.get("pieces", []):
                cap = piece.get("caption")
                if isinstance(cap, str) and cap.strip():
                    caps.append(cap.strip())
        except Exception:
            continue
        if len(caps) >= days * 5:
            break
    return caps[: days * 5]


def check_lengths(piece: dict, idea: dict) -> list[str]:
    """Return list of length-limit violations for the piece."""
    issues: list[str] = []
    plat = idea.get("platform")
    limits = LENGTH_LIMITS.get(plat, {})
    if not limits:
        return issues

    cap = piece.get("caption")
    if isinstance(cap, str):
        if "caption_first_line" in limits:
            first_line = cap.split("\n", 1)[0]
            if len(first_line) > limits["caption_first_line"]:
                issues.append(
                    f"caption first line {len(first_line)} chars > {limits['caption_first_line']} for {plat}"
                )
        elif "caption" in limits and len(cap) > limits["caption"]:
            issues.append(f"caption {len(cap)} chars > {limits['caption']} for {plat}")

    if plat == "Email":
        subj = piece.get("subject", "")
        if len(subj) > limits["subject"]:
            issues.append(f"subject {len(subj)} chars > {limits['subject']}")
        prev = piece.get("preview_text", "")
        if len(prev) > limits["preview_text"]:
            issues.append(f"preview_text {len(prev)} chars > {limits['preview_text']}")

    if plat == "Instagram Story" and piece.get("question"):
        q = piece["question"]
        if len(q) > limits["question"]:
            issues.append(f"poll question {len(q)} chars > {limits['question']}")

    # video script body word count
    body = piece.get("body_3_25s")
    if isinstance(body, str):
        wc = len(body.split())
        if wc > 50:
            issues.append(f"video body {wc} words > 50 (too long to speak in 22s)")

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


def write_one(idea: dict, today: str) -> dict:
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    user_prompt = f"""Today: {today}

CONTENT IDEA (from Strategist):
```json
{json.dumps(idea, indent=2)}
```

Write the full content for this idea using the output schema that matches `format` = `{idea.get("format")}` on platform = `{idea.get("platform")}`.

Use the brief's `hook` as the opening line — you may tighten it but keep the core meaning. Use the brief's `cta`. Use the brief's `notes` for shot/visual direction.

Output VALID JSON only. No preamble. No markdown fences.
"""

    from providers import llm_call
    content = llm_call(
        agent_name="writer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=True,
        num_ctx=6144,
    )
    return json.loads(_strip_fences(content))


def validate_piece(piece: dict, idea: dict, anti_repeat: list[str] | None = None) -> dict:
    issues = hard_check(piece, idea)
    issues.extend(check_lengths(piece, idea))

    # caption anti-repeat
    if anti_repeat:
        cap = (piece.get("caption") or "").strip()
        if cap:
            for prior in anti_repeat:
                aw, bw = set(cap.lower().split()), set(prior.lower().split())
                if aw and bw and len(aw & bw) / len(aw | bw) >= 0.7:
                    issues.append(f"caption near-duplicate of recent post (overlap >=70%)")
                    break

    if issues:
        piece["_validation_notes"] = issues
        piece["_gate_status"] = "FAIL"
    else:
        piece["_gate_status"] = "PASS"
    return piece


def rewrite_one(idea: dict, prior: dict, revision_notes: list[str], today: str) -> dict:
    """Re-run writer with explicit revision notes from Quality Gate."""
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    user_prompt = f"""Today: {today}

CONTENT IDEA:
```json
{json.dumps(idea, indent=2)}
```

YOUR PRIOR OUTPUT (it failed Quality Gate):
```json
{json.dumps({k: v for k, v in prior.items() if not k.startswith('_')}, indent=2)}
```

REVISION NOTES — fix EVERY one of these in your new output:
- {chr(10) + "- ".join(revision_notes)}

Output the FULL revised JSON only. Same schema as before. No preamble. No markdown fences.
"""

    from providers import llm_call
    content = llm_call(
        agent_name="writer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=True,
        num_ctx=6144,
        temperature=0.4,  # tighter for rewrites
    )
    return json.loads(_strip_fences(content))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="custom content_brief JSON")
    parser.add_argument("--output", type=Path, help="custom output path")
    parser.add_argument("--pick", type=int, help="only write the idea with this priority")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    if args.input:
        brief = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[writer] using custom input: {args.input}")
    else:
        brief = latest_brief(today)
    ideas = brief.get("ideas", [])
    if args.pick is not None:
        ideas = [i for i in ideas if i.get("priority") == args.pick]
        print(f"[writer] filtered to priority {args.pick}: {len(ideas)} idea(s)")
    if not ideas:
        print("[writer] no ideas in brief — nothing to write.")
        return 1

    prior_caps = recent_captions(today)

    import time as _time
    pieces: list[dict] = []
    for i, idea in enumerate(ideas):
        if i > 0:
            _time.sleep(8)  # mistral free tier ~1 req/sec but tokens/min cap is the killer

        prio = idea.get("priority")
        fmt = idea.get("format")
        plat = idea.get("platform")
        print(f"[writer] priority {prio} — {plat} / {fmt}")
        piece = write_one(idea, today)
        piece["_meta"] = {
            "priority": prio,
            "platform": plat,
            "format": fmt,
            "pillar": idea.get("pillar"),
            "informed_by_trend": idea.get("informed_by_trend"),
        }
        piece = validate_piece(piece, idea, anti_repeat=prior_caps)
        pieces.append(piece)

    output = {
        "date": today,
        "pieces": pieces,
        "_summary": brief.get("summary"),
    }

    OUT_DIR.mkdir(exist_ok=True)
    out_path = args.output or (OUT_DIR / f"copy_{today}.json")
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    pass_count = sum(1 for p in pieces if p.get("_gate_status") == "PASS")
    fail_count = len(pieces) - pass_count

    print("\n" + "=" * 60)
    print(f"COPY OUTPUT — {today} ({pass_count} pass, {fail_count} flagged)")
    print("=" * 60)
    print(json.dumps(output, indent=2, ensure_ascii=False)[:6000])
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
