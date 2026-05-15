"""Multi-provider LLM client.

Routes calls to Groq / Mistral / Gemini / Ollama via a single function.
Existing per-stage `run_*.py` modules can keep their Ollama-specific options
(num_ctx, format=json, keep_alive) by routing through Ollama directly; this
module is for the cross-provider abstraction used by `llm_call()`.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests

# Lazy imports — only load SDKs we actually need
_openai_client_cls = None
_genai_module = None


PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "models": {
            "fast":     "llama-3.3-70b-versatile",
            "smart":    "qwen-qwq-32b",
            "balanced": "llama-3.1-8b-instant",
        },
        "default_model": "llama-3.3-70b-versatile",
        "supports_json_response_format": True,
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "models": {
            "fast":     "mistral-small-latest",
            "smart":    "mistral-large-latest",
            "balanced": "ministral-8b-latest",
        },
        "default_model": "mistral-large-latest",
        "supports_json_response_format": True,
    },
    "gemini": {
        "base_url": None,  # uses google sdk
        "api_key_env": "GEMINI_API_KEY",
        "models": {
            "fast":     "gemini-2.0-flash",
            "smart":     "gemini-2.5-pro",
            "balanced": "gemini-2.0-flash",
        },
        "default_model": "gemini-2.0-flash",
        "supports_json_response_format": True,
    },
    "ollama": {
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        "api_key_env": None,
        "models": {
            "fast":     "llama3.1:latest",
            "smart":    "qwen2.5:14b",
            "balanced": "qwen2.5:7b",
        },
        "default_model": os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", ""),
        "supports_json_response_format": True,  # via native /api/chat format param
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "models": {
            "gpt_oss_120b_free":   "openai/gpt-oss-120b:free",
            "gpt_oss_20b_free":    "openai/gpt-oss-20b:free",
            "qwen3_80b_free":      "qwen/qwen3-next-80b-a3b-instruct:free",
            "gemma_31b_free":      "google/gemma-4-31b-it:free",
            "deepseek_v4_free":    "deepseek/deepseek-v4-flash:free",
            "nemotron_120b_free":  "nvidia/nemotron-3-super-120b-a12b:free",
            "glm_4_5_free":        "z-ai/glm-4.5-air:free",
            "claude_paid":         "anthropic/claude-3.5-sonnet",
            "gpt4o_paid":          "openai/gpt-4o",
        },
        "default_model": "openai/gpt-oss-120b:free",
        "supports_json_response_format": True,
    },
}


def _get_openai_class():
    global _openai_client_cls
    if _openai_client_cls is None:
        from openai import OpenAI
        _openai_client_cls = OpenAI
    return _openai_client_cls


def _get_genai():
    global _genai_module
    if _genai_module is None:
        import google.generativeai as genai
        _genai_module = genai
    return _genai_module


def call_llm(
    system_prompt: str,
    user_prompt: str,
    provider: str = "ollama",
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2048,
    json_mode: bool = False,
    num_ctx: int = 8192,
    timeout: int = 420,
    max_retries: int = 2,
) -> str:
    """Universal LLM call. Returns plain text or JSON string (if json_mode=True).

    `num_ctx` is Ollama-only. `max_tokens` works for all providers (Gemini=max_output_tokens).
    Retries on 429 rate-limit with exponential backoff.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return _call_llm_once(
                system_prompt, user_prompt, provider, model, temperature,
                max_tokens, json_mode, num_ctx, timeout,
            )
        except Exception as e:
            msg = str(e).lower()
            is_rate_limit = (
                "429" in msg or "rate" in msg and "limit" in msg
                or "quota" in msg
            )
            if not is_rate_limit or attempt == max_retries - 1:
                raise
            backoff = [10, 30, 60, 90][min(attempt, 3)]  # 10, 30, 60, 90 sec
            print(f"[providers] rate-limit hit ({provider}). Backing off {backoff}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(backoff)
            last_exc = e
    if last_exc:
        raise last_exc
    raise RuntimeError("call_llm: unreachable")


def _call_llm_once(
    system_prompt: str,
    user_prompt: str,
    provider: str,
    model: Optional[str],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    num_ctx: int,
    timeout: int,
) -> str:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        raise ValueError(f"Unknown provider '{provider}'. Choices: {list(PROVIDERS.keys())}")

    resolved_model = model or cfg["default_model"]

    # ── Ollama via /api/chat (preserve existing behaviour with format + num_ctx) ──
    if provider == "ollama":
        base = cfg["base_url"]
        body: dict = {
            "model": resolved_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
            "keep_alive": "10m",
        }
        if json_mode:
            body["format"] = "json"
        r = requests.post(f"{base}/api/chat", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()["message"]["content"]

    # ── Gemini (own SDK) ──
    if provider == "gemini":
        api_key = os.getenv(cfg["api_key_env"])
        if not api_key:
            raise EnvironmentError(f"Missing env var: {cfg['api_key_env']}")
        genai = _get_genai()
        genai.configure(api_key=api_key)
        gen_cfg_kwargs: dict = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if json_mode:
            gen_cfg_kwargs["response_mime_type"] = "application/json"
        gen_cfg = genai.types.GenerationConfig(**gen_cfg_kwargs)
        gem_model = genai.GenerativeModel(
            model_name=resolved_model,
            system_instruction=system_prompt,
        )
        resp = gem_model.generate_content(user_prompt, generation_config=gen_cfg)
        return resp.text.strip()

    # ── Groq / Mistral (OpenAI-compatible) ──
    api_key = os.getenv(cfg["api_key_env"]) if cfg["api_key_env"] else None
    if cfg["api_key_env"] and not api_key:
        raise EnvironmentError(f"Missing env var: {cfg['api_key_env']}")
    OpenAI = _get_openai_class()
    client = OpenAI(base_url=cfg["base_url"], api_key=api_key)
    kwargs: dict = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode and cfg.get("supports_json_response_format"):
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()


# ─────────────────────────────────────────────────────────────────────────
# Universal entry point used by existing run_*.py modules.
# Reads pipeline_config.AGENT_CONFIG for the agent's assigned provider.
# Falls back to Ollama default if no config / env override present.
# ─────────────────────────────────────────────────────────────────────────
def llm_call(
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = True,
    num_ctx: int = 8192,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Look up the agent's provider config + call. Args override config when given."""
    try:
        from pipeline_config import AGENT_CONFIG
        cfg = AGENT_CONFIG.get(agent_name, {})
    except ImportError:
        cfg = {}

    provider = cfg.get("provider", "ollama")
    model = cfg.get("model")
    if temperature is None:
        temperature = cfg.get("temperature", 0.4)
    if max_tokens is None:
        max_tokens = cfg.get("max_tokens", 2048)

    # Allow env override of provider for testing
    env_provider = os.getenv("PWS_PROVIDER_OVERRIDE")
    if env_provider:
        provider = env_provider
        model = os.getenv("PWS_MODEL_OVERRIDE") or None

    return call_llm(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        provider=provider,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        num_ctx=num_ctx,
    )


def has_key(provider: str) -> bool:
    """Helper for UI / health checks."""
    cfg = PROVIDERS.get(provider, {})
    env = cfg.get("api_key_env")
    if not env:
        return True  # ollama
    return bool(os.getenv(env))
