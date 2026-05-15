# Oracle Cloud Free Tier — 24/7 Always-On Deploy

Hosts the Royal Pop pipeline + FastAPI UI on a free **ARM Ampere A1** VM:
**24 GB RAM · 4 vCPU · always-on · £0 forever**.

Time: ~90 min one-time. Mostly waiting for VM provisioning.

---

## Why Oracle Free Tier

| | Oracle Free Tier | Railway | Render | Fly.io | HF Spaces |
|--|------------------|---------|--------|--------|-----------|
| Cost | £0 forever | £4/mo+ | £0 (sleeps) | needs card | £0 |
| RAM | **24 GB** | 512 MB | 512 MB | 256 MB | 16 GB |
| vCPU | **4 ARM** | shared | shared | shared | 2 |
| Always-on | ✅ | ✅ | ❌ | ✅ | ❌ (sleep after 48h) |
| Custom domain | ✅ | ✅ | ✅ | ✅ | ✅ |
| Full Linux | ✅ | container only | container only | container only | container only |

Catch: ARM Ampere is heavily over-subscribed. Provisioning fails with "Out of capacity" frequently. Retry pattern: re-submit every few hours, eventually succeeds. Some regions easier than others.

---

## Step 1 — Sign up (15 min)

1. https://www.oracle.com/cloud/free/
2. Click "Start for free"
3. Fill: country (UK), name, email
4. Verify email
5. Pick **home region** — important, can't change:
   - Recommended: `UK South (London)` for low UK latency
   - Fallback if "out of capacity": `Frankfurt`, `Amsterdam`, `Marseille`
6. Address + phone verification
7. **Credit card** — verification only, never charged (Always Free Tier is forever free)
8. Wait for account activation (~5 min email)

---

## Step 2 — Provision ARM Ampere A1 VM (10 min — or hours if "out of capacity")

After login:

1. Hamburger menu → **Compute** → **Instances**
2. **Create instance**
3. Name: `wrist-pop`
4. Image and shape:
   - Image: **Ubuntu 22.04 Minimal**
   - Shape: click **Change shape** → **Ampere** → **VM.Standard.A1.Flex**
   - OCPU: **4** (max free)
   - Memory: **24 GB** (max free)
5. Networking:
   - Use defaults (creates VCN, public subnet, public IP)
6. Add SSH keys:
   - **Generate a key pair** (download both .pub and private)
   - Save private key safely: `C:\Users\fazea\.ssh\oracle-wrist-pop.key`
7. Boot volume: 50 GB default (free up to 200 GB total across instances)
8. **Create**

If you see **"Out of host capacity"** — Oracle's classic problem with ARM. Solutions:
- Retry the Create button every 30-60 min (sometimes works on 5th try)
- Try different region (Frankfurt often more available than London)
- Run https://github.com/hitrov/oci-arm-host-capacity (auto-retries via API)

---

## Step 3 — Open firewall ports (5 min)

Oracle blocks inbound by default. Open 8000 (FastAPI), 80, 443 (future HTTPS).

1. Instance details → **Subnet** → **Default Security List**
2. **Add Ingress Rule**:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: TCP
   - Destination Port Range: `8000`
   - Description: `FastAPI`
3. Repeat for `80` and `443` (for nginx + Caddy later)

Also open OS firewall (Ubuntu's UFW + iptables):

After SSH in (next step), run:
```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## Step 4 — SSH in

Instance shows **Public IP address** (e.g. `129.213.123.45`).

From PowerShell:
```powershell
# convert .key file permissions (windows protects it)
icacls "C:\Users\fazea\.ssh\oracle-wrist-pop.key" /inheritance:r /grant:r "$($env:USERNAME):R"

ssh -i "C:\Users\fazea\.ssh\oracle-wrist-pop.key" ubuntu@PUBLIC_IP
```

First connect: type `yes` to accept fingerprint.

---

## Step 5 — Run setup script (10 min)

Once SSH'd in, paste:

```bash
curl -fsSL https://raw.githubusercontent.com/rxhils/wrist-pop/main/setup_oracle.sh | bash
```

Or manual:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv git tmux ufw

# clone repo
cd ~
git clone https://github.com/rxhils/wrist-pop.git
cd wrist-pop

# venv + deps
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements_cron.txt
pip install fastapi "uvicorn[standard]" sse-starlette

# create .env from template
cp .env.example .env
nano .env   # paste your MISTRAL_API_KEY, GEMINI_API_KEY etc
```

---

## Step 6 — Run pipeline once (verify)

Still in SSH:

```bash
cd ~/wrist-pop
source venv/bin/activate
python run_pipeline.py
```

Should see Scout → Strategist → Writer → Gate → Visual → Scheduler. ~3 min. All artifacts in `outputs/`.

---

## Step 7 — Set up daily cron on the VM

Two options:

### Option A: System cron (simplest)
```bash
crontab -e
# add this line for 06:00 UTC daily:
0 6 * * * cd /home/ubuntu/wrist-pop && /home/ubuntu/wrist-pop/venv/bin/python run_pipeline.py >> /home/ubuntu/wrist-pop/cron.log 2>&1
```

### Option B: systemd timer (cleaner, restart-safe)
```bash
sudo tee /etc/systemd/system/wrist-pop.service <<'EOF'
[Unit]
Description=Royal Pop Content Pipeline
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/wrist-pop
ExecStart=/home/ubuntu/wrist-pop/venv/bin/python run_pipeline.py
EOF

sudo tee /etc/systemd/system/wrist-pop.timer <<'EOF'
[Unit]
Description=Daily run of Royal Pop pipeline

[Timer]
OnCalendar=*-*-* 06:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wrist-pop.timer
systemctl list-timers --all | grep wrist
```

---

## Step 8 — Run FastAPI UI 24/7 (systemd service)

If you want the web UI publicly reachable at `http://YOUR_PUBLIC_IP:8000`:

```bash
sudo tee /etc/systemd/system/wrist-pop-ui.service <<'EOF'
[Unit]
Description=Royal Pop FastAPI UI
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/wrist-pop
Environment="OLLAMA_BASE_URL=http://127.0.0.1:11434"
Environment="COMFYUI_URL=http://127.0.0.1:8188"
EnvironmentFile=/home/ubuntu/wrist-pop/.env
ExecStart=/home/ubuntu/wrist-pop/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wrist-pop-ui.service
sudo systemctl status wrist-pop-ui.service
```

Open browser: `http://PUBLIC_IP:8000`. Dashboard live.

**Warning**: open port 8000 to the world without auth = anyone can hit your pipeline + chat with your Mistral key. Two fixes:
- Restrict source CIDR in Oracle ingress rule to your home IP only
- OR put Caddy + Basic Auth in front (next step)

---

## Step 9 (optional) — HTTPS + auth via Caddy

```bash
# install Caddy (auto-HTTPS via Let's Encrypt)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Caddyfile (replace YOUR_DOMAIN — you need a domain pointed to public IP)
sudo tee /etc/caddy/Caddyfile <<'EOF'
YOUR_DOMAIN.com {
    basicauth {
        you JDJhJDE0JEd2WGdSY2RGUEZBSjJ4SnhqM3FNNXVZUzlsZmtZbg==  # generate with caddy hash-password
    }
    reverse_proxy 127.0.0.1:8000
}
EOF

sudo systemctl reload caddy
```

Cheap domain: namecheap.com or porkbun.com (~£3/year). Point A record at Oracle public IP.

---

## Step 10 — what runs where

| Component | Where | RAM |
|-----------|-------|-----|
| FastAPI UI | Oracle VM systemd service | ~200 MB |
| Daily pipeline cron | Oracle VM systemd timer | spikes to ~500 MB during run |
| LLM agents | Mistral cloud API | 0 (no local LLM) |
| Ollama | NOT installed on Oracle (ARM, no GPU) | 0 |
| Image render | NOT on Oracle (no GPU) — local ComfyUI only | 0 |
| Video render | NOT on Oracle — local ComfyUI only | 0 |
| Outputs | committed to GitHub repo via cron + sync | a few MB |

Oracle VM = brain + UI. Your laptop = rendering studio (when needed).

---

## Step 11 — disaster recovery / updates

To deploy code changes:
```bash
ssh -i ... ubuntu@PUBLIC_IP
cd ~/wrist-pop
git pull
sudo systemctl restart wrist-pop-ui.service
```

To pull today's outputs locally:
```bash
# on laptop
cd "C:\claude\Royal pop\05-agents\content-system"
git pull origin main
```

---

## Notes

- Oracle Free Tier ALWAYS Free tier = no charge unlimited. Stays free even after 30-day trial expires.
- Watch CPU credits: A1.Flex burns ~24 CPU-hrs/day max (4 vCPU × 6 hrs). Plenty for daily 3-min pipeline + idle UI.
- Outbound bandwidth: 10 TB/mo free. Image renders happen on laptop so no big transfers.
- ARM = no x86 binaries. Some Python packages may need source compile. We use pure-Python deps so fine.
