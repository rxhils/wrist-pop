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
        "max_tokens": 2048,
        "description": "Synthesises tool outputs into urgency-ranked trend report.",
    },
    "strategist": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.6,
        "max_tokens": 2048,
        "description": "Trend report + project context → 3-5 reel ideas.",
    },
    "writer": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.55,
        "max_tokens": 2048,
        "description": "Idea → full script + caption + hashtags + B-roll.",
    },
    "gate": {
        "provider": "mistral",
        "model":    "mistral-small-latest",
        "temperature": 0.2,
        "max_tokens": 1024,
        "description": "Soft brand-voice review (hard checks are deterministic regex).",
    },
    "visual": {
        "provider": "mistral",
        "model":    "mistral-large-latest",
        "temperature": 0.55,
        "max_tokens": 4096,
        "description": "Shot list + FLUX/LTX prompts per piece.",
    },
    "scheduler": {
        "provider": "ollama",
        "model":    None,
        "temperature": 0.0,
        "max_tokens": 0,
        "description": "Deterministic Python — no LLM call.",
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
