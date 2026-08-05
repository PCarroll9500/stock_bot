#!/usr/bin/env python3
"""
scripts/email_scoring_report.py

Send a weekly score-vs-returns feedback-loop summary via AWS SNS. Reads the
JSON report produced by `python scripts/analyze_scoring.py --quiet --output
docs/data/scoring_report.json` (see scripts/run_weekly_report.sh) and emails
a compact digest -- overall win rate/return, and the catalyst_type and
calibration breakdowns, since those are the two sections most likely to
surface an actionable change (see picker_config.json's catalyst_type_weights).

Usage: python scripts/email_scoring_report.py [--report PATH]
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = REPO_DIR / "docs" / "data" / "scoring_report.json"
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:818007714435:stock-bot-alerts"
DASHBOARD_URL = "https://pcarroll9500.github.io/stock_bot/"
AWS_REGION = "us-east-1"


def _fmt_table(rows: dict, top_n: int = 6) -> str:
    """Render a breakdown dict ({key: {n, win_rate, avg_return_pct, ...}}) as
    a fixed-width text table, sorted by sample size descending."""
    items = sorted(rows.items(), key=lambda kv: -kv[1]["n"])[:top_n]
    lines = [f"{'key':<16} {'n':>4} {'win%':>7} {'avg%':>8}"]
    for key, stats in items:
        lines.append(
            f"{key:<16} {stats['n']:>4} {stats['win_rate']:>6.1f}% {stats['avg_return_pct']:>7.2f}%"
        )
    return "\n".join(lines)


def build_message(report: dict) -> tuple[str, str]:
    n = report.get("n", 0)
    subject = f"[Stock Bot] Weekly scoring report -- {n} realized picks"

    if n == 0:
        return subject, "No realized picks found -- nothing to report yet."

    win_rate = report.get("overall_win_rate_pct", 0.0)
    avg_return = report.get("overall_avg_return_pct", 0.0)
    calib = report.get("calibration") or {}
    corr = calib.get("correlation")
    corr_str = f"{corr:+.3f}" if corr is not None else "undefined"

    slip = report.get("slippage") or {}
    slip_line = (
        f"  mean = {slip['mean_slippage_pct']:+.3f}%  n = {slip['n']}"
        if slip else "  no slippage_pct data yet"
    )

    body = f"""WEEKLY SCORING REPORT
{'=' * 38}

Realized picks analyzed: {n}
  ({report.get('native_catalyst_type_count', 0)} native catalyst_type, {report.get('keyword_classified_count', 0)} keyword-classified)

Overall win rate : {win_rate:.1f}%
Overall avg return: {avg_return:+.3f}%

BY CATALYST_TYPE (top 6 by sample size)
{'-' * 38}
{_fmt_table(report.get('by_catalyst_type', {}))}

BY SCORE
{'-' * 38}
{_fmt_table(report.get('by_score', {}))}

CALIBRATION (expected_gain_pct vs realized return)
{'-' * 38}
  n = {calib.get('n', 0)}
  correlation = {corr_str}
  mean expected = {calib.get('mean_expected_gain_pct', 0):.2f}%
  mean actual   = {calib.get('mean_actual_return_pct', 0):.2f}%

EXECUTION SLIPPAGE (pre-flight quote vs actual fill)
{'-' * 38}
{slip_line}

Full breakdown: run `python scripts/analyze_scoring.py` locally, or see
docs/data/scoring_report.json.

{DASHBOARD_URL}
"""
    return subject, body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH,
                         help="Path to the JSON report from analyze_scoring.py --output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger(__name__)

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.error("Could not read scoring report at %s: %s", args.report, exc)
        sys.exit(1)

    subject, body = build_message(report)

    try:
        boto3.client("sns", region_name=AWS_REGION).publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=body,
        )
        log.info("Scoring report sent: %s", subject)
    except ClientError as e:
        log.error("SNS publish failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
