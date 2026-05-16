"""One-shot Supabase schema bootstrap. Idempotent — safe to re-run."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg

DB_URL = os.getenv("SUPABASE_DB_URL")
if not DB_URL:
    print("ERROR: SUPABASE_DB_URL missing in .env")
    sys.exit(1)

SCHEMA = """
create extension if not exists vector;

create table if not exists runs (
  id            bigserial primary key,
  run_date      date not null,
  agent_id      text not null,
  artifact_type text not null,
  payload       jsonb not null,
  embedding     vector(1024),
  created_at    timestamptz default now()
);
create index if not exists runs_date_agent_idx on runs(run_date, agent_id);

create table if not exists metrics (
  id              bigserial primary key,
  run_id          bigint references runs(id) on delete cascade,
  post_id         text not null,
  platform        text,
  saves           int,
  shares          int,
  comments        int,
  waitlist_signups int,
  posted_at       timestamptz,
  recorded_at     timestamptz default now()
);

create table if not exists winners (
  id                bigserial primary key,
  hook_text         text not null,
  format            text,
  engagement_score  int,
  source_run_id     bigint references runs(id) on delete set null,
  embedding         vector(1024),
  promoted_at       timestamptz default now()
);
create index if not exists winners_score_idx on winners(engagement_score desc);
"""

# pgvector ivfflat index requires at least one row of training data, so we
# create it lazily later. For now, schema only.

def main() -> int:
    print(f"[setup] connecting to {DB_URL.split('@')[1]}…")
    with psycopg.connect(DB_URL, sslmode="require") as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        print("[setup] schema applied OK")

        with conn.cursor() as cur:
            cur.execute("select tablename from pg_tables where schemaname='public' and tablename in ('runs','metrics','winners') order by tablename")
            tables = [r[0] for r in cur.fetchall()]
            print(f"[setup] tables present: {tables}")

            cur.execute("select extname from pg_extension where extname='vector'")
            ext = cur.fetchone()
            print(f"[setup] pgvector extension: {'OK' if ext else 'MISSING'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
