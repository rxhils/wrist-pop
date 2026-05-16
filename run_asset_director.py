"""Asset Director — plans stills + motion clips before Visual Brief.

Reads:
  - approved_copy_<date>.json (QA-approved Copy piece)
  - content_brief_<date>.json (Marketing Director brief)
  - master block via prompts/loader (brand + specs)

Writes:
  outputs/asset_plan_<date>.json — image_blueprint + motion_blueprint +
  dependencies + missing_assets + build_order + handoff_for_visual_brief
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "asset_director.md"
OUT_DIR = ROOT / "outputs"


def _latest(prefix: str, today: str) -> Path | None:
    direct = OUT_DIR / f"{prefix}_{today}.json"
    if direct.exists():
        return direct
    files = sorted(OUT_DIR.glob(f"{prefix}_*.json"), reverse=True)
    return files[0] if files else None


def _load(prefix: str, today: str) -> dict | list | None:
    p = _latest(prefix, today)
    if not p:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="approved_copy JSON path")
    parser.add_argument("--brief", type=Path, help="content_brief JSON path")
    parser.add_argument("--output", type=Path)
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()

    approved = (
        json.loads(args.input.read_text(encoding="utf-8")) if args.input
        else _load("approved_copy", today)
    )
    director_brief = (
        json.loads(args.brief.read_text(encoding="utf-8")) if args.brief
        else _load("content_brief", today)
    )

    if not approved:
        print("[asset_director] BLOCKED — no approved_copy. Run QA first.")
        return 1
    pieces = approved.get("pieces", []) if isinstance(approved, dict) else (approved if isinstance(approved, list) else [])
    pass_pieces = [p for p in pieces if isinstance(p, dict) and p.get("_gate_status") == "PASS"]
    if not pass_pieces:
        print("[asset_director] BLOCKED — no PASS pieces in approved_copy.")
        return 1

    from prompts import load_prompt
    system_prompt = load_prompt(PROMPT_PATH.name)
    from providers import llm_json

    plans: list[dict] = []
    for piece in pass_pieces:
        post_id = piece.get("post_id") or "unknown"
        public_piece = {k: v for k, v in piece.items() if not k.startswith("_")}
        user_prompt = f"""Today: {today}
post_id: {post_id}

MARKETING DIRECTOR BRIEF:
```json
{json.dumps(director_brief or {}, indent=2, ensure_ascii=False)[:4000]}
```

QA-APPROVED COPY PIECE:
```json
{json.dumps(public_piece, indent=2, ensure_ascii=False)[:4000]}
```

Produce the strict-JSON asset plan per your output schema. Respect:
- max 6 images + 6 motion clips
- minimum-viable-first
- every video.source_image_id must point to a defined image_id
- every motion clip needs subject_motion + background_motion + camera_motion
"""
        try:
            plan = llm_json(
                agent_name="asset_director",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                num_ctx=8192,
            )
            plan["post_id"] = plan.get("post_id") or post_id
            plans.append(plan)
        except Exception as e:
            plans.append({
                "status": "BLOCKED",
                "post_id": post_id,
                "reason": str(e)[:300],
            })

    output = {"date": today, "plans": plans}
    out_path = args.output or (OUT_DIR / f"asset_plan_{today}.json")
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    ok = sum(1 for p in plans if p.get("status") == "OK")
    print(f"\n[asset_director] {ok}/{len(plans)} plans OK. Saved: {out_path}")
    for p in plans:
        st = p.get("status")
        imgs = len(p.get("image_blueprint") or [])
        vids = len(p.get("motion_blueprint") or [])
        print(f"  {p.get('post_id'):30} {st:8}  images={imgs} videos={vids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
