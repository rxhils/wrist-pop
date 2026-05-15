"""Scheduler — final stage. No LLM call.

Reads approved_copy + visual_brief, merges per piece, assigns suggested
post slots per platform, and emits:
  - schedule_<date>.json       (machine-readable calendar)
  - notion_payload_<date>.json (rows ready for Notion DB insert via MCP)
  - digest_<date>.md           (human-readable Slack/email digest)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "outputs"

# UK posting windows by platform (local time, 24h)
POST_SLOTS = {
    "TikTok": [time(19, 0), time(20, 30)],
    "Instagram Reel": [time(12, 0), time(18, 0)],
    "Instagram Feed": [time(13, 0)],
    "Instagram Story": [time(11, 0), time(17, 0), time(21, 0)],
    "Email": [time(9, 0)],
}


def _load_json(p: Path) -> dict:
    if not p.exists():
        raise FileNotFoundError(f"missing: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _latest(prefix: str, today: str) -> Path:
    direct = OUT_DIR / f"{prefix}_{today}.json"
    if direct.exists():
        return direct
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    if not files:
        raise FileNotFoundError(f"No {prefix}_*.json")
    return files[0]


def _slot_for(platform: str, idx: int, today: str) -> str:
    slots = POST_SLOTS.get(platform, [time(12, 0)])
    t = slots[idx % len(slots)]
    return datetime.combine(date.fromisoformat(today), t).isoformat()


def _piece_text(piece: dict) -> str:
    """Pick the primary human-readable text for digest preview."""
    if "hook_0_3s" in piece:
        return piece["hook_0_3s"]
    if "caption" in piece:
        return piece["caption"]
    if "question" in piece:
        return piece["question"]
    if "subject" in piece:
        return piece["subject"]
    if "slides" in piece and piece["slides"]:
        return piece["slides"][0].get("text", "")
    return ""


def build_schedule(approved: dict, visual: dict, today: str) -> list[dict]:
    visual_by_priority = {
        b.get("_meta", {}).get("priority"): b for b in visual.get("briefs", [])
    }
    items: list[dict] = []
    slot_counter: dict[str, int] = {}

    for piece in approved.get("pieces", []):
        if piece.get("_gate_status") != "PASS":
            continue
        meta = piece.get("_meta", {})
        prio = meta.get("priority")
        plat = meta.get("platform")
        slot_idx = slot_counter.get(plat, 0)
        slot_counter[plat] = slot_idx + 1

        item = {
            "priority": prio,
            "platform": plat,
            "format": meta.get("format"),
            "pillar": meta.get("pillar"),
            "informed_by_trend": meta.get("informed_by_trend"),
            "scheduled_for": _slot_for(plat, slot_idx, today),
            "status": "DRAFT_AWAITING_APPROVAL",
            "preview": _piece_text(piece)[:160],
            "copy": {k: v for k, v in piece.items() if not k.startswith("_")},
            "visual_brief": {
                k: v
                for k, v in visual_by_priority.get(prio, {}).items()
                if not k.startswith("_")
            },
        }
        items.append(item)

    items.sort(key=lambda x: (x["priority"], x["scheduled_for"]))
    return items


def to_notion_payload(items: list[dict], today: str) -> dict:
    """Rows ready for a Notion DB. Run with Notion MCP from main chat."""
    rows = []
    for it in items:
        rows.append({
            "properties": {
                "Name": {"title": [{"text": {"content": f"P{it['priority']} {it['platform']} — {it['preview'][:60]}"}}]},
                "Date": {"date": {"start": it["scheduled_for"]}},
                "Platform": {"select": {"name": it["platform"]}},
                "Format": {"select": {"name": it["format"]}},
                "Pillar": {"select": {"name": it["pillar"] or "Other"}},
                "Status": {"select": {"name": "Draft – Awaiting Approval"}},
                "Trend Source": {"rich_text": [{"text": {"content": it["informed_by_trend"] or "—"}}]},
                "Preview": {"rich_text": [{"text": {"content": it["preview"]}}]},
            },
            "children": [
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": "Copy"}}]},
                },
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": "json",
                        "rich_text": [{"text": {"content": json.dumps(it["copy"], indent=2, ensure_ascii=False)[:1800]}}],
                    },
                },
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": "Visual Brief"}}]},
                },
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": "json",
                        "rich_text": [{"text": {"content": json.dumps(it["visual_brief"], indent=2, ensure_ascii=False)[:1800]}}],
                    },
                },
            ],
        })
    return {
        "date": today,
        "db_schema_hint": {
            "Name": "title",
            "Date": "date",
            "Platform": "select",
            "Format": "select",
            "Pillar": "select",
            "Status": "select",
            "Trend Source": "rich_text",
            "Preview": "rich_text",
        },
        "rows": rows,
    }


def to_digest_md(items: list[dict], today: str) -> str:
    lines = [
        f"# Royal Pop content brief — {today}",
        "",
        f"**{len(items)} approved pieces** awaiting your approval. Highest priority first.",
        "",
    ]
    for it in items:
        lines.extend([
            f"## P{it['priority']} · {it['platform']} / {it['format']} · {it['pillar']}",
            f"**Scheduled:** {it['scheduled_for']}",
            f"**Trend:** {it['informed_by_trend'] or '—'}",
            f"**Hook / Preview:**",
            f"> {it['preview']}",
            "",
        ])
        copy = it["copy"]
        if "body_3_25s" in copy:
            lines += [f"**Body:** {copy['body_3_25s']}", ""]
        if "cta_25_30s" in copy:
            lines += [f"**CTA:** {copy['cta_25_30s']}", ""]
        if "hashtags" in copy:
            lines += [f"**Hashtags:** {' '.join(copy.get('hashtags', []))}", ""]
        vb = it.get("visual_brief") or {}
        shots = vb.get("shot_list") or []
        if shots:
            lines.append(f"**Shots ({vb.get('aspect_ratio')}):**")
            for s in shots:
                lines.append(f"- [{s.get('type')}] {s.get('description')} ({s.get('duration_sec')}s)")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved", type=Path, help="custom approved_copy JSON")
    parser.add_argument("--visual", type=Path, help="custom visual_brief JSON")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    approved = _load_json(args.approved or _latest("approved_copy", today))
    visual = _load_json(args.visual or _latest("visual_brief", today))

    items = build_schedule(approved, visual, today)
    if not items:
        print("[scheduler] no PASS pieces to schedule.")
        return 1

    schedule = {
        "date": today,
        "count": len(items),
        "items": items,
    }
    notion = to_notion_payload(items, today)
    digest = to_digest_md(items, today)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"schedule_{today}.json").write_text(
        json.dumps(schedule, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / f"notion_payload_{today}.json").write_text(
        json.dumps(notion, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / f"digest_{today}.md").write_text(digest, encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"SCHEDULE — {today}")
    print("=" * 60)
    for it in items:
        print(
            f"  {it['scheduled_for'][11:16]}  P{it['priority']}  "
            f"{it['platform']}/{it['format']:<14}  {it['preview'][:60]}"
        )
    print(f"\nFiles:")
    print(f"  outputs/schedule_{today}.json")
    print(f"  outputs/notion_payload_{today}.json  ← feed to Notion MCP")
    print(f"  outputs/digest_{today}.md            ← Slack/email digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
