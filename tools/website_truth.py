"""Website Truth — fetch live popwriststudio.com naming + copy.

Writes brand_snapshot.json. All agents read THIS file (not the site directly)
via prompts/loader.py so every stage in one pipeline run uses the same checked
snapshot.

Source hierarchy:
  1. Live https://popwriststudio.com/
  2. Existing brand_snapshot.json on disk (last good fetch)
  3. Seed defaults (master block constants)
  4. status=BLOCKED if all three unavailable

CLI:
  python tools/website_truth.py            # fetch + write
  python tools/website_truth.py --dry      # fetch + print, no write
  python tools/website_truth.py --force    # skip cache, always refetch
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "brand_snapshot.json"

SITE_URL = "https://popwriststudio.com/"
TIMEOUT_S = 8
CACHE_TTL_S = 3600   # refetch at most once/hour unless --force

# Seed defaults — used only if site AND on-disk snapshot both unavailable.
SEED: dict[str, Any] = {
    "source": SITE_URL,
    "fetched_at": None,
    "website_status": "SEED_FALLBACK",
    "brand_name": "Pop Wrist Studio",
    "product_name": "Cradle Adapter V1",
    "cta_primary": "Join Waitlist",
    "legal_disclaimer": (
        "Pop Wrist Studio is an independent accessory brand. "
        "Not affiliated with, endorsed by, or sponsored by Swatch or Audemars Piguet."
    ),
    "colourways": [
        "Monochrome", "Arctic Blue", "Cobalt Orange", "Turquoise Pink",
        "Blue Acht", "Green Eight", "Otto Rosso", "Huit Blanc",
    ],
    "specs": {
        "inner_socket_mm": 40.35,
        "retaining_lip_mm": 0.7,
        "lug_width_mm": 22,
        "cradle_depth_mm": 6.2,
        "material": "FKM rubber",
        "crown_notch_count": 2,
    },
    "notes": [],
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_existing() -> dict | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _extract(html: str, base: dict) -> dict:
    """Best-effort parse: brand name, product name, CTA, disclaimer, colourways.

    No BeautifulSoup — keep deps minimal. Regex on stable patterns only.
    """
    out = dict(base)
    out["website_status"] = "LIVE"
    out["fetched_at"] = _now_iso()

    # <title> for brand name
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title and "pop" in title.lower():
            # title often "Brand — Tagline" or "Brand | Tagline"
            cand = re.split(r"[\|—–\-]", title, 1)[0].strip()
            if 3 <= len(cand) <= 60:
                out["brand_name"] = cand

    # <meta property="og:site_name">
    m = re.search(r'<meta[^>]+property="og:site_name"[^>]+content="([^"]+)"', html, re.I)
    if m and m.group(1).strip():
        out["brand_name"] = m.group(1).strip()

    # H1 → product name candidate
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    if m:
        h1 = re.sub(r"\s+", " ", _strip_tags(m.group(1))).strip()
        if 3 <= len(h1) <= 80 and ("cradle" in h1.lower() or "adapter" in h1.lower() or "v1" in h1.lower()):
            out["product_name"] = h1

    # CTA: look for common waitlist / pre-order phrases
    cta_patterns = [
        r"(join\s+(?:the\s+)?waitlist)",
        r"(pre[-\s]?order\s+now)",
        r"(reserve\s+yours)",
        r"(get\s+early\s+access)",
        r"(notify\s+me)",
    ]
    for pat in cta_patterns:
        m = re.search(pat, html, re.I)
        if m:
            out["cta_primary"] = m.group(1).strip().title()
            break

    # Disclaimer: footer/legal sections referencing affiliation
    m = re.search(
        r"(not\s+affiliated[^.]*?(?:swatch|audemars)[^.]*\.)",
        html, re.I | re.S,
    )
    if m:
        clean = re.sub(r"\s+", " ", _strip_tags(m.group(1))).strip()
        if 30 <= len(clean) <= 300:
            out["legal_disclaimer"] = clean

    # Colourways: look for any of the seed names actually appearing on page
    found = []
    for cw in SEED["colourways"]:
        if re.search(r"\b" + re.escape(cw) + r"\b", html, re.I):
            found.append(cw)
    if found:
        out["colourways"] = found

    return out


IMPACT_BY_FIELD = {
    "brand_name":       "CRITICAL",
    "legal_disclaimer": "CRITICAL",
    "product_name":     "HIGH",
    "cta_primary":      "HIGH",
    "colourways":       "HIGH",
}

AFFECTED_AGENTS_BY_FIELD = {
    "brand_name":       ["scout", "strategist", "writer", "gate", "asset_director", "visual", "manual_post", "output_director", "reel_director"],
    "legal_disclaimer": ["writer", "gate", "manual_post"],
    "product_name":     ["scout", "strategist", "writer", "asset_director", "visual", "reel_director"],
    "cta_primary":      ["strategist", "writer", "manual_post"],
    "colourways":       ["strategist", "writer", "asset_director", "visual"],
}


def _diff(previous: dict, current: dict) -> list[dict]:
    """Return list of change records for fields that matter downstream."""
    changes: list[dict] = []
    if not previous:
        return changes
    for field in IMPACT_BY_FIELD:
        prev_v = previous.get(field)
        cur_v = current.get(field)
        if prev_v == cur_v:
            continue
        # Normalise list compare (order-insensitive for colourways)
        if isinstance(prev_v, list) and isinstance(cur_v, list):
            if sorted(prev_v) == sorted(cur_v):
                continue
        changes.append({
            "field": field,
            "previous_value": prev_v,
            "new_value": cur_v,
            "impact": IMPACT_BY_FIELD[field],
            "affected_agents": AFFECTED_AGENTS_BY_FIELD.get(field, []),
            "recommended_action": (
                f"Review on-site change to '{field}' before publishing today's content."
                if IMPACT_BY_FIELD[field] == "CRITICAL"
                else f"Verify '{field}' aligns with downstream artifacts."
            ),
        })
    return changes


def fetch() -> dict:
    """Try site → existing snapshot → seed. Always returns a dict with website_status."""
    existing = _load_existing()

    try:
        r = requests.get(
            SITE_URL,
            timeout=TIMEOUT_S,
            headers={"User-Agent": "PopWristStudio-Truth/1.0"},
        )
        if r.status_code == 200 and r.text:
            return _extract(r.text, base=existing or SEED)
        else:
            note = f"http_{r.status_code}"
    except requests.exceptions.RequestException as e:
        note = f"fetch_error:{type(e).__name__}"

    # Fallback chain
    if existing:
        snap = dict(existing)
        snap["website_status"] = "UNREACHABLE_USING_CACHE"
        snap["last_fetch_error"] = note
        snap["fallback_at"] = _now_iso()
        return snap
    seed = dict(SEED)
    seed["website_status"] = "UNREACHABLE_USING_SEED"
    seed["last_fetch_error"] = note
    seed["fallback_at"] = _now_iso()
    return seed


def refresh_if_stale(force: bool = False) -> dict:
    """Used by pipeline: only refetch if cache > TTL or force.

    On refetch, computes diff vs previous snapshot and embeds website_change_alert
    in the new snapshot for downstream agents (especially Output Director).
    """
    existing = _load_existing()
    if not force and existing and existing.get("fetched_at"):
        try:
            from datetime import datetime
            age = time.time() - datetime.strptime(existing["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").timestamp()
            if age < CACHE_TTL_S:
                return existing
        except Exception:
            pass
    snap = fetch()

    # Diff against previous snapshot (only meaningful when both exist + new fetch was LIVE)
    changes = []
    if existing and snap.get("website_status") == "LIVE":
        changes = _diff(existing, snap)
    snap["website_change_alert"] = {
        "detected": bool(changes),
        "checked_at": _now_iso(),
        "previous_fetched_at": (existing or {}).get("fetched_at"),
        "changes": changes,
    }

    SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    return snap


def load() -> dict:
    """Read-only accessor used by prompt loader. Never refetches."""
    snap = _load_existing()
    if snap:
        return snap
    return SEED


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="fetch + print, no write")
    parser.add_argument("--force", action="store_true", help="skip cache TTL")
    args = parser.parse_args()

    snap = fetch() if args.force else refresh_if_stale(force=False)
    if args.dry:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
        return 0
    SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[website_truth] saved {SNAPSHOT_PATH.name} status={snap.get('website_status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
