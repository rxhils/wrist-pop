"""Trend research tools — Google Trends + DuckDuckGo web search."""
from __future__ import annotations

import json
import time
from typing import Any

import urllib3.util.retry as _retry_mod

_orig_retry_init = _retry_mod.Retry.__init__


def _patched_retry_init(self, *args, **kwargs):
    if "method_whitelist" in kwargs:
        kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
    return _orig_retry_init(self, *args, **kwargs)


_retry_mod.Retry.__init__ = _patched_retry_init

try:
    from crewai.tools import tool  # type: ignore
except ImportError:
    # Lightweight shim when running on host without crewai installed.
    def tool(name: str | None = None):  # type: ignore[misc]
        def decorator(fn):
            class _ToolWrapper:
                def __init__(self, fn, name):
                    self.fn = fn
                    self.name = name or fn.__name__
                    self.__doc__ = fn.__doc__
                def run(self, *args, **kwargs):
                    print(f"Using Tool: {self.name}")
                    return self.fn(*args, **kwargs)
                def __call__(self, *args, **kwargs):
                    return self.fn(*args, **kwargs)
            return _ToolWrapper(fn, name)
        if callable(name):
            # @tool without parens
            fn = name
            return tool(None)(fn)
        return decorator

from pytrends.request import TrendReq
from ddgs import DDGS

# ── optional Reddit (PRAW) ───────────────────────────────────────────
try:
    import praw  # type: ignore
    _PRAW_AVAILABLE = True
except ImportError:
    _PRAW_AVAILABLE = False

# ── optional Brave Search ────────────────────────────────────────────
import os as _os
BRAVE_API_KEY = _os.getenv("BRAVE_API_KEY", "")
REDDIT_CLIENT_ID = _os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = _os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = _os.getenv("REDDIT_USER_AGENT", "RoyalPopBot/1.0")


@tool("Google Trends interest")
def google_trends_interest(keywords: str, timeframe: str = "now 7-d", geo: str = "") -> str:
    """Get Google Trends interest-over-time for a comma-separated keyword list.

    Args:
        keywords: comma-separated terms, max 5. Example: "Royal Pop,AP Swatch"
        timeframe: pytrends timeframe. Default "now 7-d". Other: "today 1-m", "today 3-m".
        geo: ISO country code or "" for worldwide.

    Returns JSON: {keyword: {avg_interest, latest, delta_pct}}.
    """
    terms = [k.strip() for k in keywords.split(",") if k.strip()][:5]
    if not terms:
        return json.dumps({"error": "no keywords"})

    try:
        pytrends = TrendReq(hl="en-US", tz=0, retries=2, backoff_factor=0.3)
        pytrends.build_payload(terms, timeframe=timeframe, geo=geo)
        df = pytrends.interest_over_time()
        if df.empty:
            return json.dumps({k: {"avg_interest": 0, "latest": 0, "delta_pct": 0} for k in terms})

        out: dict[str, Any] = {}
        for k in terms:
            if k not in df.columns:
                out[k] = {"avg_interest": 0, "latest": 0, "delta_pct": 0}
                continue
            series = df[k].tolist()
            avg = sum(series) / max(len(series), 1)
            latest = series[-1]
            first_half = sum(series[: len(series) // 2]) / max(len(series) // 2, 1)
            second_half = sum(series[len(series) // 2 :]) / max(len(series) - len(series) // 2, 1)
            delta = ((second_half - first_half) / first_half * 100) if first_half else 0
            out[k] = {
                "avg_interest": round(avg, 1),
                "latest": int(latest),
                "delta_pct": round(delta, 1),
            }
        return json.dumps(out)
    except Exception as e:
        return json.dumps({"error": f"pytrends_failed: {e}"})


@tool("Google Trends related queries")
def google_trends_related(keyword: str, timeframe: str = "now 7-d") -> str:
    """Get rising related queries for a single keyword. Returns JSON list of rising queries."""
    try:
        pytrends = TrendReq(hl="en-US", tz=0, retries=2, backoff_factor=0.3)
        pytrends.build_payload([keyword], timeframe=timeframe)
        related = pytrends.related_queries()
        rising = related.get(keyword, {}).get("rising")
        if rising is None or rising.empty:
            return json.dumps({"rising": []})
        return json.dumps({"rising": rising.head(10).to_dict(orient="records")})
    except Exception as e:
        return json.dumps({"error": f"related_failed: {e}"})


def _ddgs_retry(fn, *args, attempts: int = 3, base_delay: float = 2.0, **kwargs):
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_err  # type: ignore[misc]


@tool("DuckDuckGo web search")
def web_search(query: str, max_results: int = 8) -> str:
    """Search the web via DuckDuckGo. Returns JSON list of {title, href, body}."""
    try:
        def _run() -> list[dict]:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        results = _ddgs_retry(_run)
        slim = [{"title": r.get("title"), "href": r.get("href"), "body": r.get("body")} for r in results]
        return json.dumps(slim)
    except Exception as e:
        return json.dumps({"error": f"search_failed: {e}"})


@tool("DuckDuckGo news search")
def news_search(query: str, max_results: int = 8) -> str:
    """Search news via DuckDuckGo. Returns JSON list of {title, url, date, body, source}."""
    try:
        def _run() -> list[dict]:
            with DDGS() as ddgs:
                return list(ddgs.news(query, max_results=max_results))
        results = _ddgs_retry(_run)
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": f"news_failed: {e}"})


@tool("Reddit search")
def reddit_search(query: str, subreddit: str = "watches+WatchExchange+streetwear", time_filter: str = "month", limit: int = 10) -> str:
    """Search Reddit via PRAW. Requires REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET env.

    Args:
        query: search query
        subreddit: comma+ joined list. Default = 'watches+WatchExchange+streetwear'.
        time_filter: 'hour'|'day'|'week'|'month'|'year'|'all'. Default 'month'.
        limit: max results, default 10.

    Returns JSON list of {title, url, subreddit, score, num_comments, created_utc, body_preview}.
    """
    if not _PRAW_AVAILABLE:
        return json.dumps({"error": "praw not installed. pip install praw"})
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return json.dumps({"error": "REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET env vars not set"})
    try:
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
            check_for_async=False,
        )
        reddit.read_only = True
        sub = reddit.subreddit(subreddit)
        results: list[dict] = []
        for post in sub.search(query, time_filter=time_filter, limit=limit):
            results.append({
                "title": post.title,
                "url": f"https://reddit.com{post.permalink}",
                "subreddit": str(post.subreddit),
                "score": post.score,
                "num_comments": post.num_comments,
                "created_utc": int(post.created_utc),
                "body_preview": (post.selftext or "")[:250],
            })
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": f"reddit_failed: {e}"})


@tool("Brave Web Search")
def brave_search(query: str, count: int = 10) -> str:
    """Brave Search API. Better than DDG (no rate limits). Requires BRAVE_API_KEY env.

    Args:
        query: search query
        count: max results (1-20, default 10)

    Returns JSON list of {title, url, description, age}.
    """
    if not BRAVE_API_KEY:
        return json.dumps({"error": "BRAVE_API_KEY env var not set. Free 2k/mo: https://brave.com/search/api/"})
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={"q": query, "count": min(count, 20)},
            timeout=15,
        )
        if r.status_code != 200:
            return json.dumps({"error": f"brave_failed: {r.status_code} {r.text[:200]}"})
        data = r.json()
        out = []
        for item in data.get("web", {}).get("results", [])[:count]:
            out.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "description": item.get("description"),
                "age": item.get("age"),
            })
        return json.dumps(out)
    except Exception as e:
        return json.dumps({"error": f"brave_failed: {e}"})


@tool("Brave News Search")
def brave_news(query: str, count: int = 10, freshness: str = "pw") -> str:
    """Brave News API. Fresher news than DDG. freshness: pd(day) pw(week) pm(month) py(year)."""
    if not BRAVE_API_KEY:
        return json.dumps({"error": "BRAVE_API_KEY env var not set"})
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/news/search",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={"q": query, "count": min(count, 20), "freshness": freshness},
            timeout=15,
        )
        if r.status_code != 200:
            return json.dumps({"error": f"brave_news_failed: {r.status_code} {r.text[:200]}"})
        data = r.json()
        out = []
        for item in data.get("results", [])[:count]:
            out.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "description": item.get("description"),
                "age": item.get("age"),
                "source": (item.get("meta_url") or {}).get("hostname"),
            })
        return json.dumps(out)
    except Exception as e:
        return json.dumps({"error": f"brave_news_failed: {e}"})


ALL_TOOLS = [
    google_trends_interest, google_trends_related,
    web_search, news_search,
    reddit_search, brave_search, brave_news,
]


def has_brave_key() -> bool:
    return bool(BRAVE_API_KEY)


def has_reddit_keys() -> bool:
    return bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and _PRAW_AVAILABLE)
