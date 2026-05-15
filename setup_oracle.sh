#!/usr/bin/env bash
# Royal Pop — Oracle Cloud ARM Ampere setup script.
# Run via:  curl -fsSL https://raw.githubusercontent.com/rxhils/wrist-pop/main/setup_oracle.sh | bash
# Assumes Ubuntu 22.04 Minimal on Oracle ARM. Run as `ubuntu` user.

set -euo pipefail

REPO="https://github.com/rxhils/wrist-pop.git"
INSTALL_DIR="/home/ubuntu/wrist-pop"
SCHED_USER="ubuntu"

echo "============================================================"
echo "  Royal Pop — Oracle Cloud Setup"
echo "  $(date -u)"
echo "============================================================"

# ── 1. system packages ──
echo "[1/7] installing system packages..."
sudo apt update -y
sudo apt install -y python3.11 python3.11-venv python3-pip git tmux curl ufw build-essential

# ── 2. firewall ports ──
echo "[2/7] opening firewall ports 80, 443, 8000..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT || true
sudo apt install -y iptables-persistent
sudo netfilter-persistent save || true

# ── 3. clone or pull repo ──
echo "[3/7] cloning repo to $INSTALL_DIR..."
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR"
    git pull
else
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 4. venv + deps ──
echo "[4/7] creating venv + installing deps..."
python3.11 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements_cron.txt
pip install "fastapi" "uvicorn[standard]" "sse-starlette"

# ── 5. .env ──
echo "[5/7] checking .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "============================================================"
    echo "  ACTION REQUIRED — edit .env with your API keys:"
    echo "    nano $INSTALL_DIR/.env"
    echo "  Then re-run only the systemd section below."
    echo "============================================================"
fi

# ── 6. systemd UI service ──
echo "[6/7] creating systemd service for FastAPI UI..."
sudo tee /etc/systemd/system/wrist-pop-ui.service > /dev/null <<EOF
[Unit]
Description=Royal Pop FastAPI UI
After=network.target

[Service]
Type=simple
User=$SCHED_USER
WorkingDirectory=$INSTALL_DIR
Environment="OLLAMA_BASE_URL=http://127.0.0.1:11434"
Environment="COMFYUI_URL=http://127.0.0.1:8188"
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
StandardOutput=append:$INSTALL_DIR/ui.log
StandardError=append:$INSTALL_DIR/ui.log

[Install]
WantedBy=multi-user.target
EOF

# ── 7. systemd daily pipeline timer ──
echo "[7/7] creating systemd timer for daily pipeline..."
sudo tee /etc/systemd/system/wrist-pop-pipeline.service > /dev/null <<EOF
[Unit]
Description=Royal Pop Content Pipeline (daily)
After=network.target

[Service]
Type=oneshot
User=$SCHED_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python run_pipeline.py
StandardOutput=append:$INSTALL_DIR/cron.log
StandardError=append:$INSTALL_DIR/cron.log
EOF

sudo tee /etc/systemd/system/wrist-pop-pipeline.timer > /dev/null <<EOF
[Unit]
Description=Daily Royal Pop pipeline at 06:00 UTC

[Timer]
OnCalendar=*-*-* 06:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload

echo ""
echo "============================================================"
echo "  SETUP COMPLETE"
echo "============================================================"
echo ""
echo "  Next steps (in this order):"
echo ""
echo "  1. Edit .env with API keys:"
echo "       nano $INSTALL_DIR/.env"
echo ""
echo "  2. Enable + start services:"
echo "       sudo systemctl enable --now wrist-pop-ui.service"
echo "       sudo systemctl enable --now wrist-pop-pipeline.timer"
echo ""
echo "  3. Verify:"
echo "       sudo systemctl status wrist-pop-ui.service"
echo "       systemctl list-timers --all | grep wrist"
echo ""
echo "  4. Manual pipeline test:"
echo "       sudo systemctl start wrist-pop-pipeline.service"
echo "       tail -f $INSTALL_DIR/cron.log"
echo ""
echo "  5. UI accessible at:"
echo "       http://\$(curl -s ifconfig.me):8000"
echo ""
echo "  6. Tail UI logs:"
echo "       tail -f $INSTALL_DIR/ui.log"
echo ""
