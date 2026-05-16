"""Video Render — Stage 8. LTX-Video 2.3 via ComfyUI :8188.

Reads visual_brief_<date>.json. For each shot with type='ai_video' and an
LTX-Video prompt, builds a minimal ComfyUI workflow, posts to /prompt, polls
/history, saves MP4 to outputs/videos/<date>/P<priority>_shot<id>.mp4.

Same VRAM strategy as image render: evict Ollama before run.

NOTE: ComfyUI-LTXVideo custom nodes required. Node class names vary by pack;
adjust LTX_* constants below if your nodes use different names.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "outputs"
VIDEOS_DIR = OUT_DIR / "videos"

COMFY_URL = os.getenv("COMFYUI_URL", "http://host.docker.internal:8188")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", "")

# Smaller dims for video — 4060 8GB needs headroom for LTX-Video.
ASPECT_DIMS = {
    "9:16": (480, 848),
    "1:1": (640, 640),
    "16:9": (848, 480),
    "4:5": (576, 720),
}

FRAME_RATE = 24
DEFAULT_DURATION_S = 4
DEFAULT_FRAMES = FRAME_RATE * DEFAULT_DURATION_S + 1  # LTX uses N*8+1 frames

LTX_CHECKPOINT = os.getenv("LTX_CHECKPOINT", "ltx-video-2b-v0.9.5.safetensors")


def evict_ollama_model() -> None:
    try:
        requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": "", "keep_alive": 0},
            timeout=15,
        )
        print(f"[render-vid] requested Ollama unload of {OLLAMA_MODEL}")
        time.sleep(3)
    except Exception as e:
        print(f"[render-vid] could not evict Ollama: {e}")


def comfy_alive() -> bool:
    try:
        r = requests.get(f"{COMFY_URL}/system_stats", timeout=5)
        return r.ok
    except Exception:
        return False


def build_workflow(
    prompt: str,
    negative: str,
    width: int,
    height: int,
    length: int,
    seed: int,
) -> dict:
    """Conservative LTX-Video text2video workflow."""
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": LTX_CHECKPOINT},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": prompt},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": negative},
        },
        "4": {
            "class_type": "EmptyLTXVLatentVideo",
            "inputs": {
                "width": width,
                "height": height,
                "length": length,
                "batch_size": 1,
            },
        },
        "5": {
            "class_type": "LTXVConditioning",
            "inputs": {
                "positive": ["2", 0],
                "negative": ["3", 0],
                "frame_rate": FRAME_RATE,
            },
        },
        "6": {
            "class_type": "LTXVScheduler",
            "inputs": {
                "steps": 30,
                "max_shift": 2.05,
                "base_shift": 0.95,
                "stretch": True,
                "terminal": 0.1,
                "latent": ["4", 0],
            },
        },
        "7": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"},
        },
        "8": {
            "class_type": "SamplerCustom",
            "inputs": {
                "model": ["1", 0],
                "add_noise": True,
                "noise_seed": seed,
                "cfg": 3.0,
                "positive": ["5", 0],
                "negative": ["5", 1],
                "sampler": ["7", 0],
                "sigmas": ["6", 0],
                "latent_image": ["4", 0],
            },
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["8", 0], "vae": ["1", 2]},
        },
        "10": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["9", 0],
                "frame_rate": FRAME_RATE,
                "loop_count": 0,
                "filename_prefix": "pws/video",
                "format": "video/h264-mp4",
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": False,
                "pingpong": False,
                "save_output": True,
            },
        },
    }


def queue_prompt(workflow: dict, client_id: str) -> str:
    r = requests.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["prompt_id"]


def wait_for_history(prompt_id: str, timeout_s: int = 1800) -> dict | None:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            r = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                return data[prompt_id]
        except Exception:
            pass
        time.sleep(5)
    return None


def fetch_video(filename: str, subfolder: str, dest: Path) -> None:
    r = requests.get(
        f"{COMFY_URL}/view",
        params={"filename": filename, "subfolder": subfolder, "type": "output"},
        timeout=120,
    )
    r.raise_for_status()
    dest.write_bytes(r.content)


def render_one(
    prompt: str,
    negative: str,
    aspect: str,
    duration_s: int,
    save_to: Path,
) -> dict:
    width, height = ASPECT_DIMS.get(aspect, (640, 640))
    length = FRAME_RATE * max(duration_s, 1) + 1
    seed = int(time.time() * 1000) % 2_000_000_000
    workflow = build_workflow(prompt, negative, width, height, length, seed)
    client_id = str(uuid.uuid4())
    pid = queue_prompt(workflow, client_id)
    print(f"[render-vid]   queued prompt_id={pid} {width}x{height} {length}f seed={seed}")
    result = wait_for_history(pid)
    if result is None:
        return {"status": "TIMEOUT", "prompt_id": pid}

    outputs = result.get("outputs", {})
    for node_id, node_out in outputs.items():
        videos = node_out.get("gifs") or node_out.get("videos") or []
        for v in videos:
            fname = v["filename"]
            subfolder = v.get("subfolder", "")
            fetch_video(fname, subfolder, save_to)
            return {"status": "OK", "prompt_id": pid, "path": str(save_to), "seed": seed}
    return {"status": "NO_OUTPUT", "prompt_id": pid}


def latest_visual_brief(today: str) -> dict:
    direct = OUT_DIR / f"visual_brief_{today}.json"
    if direct.exists():
        return json.loads(direct.read_text(encoding="utf-8"))
    files = sorted(OUT_DIR.glob("visual_brief_*.json"), reverse=True)
    if not files:
        raise FileNotFoundError("No visual_brief_*.json")
    return json.loads(files[0].read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="custom visual_brief JSON")
    parser.add_argument("--pick", type=int, help="only render shots for this priority")
    parser.add_argument("--shot", type=int, help="only render this shot id (use with --pick)")
    args, _ = parser.parse_known_args()

    today = date.today().isoformat()
    if not comfy_alive():
        print(f"[render-vid] ComfyUI not reachable at {COMFY_URL} — SKIPPING stage.")
        print(
            "[render-vid] Install ComfyUI + ComfyUI-LTXVideo custom nodes + LTX-Video 2b weights, retry."
        )
        return 0

    evict_ollama_model()

    if args.input:
        brief = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[render-vid] using custom input: {args.input}")
    else:
        brief = latest_visual_brief(today)
    briefs = brief.get("briefs", [])
    if args.pick is not None:
        briefs = [b for b in briefs if b.get("_meta", {}).get("priority") == args.pick]

    videos_today = VIDEOS_DIR / today
    videos_today.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for b in briefs:
        if b.get("_visual_status") != "PASS":
            continue
        meta = b.get("_meta", {})
        prio = meta.get("priority")
        aspect = b.get("aspect_ratio", "9:16")

        prompts_by_shot = {p.get("for_shot_id"): p for p in b.get("ai_prompts", [])}

        for shot in b.get("shot_list", []):
            if shot.get("type") != "ai_video":
                continue
            sid = shot.get("id")
            if args.shot is not None and sid != args.shot:
                continue
            p = prompts_by_shot.get(sid)
            if not p:
                continue
            tool = p.get("tool", "").lower()
            if "ltx" not in tool and "veo" not in tool and "wan" not in tool:
                continue

            dur = int(shot.get("duration_sec", DEFAULT_DURATION_S))
            dur = max(1, min(dur, 5))  # LTX practical limit
            save_to = videos_today / f"P{prio}_shot{sid}.mp4"
            print(f"[render-vid] P{prio} shot {sid} ({aspect}, {dur}s) -> {save_to.name}")
            res = render_one(
                p.get("prompt", ""),
                p.get("negative_prompt", ""),
                aspect,
                dur,
                save_to,
            )
            res["priority"] = prio
            res["shot_id"] = sid
            res["aspect"] = aspect
            res["duration_s"] = dur
            results.append(res)

    summary = {
        "date": today,
        "rendered": [r for r in results if r["status"] == "OK"],
        "failed": [r for r in results if r["status"] != "OK"],
    }
    (OUT_DIR / f"video_render_{today}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 60)
    print(f"VIDEO RENDER — {today}")
    print(f"OK: {len(summary['rendered'])}  FAIL: {len(summary['failed'])}")
    print("=" * 60)
    for r in results:
        print(f"  P{r['priority']} shot {r['shot_id']} -> {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
