"""Manual Post — status-only stub.

Reads today's manual_reel_state, seeds a manual_post_state. Preserves
existing entries on re-run. No LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "outputs"

PLATFORM_BY_SERIES = {
    "TR": ["Instagram Reel", "TikTok"],
    "CW": ["Instagram Reel", "TikTok"],
    "PT": ["Instagram Reel", "TikTok"],
    "BC": ["Instagram Reel"],
    "CM": ["Instagram Story"],
    "WL": ["Instagram Story"],
}


def _latest(prefix: str, today: str) -> Path | None:
    direct = OUT_DIR / f"{prefix}_{today}.json"
    if direct.exists():
        return direct
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    return files[0] if files else None


def _load_existing(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _series_from_post_id(post_id: str, brief_path: Path | None) -> str:
    """Try to infer series code from visual_brief if available."""
    if not brief_path or not brief_path.exists():
        return "TR"
    try:
        data = json.loads(brief_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "TR"
    items = data if isinstance(data, list) else data.get("briefs") or [data]
    for b in items:
        if (b.get("post_id") or (b.get("_meta") or {}).get("post_id")) == post_id:
            meta = b.get("_meta") or {}
            return meta.get("series") or b.get("series") or "TR"
    return "TR"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    args, _ = parser.parse_known_args()
    today = args.date

    OUT_DIR.mkdir(exist_ok=True)
    state_path = OUT_DIR / f"manual_post_state_{today}.json"

    reel_state_path = _latest("manual_reel_state", today)
    if not reel_state_path:
        print("[manual_post] no manual_reel_state yet — writing empty state")
        state_path.write_text(json.dumps({"date": today, "items": []}, indent=2), encoding="utf-8")
        return 0

    reel_state = json.loads(reel_state_path.read_text(encoding="utf-8"))
    brief_path = _latest("visual_brief", today)

    existing = _load_existing(state_path)
    by_id = {it["post_id"]: it for it in existing.get("items", [])}

    items = []
    for r in reel_state.get("items", []):
        post_id = r["post_id"]
        series = _series_from_post_id(post_id, brief_path)
        prior = by_id.get(post_id, {})
        # Engagement signal: any metric >= 8 saves OR 50+ signups => winner candidate
        m24 = prior.get("metrics_24h") or {}
        winner_candidate = bool(
            (m24.get("saves") or 0) >= 80
            or (m24.get("waitlist_signups") or 0) >= 50
            or (m24.get("shares") or 0) >= 25
        )
        items.append({
            "post_id": post_id,
            "platforms": prior.get("platforms") or PLATFORM_BY_SERIES.get(series, ["Instagram Reel"]),
            "status": prior.get("status", "WAITING"),
            "scheduled_for": prior.get("scheduled_for"),
            "posted_at": prior.get("posted_at"),
            "post_urls": prior.get("post_urls") or {},
            "metrics_1h": prior.get("metrics_1h") or {},
            "metrics_24h": m24,
            "winner_candidate": winner_candidate,
            "postmortem": prior.get("postmortem", ""),
            "notes": prior.get("notes", ""),
            "reel_state": r.get("status", "WAITING"),
            "updated_at": prior.get("updated_at") or datetime.utcnow().isoformat() + "Z",
        })

    state = {"date": today, "items": items}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[manual_post] {len(items)} item(s) tracked. Saved: {state_path.name}")
    ready = sum(1 for i in items if i.get("reel_state") == "EXPORTED" and i["status"] == "WAITING")
    if ready:
        print(f"[manual_post] {ready} reel(s) EXPORTED and ready to schedule/post.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
