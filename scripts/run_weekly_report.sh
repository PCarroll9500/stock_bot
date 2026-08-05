#!/bin/bash
# run_weekly_report.sh — git pull, run analyze_scoring.py, push scoring_report.json, email digest
#
# Closes the score-vs-returns feedback loop automatically instead of relying on
# someone remembering to run scripts/analyze_scoring.py by hand. See
# picker_config.json's catalyst_type_weights / risk_penalty comments for how
# this report's findings are meant to feed back into config.
#
# Cron entry (EC2, UTC — America/New_York):
#   0 13 * * 0 /home/ubuntu/stock_bot/scripts/run_weekly_report.sh
#   (Sunday 8:00 AM ET — before the Monday morning run, no market-hours time pressure)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_weekly_report_$(date +%Y-%m-%d).log"
exec >> "$LOG_FILE" 2>&1

echo "=== Weekly scoring report run started: $(date) ==="

cd "$REPO"

# Load env vars (includes GITHUB_PAT for authenticated git push)
set -a; source "$REPO/.env" 2>/dev/null || true; set +a

# Configure git remote with PAT so push works without interactive credentials
if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_USER:-}" ]; then
    git remote set-url origin "https://${GITHUB_USER}:${GITHUB_PAT}@github.com/${GITHUB_USER}/stock_bot.git"
fi

echo "Pulling latest code from GitHub..."
git pull origin deploy || echo "WARNING: git pull failed, continuing with existing code"

echo "Running scripts/analyze_scoring.py..."
"$REPO/.venv/bin/python" scripts/analyze_scoring.py docs/data/portfolio.json \
    --quiet --output docs/data/scoring_report.json
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "ERROR: analyze_scoring.py exited with code $EXIT_CODE — skipping push/email"
    exit $EXIT_CODE
fi

git add docs/data/scoring_report.json
if ! git diff --staged --quiet; then
    git commit -m "scoring: weekly report $(date +%Y-%m-%d)"
    git push origin deploy
    echo "scoring_report.json pushed to GitHub"
else
    echo "No scoring_report.json changes to push"
fi

echo "Sending weekly scoring report email..."
"$REPO/.venv/bin/python" "$REPO/scripts/email_scoring_report.py" || echo "WARNING: email report failed"

echo "=== Weekly scoring report run finished: $(date) ==="
