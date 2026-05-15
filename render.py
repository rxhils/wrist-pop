"""Manual ComfyUI image render — workflows + client.

One-shot image generation triggered from UI (not the batch render_image stage
that processes visual_brief.json).
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

COMFY_URL = os.getenv("COMFYUI_URL", "http://host.docker.internal:8188")
COMFY_INPUT_DIR = Path(
    os.getenv("COMFY_INPUT_DIR", r"C:\Users\fazea\Documents\ComfyUI\input")
)


class ComfyError(Exception):
    pass


# ── brand prompt building ────────────────────────────────────
COLOURWAY = {
    "Monochrome": "matte black FKM rubber strap, black cradle adapter, brushed silver bezel, black Royal Pop dial, dark PW clasp",
    "Huit Blanc": "white FKM rubber strap, white cradle adapter, white Royal Pop dial, colorful bezel markers, yellow crown",
    "Green Eight": "forest green cradle adapter, deep green FKM rubber strap, dark green dial, green hands, green crown",
}
SHOT = {
    "Hero shot": "floating hero shot, dark charcoal studio background, soft grey glow, premium reflections",
    "Wrist shot": "worn on wrist, lifestyle shot, soft natural light, casual luxury",
    "Exploded view": "three floating components, watch module, cradle adapter, FKM rubber strap, dark background",
    "Strap detail": "extreme macro of ribbed FKM rubber strap texture, black background",
    "Assembly": "watch module dropping into cradle adapter socket, dynamic moment, studio light",
    "PW clasp": "extreme macro of PW branded deployant clasp, brushed metal, engraved letters",
}
SUFFIX = "photorealistic, cinematic, 8k, luxury watch campaign"
NEGATIVE = "blurry, ugly, watermark, low quality, distorted, deformed"


def build_prompt(colourway: str, shot_type: str, user_text: str) -> str:
    parts = [
        "Pop Wrist Studio Cradle Adapter V1",
        COLOURWAY.get(colourway, ""),
        SHOT.get(shot_type, ""),
        (user_text or "").strip(),
        SUFFIX,
    ]
    return ", ".join(p for p in parts if p)


# ── workflows ────────────────────────────────────────────────
def flux_t2i(prompt: str, width: int = 1024, height: int = 1024, seed: int = 0) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-schnell.safetensors", "weight_dtype": "fp8_e4m3fn"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "t5xxl_fp8_e4m3fn.safetensors", "clip_name2": "clip_l.safetensors", "type": "flux"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["4", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "simple", "steps": 4, "denoise": 1.0}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["6", 0], "guider": ["7", 0], "sampler": ["8", 0], "sigmas": ["9", 0], "latent_image": ["5", 0]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": "pws/flux"}},
    }


def sdxl_t2i(prompt: str, negative: str, width: int = 1024, height: int = 1024, seed: int = 0, steps: int = 25) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0], "seed": seed, "steps": steps, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "pws/sdxl"}},
    }


def sdxl_turbo(prompt: str, width: int = 512, height: int = 512, seed: int = 0) -> dict:
    """SDXL Turbo. CFG MUST be 0.0 — distilled model, no CFG. 1-4 steps, 512x512."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_turbo_1.0_fp16.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": ""}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0], "seed": seed,
                "steps": 4, "cfg": 0.0,
                "sampler_name": "euler_ancestral", "scheduler": "normal", "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "pws/sdxl_turbo"}},
    }


def ltx_i2v(prompt: str, ref_filename: str, seed: int = 0) -> dict:
    """LTX-Video image-to-video. Single 5s clip per reference image."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "ltx-video-2b-v0.9.5.safetensors"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": ref_filename, "upload": "image"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "positive": ["3", 0], "negative": ["3", 0],
                "latent_image": ["2", 0], "seed": seed,
                "steps": 25, "cfg": 3.0,
                "sampler_name": "euler", "scheduler": "lcm", "denoise": 0.9,
            },
        },
        "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
        "6": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["5", 0], "frame_rate": 24, "loop_count": 0,
                "filename_prefix": f"pws/ltx_{ref_filename[:8]}",
                "format": "video/h264-mp4", "pix_fmt": "yuv420p",
                "crf": 19, "save_metadata": False, "pingpong": False, "save_output": True,
            },
        },
    }


def sdxl_i2i(prompt: str, negative: str, ref_filename: str, denoise: float = 0.65, seed: int = 0, steps: int = 30) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative}},
        "4": {"class_type": "LoadImage", "inputs": {"image": ref_filename, "upload": "image"}},
        "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0], "seed": seed, "steps": steps, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal", "denoise": float(denoise)}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "pws/sdxl_i2i"}},
    }


# ── ComfyUI client ───────────────────────────────────────────
def is_alive() -> bool:
    try:
        return requests.get(f"{COMFY_URL}/system_stats", timeout=5).ok
    except Exception:
        return False


def save_reference_b64(b64_str: str) -> str:
    try:
        COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise ComfyError(f"cannot create input dir {COMFY_INPUT_DIR}: {e}")
    # strip data URL prefix
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64_str)
    except Exception as e:
        raise ComfyError(f"bad base64: {e}")
    pil = Image.open(BytesIO(raw))
    filename = f"pws_ref_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    target = COMFY_INPUT_DIR / filename
    pil.save(target)
    return filename


def queue_workflow(workflow: dict) -> str:
    body = {"prompt": workflow, "client_id": uuid.uuid4().hex}
    try:
        r = requests.post(f"{COMFY_URL}/prompt", json=body, timeout=10)
    except requests.RequestException as e:
        raise ComfyError(f"ComfyUI unreachable: {e}")
    if not r.ok:
        raise ComfyError(f"ComfyUI {r.status_code}: {r.text[:400]}")
    data = r.json()
    pid = data.get("prompt_id")
    if not pid:
        raise ComfyError(f"no prompt_id: {data}")
    return pid


def wait_for_history(pid: str, timeout_s: int = 600) -> dict:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            r = requests.get(f"{COMFY_URL}/history/{pid}", timeout=10)
            if r.ok:
                data = r.json()
                if pid in data:
                    return data[pid]
        except requests.RequestException:
            pass
        time.sleep(1.5)
    raise ComfyError(f"timeout after {timeout_s}s")


def fetch_image_bytes(filename: str, subfolder: str, img_type: str = "output") -> bytes:
    params = {"filename": filename, "subfolder": subfolder, "type": img_type}
    r = requests.get(f"{COMFY_URL}/view", params=params, timeout=60)
    r.raise_for_status()
    return r.content


def evict_ollama() -> None:
    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    model = os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", "")
    try:
        requests.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=10,
        )
    except Exception:
        pass


def ltx_checkpoint_present() -> bool:
    """Check if LTX-Video model file is in ComfyUI checkpoints folder."""
    candidate = Path(
        os.getenv(
            "LTX_CHECKPOINT_PATH",
            r"C:\Users\fazea\Documents\ComfyUI\models\checkpoints\ltx-video-2b-v0.9.5.safetensors",
        )
    )
    return candidate.exists()


def render_video_multi(
    prompt: str,
    refs_b64: list[str],
) -> dict:
    """Generate one LTX clip per reference image (up to 5). Returns list of results."""
    if not is_alive():
        return {"status": "error", "error": "ComfyUI unreachable at " + COMFY_URL}
    if not ltx_checkpoint_present():
        return {
            "status": "error",
            "error": (
                "LTX-Video model not found. Run:\n"
                "hf download Lightricks/LTX-Video ltx-video-2b-v0.9.5.safetensors "
                "--local-dir \"C:\\Users\\fazea\\Documents\\ComfyUI\\models\\checkpoints\""
            ),
        }
    if not refs_b64:
        return {"status": "error", "error": "upload at least one reference image"}

    evict_ollama()
    time.sleep(2)

    results: list[dict] = []
    out_dir = Path("outputs") / "manual_videos"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, b64 in enumerate(refs_b64[:5]):
        try:
            ref_filename = save_reference_b64(b64)
            seed = (int(time.time() * 1000) + i * 1009) % 2_000_000_000
            wf = ltx_i2v(prompt, ref_filename, seed=seed)
            pid = queue_workflow(wf)
            result = wait_for_history(pid, timeout_s=1200)
            outputs = result.get("outputs", {})
            found = None
            for node_out in outputs.values():
                vids = node_out.get("gifs") or node_out.get("videos") or []
                if vids:
                    v = vids[0]
                    raw = fetch_image_bytes(v["filename"], v.get("subfolder", ""), v.get("type", "output"))
                    save_path = out_dir / f"pws_clip{i+1}_{int(time.time())}.mp4"
                    save_path.write_bytes(raw)
                    found = {
                        "index": i + 1,
                        "status": "ok",
                        "video_b64": base64.b64encode(raw).decode("ascii"),
                        "seed": seed,
                        "ref_filename": ref_filename,
                        "saved": str(save_path),
                    }
                    break
            results.append(found or {"index": i + 1, "status": "error", "error": "no video in result", "ref_filename": ref_filename})
        except ComfyError as e:
            results.append({"index": i + 1, "status": "error", "error": str(e)})
        except Exception as e:
            results.append({"index": i + 1, "status": "error", "error": f"{type(e).__name__}: {e}"})

    return {
        "status": "ok",
        "prompt": prompt,
        "total": len(results),
        "completed": sum(1 for r in results if r.get("status") == "ok"),
        "clips": results,
    }


def render_image(
    model: str,
    mode: str,
    colourway: str,
    shot_type: str,
    user_text: str,
    ref_b64: str | None = None,
    strength: float = 0.65,
    evict: bool = True,
    backend: str = "comfyui",
    aspect_ratio: str = "9:16",
) -> dict:
    """One-shot image gen. Returns {status, prompt, image_b64, seed, model}.

    `backend` = "comfyui" (default, local) or "replicate" (cloud FLUX).
    """
    prompt = build_prompt(colourway, shot_type, user_text)

    # ── Cloud route ──
    if backend == "replicate":
        from cloud_render import render_flux, is_available
        if not is_available():
            return {
                "status": "error",
                "error": "Replicate not configured. Set REPLICATE_API_TOKEN env var.",
                "prompt": prompt,
            }
        cloud_model = None
        if model and model.lower() not in ("flux.1 schnell", "flux schnell", "sdxl", "sdxl turbo"):
            cloud_model = model  # allow explicit model override
        # FLUX schnell on Replicate handles t2i only — i2i needs different model path
        result = render_flux(prompt, aspect_ratio=aspect_ratio, model=cloud_model)
        if result.get("status") == "ok":
            from pathlib import Path as _P
            out_dir = _P("outputs") / "manual_renders"
            out_dir.mkdir(parents=True, exist_ok=True)
            save_path = out_dir / f"pws_cloud_{int(time.time())}.png"
            save_path.write_bytes(base64.b64decode(result["image_b64"]))
            result["saved"] = str(save_path)
            result["prompt"] = prompt
            result["backend"] = "replicate"
        return result

    # ── Local ComfyUI route (original) ──
    if not is_alive():
        return {"status": "error", "error": "ComfyUI unreachable at " + COMFY_URL}
    if evict:
        evict_ollama()
        time.sleep(2)

    prompt = build_prompt(colourway, shot_type, user_text)
    seed = int(time.time() * 1000) % 2_000_000_000

    try:
        if model == "FLUX.1 schnell":
            wf = flux_t2i(prompt, seed=seed)
            label = "FLUX.1 schnell"
        elif model == "SDXL Turbo":
            wf = sdxl_turbo(prompt, seed=seed)
            label = "SDXL Turbo"
        elif mode == "Image to image":
            if not ref_b64:
                return {"status": "error", "error": "image-to-image requires a reference image"}
            ref_filename = save_reference_b64(ref_b64)
            wf = sdxl_i2i(prompt, NEGATIVE, ref_filename, denoise=float(strength), seed=seed)
            label = "SDXL (img2img)"
        else:
            wf = sdxl_t2i(prompt, NEGATIVE, seed=seed)
            label = "SDXL"

        pid = queue_workflow(wf)
        result = wait_for_history(pid)
        outputs = result.get("outputs", {})
        for node_out in outputs.values():
            imgs = node_out.get("images") or []
            if imgs:
                img = imgs[0]
                raw = fetch_image_bytes(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
                # save to outputs/manual_renders/
                from pathlib import Path as _P
                out_dir = _P("outputs") / "manual_renders"
                out_dir.mkdir(parents=True, exist_ok=True)
                save_path = out_dir / f"pws_{int(time.time())}.png"
                save_path.write_bytes(raw)
                return {
                    "status": "ok",
                    "prompt": prompt,
                    "image_b64": base64.b64encode(raw).decode("ascii"),
                    "seed": seed,
                    "model": label,
                    "saved": str(save_path),
                }
        return {"status": "error", "error": "no image in result"}
    except ComfyError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
