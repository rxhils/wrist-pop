"""Cloud image render via Replicate (FLUX models).

Drop-in alternative to local ComfyUI. Used when:
- ComfyUI unreachable
- User picks "Cloud" in UI radio
- Pipeline render stage runs on HF Space (no GPU)

Costs (Replicate, approx GBP):
- flux-schnell:    ~£0.003 / image (fast 1-4 step)
- flux-1.1-pro:    ~£0.03 / image
- flux-pro-1.1-ultra: ~£0.06 / image
- flux-2-max:      ~£0.04 / image

Env vars:
  REPLICATE_API_TOKEN  required
  REPLICATE_FLUX_MODEL optional — overrides default model
"""
from __future__ import annotations

import base64
import os
import time
from io import BytesIO
from pathlib import Path

import requests

# Default model: flux-schnell (cheap + fast). Override via env.
DEFAULT_MODEL = os.getenv("REPLICATE_FLUX_MODEL", "black-forest-labs/flux-schnell")

# Aspect ratio → Replicate input mapping
ASPECT_MAP = {
    "9:16": "9:16",
    "1:1": "1:1",
    "4:5": "4:5",
    "16:9": "16:9",
    "3:4": "3:4",
}


def _client():
    try:
        import replicate
    except ImportError as e:
        raise RuntimeError("Replicate SDK not installed. pip install replicate") from e
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise RuntimeError("REPLICATE_API_TOKEN env var missing")
    return replicate


def is_available() -> bool:
    """Quick check — env var present + module importable."""
    if not os.getenv("REPLICATE_API_TOKEN"):
        return False
    try:
        import replicate  # noqa: F401
        return True
    except ImportError:
        return False


def render_flux(
    prompt: str,
    aspect_ratio: str = "9:16",
    model: str | None = None,
    output_format: str = "png",
    guidance_scale: float = 3.5,
    seed: int | None = None,
) -> dict:
    """Generate one image via Replicate FLUX. Returns dict with image_b64 + meta.

    Aspect ratio: 9:16 (default, for IG Reels/TikTok), 1:1, 4:5, 16:9, 3:4.
    Returns {status, image_b64, url, model, seed, elapsed_s} or {status: error, error}.
    """
    if not is_available():
        return {"status": "error", "error": "REPLICATE_API_TOKEN missing or replicate SDK not installed"}

    chosen_model = model or DEFAULT_MODEL
    chosen_aspect = ASPECT_MAP.get(aspect_ratio, "9:16")
    started = time.time()

    inputs: dict = {
        "prompt": prompt,
        "aspect_ratio": chosen_aspect,
        "output_format": output_format,
    }
    # flux-schnell ignores guidance_scale, pro models use it
    if "pro" in chosen_model or "ultra" in chosen_model or "flux-2" in chosen_model:
        inputs["guidance_scale"] = guidance_scale
    if seed is not None:
        inputs["seed"] = int(seed)

    try:
        replicate = _client()
        # Replicate `run` returns iterator of FileOutput objects for newer models
        output = replicate.run(chosen_model, input=inputs)
    except Exception as e:
        return {"status": "error", "error": f"replicate api: {e}", "model": chosen_model}

    # Normalise output shape — Replicate returns list[FileOutput], list[str url], or single
    url: str | None = None
    if isinstance(output, list) and output:
        first = output[0]
        url = str(first) if hasattr(first, "__str__") else None
    elif isinstance(output, (str, bytes)):
        url = str(output)
    elif hasattr(output, "url"):
        url = output.url

    if not url:
        return {"status": "error", "error": f"no URL in Replicate output: {type(output).__name__}", "model": chosen_model}

    # Fetch the image bytes
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        img_bytes = r.content
    except Exception as e:
        return {"status": "error", "error": f"fetch image: {e}", "url": url}

    elapsed = round(time.time() - started, 1)
    return {
        "status": "ok",
        "image_b64": base64.b64encode(img_bytes).decode("ascii"),
        "url": url,
        "model": chosen_model,
        "seed": inputs.get("seed"),
        "elapsed_s": elapsed,
    }
