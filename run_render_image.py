"""Image Render — Stage 7. FLUX.1 schnell via ComfyUI :8188.

Reads visual_brief_<date>.json. For each shot with type='ai_image' and a Flux
prompt, builds a minimal ComfyUI workflow, posts to /prompt, polls /history,
saves PNG to outputs/images/<date>/P<priority>_shot<id>.png.

VRAM strategy: Ollama qwen2.5:14b uses ~8GB on a 4060 Laptop's 8GB. Before
running, we call /api/delete or simply trigger an unload via Ollama's no-op.
Cleanest: hit the keep_alive=0 endpoint on Ollama to evict the model.
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
IMAGES_DIR = OUT_DIR / "images"

COMFY_URL = os.getenv("COMFYUI_URL", "http://host.docker.internal:8188")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", "")

ASPECT_DIMS = {
    "9:16": (768, 1344),
    "1:1": (1024, 1024),
    "4:5": (864, 1080),
    "16:9": (1344, 768),
}

# Minimal FLUX.1 schnell text2img workflow for ComfyUI API.
# Adjust filenames to match your ComfyUI/models/* paths.
FLUX_UNET = os.getenv("FLUX_UNET", "flux1-schnell.safetensors")
FLUX_VAE = os.getenv("FLUX_VAE", "ae.safetensors")
FLUX_CLIP_L = os.getenv("FLUX_CLIP_L", "clip_l.safetensors")
FLUX_CLIP_T5 = os.getenv("FLUX_CLIP_T5", "t5xxl_fp8_e4m3fn.safetensors")


def evict_ollama_model() -> None:
    """Free GPU before render by asking Ollama to unload model (keep_alive=0)."""
    try:
        requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": "", "keep_alive": 0},
            timeout=15,
        )
        print(f"[render-img] requested Ollama unload of {OLLAMA_MODEL}")
        time.sleep(3)
    except Exception as e:
        print(f"[render-img] could not evict Ollama: {e}")


def comfy_alive() -> bool:
    try:
        r = requests.get(f"{COMFY_URL}/system_stats", timeout=5)
        return r.ok
    except Exception:
        return False


def build_workflow(prompt: str, negative: str, width: int, height: int, seed: int) -> dict:
    """Minimal FLUX.1 schnell workflow. 4 steps, cfg 1.0, euler/simple."""
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": FLUX_UNET, "weight_dtype": "fp8_e4m3fn"},
        },
        "2": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": FLUX_CLIP_T5,
                "clip_name2": FLUX_CLIP_L,
                "type": "flux",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": FLUX_VAE},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": prompt},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": negative},
        },
        "6": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
                "seed": seed,
                "steps": 4,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["7", 0], "vae": ["3", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "pws/render"},
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


def wait_for_history(prompt_id: str, timeout_s: int = 600) -> dict | None:
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
        time.sleep(2)
    return None


def fetch_image(filename: str, subfolder: str, dest: Path) -> None:
    r = requests.get(
        f"{COMFY_URL}/view",
        params={"filename": filename, "subfolder": subfolder, "type": "output"},
        timeout=60,
    )
    r.raise_for_status()
    dest.write_bytes(r.content)


def render_one(prompt: str, negative: str, aspect: str, save_to: Path) -> dict:
    width, height = ASPECT_DIMS.get(aspect, (1024, 1024))
    seed = int(time.time() * 1000) % 2_000_000_000
    workflow = build_workflow(prompt, negative, width, height, seed)
    client_id = str(uuid.uuid4())
    pid = queue_prompt(workflow, client_id)
    print(f"[render-img]   queued prompt_id={pid} {width}x{height} seed={seed}")
    result = wait_for_history(pid)
    if result is None:
        return {"status": "TIMEOUT", "prompt_id": pid}

    outputs = result.get("outputs", {})
    saved: list[str] = []
    for node_id, node_out in outputs.items():
        for img in node_out.get("images", []):
            fname = img["filename"]
            subfolder = img.get("subfolder", "")
            fetch_image(fname, subfolder, save_to)
            saved.append(str(save_to))
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
        print(f"[render-img] ComfyUI not reachable at {COMFY_URL} — SKIPPING stage.")
        print("[render-img] Start ComfyUI on host, ensure FLUX.1 schnell weights present, retry.")
        return 0

    evict_ollama_model()

    if args.input:
        brief = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"[render-img] using custom input: {args.input}")
    else:
        brief = latest_visual_brief(today)
    briefs = brief.get("briefs", [])
    if args.pick is not None:
        briefs = [b for b in briefs if b.get("_meta", {}).get("priority") == args.pick]

    images_today = IMAGES_DIR / today
    images_today.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for b in briefs:
        if b.get("_visual_status") != "PASS":
            continue
        meta = b.get("_meta", {})
        prio = meta.get("priority")
        aspect = b.get("aspect_ratio", "1:1")

        prompts_by_shot = {p.get("for_shot_id"): p for p in b.get("ai_prompts", [])}

        for shot in b.get("shot_list", []):
            if shot.get("type") != "ai_image":
                continue
            sid = shot.get("id")
            if args.shot is not None and sid != args.shot:
                continue
            p = prompts_by_shot.get(sid)
            if not p:
                continue
            tool = p.get("tool", "").lower()
            if "flux" not in tool and "stable diffusion" not in tool:
                continue

            save_to = images_today / f"P{prio}_shot{sid}.png"
            print(f"[render-img] P{prio} shot {sid} ({aspect}) -> {save_to.name}")
            res = render_one(
                p.get("prompt", ""),
                p.get("negative_prompt", ""),
                aspect,
                save_to,
            )
            res["priority"] = prio
            res["shot_id"] = sid
            res["aspect"] = aspect
            results.append(res)

    summary = {
        "date": today,
        "rendered": [r for r in results if r["status"] == "OK"],
        "failed": [r for r in results if r["status"] != "OK"],
    }
    (OUT_DIR / f"image_render_{today}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 60)
    print(f"IMAGE RENDER — {today}")
    print(f"OK: {len(summary['rendered'])}  FAIL: {len(summary['failed'])}")
    print("=" * 60)
    for r in results:
        print(f"  P{r['priority']} shot {r['shot_id']} -> {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
