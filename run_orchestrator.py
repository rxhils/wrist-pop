"""Orchestrator — top-level agent that reads operator intent, plans + runs tools,
streams progress, returns consolidated brief.

Two modes:
  CLI:     python run_orchestrator.py --intent "..." [--refs path1 path2]
  Library: from run_orchestrator import run_session
           run_session(intent, refs, on_progress=callback)

The Orchestrator uses ReAct-style JSON tool-calling: each turn it emits a
decision JSON; the runner parses it, executes the tool, feeds result back as
next user_message, and loops until DONE or BLOCKED.
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
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "orchestrator.md"
OUT_DIR = ROOT / "outputs" / "orchestrator"

MAX_TURNS = 12
COST_CAP_USD = 0.50


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_run_id() -> str:
    return f"orch_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _session_dir(run_id: str) -> Path:
    d = OUT_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "references").mkdir(exist_ok=True)
    return d


def run_session(
    intent: str,
    refs: list[str] | None = None,
    *,
    on_progress: Callable[[dict], None] | None = None,
    run_id: str | None = None,
) -> dict:
    """Drive the orchestrator. Returns the final session record.

    `on_progress(event)` is called for every event:
      {type: "plan_step" | "tool_start" | "tool_done" | "operator_message" | "done" | "blocked",
       ...payload}
    """
    from prompts import load_prompt
    from providers import call_llm, extract_json
    from tools.agent_tools import dispatch, TOOL_CATALOG

    run_id = run_id or _gen_run_id()
    sdir = _session_dir(run_id)
    refs = refs or []

    # Status file (live JSONL stream operator can tail)
    status_path = sdir / "status.jsonl"

    def _emit(event: dict):
        event["_at"] = _now_iso()
        try:
            with status_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    # Initial state
    system_prompt = load_prompt("orchestrator.md")
    history: list[dict] = []
    tools_run: list[dict] = []
    total_cost = 0.0
    total_latency = 0.0
    t_session = time.time()

    user_msg = (
        f"OPERATOR INTENT:\n{intent}\n\n"
        f"REFERENCE IMAGES ({len(refs)}):\n"
        + ("\n".join(f"  - {r}" for r in refs) if refs else "(none)")
        + "\n\nBuild a plan. Emit one decision JSON. I will execute and return the result."
    )

    _emit({"type": "session_start", "run_id": run_id, "intent": intent, "refs_count": len(refs)})

    final_result: dict | None = None

    for turn in range(1, MAX_TURNS + 1):
        if total_cost >= COST_CAP_USD:
            _emit({"type": "blocked", "reason": f"cost cap ${COST_CAP_USD} hit", "turn": turn})
            return _finalise(run_id, sdir, intent, refs, tools_run, total_cost, total_latency, blocked="cost_cap")

        # ── LLM turn ──
        prior = "\n\n".join(
            f"[TURN {h['turn']} — your decision]\n{json.dumps(h['decision'], indent=2)}\n\n[TURN {h['turn']} — tool result]\n{json.dumps(h['result'], indent=2, default=str)[:2500]}"
            for h in history
        )
        full_user = user_msg if not prior else (user_msg + "\n\n──── HISTORY ────\n" + prior + "\n\n──── END HISTORY ────\nNext decision?")

        try:
            raw = call_llm(
                system_prompt=system_prompt,
                user_prompt=full_user,
                provider="mistral",
                model="mistral-large-latest",
                json_mode=True,
                max_tokens=4096,
                temperature=0.3,
            )
            decision = extract_json(raw)
        except Exception as e:
            _emit({"type": "blocked", "reason": f"orchestrator LLM failed: {e}", "turn": turn})
            return _finalise(run_id, sdir, intent, refs, tools_run, total_cost, total_latency, blocked=str(e)[:200])

        if not isinstance(decision, dict):
            _emit({"type": "blocked", "reason": "orchestrator returned non-dict", "turn": turn})
            break

        if decision.get("operator_message"):
            _emit({"type": "operator_message", "turn": turn, "message": decision["operator_message"]})

        action = (decision.get("decision") or "").upper()

        if action == "DONE":
            # Orchestrator believes it's finished — capture final brief
            final_result = decision
            _emit({"type": "done", "turn": turn, "status_line": decision.get("_status_line", "complete")})
            break

        if action == "BLOCKED":
            _emit({"type": "blocked", "reason": decision.get("reasoning", "no reason"), "turn": turn})
            return _finalise(run_id, sdir, intent, refs, tools_run, total_cost, total_latency, blocked=decision.get("reasoning"))

        if action not in ("RUN_TOOL", "RETRY"):
            history.append({"turn": turn, "decision": decision, "result": {"error": f"unknown action {action}"}})
            continue

        tool = decision.get("tool")
        args = decision.get("args") or {}
        if tool not in TOOL_CATALOG:
            history.append({"turn": turn, "decision": decision, "result": {"error": f"unknown tool {tool}"}})
            continue

        _emit({"type": "tool_start", "turn": turn, "tool": tool, "args": args, "reasoning": decision.get("reasoning", "")})

        # ── execute tool ──
        # Inject refs into image_director / asset_director if operator provided
        if refs and tool in ("image_director",) and "refs" not in args:
            args["refs"] = refs

        result = dispatch(tool, args)
        cost = float(result.get("cost_usd") or 0)
        latency = float(result.get("latency_s") or 0)
        total_cost += cost
        total_latency += latency

        tools_run.append({
            "tool": tool,
            "args": args,
            "status_line": result.get("status_line"),
            "ok": result.get("ok"),
            "cost_usd": cost,
            "latency_s": latency,
            "artifact_path": result.get("artifact_path"),
            "turn": turn,
        })

        _emit({
            "type": "tool_done",
            "turn": turn,
            "tool": tool,
            "status_line": result.get("status_line"),
            "ok": result.get("ok"),
            "cost_usd": cost,
            "latency_s": latency,
            "running_total_cost": round(total_cost, 4),
        })

        # Feed result to next turn (trim artifact to keep prompt manageable)
        result_for_history = {
            "ok": result.get("ok"),
            "status_line": result.get("status_line"),
            "artifact_summary": _summarise(result.get("artifact")),
            "artifact_path": result.get("artifact_path"),
        }
        history.append({"turn": turn, "decision": decision, "result": result_for_history})

    # ── finalise ──
    session_total_s = round(time.time() - t_session, 1)
    final = _finalise(
        run_id, sdir, intent, refs, tools_run, total_cost, total_latency,
        blocked=None if final_result else "max_turns",
        final_decision=final_result,
        wall_seconds=session_total_s,
    )
    return final


def _summarise(artifact: Any, max_chars: int = 1500) -> str:
    if artifact is None:
        return "(no artifact)"
    try:
        s = json.dumps(artifact, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(artifact)[:max_chars]
    return s if len(s) <= max_chars else s[:max_chars] + f"\n... [+{len(s)-max_chars} chars]"


def _finalise(
    run_id: str,
    sdir: Path,
    intent: str,
    refs: list[str],
    tools_run: list[dict],
    total_cost: float,
    total_latency: float,
    *,
    blocked: str | None = None,
    final_decision: dict | None = None,
    wall_seconds: float | None = None,
) -> dict:
    record = {
        "run_id": run_id,
        "intent": intent,
        "reference_paths": refs,
        "started_at": tools_run[0].get("status_line") if tools_run else None,
        "finished_at": _now_iso(),
        "tools_run": tools_run,
        "total_cost_usd": round(total_cost, 4),
        "total_latency_s": round(total_latency, 1),
        "wall_seconds": wall_seconds,
        "blocked": blocked,
        "final_brief": (final_decision or {}).get("consolidated_brief") if final_decision else None,
        "session_summary": (final_decision or {}).get("session_summary") if final_decision else None,
    }
    (sdir / "session.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intent", required=True)
    parser.add_argument("--refs", nargs="*", default=[])
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    def _print_event(ev: dict):
        et = ev.get("type", "?")
        if et == "operator_message":
            print(f"  → {ev.get('message')}")
        elif et == "tool_start":
            print(f"\n[turn {ev.get('turn')}] {ev.get('tool')}({ev.get('args')}) — {ev.get('reasoning', '')[:80]}")
        elif et == "tool_done":
            ok = "✓" if ev.get("ok") else "✗"
            print(f"  {ok} {ev.get('tool')}: {ev.get('status_line')}  (${ev.get('cost_usd'):.3f}, {ev.get('latency_s')}s)")
        elif et == "done":
            print(f"\n[DONE] {ev.get('status_line')}")
        elif et == "blocked":
            print(f"\n[BLOCKED] {ev.get('reason')}")
        elif et == "session_start":
            print(f"\n=== ORCHESTRATOR SESSION {ev.get('run_id')} ===")
            print(f"Intent: {ev.get('intent')}")
            print(f"Refs:   {ev.get('refs_count')}")

    result = run_session(
        intent=args.intent,
        refs=args.refs,
        on_progress=_print_event,
        run_id=args.run_id,
    )

    print(f"\nSession saved to outputs/orchestrator/{result['run_id']}/session.json")
    print(f"Total: {len(result['tools_run'])} tools · ${result['total_cost_usd']:.3f} · {result.get('wall_seconds')}s")
    return 0 if not result.get("blocked") else 1


if __name__ == "__main__":
    sys.exit(main())
