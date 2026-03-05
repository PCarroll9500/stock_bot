#!/bin/bash
# run_close.sh — git pull, run close_of_day.py, push portfolio.json, send email report
#
# Cron entry (EC2, UTC):
#   30 19 * * 1-5 /home/ubuntu/stock_bot/scripts/run_close.sh

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_close_$(date +%Y-%m-%d).log"
exec >> "$LOG_FILE" 2>&1

echo "=== Close-of-day run started: $(date) ==="

cd "$REPO"
echo "Pulling latest code from GitHub..."
git pull origin main || echo "WARNING: git pull failed, continuing with existing code"

# Sell positions and record close prices
echo "Running close_of_day.py..."
"$REPO/.venv/bin/python" scripts/close_of_day.py
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "ERROR: close_of_day.py exited with code $EXIT_CODE"
    exit $EXIT_CODE
fi

# Push updated portfolio.json to GitHub
git add docs/data/portfolio.json
if ! git diff --staged --quiet; then
    git commit -m "portfolio: close $(date +%Y-%m-%d)"
    git push origin main
    echo "portfolio.json pushed to GitHub"
else
    echo "No portfolio changes to push"
fi

# Send close email report
echo "Sending close email report..."
"$REPO/.venv/bin/python" "$REPO/scripts/email_close_report.py" || echo "WARNING: email report failed"

echo "=== Close-of-day run finished: $(date) ==="
