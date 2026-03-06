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
echo "Pulling latest code from GitHub..."
git pull origin main || echo "WARNING: git pull failed, continuing with existing code"

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

# Run the bot
echo "Running main.py..."
"$REPO/.venv/bin/python" -m stock_bot.main
EXIT_CODE=$?

# Push morning picks to GitHub
git add docs/data/portfolio.json
if ! git diff --staged --quiet; then
    git commit -m "portfolio: morning picks $(date +%Y-%m-%d)"
    git push origin main
    echo "portfolio.json pushed to GitHub"
fi

# Send morning email report
echo "Sending morning email report..."
"$REPO/.venv/bin/python" "$REPO/scripts/email_morning_report.py" || echo "WARNING: email report failed"

echo "=== Morning run finished: $(date) ==="
exit $EXIT_CODE
