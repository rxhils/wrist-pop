"""Social media data sources for Scout.

Currently: YouTube Data API v3 (free 10k units/day with GOOGLE_API_KEY).
Future: Instagram + TikTok via Apify when APIFY_TOKEN added.

All functions silent-fail (return empty list / status-dict) when keys missing,
so Scout doesn't break — it just gets less signal.
"""
from __future__ import annotations

import os
from typing import Any

import requests


def has_youtube_key() -> bool:
    return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("YOUTUBE_API_KEY"))


def youtube_search(query: str, max_results: int = 10, days: int = 30) -> list[dict]:
    """Search YouTube via Data API v3. ~100 units per call. Returns normalised list.

    Each item:
      {platform, post_url, author, title, description, posted_at,
       view_count, like_count, comment_count, thumbnail_url}
    """
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("YOUTUBE_API_KEY")
    if not key:
        return [{"_error": "GOOGLE_API_KEY missing"}]

    from datetime import datetime, timedelta, timezone
    published_after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "relevance",
                "maxResults": min(max_results, 25),
                "publishedAfter": published_after,
                "regionCode": "GB",
                "relevanceLanguage": "en",
                "key": key,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return [{"_error": f"YouTube search {r.status_code}: {r.text[:200]}"}]
        items = r.json().get("items", [])
    except Exception as e:
        return [{"_error": f"YouTube search exception: {e}"}]

    # Second call: batch video.list for stats (1 unit per video)
    video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    stats_map: dict[str, dict] = {}
    if video_ids:
        try:
            rs = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "statistics,contentDetails",
                    "id": ",".join(video_ids),
                    "key": key,
                },
                timeout=15,
            )
            if rs.status_code == 200:
                for v in rs.json().get("items", []):
                    stats_map[v["id"]] = v
        except Exception:
            pass

    results: list[dict] = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        if not vid:
            continue
        sn = it.get("snippet") or {}
        stats = (stats_map.get(vid, {}).get("statistics") or {})
        thumbs = sn.get("thumbnails") or {}
        results.append({
            "platform": "youtube",
            "post_url": f"https://www.youtube.com/watch?v={vid}",
            "video_id": vid,
            "author": sn.get("channelTitle"),
            "title": sn.get("title"),
            "description": (sn.get("description") or "")[:300],
            "posted_at": sn.get("publishedAt"),
            "view_count": int(stats.get("viewCount") or 0),
            "like_count": int(stats.get("likeCount") or 0),
            "comment_count": int(stats.get("commentCount") or 0),
            "thumbnail_url": (thumbs.get("high") or thumbs.get("default") or {}).get("url"),
        })
    # Sort by views descending — Scout sees the most-watched ones first
    results.sort(key=lambda x: x.get("view_count", 0), reverse=True)
    return results
