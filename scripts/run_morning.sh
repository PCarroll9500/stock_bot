#!/bin/bash
# Morning trading run — called by cron at 9:31 AM ET (Mon–Fri)
# Runs the bot, then pushes updated portfolio.json to GitHub for the website.
#
# Cron entry:
#   31 9 * * 1-5 /home/patrick/dev/github/stock_bot/scripts/run_morning.sh

set -euo pipefail

REPO=/home/patrick/dev/github/stock_bot
DATE=$(date +%Y-%m-%d)
LOG="$REPO/logs/run_${DATE//-/}.log"

mkdir -p "$REPO/logs"

echo "[$(date)] Starting morning run" >> "$LOG"
cd "$REPO"

# Run the bot
"$REPO/.venv/bin/python" -m stock_bot.main >> "$LOG" 2>&1

# Push updated portfolio.json to GitHub so the website refreshes
git add docs/data/portfolio.json
if ! git diff --staged --quiet; then
    git commit -m "portfolio: morning picks $DATE"
    git push origin HEAD
    echo "[$(date)] portfolio.json pushed to GitHub" >> "$LOG"
else
    echo "[$(date)] No portfolio changes to push" >> "$LOG"
fi
