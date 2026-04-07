#!/bin/bash
# run_close.sh — git pull, run close_of_day.py, push portfolio.json, send email report
#
# Cron entry (EC2, ET — America/New_York):
#   30 14 * * 1-5 /home/ubuntu/stock_bot/scripts/run_close.sh
#   (2:30 PM ET — sells ~90 min before market close, before EventBridge stops instance at 3:30 PM)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_close_$(date +%Y-%m-%d).log"
exec >> "$LOG_FILE" 2>&1

echo "=== Close-of-day run started: $(date) ==="

cd "$REPO"

# Load env vars (includes GITHUB_PAT for authenticated git push)
set -a; source "$REPO/.env" 2>/dev/null || true; set +a

# Configure git remote with PAT so push works without interactive credentials
if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_USER:-}" ]; then
    git remote set-url origin "https://${GITHUB_USER}:${GITHUB_PAT}@github.com/${GITHUB_USER}/stock_bot.git"
fi

echo "Pulling latest code from GitHub..."
git pull origin deploy || echo "WARNING: git pull failed, continuing with existing code"

# Sell positions and record close prices
# Hard 45-min timeout — EventBridge stops the instance at 3:30 PM (1h after this runs)
# so close_of_day must finish with time to spare for the git push.
echo "Running close_of_day.py..."
timeout 2700 "$REPO/.venv/bin/python" scripts/close_of_day.py
EXIT_CODE=$?

# Only push portfolio.json if close_of_day succeeded — prevents corrupting dashboard on failure
if [ $EXIT_CODE -ne 0 ]; then
    echo "ERROR: close_of_day.py exited with code $EXIT_CODE — skipping portfolio push to avoid corrupt data"
else
    git add docs/data/portfolio.json
    if ! git diff --staged --quiet; then
        git commit -m "portfolio: close $(date +%Y-%m-%d)"
        git push origin deploy
        echo "portfolio.json pushed to GitHub"
    else
        echo "No portfolio changes to push"
    fi
fi

# Send close email report
echo "Sending close email report..."
"$REPO/.venv/bin/python" "$REPO/scripts/email_close_report.py" || echo "WARNING: email report failed"

echo "=== Close-of-day run finished: $(date) ==="
