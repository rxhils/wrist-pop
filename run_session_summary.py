"""Session Summary — final pipeline agent.

Reads EVERY artifact today's pipeline produced, writes one comprehensive
narrative + structured digest. Operator reads this once per day instead of
opening 10 separate artifacts.

Also: promotes any `self_learning_seeds[].promote_to_cloud=true` items to
the Supabase `winners` table so tomorrow's agents see them via the
[SELF-LEARNING — KNOWN WINNERS] block.
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
PROMPT_PATH = ROOT / "prompts" / "session_summary.md"
OUT_DIR = ROOT / "outputs"

# All artifact prefixes session summary considers
ARTIFACT_PREFIXES = [
    "trend_report",
    "content_brief",
    "copy",
    "approved_copy",
    "asset_plan",
    "visual_brief",
    "manual_reel_state",
    "manual_post_state",
    "reel_ideas",
    "image_ideas",
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
        return p, {"_parse_error": True}


def _truncate(obj, limit: int = 1500) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"\n... [+{len(s) - limit} chars]"


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

    if not artifacts:
        print("[session_summary] BLOCKED — no artifacts to summarise. Run pipeline first.")
        return 1

    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)

    blocks = [f"Today: {today}", ""]
    if missing:
        blocks.append(f"MISSING ARTIFACTS (note in contradictions_or_gaps): {', '.join(missing)}")
        blocks.append("")
    for prefix, payload in artifacts.items():
        blocks.append(f"=== {prefix.upper()} ({payload['path']}) ===")
        blocks.append(_truncate(payload["data"]))
        blocks.append("")
    blocks.append(
        "Produce the strict-JSON output per your schema, followed by the markdown SESSION NARRATIVE block. "
        "Cite specific upstream artifacts in key_decisions. Promote winning hooks/archetypes via self_learning_seeds."
    )
    user_prompt = "\n".join(blocks)

    from providers import call_llm, llm_json, extract_json
    summary = None
    raw_text = None
    err_log = []

    try:
        # Try JSON-mode first
        summary = llm_json(
            agent_name="session_summary",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=16384,
        )
    except Exception as e:
        err_log.append(f"primary_json: {type(e).__name__}: {e}")
        print(f"[session_summary] JSON mode failed ({type(e).__name__}). Trying text mode for narrative…")
        try:
            raw_text = call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider="mistral",
                model="mistral-large-latest",
                json_mode=False,
                max_tokens=8192,
                temperature=0.4,
            )
            # Try to extract JSON head from text
            try:
                summary = extract_json(raw_text)
            except Exception:
                summary = {"_text_only": raw_text, "_errors": err_log}
        except Exception as e2:
            err_log.append(f"text_fallback: {type(e2).__name__}: {e2}")
            summary = {"_errors": err_log, "_status_line": "BLOCKED — all providers failed"}

    json_path = OUT_DIR / f"session_summary_{today}.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {json_path}")

    # Markdown narrative
    md_lines = [
        f"# Pop Wrist Studio — Session Summary",
        f"_Date: {today}_",
        "",
    ]
    if isinstance(summary, dict):
        sl = summary.get("_status_line")
        if sl:
            md_lines.append(f"**{sl}**")
            md_lines.append("")
        if summary.get("campaign_thread"):
            md_lines.append(f"### Campaign Thread")
            md_lines.append(f"_{summary['campaign_thread']}_")
            md_lines.append("")
        ph = summary.get("pipeline_health") or {}
        if ph:
            md_lines.append("### Pipeline Health")
            md_lines.append(f"- agents run: **{ph.get('agents_run', 0)}**")
            md_lines.append(f"- passes first attempt: **{ph.get('passes_first_attempt', 0)}**")
            md_lines.append(f"- retries: {ph.get('retries', 0)}")
            md_lines.append(f"- parse failures: {ph.get('parse_failures', 0)}")
            md_lines.append(f"- blocked items: {ph.get('blocked_items', 0)}")
            md_lines.append(f"- cost: **${ph.get('total_cost_usd_estimate', 0):.3f}**")
            md_lines.append(f"- duration: ~{ph.get('duration_estimate_minutes', 0)} min")
            md_lines.append("")
        wa = summary.get("winning_assets") or {}
        if wa.get("top_reel"):
            tr = wa["top_reel"]
            md_lines.append("### Top Reel")
            md_lines.append(f"- **{tr.get('idea_id')}** · viral {tr.get('viral_score')}/10")
            md_lines.append(f'  > "{tr.get("hook", "")}"')
            md_lines.append(f"  - {tr.get('why_picked', '')}")
            md_lines.append("")
        if wa.get("top_image"):
            ti = wa["top_image"]
            md_lines.append("### Top Image")
            md_lines.append(f"- **{ti.get('idea_id')}** · {ti.get('archetype')} · viral {ti.get('viral_score')}/10")
            md_lines.append(f"  - {ti.get('why_picked', '')}")
            md_lines.append("")
        if wa.get("hero_action_from_console"):
            md_lines.append("### Hero Action (from Console)")
            md_lines.append(f"**{wa['hero_action_from_console']}**")
            md_lines.append("")
        kd = summary.get("key_decisions") or []
        if kd:
            md_lines.append("### Key Decisions")
            for d in kd:
                md_lines.append(f"- **{d.get('stage')}** — {d.get('decision')} _(evidence: {d.get('evidence', '—')})_")
            md_lines.append("")
        gaps = summary.get("contradictions_or_gaps") or []
        if gaps:
            md_lines.append("### Contradictions / Gaps")
            for g in gaps:
                md_lines.append(f"- ⚠ **{g.get('type')}**: {g.get('details')} (stages: {g.get('stage_a')} ↔ {g.get('stage_b')})")
            md_lines.append("")
        n3 = summary.get("operator_next_3") or []
        if n3:
            md_lines.append("### Operator — Next 3 Actions")
            for i, a in enumerate(n3, 1):
                md_lines.append(f"{i}. {a}")
            md_lines.append("")
        if summary.get("what_to_do_tomorrow"):
            md_lines.append("### Tomorrow's Focus")
            md_lines.append(summary["what_to_do_tomorrow"])
            md_lines.append("")

    md_path = OUT_DIR / f"session_summary_{today}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Saved: {md_path}")

    # Self-learning: push promoted seeds to Supabase winners table
    seeds = (summary.get("self_learning_seeds") or []) if isinstance(summary, dict) else []
    promoted = 0
    for seed in seeds:
        if not seed.get("promote_to_cloud"):
            continue
        try:
            import cloud_store
            if not cloud_store.is_configured():
                continue
            stype = (seed.get("seed_type") or "").upper()
            value = seed.get("value") or ""
            if not value or stype in ("DEAD_ANGLE", "BLOCKED_RECIPE"):
                continue
            # Treat winning_hook_candidate as a winner with score 7 (above threshold)
            score = 8 if "WINNING" in stype else 5
            wid = cloud_store.promote_winner(
                value,
                format="SESSION_SEED",
                engagement_score=score,
            )
            if wid:
                promoted += 1
        except Exception as e:
            print(f"[session_summary] seed promote failed: {e}")
    if promoted:
        print(f"[session_summary] promoted {promoted} seeds to cloud winners table")

    return 0 if isinstance(summary, dict) and not summary.get("_errors") else 1


if __name__ == "__main__":
    sys.exit(main())
