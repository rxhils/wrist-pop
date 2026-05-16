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

def soft_check(piece: dict, director_brief: dict) -> dict:
    """LLM QA review against new schema. Returns full QA JSON (status/score/problems)."""
    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)

    user_prompt = f"""MARKETING DIRECTOR BRIEF:
```json
{json.dumps(director_brief, indent=2, ensure_ascii=False)}
```

COPY PIECE TO REVIEW:
```json
{json.dumps({k: v for k, v in piece.items() if not k.startswith('_')}, indent=2, ensure_ascii=False)}
```

Run all 6 QA gates. Return strict JSON per schema in your system prompt.
"""

    from providers import llm_json
    return llm_json(
        agent_name="gate",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        num_ctx=6144,
    )

def gate_one(piece: dict, director_brief: dict, today: str) -> dict:
    """Hard regex + auto-fix → LLM soft QA. BLOCK on hard fail after retries.

    LLM QA status PASS  → _gate_status PASS
    LLM QA status REVISE → write rewrite_one once, re-soft-check
    LLM QA status BLOCK  → _gate_status BLOCK, do not retry
    """
    current = {k: v for k, v in piece.items() if not k.startswith("_")}
    history: list[dict] = []
    auto_fix_log: list[str] = []

    for attempt in range(MAX_RETRIES + 1):
        # Deterministic hard regex
        hard_issues = hard_check(current, director_brief)
        if hard_issues:
            fixed, applied = auto_fix_piece(current)
            if applied:
                auto_fix_log.extend(applied)
                current = fixed
                hard_issues = hard_check(current, director_brief)

        attempt_log = {"attempt": attempt, "hard_issues": hard_issues, "auto_fix_applied": list(auto_fix_log)}

        if hard_issues and attempt >= MAX_RETRIES:
            attempt_log["final"] = "HARD_FAIL"
            history.append(attempt_log)
            break

        if hard_issues:
            print(f"[gate] hard FAIL attempt {attempt+1}: {len(hard_issues)} issue(s). Rewriting...")
            try:
                current = rewrite_one(director_brief, current, hard_issues, today)
            except Exception as e:
                attempt_log["rewrite_error"] = str(e)
                history.append(attempt_log)
                break
            history.append(attempt_log)
            continue

        # No hard issues → LLM soft QA
        tox_issues = toxicity_check(current) if detoxify_available() else []
        soft_result = soft_check(current, director_brief)
        soft_status = (soft_result.get("status") or "").upper()
        attempt_log["soft_status"] = soft_status
        attempt_log["soft_score"] = soft_result.get("score")
        attempt_log["soft_problems"] = soft_result.get("problems")
        history.append(attempt_log)

        if soft_status == "PASS":
            current["_meta"] = piece.get("_meta", {})
            current["_gate_status"] = "PASS"
            current["_gate_attempts"] = attempt + 1
            current["_gate_history"] = history
            current["_auto_fix_applied"] = auto_fix_log
            current["_qa_score"] = soft_result.get("score")
            current["_qa_approved_elements"] = soft_result.get("approved_elements")
            current["_qa_visual_instruction"] = soft_result.get("final_instruction_for_visual_brief")
            if tox_issues:
                current["_toxicity_advisory"] = tox_issues
            return current

        if soft_status == "BLOCK":
            current["_meta"] = piece.get("_meta", {})
            current["_gate_status"] = "BLOCK"
            current["_gate_attempts"] = attempt + 1
            current["_gate_history"] = history
            current["_qa_problems"] = soft_result.get("problems")
            return current

        # REVISE — try one rewrite
        if attempt >= MAX_RETRIES:
            break
        notes = [p.get("fix") for p in (soft_result.get("problems") or []) if p.get("fix")]
        if not notes:
            notes = [soft_result.get("final_instruction_for_copy_if_revision_needed") or "tighten brand voice"]
        try:
            current = rewrite_one(director_brief, current, notes, today)
        except Exception as e:
            history[-1]["rewrite_error"] = str(e)
            break

    current["_meta"] = piece.get("_meta", {})
    current["_gate_status"] = "BLOCK" if current.get("_gate_status") != "PASS" else "PASS"
    current.setdefault("_gate_attempts", len(history))
    current["_gate_history"] = history
    current["_auto_fix_applied"] = auto_fix_log
    return current

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
    pieces = copy_doc.get("pieces", []) if isinstance(copy_doc, dict) else (copy_doc if isinstance(copy_doc, list) else [])

    brief_path = args.brief or (OUT_DIR / f"content_brief_{today}.json")
    director_brief = (
        json.loads(brief_path.read_text(encoding="utf-8"))
        if brief_path.exists() else {}
    )

    import time as _time
    reviewed: list[dict] = []
    for i, piece in enumerate(pieces):
        if i > 0:
            _time.sleep(6)
        post_id = piece.get("post_id") or piece.get("_meta", {}).get("campaign_id") or f"piece-{i}"
        print(f"[gate] reviewing {post_id}...")
        reviewed.append(gate_one(piece, director_brief, today))

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
