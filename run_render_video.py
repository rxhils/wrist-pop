"""Video Render — cloud-first (fal.ai).

CLI:
  # i2v from a file
  python run_render_video.py --prompt "..." --init outputs/renders/hero.png --model ltx_draft --takes 3

  # t2v
  python run_render_video.py --prompt "..." --model wan_hero

  # from a visual_brief shot
  python run_render_video.py --from-brief visual_brief_2026-05-16.json --post-id W1-01 --shot 2

Writes:
  outputs/renders/<post_id>_<shot>_<model>_t<take>.mp4
  outputs/renders/<post_id>_<shot>_<model>_t<take>.json     (sidecar metadata)
  outputs/video_render_<date>.json                          (aggregate index)

For ComfyUI/local LTX-Video, see run_render_video_comfy.py (legacy).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import video_providers

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "outputs"
RENDERS_DIR = OUT_DIR / "renders"


def _download(url: str, dest: Path) -> int:
    req = Request(url, headers={"User-Agent": "PopWristStudio/1.0"})
    with urlopen(req, timeout=120) as r:
        data = r.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


def _index_add(today: str, entry: dict) -> Path:
    idx_path = OUT_DIR / f"video_render_{today}.json"
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            idx = {"date": today, "takes": []}
    else:
        idx = {"date": today, "takes": []}
    idx["takes"].append(entry)
    idx_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")
    return idx_path


def render_take(
    *,
    prompt: str,
    model: str,
    post_id: str,
    shot_no: int = 1,
    take: int = 1,
    init_image_path: str | None = None,
    init_image_url: str | None = None,
    duration_s: int = 5,
    aspect: str = "9:16",
    negative_prompt: str | None = None,
    today: str | None = None,
) -> dict:
    today = today or date.today().isoformat()
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"[render-vid] post={post_id} shot={shot_no} take={take} model={model} aspect={aspect} dur={duration_s}s")
    if init_image_path:
        print(f"[render-vid] init: {init_image_path}")

    result = video_providers.generate(
        model,
        prompt,
        init_image_path=init_image_path,
        init_image_url=init_image_url,
        duration_s=duration_s,
        aspect=aspect,
        negative_prompt=negative_prompt,
    )

    safe_post = "".join(c if c.isalnum() or c in "-_" else "_" for c in post_id)[:40]
    filename = f"{safe_post}_s{shot_no}_{model}_t{take}.mp4"
    mp4_path = RENDERS_DIR / filename
    size = _download(result["video_url"], mp4_path)
    print(f"[render-vid] downloaded {size//1024} KB -> {mp4_path}")

    elapsed = round(time.time() - t0, 1)
    sidecar = {
        "post_id": post_id,
        "shot_no": shot_no,
        "take": take,
        "model": model,
        "label": result["label"],
        "video_path": str(mp4_path.relative_to(OUT_DIR)).replace("\\", "/"),
        "video_url": result["video_url"],
        "prompt": prompt,
        "init_image_path": init_image_path,
        "init_image_url": init_image_url,
        "duration_s": duration_s,
        "aspect": aspect,
        "negative_prompt": negative_prompt,
        "cost_usd": result["cost_usd"],
        "latency_s": result["latency_s"],
        "wall_time_s": elapsed,
        "request_id": result.get("request_id"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "size_bytes": size,
    }
    sidecar_path = mp4_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    _index_add(today, sidecar)
    return sidecar


def _load_brief_shot(brief_path: Path, post_id: str, shot_no: int) -> dict | None:
    data = json.loads(brief_path.read_text(encoding="utf-8"))
    briefs = data if isinstance(data, list) else (data.get("briefs") or [data])
    for b in briefs:
        if (b.get("post_id") or (b.get("_meta") or {}).get("post_id")) == post_id:
            shots = b.get("shot_list") or []
            for s in shots:
                if s.get("shot_no") == shot_no:
                    return s
    return None


def _shot_to_prompt(shot: dict) -> str:
    parts = [
        shot.get("subject"),
        shot.get("action"),
        shot.get("framing"),
        shot.get("must_capture"),
    ]
    return ". ".join([p for p in parts if p])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=str)
    parser.add_argument("--init", type=str, help="path to init image (i2v)")
    parser.add_argument("--init-url", type=str, help="HTTP url for init image")
    parser.add_argument("--model", default="ltx_draft", choices=list(video_providers.MODEL_REGISTRY))
    parser.add_argument("--post-id", default=f"adhoc-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--shot", type=int, default=1)
    parser.add_argument("--take", type=int, default=1)
    parser.add_argument("--takes", type=int, default=1, help="number of takes to render")
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--aspect", default="9:16")
    parser.add_argument("--negative", type=str, default=None)
    parser.add_argument("--from-brief", type=Path, help="path to a visual_brief JSON")
    args = parser.parse_args()

    if not video_providers.is_configured():
        print("[render-vid] FAL_KEY missing in .env — sign up at https://fal.ai")
        return 1

    prompt = args.prompt
    if args.from_brief:
        shot = _load_brief_shot(args.from_brief, args.post_id, args.shot)
        if shot:
            prompt = _shot_to_prompt(shot)
            print(f"[render-vid] using brief shot prompt: {prompt[:120]}")
        else:
            print(f"[render-vid] shot {args.shot} not found for post {args.post_id}")
            return 1
    if not prompt:
        print("[render-vid] --prompt required (or --from-brief)")
        return 1

    today = date.today().isoformat()
    successes: list[dict] = []
    for t in range(1, args.takes + 1):
        try:
            entry = render_take(
                prompt=prompt,
                model=args.model,
                post_id=args.post_id,
                shot_no=args.shot,
                take=t,
                init_image_path=args.init,
                init_image_url=args.init_url,
                duration_s=args.duration,
                aspect=args.aspect,
                negative_prompt=args.negative,
                today=today,
            )
            successes.append(entry)
        except Exception as e:
            print(f"[render-vid] take {t} FAILED: {e}")

    print(f"[render-vid] {len(successes)}/{args.takes} take(s) done")
    for s in successes:
        print(f"  {s['video_path']}  cost=${s['cost_usd']:.2f}  latency={s['latency_s']}s")
    return 0 if successes else 1


if __name__ == "__main__":
    sys.exit(main())
