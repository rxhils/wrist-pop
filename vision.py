"""Vision Analyst — analyse uploaded product images via Ollama vision models.

Used to enrich Visual Brief + Image Render prompts with concrete details
extracted from real photos.
"""
from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

VISION_MODEL_NAMES = ("qwen2.5vl", "llama3.2-vision", "llava", "gemma3", "minicpm-v")
DEFAULT_PREFERENCE = ("qwen2.5vl", "llama3.2-vision", "llava")

VISION_SYSTEM = (
    "You are a product photography analyst for Pop Wrist Studio, a luxury watch "
    "accessory brand. Analyse product images and describe them in structured "
    "detail for use in AI image generation prompts. Return ONLY valid JSON."
)

ANALYSIS_PROMPT = """Analyse this product image. Return ONLY a JSON object with these fields:

{
  "colourway": "Monochrome | Huit Blanc | Green Eight | Unknown",
  "materials": "describe visible materials — strap texture, metal finish, case material",
  "lighting": "describe lighting — studio | natural | hard | soft, direction",
  "background": "describe background — colour, texture, gradient",
  "composition": "describe shot — hero float, wrist shot, close-up, angle",
  "key_details": "3-5 specific visual details that make this image distinctive",
  "flux_prompt_addition": "10-15 word phrase to enhance an AI image prompt based on this image",
  "quality_score": "high | medium | low",
  "notes": "anything unusual or worth noting"
}

Return ONLY the JSON object. No preamble, no markdown fences."""


def get_available_vision_models() -> list[str]:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=20)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        return [m for m in models if any(v in m.lower() for v in VISION_MODEL_NAMES)]
    except Exception as e:
        print(f"[vision] tags fetch failed: {e}", flush=True)
        return []


def pick_default_model() -> str | None:
    available = get_available_vision_models()
    for pref in DEFAULT_PREFERENCE:
        for m in available:
            if pref in m.lower():
                return m
    return available[0] if available else None


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    return s.strip()


def analyse_image_b64(image_b64: str, model: str = "qwen2.5vl") -> dict:
    """Send a base64 image to an Ollama vision model. Return parsed JSON."""
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": ANALYSIS_PROMPT, "images": [image_b64]},
        ],
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "keep_alive": "5m",
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=120)
        r.raise_for_status()
        raw = r.json()["message"]["content"]
    except requests.RequestException as e:
        return {"error": f"ollama: {e}"}

    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"raw_description": raw[:1500], "error": "could not parse as JSON"}


def analyse_batch(images_b64: list[str], model: str = "qwen2.5vl") -> list[dict]:
    results: list[dict] = []
    for i, b64 in enumerate(images_b64[:5]):
        try:
            data = analyse_image_b64(b64, model)
            data["index"] = i + 1
        except Exception as e:
            data = {"index": i + 1, "error": str(e)}
        results.append(data)
    return results


def build_context_summary(analyses: list[dict]) -> str:
    """Combine multiple analyses into a prompt-builder-friendly summary."""
    lines: list[str] = []
    for a in analyses:
        if a.get("error") and not a.get("raw_description"):
            lines.append(f"Image {a.get('index', '?')}: [analysis failed — {a['error']}]")
            continue
        cw = a.get("colourway", "unknown")
        mat = a.get("materials", "")
        lig = a.get("lighting", "")
        det = a.get("key_details", "")
        add = a.get("flux_prompt_addition", "")
        lines.append(
            f"Image {a.get('index', '?')} ({cw}): {mat}. "
            f"Lighting: {lig}. Key: {det}. Prompt addition: {add}"
        )
    return "\n".join(lines)
