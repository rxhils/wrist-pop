"""Cloud video generation wrappers — fal.ai primary, Replicate fallback.

Three production models exposed:
  - ltx_draft  : Lightricks LTX-Video 0.9.8 13B distilled (i2v) — fastest, cheapest, iterate
  - wan_hero   : Alibaba Wan 2.2 i2v A14B 720p — hero quality
  - ltx2_4k    : Lightricks LTX-2 — optional final upscale

Public API:
    generate(model_key, prompt, *, init_image_path=None, init_image_url=None,
             duration_s=5, aspect='9:16', negative_prompt=None, seed=None)
        -> { 'video_url': str, 'model': str, 'cost_usd': float,
             'latency_s': float, 'request_id': str, 'job_log': list[str] }

All exceptions wrapped into RuntimeError with descriptive context.
"""
from __future__ import annotations

import os
import time
from typing import Optional

MODEL_REGISTRY: dict[str, dict] = {
    "ltx_draft": {
        "label": "LTX-Video 0.9 (draft)",
        "fal_slug": "fal-ai/ltx-video/image-to-video",
        "fal_t2v_slug": "fal-ai/ltx-video",
        "default_aspect": "9:16",
        "max_duration_s": 5,
        "cost_per_clip_estimate": 0.02,
        "supports_i2v": True,
        "supports_t2v": True,
        "use_for": "fast iteration · $0.02 flat per clip",
    },
    "wan_hero": {
        "label": "Wan 2.7 (hero · 1080p · 15s · audio)",
        "fal_slug": "fal-ai/wan/v2.7/image-to-video",
        "fal_t2v_slug": "fal-ai/wan/v2.7/text-to-video",
        "default_aspect": "9:16",
        "max_duration_s": 15,
        "cost_per_clip_estimate": 0.75,  # 5s @ $0.15
        "supports_i2v": True,
        "supports_t2v": True,
        "use_for": "best quality · 1080p $0.15/s · up to 15s",
    },
    "wan_flash": {
        "label": "Wan 2.6 flash (cheap bulk · 1080p)",
        "fal_slug": "fal-ai/wan/v2.6/image-to-video/flash",
        "fal_t2v_slug": "fal-ai/wan/v2.6/image-to-video/flash",
        "default_aspect": "9:16",
        "max_duration_s": 15,
        "cost_per_clip_estimate": 0.25,  # 5s @ $0.05
        "supports_i2v": True,
        "supports_t2v": False,
        "use_for": "bulk iteration · 1080p $0.05/s",
    },
    "ltx2_4k": {
        "label": "LTX-2 (1080p / 4K final)",
        "fal_slug": "fal-ai/ltx-2/image-to-video",
        "fal_t2v_slug": "fal-ai/ltx-2/text-to-video",
        "default_aspect": "9:16",
        "max_duration_s": 10,
        "cost_per_clip_estimate": 0.60,
        "supports_i2v": True,
        "supports_t2v": True,
        "use_for": "final cut · 1080p $0.06/s · audio optional",
    },
}


def _build_args_for_model(
    model_key: str,
    *,
    is_i2v: bool,
    prompt: str,
    image_url: Optional[str],
    duration_s: int,
    aspect: str,
    negative_prompt: Optional[str],
    seed: Optional[int],
    extra: Optional[dict],
) -> dict:
    """Per-model argument shaping. Each fal slug accepts different params."""
    args: dict = {"prompt": prompt[:1500]}
    if image_url:
        args["image_url"] = image_url
    if seed is not None:
        args["seed"] = seed

    if model_key == "ltx_draft":
        # LTX 0.9 supports prompt, guidance_scale, inference_steps, seed.
        # Output dimensions inherited from init image for i2v.
        if negative_prompt:
            args["negative_prompt"] = negative_prompt[:500]
    elif model_key in ("wan_hero", "wan_flash"):
        # Wan 2.6/2.7: prompt, aspect_ratio, duration, resolution.
        args["aspect_ratio"] = aspect
        args["duration"] = max(5, min(15, duration_s))
        args["resolution"] = "1080p"
        if negative_prompt:
            args["negative_prompt"] = negative_prompt[:500]
    elif model_key == "ltx2_4k":
        # LTX-2: resolution (1080p/1440p/2160p), duration (6/8/10), aspect_ratio.
        clamped_dur = max(6, min(10, duration_s))
        # snap to discrete supported values
        if clamped_dur < 7:   clamped_dur = 6
        elif clamped_dur < 9: clamped_dur = 8
        else:                 clamped_dur = 10
        args["duration"] = clamped_dur
        args["resolution"] = "1080p"
        args["aspect_ratio"] = aspect
        args["audio"] = False
    if extra:
        args.update(extra)
    return args


def is_configured() -> bool:
    """True only when FAL_KEY is set. Replicate fallback not yet implemented —
    do NOT return true on REPLICATE_API_TOKEN alone, otherwise the UI reports
    'ready' but every generate raises 'FAL_KEY missing'.
    """
    return bool(os.getenv("FAL_KEY"))


def list_models() -> list[dict]:
    return [{"key": k, **v} for k, v in MODEL_REGISTRY.items()]


def _aspect_to_dims(aspect: str) -> tuple[int, int]:
    # Used only when provider accepts width/height instead of aspect_ratio string.
    return {
        "9:16": (720, 1280),
        "1:1":  (768, 768),
        "16:9": (1280, 720),
        "4:5":  (720, 900),
    }.get(aspect, (720, 1280))


# ───────────────────────── fal.ai client ─────────────────────────
def _fal_submit(
    slug: str,
    arguments: dict,
    *,
    timeout: int = 600,
) -> tuple[dict, list[str]]:
    """Submit a fal job + poll until complete. Returns (result_dict, log_lines)."""
    if not os.getenv("FAL_KEY"):
        raise RuntimeError("FAL_KEY missing in .env — sign up at https://fal.ai")
    try:
        import fal_client
    except ImportError as e:
        raise RuntimeError(f"fal-client not installed: {e}")

    logs: list[str] = []

    def _on_queue(update):
        if hasattr(update, "logs") and update.logs:
            for entry in update.logs:
                msg = entry.get("message") if isinstance(entry, dict) else str(entry)
                if msg:
                    logs.append(msg)

    try:
        result = fal_client.subscribe(
            slug,
            arguments=arguments,
            with_logs=True,
            on_queue_update=_on_queue,
        )
    except Exception as e:
        raise RuntimeError(f"fal.ai submit failed for {slug}: {e}") from e
    return result, logs


def _extract_video_url(result: dict) -> str | None:
    if not isinstance(result, dict):
        return None
    v = result.get("video")
    if isinstance(v, dict) and v.get("url"):
        return v["url"]
    if isinstance(v, str) and v.startswith("http"):
        return v
    if isinstance(result.get("url"), str):
        return result["url"]
    # Some models return videos[]
    videos = result.get("videos")
    if isinstance(videos, list) and videos:
        first = videos[0]
        if isinstance(first, dict) and first.get("url"):
            return first["url"]
        if isinstance(first, str):
            return first
    return None


# ───────────────────────── Public ─────────────────────────
def generate(
    model_key: str,
    prompt: str,
    *,
    init_image_path: Optional[str] = None,
    init_image_url: Optional[str] = None,
    duration_s: int = 5,
    aspect: str = "9:16",
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    extra: Optional[dict] = None,
) -> dict:
    cfg = MODEL_REGISTRY.get(model_key)
    if not cfg:
        raise RuntimeError(f"unknown model_key '{model_key}'. Choices: {list(MODEL_REGISTRY)}")

    is_i2v = bool(init_image_path or init_image_url)
    if is_i2v and not cfg.get("supports_i2v"):
        raise RuntimeError(f"{model_key} does not support i2v")
    if not is_i2v and not cfg.get("supports_t2v"):
        raise RuntimeError(f"{model_key} does not support t2v")

    slug = cfg["fal_slug"] if is_i2v else cfg["fal_t2v_slug"]

    # Upload init image to fal storage if path given (so URL is reachable)
    image_url = init_image_url
    if init_image_path and not image_url:
        try:
            import fal_client
            image_url = fal_client.upload_file(init_image_path)
        except Exception as e:
            raise RuntimeError(f"fal upload failed for {init_image_path}: {e}")

    args = _build_args_for_model(
        model_key,
        is_i2v=is_i2v,
        prompt=prompt,
        image_url=image_url,
        duration_s=duration_s,
        aspect=aspect,
        negative_prompt=negative_prompt,
        seed=seed,
        extra=extra,
    )

    t0 = time.time()
    result, logs = _fal_submit(slug, args)
    latency = time.time() - t0

    video_url = _extract_video_url(result)
    if not video_url:
        raise RuntimeError(f"no video URL in fal response: {result!r}"[:500])

    # Actual cost calc per model — always use the ACTUAL duration sent to fal
    # (caller may have asked for 60s but model clamps to 15s).
    actual_dur = args.get("duration", duration_s)
    if model_key == "wan_hero":
        cost = 0.15 * actual_dur    # 1080p tier
    elif model_key == "wan_flash":
        cost = 0.05 * actual_dur
    elif model_key == "ltx2_4k":
        cost = 0.06 * actual_dur
    else:
        cost = cfg["cost_per_clip_estimate"]

    return {
        "video_url": video_url,
        "model": model_key,
        "label": cfg["label"],
        "cost_usd": round(cost, 3),
        "latency_s": round(latency, 1),
        "request_id": (result.get("request_id") if isinstance(result, dict) else None),
        "raw_result": result,
        "job_log": logs[-30:],
        "args": {k: v for k, v in args.items() if k != "image_url" or len(str(v)) < 200},
    }
