"""Output Director — final operator console.

Reads ALL upstream outputs for today (trend_report, content_brief, copy,
approved_copy, visual_brief, manual_reel_state, manual_post_state), passes
them to an LLM that synthesises into one operator checklist.

Saves: outputs/operator_console_YYYY-MM-DD.json (strict schema) + .md (human read).
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
PROMPT_PATH = ROOT / "prompts" / "output_director.md"
OUT_DIR = ROOT / "outputs"

ARTIFACT_PREFIXES = [
    "trend_report",
    "content_brief",
    "copy",
    "approved_copy",
    "visual_brief",
    "manual_reel_state",
    "manual_post_state",
]

def _latest(prefix: str, today: str) -> Path | None:
    direct = OUT_DIR / f"{prefix}_{today}.json"
    if direct.exists():
        return direct
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    return files[0] if files else None

def _load_artifact(prefix: str, today: str) -> tuple[Path | None, dict | list | None]:
    p = _latest(prefix, today)
    if not p:
        return None, None
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return p, {"_parse_error": str(e)}

def _truncate(obj, limit: int = 3000) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} chars]"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    args, _ = parser.parse_known_args()
    today = args.date

    OUT_DIR.mkdir(exist_ok=True)

    artifacts: dict[str, dict] = {}
    missing: list[str] = []
    for prefix in ARTIFACT_PREFIXES:
        path, data = _load_artifact(prefix, today)
        if data is None:
            missing.append(prefix)
        else:
            artifacts[prefix] = {
                "path": str(path.relative_to(ROOT)) if path else None,
                "data": data,
            }

    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)

    user_blocks = [f"Today: {today}", ""]
    if missing:
        user_blocks.append(f"MISSING ARTIFACTS: {', '.join(missing)}")
        user_blocks.append("")

    for prefix, payload in artifacts.items():
        user_blocks.append(f"=== {prefix.upper()} ({payload['path']}) ===")
        user_blocks.append(_truncate(payload["data"]))
        user_blocks.append("")

    user_blocks.append(
        "Now synthesise into the strict JSON schema in your system prompt, "
        "followed by the human-readable section. Be specific. Use real post_ids. "
        "Mark genuinely missing items as blocked with required_fix."
    )
    user_prompt = "\n".join(user_blocks)

    from providers import call_llm, llm_json, extract_json
    console = None
    err_log = []
    try:
        console = llm_json(
            agent_name="output_director",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=12288,
        )
    except Exception as e:
        err_log.append(f"primary: {type(e).__name__}: {e}")
        print(f"[output_director] primary failed ({type(e).__name__}). Trying mistral-large fallback…")
        try:
            text = call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider="mistral",
                model="mistral-large-latest",
                json_mode=True,
                max_tokens=8192,
                temperature=0.3,
            )
            console = extract_json(text)
        except Exception as e2:
            err_log.append(f"mistral_fallback: {type(e2).__name__}: {e2}")
            print(f"[output_director] mistral fallback failed ({type(e2).__name__}). Trying openrouter gpt-oss-20b text-only…")
            try:
                text = call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    provider="openrouter",
                    model="openai/gpt-oss-20b:free",
                    json_mode=False,
                    max_tokens=8192,
                    temperature=0.3,
                )
                console = {"_text_only": text, "_errors": err_log}
            except Exception as e3:
                err_log.append(f"text_fallback: {type(e3).__name__}: {e3}")
                console = {"_text_only": "(all providers failed)", "_errors": err_log}

    # If model returned a list instead of object, wrap defensively.
    if isinstance(console, list):
        console = {"_raw_list": console, "_parse_warning": "model returned array, expected object"}

    json_path = OUT_DIR / f"operator_console_{today}.json"
    json_path.write_text(json.dumps(console, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {json_path}")

    # If LLM also returned human-readable markdown (after the JSON), it's already
    # inside `_text_only`. Otherwise compose a simple markdown summary from JSON.
    md_path = OUT_DIR / f"operator_console_{today}.md"
    if "_text_only" in console:
        md_path.write_text(console["_text_only"], encoding="utf-8")
    else:
        lines = [f"# Operator Console — {today}", ""]
        cs = console.get("campaign_status", {}) or {}
        lines.append(f"**Urgency:** {cs.get('urgency', '?')}")
        lines.append(f"**Focus:** {cs.get('current_focus', '?')}")
        lines.append(f"**Stage:** {cs.get('campaign_stage', '?')}")
        lines.append("")
        if console.get("today_priority"):
            lines.append(f"## Today priority\n{console['today_priority']}\n")
        if console.get("next_3_actions"):
            lines.append("## Next 3 actions")
            for i, a in enumerate(console["next_3_actions"], 1):
                lines.append(f"{i}. {a}")
            lines.append("")
        if console.get("blocked_items"):
            lines.append("## Blocked")
            for b in console["blocked_items"]:
                lines.append(f"- **{b.get('post_id', '?')}** ({b.get('stage', '?')}): {b.get('reason', '')} → _{b.get('required_fix', '')}_")
            lines.append("")
        if console.get("approved_deliverables"):
            lines.append("## Approved")
            for d in console["approved_deliverables"]:
                lines.append(f"- {d.get('post_id', '?')} [{d.get('series', '?')}] — {d.get('angle', '')}")
            lines.append("")
        if console.get("success_condition"):
            lines.append(f"## Success today\n{console['success_condition']}")
        md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved: {md_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
