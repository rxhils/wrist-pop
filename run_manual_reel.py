"""Manual Reel — status-only stub.

Reads today's visual_brief, seeds a state file with one entry per post_id,
preserving any existing manual entries. Writes JSON. No LLM.
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    args, _ = parser.parse_known_args()
    today = args.date

    OUT_DIR.mkdir(exist_ok=True)
    state_path = OUT_DIR / f"manual_reel_state_{today}.json"

    brief_path = _latest("visual_brief", today)
    if not brief_path:
        print("[manual_reel] no visual_brief yet — writing empty state")
        state_path.write_text(json.dumps({"date": today, "items": []}, indent=2), encoding="utf-8")
        return 0

    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    briefs = brief if isinstance(brief, list) else brief.get("briefs") or [brief]

    existing = _load_existing(state_path)
    by_id = {it["post_id"]: it for it in existing.get("items", [])}

    items = []
    for b in briefs:
        meta = b.get("_meta") or {}
        post_id = b.get("post_id") or meta.get("post_id") or meta.get("id") or "unknown"
        prior = by_id.get(post_id, {})
        items.append({
            "post_id": post_id,
            "visual_brief_path": str(brief_path.relative_to(ROOT)),
            "status": prior.get("status", "WAITING"),
            "exported_file": prior.get("exported_file"),
            "notes": prior.get("notes", ""),
            "updated_at": prior.get("updated_at") or datetime.utcnow().isoformat() + "Z",
        })

    state = {"date": today, "items": items}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[manual_reel] {len(items)} item(s) tracked. Saved: {state_path.name}")
    waiting = sum(1 for i in items if i["status"] == "WAITING")
    if waiting:
        print(f"[manual_reel] {waiting} reel(s) WAITING for human production.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
