# Open-WebUI Integration

3 files. Paste each into Open-WebUI Workspace.

## Bring up the stack

```powershell
cd "C:\claude\Royal pop\05-agents\content-system"
docker compose up -d
```

This starts:
- `pws-agent` (FastAPI :8000)
- `pws-openwebui` (Open-WebUI :3000)

First Open-WebUI load takes ~30s to migrate DB. Open `http://localhost:3000`.

## 1 — Install the Tool

1. Workspace → **Tools** → `+` (top right).
2. Paste contents of `royal_pop_tool.py`.
3. Name: `Royal Pop Content Pipeline`. Save.
4. In any chat: click the tool icon (bottom of composer) → enable
   `Royal Pop Content Pipeline`.

The LLM can now call any of 13 methods (run_scout, run_writer, run_pipeline,
get_latest_output, evict_ollama, etc.) when your message implies it.

## 2 — Install the Pipe (custom model entries)

1. Workspace → **Functions** → `+`.
2. Paste contents of `royal_pop_pipe.py`. Save → enable toggle.
3. New chat → model dropdown shows **9 new entries** under "Royal Pop":
   - `Pipeline (auto)` — orchestrator mode (combine with the Tool above)
   - `Scout` `Strategist` `Writer` `Gate` `Visual Brief` `Scheduler` `Image Render` `Video Render` — each routes the message to that stage directly

Per-stage entries are "press-button" style: any message you send while that
model is selected fires the stage. Supports inline flags in the message:
```
pick=2     # only priority 2
shot=3     # only shot id 3 (render stages)
from=gate  # pipeline --from gate
only=writer
```

## 3 — Install the System Prompt (optional, recommended)

1. Workspace → **Models** → pick `qwen2.5:14b` → Edit.
2. Paste contents of `system_prompt.md` into the `System Prompt` field. Save.

OR per-chat: in any chat with the Tool enabled, go to Chat Controls (top right)
→ System Prompt → paste.

## Usage examples

### Auto mode (Pipeline (auto) + Tool enabled)
> "run scout for today"
LLM calls `run_scout()` → result streams back.

> "show me what the writer produced for priority 1"
LLM calls `get_latest_output("writer")` → JSON pretty-print.

> "render image for priority 1, shot 3"
LLM calls `evict_ollama()` then `run_image_render(pick=1, shot=3)`.

### Direct mode (per-stage models, no tool needed)
- Select model `Royal Pop / Writer`. Send "go pick=1" → fires writer for P1.
- Select model `Royal Pop / Pipeline (auto)` and disable tool → orchestrator
  chat mode (LLM thinks but can't run anything; for planning conversations).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "ollama unreachable" in Open-WebUI Settings | Ollama not running on host. Start it. |
| 9 Royal Pop models not in dropdown | Function not enabled, or Open-WebUI restart needed |
| Tool returns "FastAPI unreachable" | `docker compose ps` — confirm pws-agent running on :8000 |
| Render stages don't fire | ComfyUI not on host :8188. Render auto-skips by design. |
| VRAM OOM during image render | Tool didn't evict in time — call `evict_ollama()` manually, wait 5s, retry |

## VRAM strategy

`OLLAMA_KEEP_ALIVE=10m` (set in docker-compose). qwen2.5:14b stays warm during
chat for fast replies. Tool's `evict_ollama()` forces unload before render
stages (which already do this internally too).
