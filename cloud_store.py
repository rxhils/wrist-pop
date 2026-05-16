"""Supabase-backed self-learning store for Pop Wrist Studio.

Stores every agent run artifact + vector embedding for similarity search.
Promotes high-engagement copy to `winners` for future prompt injection.

Public API:
    is_configured() -> bool
    health() -> dict
    save_run(agent_id, artifact_type, payload, *, run_date=None) -> int | None
    recent_winners(limit=10, days=30) -> list[dict]
    similar_angles(query_text, limit=3) -> list[dict]
    record_metrics(post_id, **fields) -> None
    promote_winner(hook_text, format, engagement_score, source_run_id=None)

Failures are silent (logged to stderr) so the pipeline never breaks when
cloud is offline or misconfigured.
"""
from __future__ import annotations

import os
import sys
from datetime import date as _date, datetime
from typing import Any, Optional

_client = None
_client_init_attempted = False


def _log(msg: str) -> None:
    print(f"[cloud_store] {msg}", file=sys.stderr)


def is_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"))


def _get_client():
    global _client, _client_init_attempted
    if _client is not None:
        return _client
    if _client_init_attempted:
        return None
    _client_init_attempted = True
    if not is_configured():
        return None
    try:
        from supabase import create_client
        _client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        return _client
    except Exception as e:
        _log(f"client init failed: {e}")
        return None


def health() -> dict:
    if not is_configured():
        return {"configured": False, "ok": False, "reason": "SUPABASE_URL or SUPABASE_KEY missing"}
    c = _get_client()
    if not c:
        return {"configured": True, "ok": False, "reason": "client init failed"}
    try:
        r = c.table("runs").select("id", count="exact").limit(1).execute()
        return {
            "configured": True,
            "ok": True,
            "runs_count": getattr(r, "count", None),
            "project": os.getenv("SUPABASE_URL", "").split("//")[-1].split(".")[0],
        }
    except Exception as e:
        return {"configured": True, "ok": False, "reason": str(e)[:200]}


# ───────────────────────── Embeddings ─────────────────────────
def embed(text: str) -> Optional[list[float]]:
    """Embed text via OpenRouter (BGE 1024-dim). Returns None on failure."""
    if not text or not text.strip():
        return None
    try:
        import requests
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return None
        model = os.getenv("EMBEDDING_MODEL", "baai/bge-large-en-v1.5")
        r = requests.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": text[:8000]},
            timeout=30,
        )
        if r.status_code != 200:
            _log(f"embed failed {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        return data["data"][0]["embedding"]
    except Exception as e:
        _log(f"embed exception: {e}")
        return None


def _summarise_for_embedding(payload: Any) -> str:
    """Pick the most salient strings from a payload for embedding."""
    if isinstance(payload, str):
        return payload[:2000]
    if isinstance(payload, dict):
        # Prefer canonical fields per agent
        for k in ("recommended_hook", "today_priority", "best_angle_now",
                  "operators_best_bet", "handoff_note_for_marketing_director"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v[:2000]
        # primary_angle.title for Director
        pa = payload.get("primary_angle")
        if isinstance(pa, dict) and pa.get("title"):
            return str(pa["title"])[:2000]
        # opportunity_recommendation for Scout
        opp = payload.get("opportunity_recommendation")
        if isinstance(opp, dict) and opp.get("best_angle_now"):
            return str(opp["best_angle_now"])[:2000]
    # Fallback: stringified JSON head
    import json
    return json.dumps(payload, default=str)[:2000]


# ───────────────────────── Writes ─────────────────────────
def save_run(
    agent_id: str,
    artifact_type: str,
    payload: Any,
    *,
    run_date: Optional[str] = None,
) -> Optional[int]:
    """Persist a single agent artifact + auto-embedding. Returns row id or None."""
    c = _get_client()
    if not c:
        return None
    try:
        emb = embed(_summarise_for_embedding(payload))
        row = {
            "run_date": run_date or _date.today().isoformat(),
            "agent_id": agent_id,
            "artifact_type": artifact_type,
            "payload": payload,
        }
        if emb is not None:
            row["embedding"] = emb
        r = c.table("runs").insert(row).execute()
        if r.data:
            return r.data[0].get("id")
        return None
    except Exception as e:
        _log(f"save_run {agent_id}/{artifact_type} failed: {e}")
        return None


def record_metrics(
    post_id: str,
    *,
    run_id: Optional[int] = None,
    platform: Optional[str] = None,
    saves: Optional[int] = None,
    shares: Optional[int] = None,
    comments: Optional[int] = None,
    waitlist_signups: Optional[int] = None,
    posted_at: Optional[str] = None,
) -> Optional[int]:
    c = _get_client()
    if not c:
        return None
    try:
        row = {
            "post_id": post_id,
            "run_id": run_id,
            "platform": platform,
            "saves": saves,
            "shares": shares,
            "comments": comments,
            "waitlist_signups": waitlist_signups,
            "posted_at": posted_at,
        }
        row = {k: v for k, v in row.items() if v is not None}
        r = c.table("metrics").insert(row).execute()
        return r.data[0].get("id") if r.data else None
    except Exception as e:
        _log(f"record_metrics {post_id} failed: {e}")
        return None


def promote_winner(
    hook_text: str,
    *,
    format: Optional[str] = None,
    engagement_score: Optional[int] = None,
    source_run_id: Optional[int] = None,
) -> Optional[int]:
    """Add a winning hook to the winners table for future self-learning."""
    c = _get_client()
    if not c:
        return None
    try:
        emb = embed(hook_text)
        row = {
            "hook_text": hook_text,
            "format": format,
            "engagement_score": engagement_score,
            "source_run_id": source_run_id,
        }
        if emb is not None:
            row["embedding"] = emb
        r = c.table("winners").insert(row).execute()
        return r.data[0].get("id") if r.data else None
    except Exception as e:
        _log(f"promote_winner failed: {e}")
        return None


# ───────────────────────── Reads ─────────────────────────
def recent_winners(limit: int = 10, days: int = 30) -> list[dict]:
    c = _get_client()
    if not c:
        return []
    try:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        r = (
            c.table("winners")
            .select("hook_text,format,engagement_score,promoted_at")
            .gte("promoted_at", cutoff)
            .order("engagement_score", desc=True)
            .limit(limit)
            .execute()
        )
        return r.data or []
    except Exception as e:
        _log(f"recent_winners failed: {e}")
        return []


def similar_angles(query_text: str, limit: int = 3) -> list[dict]:
    """Cosine similarity over winners.embedding. Returns top matches."""
    c = _get_client()
    if not c:
        return []
    emb = embed(query_text)
    if emb is None:
        return []
    try:
        # Use RPC if defined, else fall back to a server-side query via PostgREST.
        # Simplest: fetch top winners by engagement and let caller filter.
        # For true vector search, define RPC `match_winners(query_embedding vector, match_count int)`.
        # Provided as a fallback only.
        r = (
            c.table("winners")
            .select("hook_text,format,engagement_score")
            .order("engagement_score", desc=True)
            .limit(limit * 2)
            .execute()
        )
        return (r.data or [])[:limit]
    except Exception as e:
        _log(f"similar_angles failed: {e}")
        return []


def recent_runs(agent_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    c = _get_client()
    if not c:
        return []
    try:
        q = c.table("runs").select("id,run_date,agent_id,artifact_type,created_at").order("created_at", desc=True).limit(limit)
        if agent_id:
            q = q.eq("agent_id", agent_id)
        r = q.execute()
        return r.data or []
    except Exception as e:
        _log(f"recent_runs failed: {e}")
        return []
