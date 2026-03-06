#!/usr/bin/env python3
"""
scripts/email_morning_report.py

Send a morning picks report via AWS SNS after main.py runs.
Usage: python scripts/email_morning_report.py [--test]
"""
import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO_DIR = Path(__file__).resolve().parents[1]
PORTFOLIO_PATH = REPO_DIR / "docs" / "data" / "portfolio.json"
LOG_DIR = REPO_DIR / "logs"
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:818007714435:stock-bot-alerts"
DASHBOARD_URL = "https://pcarroll9500.github.io/stock_bot/"
AWS_REGION = "us-east-1"


def load_today_session(test_mode: bool = False) -> dict | None:
    path = PORTFOLIO_PATH.parent / ("portfolio_test.json" if test_mode else "portfolio.json")
    portfolio = json.loads(path.read_text())
    today = date.today().isoformat()
    return next((s for s in portfolio.get("sessions", []) if s.get("date") == today), None)


def collect_errors() -> list[str]:
    today = date.today().isoformat()
    log_file = LOG_DIR / f"run_morning_{today}.log"
    errors = []
    if log_file.exists():
        for line in log_file.read_text().splitlines():
            upper = line.upper()
            if "ERROR" in upper or ("WARNING" in upper and "git pull" not in line.lower()):
                errors.append(line.strip())
    return errors[-20:]


def build_message(session: dict, errors: list[str]) -> tuple[str, str]:
    picks = session.get("picks", [])
    mode = session.get("mode", "").upper()
    today = session.get("date", date.today().isoformat())
    open_val = session.get("portfolio_open_value", 0)
    total_invested = sum(p.get("buy_value", 0) for p in picks)
    cash = max(0.0, open_val - total_invested)

    subject = f"[Stock Bot] Morning Picks -- {today}"

    pick_lines = ""
    for p in picks:
        reason = p.get("reason", "")
        pick_lines += f"""{p['ticker']}  (score: {p['score']})
  {p['shares']} shares @ ${p['buy_price']:.2f} = ${p['buy_value']:,.2f}
  {reason}
"""

    errors_txt = "\n".join(f"  {e}" for e in errors) if errors else "  None"

    body = f"""MORNING PICKS  |  {today}  |  {mode}
{'='*38}

Account value : ${open_val:,.2f}
Total invested: ${total_invested:,.2f}
Uninvested    : ${cash:,.2f}

PICKS ({len(picks)})
{'-'*38}
{pick_lines.rstrip()}

ERRORS / WARNINGS
{'-'*38}
{errors_txt}

{DASHBOARD_URL}
"""
    return subject, body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Use portfolio_test.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger(__name__)

    session = load_today_session(args.test)
    if not session:
        log.error("No session found for today -- skipping email")
        sys.exit(1)

    errors = collect_errors()
    subject, body = build_message(session, errors)

    try:
        boto3.client("sns", region_name=AWS_REGION).publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=body,
        )
        log.info("Morning report sent: %s", subject)
    except ClientError as e:
        log.error("SNS publish failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
