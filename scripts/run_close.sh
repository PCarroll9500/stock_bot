#!/bin/bash
# End-of-day run — called by cron at 4:05 PM ET (Mon–Fri)
# Sells all positions, records close prices, then pushes portfolio.json to GitHub.
#
# Cron entry:
#   5 16 * * 1-5 /home/patrick/dev/github/stock_bot/scripts/run_close.sh

set -euo pipefail

REPO=/home/patrick/dev/github/stock_bot
DATE=$(date +%Y-%m-%d)
LOG="$REPO/logs/close_${DATE//-/}.log"

mkdir -p "$REPO/logs"

echo "[$(date)] Starting close-of-day run" >> "$LOG"
cd "$REPO"

# Sell positions and record close prices
"$REPO/.venv/bin/python" scripts/close_of_day.py >> "$LOG" 2>&1

# Push updated portfolio.json to GitHub so the website refreshes
git add docs/data/portfolio.json
if ! git diff --staged --quiet; then
    git commit -m "portfolio: close $DATE"
    git push origin HEAD
    echo "[$(date)] portfolio.json pushed to GitHub" >> "$LOG"
else
    echo "[$(date)] No portfolio changes to push" >> "$LOG"
fi
