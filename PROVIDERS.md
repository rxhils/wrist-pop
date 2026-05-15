# Multi-Provider LLM Routing

Existing pipeline now routes per-agent LLM calls through any of:
- **Groq** (free, fast — Llama 3.3 70B, Qwen QwQ 32B, Llama 3.1 8B)
- **Mistral** (free tier — Mistral Large, Small, Ministral 8B)
- **Gemini** (free tier — 2.0 Flash, 2.5 Pro)
- **Ollama** (local — qwen2.5:14b, llama3.1:8b)

**Why:** offload text agents to free cloud APIs → frees local RAM for ComfyUI renders.

## Files

| File | Role |
|------|------|
| `providers.py` | unified `call_llm()` + `llm_call(agent_name=...)` entry point |
| `pipeline_config.py` | per-agent provider/model assignments |
| `.env` | API keys (see `.env.example`) |

## Default assignments

| Agent | Provider | Model | Why |
|-------|----------|-------|-----|
| scout | groq | llama-3.3-70b-versatile | fast research synthesis |
| strategist | groq | llama-3.3-70b-versatile | speed + 70B quality |
| writer | mistral | mistral-large-latest | best writing quality |
| gate | groq | llama-3.1-8b-instant | cheap rule checker |
| visual | gemini | gemini-2.5-pro | long context for shot lists |
| scheduler | (none, deterministic) | — | no LLM call |

Override any one in `pipeline_config.py`. Or override all temporarily via CLI.

## Get API keys (5 min total)

| Provider | Sign up | Free tier |
|----------|---------|-----------|
| Groq | https://console.groq.com/keys | 30 req/min on 70B |
| Mistral | https://console.mistral.ai/ | generous free tier |
| Gemini | https://aistudio.google.com/apikey | 15 RPM Flash, 2 RPM Pro |

Paste keys into `.env`:
```
GROQ_API_KEY=gsk_...
MISTRAL_API_KEY=...
GEMINI_API_KEY=AIza...
```

If a key is missing, that agent will error — set provider to `ollama` in
`pipeline_config.py` for that agent OR pass `--provider ollama` to fall back.

## CLI usage

```powershell
# default — uses per-agent config (Groq + Mistral + Gemini mix)
python run_pipeline.py

# inspect current assignments
python run_pipeline.py --list-config

# override ALL agents to one provider for this run
python run_pipeline.py --provider groq
python run_pipeline.py --provider ollama       # fully local (high RAM)
python run_pipeline.py --provider mistral --model mistral-large-latest

# combine with stage selection
python run_pipeline.py --provider groq --only strategist
python run_pipeline.py --from gate --provider ollama

# single agent direct
python run_strategist.py    # reads pipeline_config for provider
```

## Env override (no CLI)

```powershell
$env:PWS_PROVIDER_OVERRIDE = "groq"   # routes all agents through Groq
$env:PWS_MODEL_OVERRIDE = "llama-3.3-70b-versatile"
python run_pipeline.py
```

## Backward compatibility

- All previous behaviour preserved when keys are absent
- `MODEL_CREATIVE` env var still respected as Ollama default
- All Ollama-specific options (num_ctx, format=json, keep_alive) preserved when routing to Ollama
- Cloud providers get equivalent JSON-mode through `response_format` / `response_mime_type`

## How it works inside each agent

```python
from providers import llm_call

content = llm_call(
    agent_name="strategist",   # <-- looks up pipeline_config.AGENT_CONFIG[name]
    system_prompt=...,
    user_prompt=...,
    json_mode=True,
    num_ctx=8192,              # Ollama-only, ignored for cloud
)
```

`agent_name` keys: `scout`, `strategist`, `writer`, `gate`, `visual` (`scheduler` deterministic).

## Verify keys live

```powershell
python -c "from providers import has_key; print({p: has_key(p) for p in ['groq','mistral','gemini','ollama']})"
```

## Cost: still £0 monthly

All three cloud providers have generous free tiers covering daily pipeline runs:
- Groq: ~14,400 req/day at 70B
- Mistral: free tier (limits per current ToS)
- Gemini: ~21,600 req/day at Flash, ~2,880 at Pro

Royal Pop pipeline = ~15 LLM calls per full run. Plenty of headroom.
