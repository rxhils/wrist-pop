"""Trend Scout task definition."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from crewai import Task

CONFIG_PATH = Path(__file__).parent.parent / "config" / "keywords.yaml"


def _load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_scout_task(agent) -> Task:
    cfg = _load_config()
    primary = ", ".join(cfg["primary"])
    secondary = ", ".join(cfg["secondary"])
    timeframe = cfg.get("trends_timeframe", "now 7-d")
    geo = cfg.get("trends_geo", "")
    today = date.today().isoformat()

    description = f"""Research today's ({today}) trends for Pop Wrist Studio.

PRIMARY KEYWORDS: {primary}
SECONDARY KEYWORDS: {secondary}
TIMEFRAME: {timeframe}
GEO: {geo or 'worldwide'}

STEPS:
1. Call `Google Trends interest` with the PRIMARY keywords (comma-separated). Note avg_interest, latest, delta_pct.
2. Call `Google Trends related queries` on "Royal Pop" — capture rising queries.
3. Call `DuckDuckGo news search` with query: "Royal Pop OR AP Swatch watch" for last-week news.
4. Call `DuckDuckGo web search` with query: "Royal Pop strap accessory" — community signals.
5. Synthesise 3–5 trends, urgency-scored 1–10. Highest urgency first.
6. Write a 1–2 sentence content recommendation referencing one specific trend.

OUTPUT: Valid JSON only. Schema in your system prompt. NO markdown fences. NO commentary."""

    return Task(
        description=description,
        agent=agent,
        expected_output=(
            "A single JSON object with keys: date, top_trends (array of 3-5 objects), "
            "recommendation (string). No prose outside the JSON."
        ),
        output_file=f"outputs/trend_report_{today}.json",
    )
