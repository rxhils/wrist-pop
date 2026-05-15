# 24/7 Daily Pipeline via GitHub Actions

Runs the content pipeline at **06:00 UTC every day** on free GitHub-hosted runners.
Outputs commit back to the repo. No local laptop needed.

**Cost: £0/month.** GitHub Actions free tier: 2000 min/mo. Daily run ≈ 3 min → 90 min/mo. Well under.

---

## What runs

- Scout → Strategist → Writer → Gate → Visual → Scheduler
- LLM calls via Mistral / Gemini (free cloud)
- Render stages (FLUX, LTX-Video) **auto-skip** (no ComfyUI on runner)
- All 8 daily artefacts saved to `outputs/`
- Commits + pushes outputs back to repo

---

## Setup — 8 steps (~15 min one-time)

### 1. Create private GitHub repo

Go to https://github.com/new
- Name: e.g. `royal-pop-pipeline`
- Visibility: **Private** (recommended — has your brand context)
- Don't init with README

### 2. Push code from local

In PowerShell:

```powershell
cd "C:\claude\Royal pop\05-agents\content-system"

# initialise git
git init
git branch -M main

# safety: make sure .env is gitignored
echo .env >> .gitignore
echo __pycache__/ >> .gitignore
echo "*.pyc" >> .gitignore
echo .venv/ >> .gitignore
echo venv/ >> .gitignore

# first commit
git add .
git commit -m "Initial pipeline + Mistral routing"

# connect to your new repo (replace USER and REPO)
git remote add origin https://github.com/USER/REPO.git
git push -u origin main
```

### 3. Add API key secrets

GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add each:

| Name | Value | Required? |
|------|-------|-----------|
| `MISTRAL_API_KEY` | your Mistral key | YES (writer + strategist + scout + gate + visual) |
| `GEMINI_API_KEY` | your Gemini key (when fixed) | optional, fallback |
| `GROQ_API_KEY` | your Groq key (when you get one) | optional |
| `BRAVE_API_KEY` | Brave Search key | optional (better trends) |
| `REDDIT_CLIENT_ID` | from reddit.com/prefs/apps | optional |
| `REDDIT_CLIENT_SECRET` | same | optional |

### 4. Enable Actions

Repo → **Actions** tab → "I understand my workflows, enable them"

### 5. First manual run (sanity check)

Actions → **Daily Content Pipeline** → **Run workflow** → choose `main` → Run.

Watch the logs. Should complete in 3–5 min. Render stages will show "ComfyUI not reachable — SKIPPING" (expected on runner).

### 6. Verify outputs committed

After workflow finishes:
- Repo home → see new commit: "pipeline run YYYY-MM-DD"
- Browse `outputs/` folder → today's JSON + digest.md present

### 7. Pull on laptop

When you want to review:
```powershell
cd "C:\claude\Royal pop\05-agents\content-system"
git pull
# outputs/digest_YYYY-MM-DD.md = readable today's brief
# outputs/notion_payload_YYYY-MM-DD.json = ready for Notion push
```

### 8. (Optional) Adjust schedule

Edit `.github/workflows/daily-pipeline.yml` — change the cron line:

```yaml
- cron: '0 6 * * *'   # 06:00 UTC daily — change as needed
```

Use https://crontab.guru/ to build other times. E.g. `30 5 * * *` = 05:30 UTC.

---

## Manual override per run

Workflow dispatch UI accepts inputs:
- `provider` — override all agents (e.g. `mistral` only, or `gemini` only)
- `stage_from` — start at a specific stage (e.g. `writer` to skip Scout/Strategist)

---

## What you DON'T need

- ❌ No local laptop running
- ❌ No Ollama (cloud LLM only)
- ❌ No ComfyUI (renders skipped on runner)
- ❌ No Docker
- ❌ No Railway / Render / Fly.io
- ❌ No always-on server

---

## What you still do locally

- **Image + video renders** — only when you sit down with ComfyUI open
- Read `outputs/digest_YYYY-MM-DD.md` (Slack/email-style summary)
- Push `outputs/notion_payload_YYYY-MM-DD.json` to Notion via MCP from main Claude chat (one-click)
- Approve content → film/post

---

## Cost guard

GitHub free tier: 2000 mins/mo private repos. Daily 3-min run = 90 min/mo. Leaves 1900 mins for manual runs, debugging.

If you upgrade to GitHub Pro ($4/mo) → 3000 mins. Not needed for this workload.

Mistral free tier covers daily pipeline usage easily.

---

## Failure modes + handling

| Failure | Behaviour |
|---------|-----------|
| Mistral rate-limit | Built-in retries with 10/30/60/90s backoff |
| Render stage (ComfyUI down) | Skipped cleanly, pipeline continues |
| LLM key missing | That agent errors. Set provider to ollama in `pipeline_config.py` OR add the key |
| Outputs unchanged | Commit skipped, no empty commits |
| Workflow run > 30 min | Auto-killed (timeout-minutes: 30) |

---

## Disable the cron

If you want to pause:
- Comment out the `schedule:` lines in `daily-pipeline.yml` and commit
- OR disable the workflow from Actions tab → ⋯ → Disable workflow

---

## Cron schedule cheat sheet

| Cron | When (UTC) |
|------|------------|
| `0 6 * * *` | 06:00 every day |
| `0 6 * * 1-5` | 06:00 weekdays only |
| `0 6,18 * * *` | 06:00 + 18:00 daily |
| `30 5 * * *` | 05:30 daily |
| `0 */6 * * *` | every 6 hours |

UK note: GitHub uses UTC. BST (Mar–Oct) = UTC+1, GMT (Oct–Mar) = UTC+0.
