"""Quality Gate — hard checks + LLM soft checks + auto-retry via Writer.

Pipeline:
  load copy_<date>.json
  for each piece:
    loop up to MAX_RETRIES:
      run hard_check (banned phrases, disclaimer, hashtags)
      run LLM soft check (tone, specificity, CTA realism)
      if PASS → break
      else → rewrite via Writer with combined revision notes
  emit approved_copy_<date>.json
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

from run_writer import rewrite_one  # noqa: E402
from tools.safety import (  # noqa: E402
    auto_fix_piece,
    detoxify_available,
    hard_check,
    toxicity_check,
)

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "quality_gate.md"
OUT_DIR = ROOT / "outputs"
MAX_RETRIES = 2


def latest_copy(today: str) -> dict:
    candidate = OUT_DIR / f"copy_{today}.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    files = sorted(OUT_DIR.glob("copy_*.json"), reverse=True)
    if not files:
        raise FileNotFoundError("No copy_*.json. Run run_writer.py first.")
    print(f"[gate] fallback: {files[0].name}")
    return json.loads(files[0].read_text(encoding="utf-8"))


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    return s.strip()


def soft_check(piece: dict, idea: dict) -> dict:
    """LLM tone/voice review. Returns {status, issues, revision_notes}."""
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    user_prompt = f"""ORIGINAL IDEA (from Strategist):
```json
{json.dumps(idea, indent=2)}
```

CONTENT PIECE TO REVIEW (from Copy Writer):
```json
{json.dumps({k: v for k, v in piece.items() if not k.startswith('_')}, indent=2, ensure_ascii=False)}
```

Review against the soft checks in your system prompt. Return JSON only.
"""

    from providers import llm_call
    content = llm_call(
        agent_name="gate",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=True,
        num_ctx=6144,
    )
    return json.loads(_strip_fences(content))


def gate_one(piece: dict, idea: dict, today: str) -> dict:
    """Hard checks BLOCK + retry. Soft check is ADVISORY — attached as notes only.

    Rationale: small local model can fix one soft issue but often creates a new
    one. Soft-blocking causes infinite loops. Hard rules (banned phrases,
    disclaimer, hashtag counts) are deterministic and stay blocking. Soft brand
    voice notes attached to the PASS piece so the founder can decide manually.
    """
    current = {k: v for k, v in piece.items() if not k.startswith("_")}
    history: list[dict] = []
    prio = piece.get("_meta", {}).get("priority")
    auto_fix_log: list[str] = []

    for attempt in range(MAX_RETRIES + 1):
        hard_issues = hard_check(current, idea)

        # Try auto-fix BEFORE retrying via LLM. Fast + deterministic.
        if hard_issues:
            fixed, applied = auto_fix_piece(current)
            if applied:
                auto_fix_log.extend(applied)
                current = fixed
                hard_issues = hard_check(current, idea)

        history.append({
            "attempt": attempt,
            "hard_issues": hard_issues,
            "auto_fix_applied": list(applied) if 'applied' in locals() else [],
        })

        if not hard_issues:
            tox_issues = toxicity_check(current) if detoxify_available() else []
            soft_result = soft_check(current, idea)
            current["_meta"] = piece.get("_meta", {})
            current["_gate_status"] = "PASS"
            current["_gate_attempts"] = attempt + 1
            current["_gate_history"] = history
            current["_auto_fix_applied"] = auto_fix_log
            if tox_issues:
                current["_toxicity_advisory"] = tox_issues
            if soft_result.get("status") == "FAIL":
                current["_advisory_notes"] = soft_result.get("issues", [])
                current["_advisory_revisions"] = soft_result.get("revision_notes", [])
            return current

        if attempt >= MAX_RETRIES:
            break

        print(
            f"[gate] P{prio} attempt {attempt + 1} hard FAIL "
            f"({len(hard_issues)} issues, {len(auto_fix_log)} auto-fixes so far). Rewriting..."
        )
        try:
            current = rewrite_one(idea, current, hard_issues, today)
        except Exception as e:
            history[-1]["rewrite_error"] = str(e)
            break

    current["_meta"] = piece.get("_meta", {})
    current["_gate_status"] = "FAIL"
    current["_gate_attempts"] = len(history)
    current["_gate_history"] = history
    current["_auto_fix_applied"] = auto_fix_log
    return current


def _idea_from_meta(piece: dict, brief: dict) -> dict:
    meta = piece.get("_meta", {})
    for idea in brief.get("ideas", []):
        if idea.get("priority") == meta.get("priority"):
            return idea
    return {
        "platform": meta.get("platform"),
        "format": meta.get("format"),
        "pillar": meta.get("pillar"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="custom copy_<date>.json")
    parser.add_argument("--brief", type=Path, help="custom content_brief_<date>.json")
    parser.add_argument("--output", type=Path, help="custom output path")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    if args.input:
        copy_doc = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[gate] using custom input: {args.input}")
    else:
        copy_doc = latest_copy(today)
    pieces = copy_doc.get("pieces", [])

    brief_path = args.brief or (OUT_DIR / f"content_brief_{today}.json")
    brief = (
        json.loads(brief_path.read_text(encoding="utf-8"))
        if brief_path.exists()
        else {"ideas": []}
    )

    import time as _time
    reviewed: list[dict] = []
    for i, piece in enumerate(pieces):
        if i > 0:
            _time.sleep(6)
        idea = _idea_from_meta(piece, brief)
        prio = piece.get("_meta", {}).get("priority")
        print(f"[gate] reviewing priority {prio} ({idea.get('format')})...")
        reviewed.append(gate_one(piece, idea, today))

    pass_count = sum(1 for p in reviewed if p.get("_gate_status") == "PASS")
    output = {
        "date": today,
        "pieces": reviewed,
        "_stats": {
            "total": len(reviewed),
            "passed": pass_count,
            "failed": len(reviewed) - pass_count,
        },
    }

    out_path = args.output or (OUT_DIR / f"approved_copy_{today}.json")
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"QUALITY GATE — {today}")
    print(f"Pass: {pass_count}/{len(reviewed)}")
    print("=" * 60)
    for p in reviewed:
        meta = p.get("_meta", {})
        print(
            f"  P{meta.get('priority')} {meta.get('platform')}/{meta.get('format')}: "
            f"{p.get('_gate_status')} (attempts: {p.get('_gate_attempts')})"
        )
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
