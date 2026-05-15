"""Trend Scout Agent — CrewAI agent that runs daily trend research."""
from __future__ import annotations

import os
from pathlib import Path

from crewai import Agent, LLM

from tools.trend_tools import ALL_TOOLS

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "trend_scout.md"


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_trend_scout() -> Agent:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    model = os.getenv("MODEL_CREATIVE", "ollama/qwen2.5:14b")

    llm = LLM(
        model=model,
        base_url=base_url,
        temperature=0.2,
    )

    return Agent(
        role="Trend Research Specialist",
        goal=(
            "Identify the 3-5 most urgent and relevant trends about Royal Pop, AP Swatch, "
            "and the watch-accessory community. Output structured JSON only."
        ),
        backstory=_load_system_prompt(),
        tools=ALL_TOOLS,
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=10,
    )
