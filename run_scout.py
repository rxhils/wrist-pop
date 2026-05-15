"""Trend Scout — deterministic tool calls + one LLM synthesis call.

Avoids CrewAI ReAct loop because small local models hallucinate inside it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

from tools.trend_tools import (
    google_trends_interest,
    google_trends_related,
    news_search,
    web_search,
    brave_search,
    brave_news,
    reddit_search,
    has_brave_key,
    has_reddit_keys,
)

ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts" / "trend_scout.md"
CONFIG_PATH = ROOT / "config" / "keywords.yaml"
OUT_DIR = ROOT / "outputs"


def _call(tool, **kwargs) -> dict | list:
    raw = tool.run(**kwargs)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


def collect_signals(cfg: dict) -> dict:
    primary = cfg["primary"]
    timeframe = cfg.get("trends_timeframe", "now 7-d")
    geo = cfg.get("trends_geo", "")
    reddit_subs = "+".join(cfg.get("reddit_subs", ["watches", "WatchExchange"]))

    print("[scout] calling pytrends interest...")
    trends = _call(
        google_trends_interest,
        keywords=",".join(primary),
        timeframe=timeframe,
        geo=geo,
    )

    print("[scout] calling pytrends related queries (Royal Pop)...")
    related = _call(google_trends_related, keyword="Royal Pop", timeframe=timeframe)

    # Prefer Brave search if key present, fall back to DDG
    search_fn = brave_search if has_brave_key() else web_search
    news_fn = brave_news if has_brave_key() else news_search
    search_label = "brave_search" if has_brave_key() else "ddg_web"
    news_label = "brave_news" if has_brave_key() else "ddg_news"
    print(f"[scout] using {search_label} for competitor scan...")

    competitor_queries = [
        "Royal Pop watch strap",
        "AP Swatch wrist conversion",
        "Helvetus Royal Pop strap",
        "Wristbuddys Royal Pop",
        "Wisstraps Royal Pop",
        "Delugs Royal Pop",
    ]
    web_results: dict[str, list | dict] = {}
    for q in competitor_queries:
        if has_brave_key():
            web_results[q] = _call(search_fn, query=q, count=5)
        else:
            web_results[q] = _call(search_fn, query=q, max_results=5)

    print(f"[scout] using {news_label} for news...")
    if has_brave_key():
        news = _call(news_fn, query="Royal Pop OR AP Swatch watch strap", count=8, freshness="pw")
    else:
        news = _call(news_fn, query="Royal Pop OR AP Swatch watch", max_results=6)

    # Reddit — only if creds present
    reddit_results: dict[str, list | dict] = {}
    if has_reddit_keys():
        print(f"[scout] calling Reddit ({reddit_subs}) ...")
        reddit_queries = [
            "Royal Pop strap",
            "Royal Pop wrist",
            "AP Swatch wrist",
        ]
        for q in reddit_queries:
            reddit_results[q] = _call(
                reddit_search,
                query=q,
                subreddit=reddit_subs,
                time_filter="month",
                limit=8,
            )
    else:
        reddit_results = {"_skipped": "REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET not set"}

    return {
        "google_trends_interest": trends,
        "google_trends_related_royal_pop": related,
        "web_search": web_results,
        "news_search": news,
        "reddit_search": reddit_results,
        "_sources_used": {
            "web": search_label,
            "news": news_label,
            "reddit": "praw" if has_reddit_keys() else "skipped",
        },
    }


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    return s.strip()


def _extract_url_catalog(signals: dict) -> list[dict]:
    catalog: list[dict] = []
    web = signals.get("web_search", {})
    if isinstance(web, dict):
        for query, results in web.items():
            if isinstance(results, list):
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    url = r.get("url") or r.get("href")  # Brave uses 'url', DDG uses 'href'
                    if url:
                        catalog.append({
                            "url": url,
                            "title": (r.get("title") or "")[:120],
                            "snippet": (r.get("description") or r.get("body") or "")[:200],
                            "from_query": query,
                            "age": r.get("age"),
                        })
    news = signals.get("news_search")
    if isinstance(news, list):
        for r in news:
            if isinstance(r, dict) and r.get("url"):
                catalog.append({
                    "url": r["url"],
                    "title": (r.get("title") or "")[:120],
                    "snippet": (r.get("description") or r.get("body") or "")[:200],
                    "from_query": "news",
                    "age": r.get("age"),
                })
    # Reddit posts
    reddit = signals.get("reddit_search", {})
    if isinstance(reddit, dict):
        for query, results in reddit.items():
            if query.startswith("_"):
                continue
            if isinstance(results, list):
                for r in results:
                    if isinstance(r, dict) and r.get("url"):
                        catalog.append({
                            "url": r["url"],
                            "title": (r.get("title") or "")[:120],
                            "snippet": (r.get("body_preview") or "")[:200],
                            "from_query": f"reddit:{query}",
                            "score": r.get("score"),
                            "comments": r.get("num_comments"),
                        })
    return catalog


def synthesize(signals: dict, today: str) -> dict:
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    catalog = _extract_url_catalog(signals)
    trends_errored = isinstance(signals.get("google_trends_interest"), dict) and "error" in signals["google_trends_interest"]

    if not catalog:
        return {
            "date": today,
            "top_trends": [],
            "recommendation": "No real sources retrieved. Retry later.",
        }

    user_prompt = f"""Today is {today}.

Google Trends status: {"ERRORED — do NOT include Google Trends trends" if trends_errored else "OK"}

You have {len(catalog)} REAL search results. You MUST pick source_url values ONLY from this catalog. Copy the `url` field exactly.

CATALOG:
```json
{json.dumps(catalog, indent=2)[:12000]}
```

Cluster results into 3–5 distinct trends (one trend may cite ONE URL from the catalog, even if multiple results cover it — pick the strongest single source). Rank by urgency. Competitor sites (helvetus, wristbuddys, wisstraps, delugs) launching Royal Pop straps = urgency 9-10. Review articles = urgency 7-8.

Output valid JSON only. Schema in system prompt. Every `source_url` must come from the catalog above.
"""

    from providers import llm_call
    content = llm_call(
        agent_name="scout",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=True,
        num_ctx=8192,
    )
    cleaned = _strip_fences(content)
    return json.loads(cleaned)


def _collect_real_urls(signals: dict) -> set[str]:
    urls: set[str] = set()
    web = signals.get("web_search", {})
    if isinstance(web, dict):
        for results in web.values():
            if isinstance(results, list):
                for r in results:
                    if isinstance(r, dict):
                        u = r.get("url") or r.get("href")
                        if u:
                            urls.add(u)
    reddit = signals.get("reddit_search", {})
    if isinstance(reddit, dict):
        for q, results in reddit.items():
            if q.startswith("_"):
                continue
            if isinstance(results, list):
                for r in results:
                    if isinstance(r, dict) and r.get("url"):
                        urls.add(r["url"])
    news = signals.get("news_search")
    if isinstance(news, list):
        for r in news:
            if isinstance(r, dict) and r.get("url"):
                urls.add(r["url"])
    return urls


def validate_report(report: dict, signals: dict) -> dict:
    real_urls = _collect_real_urls(signals)
    trends_errored = isinstance(signals.get("google_trends_interest"), dict) and "error" in signals["google_trends_interest"]

    cleaned: list[dict] = []
    dropped: list[str] = []
    for t in report.get("top_trends", []):
        url = (t.get("source_url") or "").strip()
        plat = t.get("platform", "")
        if plat == "Google Trends" and trends_errored:
            dropped.append(f"DROP fabricated Google Trends entry: {t.get('topic')}")
            continue
        if not url or url not in real_urls:
            dropped.append(f"DROP fabricated URL: {url!r} ({t.get('topic')})")
            continue
        cleaned.append(t)

    cleaned.sort(key=lambda x: x.get("urgency", 0), reverse=True)
    report["top_trends"] = cleaned[:5]
    if dropped:
        report["_validation_notes"] = dropped
    if not cleaned:
        report["recommendation"] = "No verifiable trends found. Retry later."
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", type=Path, help="custom keywords YAML")
    parser.add_argument("--output", type=Path, help="custom output path for trend_report")
    parser.add_argument("--signals-from", type=Path, help="skip tools, load raw_signals JSON")
    args, _ = parser.parse_known_args()

    cfg_path = args.keywords or CONFIG_PATH
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    today = date.today().isoformat()

    if args.signals_from:
        signals = json.loads(args.signals_from.read_text(encoding="utf-8"))
        print(f"[scout] reusing signals from {args.signals_from}")
    else:
        signals = collect_signals(cfg)
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / f"raw_signals_{today}.json").write_text(
            json.dumps(signals, indent=2, default=str), encoding="utf-8"
        )

    report = synthesize(signals, today)
    report = validate_report(report, signals)

    out_path = args.output or (OUT_DIR / f"trend_report_{today}.json")
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"TREND REPORT — {today}")
    print("=" * 60)
    print(json.dumps(report, indent=2))
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
