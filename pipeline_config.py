"""Per-agent provider + model config.

Maps existing agent stage IDs to a provider. Each `run_*.py` reads its own
entry via `providers.llm_call(agent_name=...)`.

Override globally with --provider CLI flag or PWS_PROVIDER_OVERRIDE env var.
"""

AGENT_CONFIG = {
    "scout": {
        "provider": "mistral",
        "model":    "mistral-small-latest",
        "temperature": 0.2,
        "max_tokens": 8192,
        "description": "Synthesises tool outputs into urgency-ranked trend report.",
    },
    "strategist": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.6,
        "max_tokens": 8192,
        "description": "Trend report + project context → 3-5 reel ideas.",
    },
    "writer": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.55,
        "max_tokens": 8192,
        "description": "Idea → full script + caption + hashtags + B-roll.",
    },
    "gate": {
        "provider": "mistral",
        "model":    "mistral-small-latest",
        "temperature": 0.2,
        "max_tokens": 4096,
        "description": "Soft brand-voice review (hard checks are deterministic regex).",
    },
    "visual": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.55,
        "max_tokens": 8192,
        "description": "Shot list + FLUX/LTX prompts per piece.",
    },
    "scheduler": {
        "provider": "ollama",
        "model":    None,
        "temperature": 0.0,
        "max_tokens": 0,
        "description": "Deterministic Python — no LLM call.",
    },
    "asset_director": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.4,
        "max_tokens": 8192,
        "description": "Plans stills + motion clips before Visual Brief; minimum-viable-first.",
    },
    "manual_reel": {
        "provider": "ollama",
        "model":    None,
        "temperature": 0.0,
        "max_tokens": 0,
        "description": "Human-only step — status tracker, no LLM.",
    },
    "manual_post": {
        "provider": "ollama",
        "model":    None,
        "temperature": 0.0,
        "max_tokens": 0,
        "description": "Human-only step — status tracker, no LLM.",
    },
    "output_director": {
        "provider": "openrouter",
        "model":    "openai/gpt-oss-120b:free",
        "temperature": 0.3,
        "max_tokens": 8192,
        "description": "Synthesises all upstream artifacts into operator console.",
    },
    "reel_director": {
        "provider": "openrouter",
        "model":    "openai/gpt-oss-120b:free",
        "temperature": 0.55,
        "max_tokens": 12288,
        "description": "Generates 5 ranked reel/TikTok ideas from full pipeline output (qwen3-80b is preferred but rate-limited; gpt-oss-120b is reliable fallback).",
    },
}


def set_all_agents(provider: str, model: str | None = None) -> None:
    for agent in AGENT_CONFIG.values():
        agent["provider"] = provider
        if model is not None:
            agent["model"] = model
    print(f"[pipeline_config] all agents → {provider} / {model or 'default'}")


def reset_to_defaults() -> None:
    import importlib, sys
    importlib.reload(sys.modules[__name__])
