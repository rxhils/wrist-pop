"""Prompt loader — resolves @include directives + optional self-learning injection.

Usage:
    from prompts.loader import load_prompt
    text = load_prompt("trend_scout.md")                    # raw + master
    text = load_prompt("trend_scout.md", with_learning=True) # also injects past winners

@include directives inline file contents (e.g. @include _master.md).
Self-learning block injects a [KNOWN WINNERS] section if cloud_store is configured.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent

_INCLUDE_RE = re.compile(r"^@include\s+(\S+)\s*$", re.MULTILINE)


def _read_with_includes(name: str, _seen: set[str] | None = None) -> str:
    _seen = _seen or set()
    p = PROMPTS_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"prompt not found: {p}")
    if str(p) in _seen:
        raise RuntimeError(f"prompt include cycle: {p}")
    _seen.add(str(p))
    text = p.read_text(encoding="utf-8")

    def _sub(m: re.Match) -> str:
        return _read_with_includes(m.group(1), _seen)

    return _INCLUDE_RE.sub(_sub, text)


def _brand_snapshot_block() -> str:
    """Append the live brand_snapshot.json as a [BRAND TRUTH SNAPSHOT] block.
    Read-only — does not refetch. Pipeline runner is responsible for refresh.
    """
    try:
        import sys
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
        from tools.website_truth import load as _load_snap
        snap = _load_snap()
    except Exception:
        return ""
    if not snap:
        return ""
    try:
        import json as _json
        return (
            "\n\n[BRAND TRUTH SNAPSHOT — read first, override master block defaults where this differs]\n"
            "```json\n" + _json.dumps(snap, indent=2, ensure_ascii=False) + "\n```\n"
        )
    except Exception:
        return ""


def _pipeline_state_block() -> str:
    """Append a [PIPELINE STATE SO FAR] block summarising every artifact produced
    in today's run. Each downstream agent gets full chain visibility, not just its
    immediate upstream parent.

    Reads outputs/<prefix>_<today>.json for the standard 10 artifact prefixes.
    Empty string when no artifacts exist yet (first stage of the day).
    """
    try:
        import json as _json
        from pathlib import Path as _P
        from datetime import date as _date
        outdir = _P(__file__).resolve().parent.parent / "outputs"
        today = _date.today().isoformat()
    except Exception:
        return ""

    PREFIXES = [
        ("trend_report",       "Scout"),
        ("content_brief",      "Marketing Director"),
        ("copy",               "Copy"),
        ("approved_copy",      "QA"),
        ("asset_plan",         "Asset Director"),
        ("visual_brief",       "Visual Brief"),
        ("manual_reel_state",  "Manual Reel"),
        ("manual_post_state",  "Manual Post"),
        ("reel_ideas",         "Reel Director"),
        ("image_ideas",        "Image Director"),
        ("operator_console",   "Output Director"),
    ]

    lines: list[str] = []
    for prefix, label in PREFIXES:
        p = outdir / f"{prefix}_{today}.json"
        if not p.exists():
            continue
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Extract _status_line if present, else summarise key fields
        status = None
        if isinstance(d, dict):
            status = d.get("_status_line")
            if not status and "pieces" in d and d["pieces"]:
                status = d["pieces"][0].get("_status_line") if isinstance(d["pieces"][0], dict) else None
            if not status and "plans" in d and d["plans"]:
                status = d["plans"][0].get("_status_line") if isinstance(d["plans"][0], dict) else None
            if not status and "ideas" in d and isinstance(d["ideas"], list):
                status = f"{len(d['ideas'])} ideas generated"
        if not status:
            status = f"(no _status_line; file present)"
        lines.append(f"  {label:20} → {status}")

    if not lines:
        return ""
    return (
        "\n\n[PIPELINE STATE SO FAR — what every agent before you has already done today]\n"
        + "\n".join(lines)
        + "\n\nUse this context to:\n"
        "- avoid re-asking questions earlier agents already answered\n"
        "- reference upstream decisions by quoting their _status_line\n"
        "- maintain narrative continuity across the chain\n"
    )


def _learning_block() -> str:
    """Pull last 30d winners from cloud_store. Empty string if cloud unavailable."""
    try:
        import sys
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
        import cloud_store
        if not cloud_store.is_configured():
            return ""
        winners = cloud_store.recent_winners(limit=10, days=30)
    except Exception:
        return ""
    if not winners:
        return ""
    lines = [
        "",
        "[SELF-LEARNING — KNOWN WINNERS LAST 30 DAYS]",
        "These hooks scored highly in past posts. Reuse the structural pattern, NOT the exact words.",
    ]
    for w in winners:
        lines.append(
            f"- engagement={w.get('engagement_score', '?')}/10  format={w.get('format', '?')}  hook={w.get('hook_text', '')!r}"
        )
    lines.append("")
    return "\n".join(lines)


def load_prompt(name: str, with_learning: bool = True, with_snapshot: bool = True, with_pipeline_state: bool = True) -> str:
    """Read a prompt file with @include resolution + optional dynamic blocks.

    Dynamic blocks appended in order:
      1. [BRAND TRUTH SNAPSHOT] from brand_snapshot.json (with_snapshot=True)
      2. [PIPELINE STATE SO FAR] from today's artifacts (with_pipeline_state=True)
      3. [SELF-LEARNING — KNOWN WINNERS] from cloud_store (with_learning=True)

    Each silently no-ops if its data source is missing.
    Pass all False to get raw on-disk file (used by /api/prompts viewer when inlined=false).
    """
    text = _read_with_includes(name)
    if with_snapshot:
        snap_block = _brand_snapshot_block()
        if snap_block:
            text = text + snap_block
    if with_pipeline_state:
        state_block = _pipeline_state_block()
        if state_block:
            text = text + state_block
    if with_learning:
        block = _learning_block()
        if block:
            text = text + "\n" + block
    return text
