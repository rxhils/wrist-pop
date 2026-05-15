"""Interactive REPL per agent — load that agent's system prompt + chat with qwen2.5:14b.

Usage:
  python chat.py scout
  python chat.py strategist
  python chat.py writer
  python chat.py gate
  python chat.py visual
  python chat.py scheduler        # deterministic — meta-prompt about its behavior
  python chat.py render_image     # deterministic — meta-prompt
  python chat.py render_video     # deterministic — meta-prompt

In-REPL commands:
  /reset       drop conversation history, keep system prompt
  /system      print loaded system prompt
  /save FILE   save last assistant response to outputs/FILE
  /input FILE  load JSON file into conversation as user message
  /json        toggle JSON response mode on/off
  /quit        exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
PROMPTS = ROOT / "prompts"
OUT_DIR = ROOT / "outputs"

load_dotenv(ROOT / ".env")

AGENTS = {
    "scout": "trend_scout.md",
    "strategist": "strategist.md",
    "writer": "copy_writer.md",
    "gate": "quality_gate.md",
    "visual": "visual_brief.md",
    "scheduler": None,  # deterministic — falls back to meta-prompt
    "render_image": None,
    "render_video": None,
}

META_PROMPTS = {
    "scheduler": """You are a meta-assistant for the Scheduler stage of Royal Pop's content pipeline.
This stage is DETERMINISTIC Python (no LLM call). It reads approved_copy + visual_brief, assigns posting times
per platform (UK windows), and emits schedule + Notion payload + markdown digest.

The user wants to discuss its behavior, config, or output format. Help them understand
or modify the scheduler. POST_SLOTS dict in run_scheduler.py controls posting times.
""",
    "render_image": """You are a meta-assistant for the Image Render stage (Stage 7).
This stage is DETERMINISTIC Python — sends FLUX.1 schnell prompts to ComfyUI :8188.
Filters visual_brief shots where type='ai_image' and tool contains 'Flux'.
Workflow is a minimal FLUX schnell text2img: UNETLoader → DualCLIPLoader → VAELoader →
CLIPTextEncode → EmptySD3LatentImage → KSampler(4 steps, cfg 1.0, euler/simple) → VAEDecode → SaveImage.

The user wants to discuss config, troubleshoot ComfyUI, or modify the workflow. Help them.
""",
    "render_video": """You are a meta-assistant for the Video Render stage (Stage 8).
This stage is DETERMINISTIC Python — sends LTX-Video 2.3 prompts to ComfyUI :8188.
Filters visual_brief shots where type='ai_video' and tool contains 'LTX'.
Workflow uses CheckpointLoaderSimple + LTXVConditioning + LTXVScheduler + SamplerCustom + VHS_VideoCombine.

The user wants to discuss config, troubleshoot LTX-Video, or modify the workflow. Help them.
""",
}


def load_system_prompt(agent: str) -> str:
    fname = AGENTS.get(agent)
    if fname:
        return (PROMPTS / fname).read_text(encoding="utf-8")
    meta = META_PROMPTS.get(agent)
    if meta:
        return meta
    raise ValueError(f"unknown agent: {agent}")


def chat_call(messages: list[dict], json_mode: bool) -> str:
    url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    model = os.getenv("MODEL_CREATIVE", "qwen2.5:14b").replace("ollama/", "")
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.4, "num_ctx": 8192},
    }
    if json_mode:
        body["format"] = "json"
    r = requests.post(f"{url}/api/chat", json=body, timeout=420)
    r.raise_for_status()
    return r.json()["message"]["content"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=list(AGENTS.keys()))
    parser.add_argument("--json", action="store_true", help="force JSON response mode")
    parser.add_argument(
        "--input",
        type=Path,
        help="seed conversation with contents of this file as first user message",
    )
    args = parser.parse_args()

    system_prompt = load_system_prompt(args.agent)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    json_mode = args.json

    is_deterministic = AGENTS.get(args.agent) is None
    mode_label = "META" if is_deterministic else "AGENT"

    print(f"\n[chat] {mode_label}: {args.agent}")
    print(f"[chat] model: {os.getenv('MODEL_CREATIVE', 'qwen2.5:14b')}  json_mode: {json_mode}")
    print("[chat] commands: /reset /system /save FILE /input FILE /json /quit")
    print("[chat] empty line submits multi-line input.\n")

    if args.input:
        try:
            content = args.input.read_text(encoding="utf-8")
            messages.append({"role": "user", "content": f"Loaded from {args.input.name}:\n\n{content}"})
            print(f"[chat] loaded {args.input.name} as first user message.")
            reply = chat_call(messages, json_mode)
            messages.append({"role": "assistant", "content": reply})
            print(f"\nassistant>\n{reply}\n")
        except Exception as e:
            print(f"[chat] could not load input: {e}")

    last_reply = ""
    buf: list[str] = []

    while True:
        try:
            line = input("you> " if not buf else "...> ")
        except (EOFError, KeyboardInterrupt):
            print("\n[chat] bye.")
            return 0

        if line.strip().lower() in {"/quit", "/exit", "/q"}:
            return 0
        if line.strip() == "/reset":
            messages = [{"role": "system", "content": system_prompt}]
            buf = []
            print("[chat] history cleared.")
            continue
        if line.strip() == "/system":
            print("\n--- SYSTEM PROMPT ---")
            print(system_prompt)
            print("--- END ---\n")
            continue
        if line.strip() == "/json":
            json_mode = not json_mode
            print(f"[chat] json_mode = {json_mode}")
            continue
        if line.strip().startswith("/save "):
            fname = line.strip().split(maxsplit=1)[1]
            target = OUT_DIR / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(last_reply, encoding="utf-8")
            print(f"[chat] saved last reply -> {target}")
            continue
        if line.strip().startswith("/input "):
            fpath = line.strip().split(maxsplit=1)[1]
            try:
                p = Path(fpath)
                if not p.is_absolute():
                    p = OUT_DIR / fpath if (OUT_DIR / fpath).exists() else Path(fpath)
                content = p.read_text(encoding="utf-8")
                buf.append(f"Loaded from {p.name}:\n\n{content}")
                print(f"[chat] appended {p.name} to buffer. Type more or empty line to send.")
            except Exception as e:
                print(f"[chat] could not load: {e}")
            continue

        if line == "":
            if not buf:
                continue
            user_msg = "\n".join(buf)
            buf = []
            messages.append({"role": "user", "content": user_msg})
            try:
                last_reply = chat_call(messages, json_mode)
            except Exception as e:
                print(f"[chat] error: {e}")
                messages.pop()
                continue
            messages.append({"role": "assistant", "content": last_reply})
            print(f"\nassistant>\n{last_reply}\n")
        else:
            buf.append(line)


if __name__ == "__main__":
    sys.exit(main())
