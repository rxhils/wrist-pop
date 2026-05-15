"""Shared brand-safety / structural validation.

Hard checks (regex + spec match) → blocking.
Auto-fix common violations BEFORE failing (Buy Now → Pre-Order Now).
Optional Detoxify toxicity scan if installed.
"""
from __future__ import annotations

import re
from typing import Any

# ── banned & braggy phrases ──────────────────────────────────────
BANNED_PHRASES = [
    "official",
    "endorsed",
    "partnered",
    "licensed",
    "collaboration",
    "ap × swatch strap",
    "ap collaboration",
    "buy now",
    "add to cart",
    "shop now",
    "revolutionary",
    "insane",
    "game-changer",
    "game changer",
    "ultimate",
    "best in class",
]
BRAGGY_PHRASES = [
    "ours are better",
    "ours is better",
    "we're the best",
    "we are the best",
    "still the best",
    "we beat",
    "nobody else",
    "better than anyone",
    "we win",
]

# Auto-fix map (case-insensitive substitution). Run BEFORE check.
AUTO_FIX = {
    r"\bbuy\s+now\b": "Pre-Order Now",
    r"\badd\s+to\s+cart\b": "Comment WRIST for early access",
    r"\bshop\s+now\b": "Join the waitlist",
    r"\bgame[- ]changer\b": "precision-fit",
    r"\brevolutionary\b": "precise",
    r"\binsane\b": "extreme",
    r"\bultimate\b": "complete",
}

DISCLAIMER = "Independent brand. Not affiliated with Swatch or Audemars Piguet."

PUBLIC_FACING_FORMATS = {"Video script", "Carousel", "Caption", "Email copy"}

# Real product specs — used in regex spec validation
VALID_PRICES = {"£59", "£79", "£99"}
INVALID_SPECS = [
    r"\b40\.35\s*mm\b",
    r"\b6\.2\s*mm\b",
    r"\b0\.7\s*mm\b",
    r"\b22\s*mm\b",
    r"\bFKM\s+rubber\b",  # we use silicone by default in validation phase
]

# ── optional detoxify ────────────────────────────────────────────
try:
    from detoxify import Detoxify  # type: ignore
    _DETOX_AVAILABLE = True
    _detox_model = None  # lazy load
except ImportError:
    _DETOX_AVAILABLE = False


def gather_text(value: Any) -> str:
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, dict):
            for k, item in v.items():
                if not str(k).startswith("_"):
                    walk(item)

    walk(value)
    return " ".join(parts)


def auto_fix_piece(piece: dict) -> tuple[dict, list[str]]:
    """Apply AUTO_FIX substitutions in-place. Return (piece, list of fixes applied)."""
    applied: list[str] = []

    def fix_str(s: str) -> str:
        for pat, repl in AUTO_FIX.items():
            new = re.sub(pat, repl, s, flags=re.IGNORECASE)
            if new != s:
                applied.append(f"auto-fixed '{pat}' -> '{repl}'")
                s = new
        return s

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return fix_str(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: (v[k] if k.startswith("_") else walk(v[k])) for k in v}
        return v

    cleaned = walk(piece)
    return cleaned, applied


def _word_in_text(word: str, text_lc: str) -> bool:
    """Word-boundary match (catches 'official' but NOT inside 'unofficial')."""
    return bool(re.search(r"\b" + re.escape(word) + r"\b", text_lc, flags=re.IGNORECASE))


def hard_check(piece: dict, idea: dict) -> list[str]:
    text = gather_text(piece)
    text_lc = text.lower()
    issues: list[str] = []

    # banned phrases — regex word-boundary
    for banned in BANNED_PHRASES:
        if _word_in_text(banned, text):
            issues.append(f"banned phrase '{banned}' present")

    # braggy phrases — substring fine (these are multi-word)
    for braggy in BRAGGY_PHRASES:
        if braggy in text_lc:
            issues.append(f"braggy phrase '{braggy}' present")

    # invalid specs (different product's specs)
    for pat in INVALID_SPECS:
        if re.search(pat, text, flags=re.IGNORECASE):
            issues.append(f"invalid spec pattern '{pat}' present (belongs to a different product)")

    # disclaimer required on public-facing
    if idea.get("format") in PUBLIC_FACING_FORMATS:
        if DISCLAIMER.lower() not in text_lc:
            issues.append("required disclaimer missing")

    # hashtag count
    hashtags = piece.get("hashtags")
    if hashtags is not None:
        if not isinstance(hashtags, list):
            issues.append("hashtags must be a list")
        elif len(hashtags) > 8:
            issues.append(f"too many hashtags: {len(hashtags)} (max 8)")
        elif len(hashtags) < 4 and idea.get("format") in {"Video script", "Carousel", "Caption"}:
            issues.append(f"too few hashtags: {len(hashtags)} (min 4)")

    return issues


def toxicity_check(piece: dict, threshold: float = 0.6) -> list[str]:
    """Optional Detoxify scan. Returns issues if toxicity exceeds threshold."""
    if not _DETOX_AVAILABLE:
        return []
    global _detox_model
    if _detox_model is None:
        try:
            _detox_model = Detoxify("original-small")
        except Exception as e:
            return [f"detoxify load failed: {e}"]
    text = gather_text(piece)
    if not text.strip():
        return []
    try:
        scores = _detox_model.predict(text)
    except Exception as e:
        return [f"detoxify predict failed: {e}"]
    issues: list[str] = []
    for label, score in scores.items():
        if isinstance(score, (int, float)) and score >= threshold:
            issues.append(f"toxicity '{label}' = {score:.2f} (>= {threshold})")
    return issues


def detoxify_available() -> bool:
    return _DETOX_AVAILABLE
