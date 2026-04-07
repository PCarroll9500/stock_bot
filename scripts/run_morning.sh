#!/bin/bash
# run_morning.sh — git pull, wait for Gateway, run main.py, send email report
#
# Cron entry (EC2, UTC):
#   5 14 * * 1-5 /home/ubuntu/stock_bot/scripts/run_morning.sh

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_morning_$(date +%Y-%m-%d).log"
exec >> "$LOG_FILE" 2>&1

echo "=== Morning run started: $(date) ==="

cd "$REPO"

# Load env vars (includes GITHUB_PAT for authenticated git push)
set -a; source "$REPO/.env" 2>/dev/null || true; set +a

# Configure git remote with PAT so push works without interactive credentials
if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_USER:-}" ]; then
    git remote set-url origin "https://${GITHUB_USER}:${GITHUB_PAT}@github.com/${GITHUB_USER}/stock_bot.git"
fi

echo "Pulling latest code from GitHub..."
git stash 2>/dev/null || true
git pull origin deploy || echo "WARNING: git pull failed, continuing with existing code"
git stash pop 2>/dev/null || git stash drop 2>/dev/null || true

# Wait for IBKR Gateway to be ready (retry up to 20 minutes)
# Uses a real API handshake written to a temp file — avoids shell quoting issues with -c "..."
# TCP connect alone passes even when the gateway isn't ready (socat is always up)
echo "Waiting for IBKR Gateway on port 4002..."
cat > /tmp/gw_check.py << 'PYEOF'
import socket, sys
try:
    s = socket.socket()
    s.settimeout(5)
    s.connect(('127.0.0.1', 4002))
    s.send(b'API\x00\x00\x00\x00\x09v100..176')
    data = s.recv(64)
    s.close()
    sys.exit(0 if data else 1)
except Exception:
    sys.exit(1)
PYEOF

READY=0
for i in $(seq 1 240); do
    if "$REPO/.venv/bin/python" /tmp/gw_check.py 2>/dev/null; then
        echo "Gateway ready after $((i * 5))s"
        READY=1
        break
    fi
    sleep 5
done
rm -f /tmp/gw_check.py

if [ "$READY" -eq 0 ]; then
    echo "ERROR: Gateway not ready after 20 minutes — aborting"
    exit 1
fi

# Clear any leftover positions/orders from previous session before running
# Hard 2-minute timeout prevents a hung Gateway from blocking main.py all day
echo "Running open_market_liquidate.py..."
timeout 120 "$REPO/.venv/bin/python" "$REPO/scripts/open_market_liquidate.py" || echo "WARNING: open_market_liquidate failed or timed out after 120s"

# Run the bot
# Hard 1-hour timeout — main.py should complete in well under an hour.
# Guards against an asyncio hang blocking the entire trading session.
echo "Running main.py..."
timeout 3600 "$REPO/.venv/bin/python" -m stock_bot.main
EXIT_CODE=$?

# Push morning picks to GitHub
git add docs/data/portfolio.json
if ! git diff --staged --quiet; then
    git commit -m "portfolio: morning picks $(date +%Y-%m-%d)"
    git push origin deploy
    echo "portfolio.json pushed to GitHub"
fi

# Send morning email report
echo "Sending morning email report..."
"$REPO/.venv/bin/python" "$REPO/scripts/email_morning_report.py" || echo "WARNING: email report failed"

echo "=== Morning run finished: $(date) ==="
exit $EXIT_CODE
