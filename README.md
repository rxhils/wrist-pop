---
title: Royal Pop Studio
emoji: ⚙️
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Pop Wrist Studio — Agentic Content System

8-stage multi-agent pipeline. Trend research → strategy → copy → quality gate → visual brief → schedule → image render → video render. All local Ollama (`qwen2.5:14b`) + ComfyUI (FLUX.1 schnell + LTX-Video). Zero API cost.

## Pipeline stages

| # | Stage | File | LLM | Notes |
|---|-------|------|-----|-------|
| 1 | Scout | `run_scout.py` | qwen2.5:14b | pytrends + ddgs → `trend_report` |
| 2 | Strategist | `run_strategist.py` | qwen2.5:14b | trend → 3–5 ideas → `content_brief` |
| 3 | Writer | `run_writer.py` | qwen2.5:14b | idea → script → `copy` |
| 4 | Gate | `run_gate.py` | qwen2.5:14b | hard block + advisory soft → `approved_copy` |
| 5 | Visual | `run_visual.py` | qwen2.5:14b | shot list + FLUX/LTX prompts → `visual_brief` |
| 6 | Scheduler | `run_scheduler.py` | none | time slots + Notion payload → `schedule` + `digest.md` |
| 7 | Image Render | `run_render_image.py` | none | FLUX.1 schnell via ComfyUI :8188 → `outputs/images/<date>/*.png` |
| 8 | Video Render | `run_render_video.py` | none | LTX-Video 2.3 via ComfyUI :8188 → `outputs/videos/<date>/*.mp4` |

## Run

### Full pipeline
```powershell
docker compose run --rm agent python run_pipeline.py
```

### Single stage
```powershell
docker compose run --rm agent python run_pipeline.py --only writer
docker compose run --rm agent python run_pipeline.py --from gate
```

### Direct agent run with custom input
```powershell
# Strategist using a hand-edited trend report
docker compose run --rm agent python run_strategist.py --input my_trends.json --output my_brief.json

# Writer on one specific idea (priority 1)
docker compose run --rm agent python run_writer.py --pick 1

# Visual brief for one approved piece
docker compose run --rm agent python run_visual.py --pick 2

# Image render for one shot only
docker compose run --rm agent python run_render_image.py --pick 1 --shot 3

# Scheduler from custom approved + visual files
docker compose run --rm agent python run_scheduler.py --approved approved.json --visual visual.json
```

### Interactive chat with an agent
Load that agent's system prompt + multi-turn REPL on qwen2.5:14b.

```powershell
docker compose run --rm agent python chat.py scout
docker compose run --rm agent python chat.py strategist --json
docker compose run --rm agent python chat.py writer
docker compose run --rm agent python chat.py gate
docker compose run --rm agent python chat.py visual
docker compose run --rm agent python chat.py scheduler   # meta-prompt (deterministic stage)
docker compose run --rm agent python chat.py render_image
docker compose run --rm agent python chat.py render_video
```

In-REPL commands:
| Command | Effect |
|---------|--------|
| `/reset` | drop conversation history, keep system prompt |
| `/system` | print the loaded system prompt |
| `/save outputs/test.json` | save last reply to file |
| `/input outputs/trend_report_<date>.json` | load file as next user message |
| `/json` | toggle JSON response mode |
| `/quit` | exit |

Empty line submits multi-line buffer.

## Daily outputs

```
outputs/<date>/
├── raw_signals_<date>.json       Scout raw tool dumps
├── trend_report_<date>.json      Scout final
├── content_brief_<date>.json     Strategist
├── copy_<date>.json              Writer draft
├── approved_copy_<date>.json     Gate-approved (+ advisory notes)
├── visual_brief_<date>.json      Shot lists + AI prompts
├── schedule_<date>.json          Time-slotted calendar
├── notion_payload_<date>.json    Ready for Notion MCP
├── digest_<date>.md              Slack/email markdown
├── images/<date>/P*_shot*.png    FLUX renders (Stage 7)
└── videos/<date>/P*_shot*.mp4    LTX-Video renders (Stage 8)
```

## Setup

### One-time
```powershell
cd "C:\claude\Royal pop\05-agents\content-system"
copy .env.example .env
docker compose build agent
ollama pull qwen2.5:14b
```

### ComfyUI (for stages 7+8)
1. Install ComfyUI portable for Windows
2. Custom node: `ComfyUI-LTXVideo` (Lightricks), `ComfyUI-VideoHelperSuite`
3. Weights:
   - `flux1-schnell.safetensors` → `ComfyUI/models/unet/`
   - `ae.safetensors` → `ComfyUI/models/vae/`
   - `clip_l.safetensors` + `t5xxl_fp8_e4m3fn.safetensors` → `ComfyUI/models/clip/`
   - `ltx-video-2b-v0.9.5.safetensors` → `ComfyUI/models/checkpoints/`
4. Start: `python main.py --listen 0.0.0.0` (so Docker reaches it)

Render agents auto-skip if ComfyUI :8188 unreachable. Ollama evicted from VRAM before each render call (`keep_alive=0`).

## Models

| Model | Size | Role |
|-------|------|------|
| `qwen2.5:14b` | 8.4GB | All LLM stages (1–5) |
| `llama3.1:8b` | 4.6GB | Idle backup |
| FLUX.1 schnell (Q4) | ~6GB VRAM | Stage 7 |
| LTX-Video 2.3 | ~6-8GB VRAM | Stage 8 |

VRAM budget: 8GB on RTX 4060 Laptop. Pipeline runs Ollama OR ComfyUI, never both. Eviction handled automatically.

## Build status
- [x] Stage 1–6: agents shipped, pipeline tested 5/5 pass
- [x] Stage 7–8: render agents shipped, auto-skip when ComfyUI down
- [x] Chat REPL per agent
- [x] `--input` / `--output` flags on all 8 stages
- [ ] Reddit PRAW signal
- [ ] Daily cron 6am
- [ ] Slack approval + Buffer push
- [ ] Gradio UI dashboard
