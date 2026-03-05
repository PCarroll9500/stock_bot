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

# Wait for IBKR Gateway to be ready (retry up to 3 minutes)
echo "Waiting for IBKR Gateway on port 4002..."
READY=0
for i in $(seq 1 36); do
    if "$REPO/.venv/bin/python" -c "
import socket, sys
s = socket.socket(); s.settimeout(3)
try:
    s.connect(('127.0.0.1', 4002)); s.close(); sys.exit(0)
except: sys.exit(1)
" 2>/dev/null; then
        echo "Gateway ready after $((i * 5))s"
        READY=1
        break
    fi
    sleep 5
done

if [ "$READY" -eq 0 ]; then
    echo "ERROR: Gateway not ready after 3 minutes — aborting"
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
