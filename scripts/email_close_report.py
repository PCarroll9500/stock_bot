#!/usr/bin/env python3
"""
scripts/email_close_report.py

Send a close-of-day report via AWS SNS after close_of_day.py runs.
Usage: python scripts/email_close_report.py [--test]
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


def load_today_session(test_mode: bool = False) -> tuple[dict | None, dict]:
    path = PORTFOLIO_PATH.parent / ("portfolio_test.json" if test_mode else "portfolio.json")
    portfolio = json.loads(path.read_text())
    today = date.today().isoformat()
    session = next((s for s in portfolio.get("sessions", []) if s.get("date") == today), None)
    return session, portfolio


def collect_errors() -> list[str]:
    today = date.today().isoformat()
    errors = []
    for log_name in (f"run_close_{today}.log", f"close_of_day_{today}.log"):
        log_file = LOG_DIR / log_name
        if log_file.exists():
            for line in log_file.read_text().splitlines():
                upper = line.upper()
                if "ERROR" in upper or ("WARNING" in upper and "git pull" not in line.lower()):
                    errors.append(line.strip())
    return errors[-20:]


def build_message(session: dict, portfolio: dict, errors: list[str]) -> tuple[str, str]:
    picks = session.get("picks", [])
    mode = session.get("mode", "").upper()
    today = session.get("date", date.today().isoformat())

    open_val = session.get("portfolio_open_value", 0)
    close_val = session.get("portfolio_close_value", 0)
    day_return_usd = session.get("session_return_usd", 0)
    day_return_pct = session.get("session_return_pct", 0)
    qqq_pct = session.get("qqq_day_return_pct") or 0

    initial = float(portfolio.get("initial_investment", 10_000))
    total_return_usd = close_val - initial
    total_return_pct = (total_return_usd / initial * 100) if initial else 0

    def signed(val: float) -> str:
        return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"

    def signed_pct(val: float) -> str:
        return f"+{val:.2f}%" if val >= 0 else f"{val:.2f}%"

    subject = (
        f"[Stock Bot] Close -- "
        f"{signed_pct(day_return_pct)} ({signed(day_return_usd)}) | {today}"
    )

    pick_lines = ""
    for p in picks:
        ret_usd = p.get("day_return_usd", 0)
        ret_pct = p.get("day_return_pct", 0)
        close_px = f"${p['close_price']:.2f}" if p.get("close_price") else "N/A"
        pick_lines += f"""{p['ticker']}  {signed_pct(ret_pct)} ({signed(ret_usd)})
  Buy ${p.get('buy_price', 0):.2f}  ->  Close {close_px}
  {p['shares']} shares  |  score: {p['score']}
"""

    errors_txt = "\n".join(f"  {e}" for e in errors) if errors else "  None"

    body = f"""CLOSE REPORT  |  {today}  |  {mode}
{'='*38}

TODAY
  Return : {signed_pct(day_return_pct)} ({signed(day_return_usd)})
  vs QQQ : {signed_pct(qqq_pct)}
  Open   : ${open_val:,.2f}
  Close  : ${close_val:,.2f}

ALL TIME
  Invested: ${initial:,.2f}
  Value   : ${close_val:,.2f}
  Return  : {signed_pct(total_return_pct)} ({signed(total_return_usd)})

POSITIONS ({len(picks)})
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

    session, portfolio = load_today_session(args.test)
    if not session:
        log.error("No session found for today -- skipping email")
        sys.exit(1)

    errors = collect_errors()
    subject, body = build_message(session, portfolio, errors)

    try:
        boto3.client("sns", region_name=AWS_REGION).publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=body,
        )
        log.info("Close report sent: %s", subject)
    except ClientError as e:
        log.error("SNS publish failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
