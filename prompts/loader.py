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


def load_prompt(name: str, with_learning: bool = True) -> str:
    """Read a prompt file with @include resolution + optional learning injection.

    `with_learning` defaults True — quietly no-ops if cloud_store not configured.
    Pass False to get the raw on-disk file (useful for the /api/prompts UI viewer).
    """
    text = _read_with_includes(name)
    if with_learning:
        block = _learning_block()
        if block:
            text = text + "\n" + block
    return text
