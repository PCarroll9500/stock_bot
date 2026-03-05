#!/usr/bin/env python3
"""
scripts/email_morning_report.py

Send a morning picks report via AWS SES after main.py runs.
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
SENDER = "patrickcarroll.it@outlook.com"
RECIPIENT = "patrickcarroll.it@outlook.com"
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
    return errors[-20:]  # cap at 20 lines


def build_email(session: dict, errors: list[str]) -> tuple[str, str, str]:
    picks = session.get("picks", [])
    mode = session.get("mode", "").upper()
    today = session.get("date", date.today().isoformat())
    open_val = session.get("portfolio_open_value", 0)
    total_invested = sum(p.get("buy_value", 0) for p in picks)
    cash = max(0.0, open_val - total_invested)

    subject = f"[Stock Bot] Morning Picks — {today}"

    # ── Plain text ────────────────────────────────────────────────────────────
    col = "{:<6}  {:>5}  {:>6}  {:>6}  {:>10}  {:>10}"
    header = col.format("TICKER", "SCORE", "ALLOC", "SHARES", "BUY PRICE", "BUY VALUE")
    divider = "-" * len(header)
    rows_txt = "\n".join(
        col.format(
            p["ticker"],
            p["score"],
            f"{p['allocation_pct']:.1f}%",
            p["shares"],
            f"${p['buy_price']:.2f}",
            f"${p['buy_value']:,.2f}",
        )
        for p in picks
    )
    errors_txt = "\n".join(errors) if errors else "None"

    text_body = f"""STOCK BOT — MORNING PICKS
{'='*55}
Date: {today}  |  Mode: {mode}
Account open value: ${open_val:,.2f}

{header}
{divider}
{rows_txt}
{divider}
Total invested : ${total_invested:,.2f}
Uninvested cash: ${cash:,.2f}

ERRORS / WARNINGS
{errors_txt}

Dashboard: {DASHBOARD_URL}
"""

    # ── HTML ──────────────────────────────────────────────────────────────────
    rows_html = ""
    for i, p in enumerate(picks, 1):
        direction_color = "#27ae60" if p.get("direction") == "bullish" else "#e74c3c"
        rows_html += f"""
        <tr>
          <td style="color:#888">{i}</td>
          <td><strong>{p['ticker']}</strong></td>
          <td style="text-align:center">{p['score']}</td>
          <td style="text-align:right">{p['allocation_pct']:.1f}%</td>
          <td style="color:{direction_color};text-align:center">{p.get('direction','').capitalize()}</td>
          <td style="text-align:right">{p['shares']:,}</td>
          <td style="text-align:right">${p['buy_price']:.2f}</td>
          <td style="text-align:right">${p['buy_value']:,.2f}</td>
          <td style="font-size:12px;color:#666;max-width:260px">{p.get('reason','')[:100]}</td>
        </tr>"""

    errors_html = (
        "<ul style='margin:0;padding-left:20px'>"
        + "".join(f"<li style='color:#c0392b;font-size:13px'>{e}</li>" for e in errors)
        + "</ul>"
        if errors
        else "<span style='color:#27ae60'>None</span>"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body{{font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:960px;margin:auto;padding:20px;}}
  h1{{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px;margin-bottom:6px;}}
  h2{{color:#2c3e50;margin-top:28px;margin-bottom:8px;font-size:16px;}}
  table{{border-collapse:collapse;width:100%;margin-top:8px;}}
  th{{background:#3498db;color:#fff;padding:9px 12px;text-align:left;font-size:13px;}}
  td{{padding:8px 12px;border-bottom:1px solid #eee;vertical-align:top;}}
  tr:hover td{{background:#f5f9ff;}}
  .chip{{display:inline-block;background:#eaf4ff;border-radius:4px;padding:4px 10px;margin:4px;}}
  .chip-label{{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;display:block;}}
  .chip-value{{font-size:17px;font-weight:700;color:#2c3e50;}}
  .footer{{margin-top:30px;padding-top:14px;border-top:1px solid #ddd;color:#999;font-size:12px;}}
  a{{color:#3498db;text-decoration:none;}}
</style>
</head>
<body>
<h1>🤖 Stock Bot &mdash; Morning Picks</h1>
<p style="color:#666;margin-top:0"><strong>Date:</strong> {today} &nbsp;|&nbsp; <strong>Mode:</strong> {mode}</p>

<div style="margin:16px 0">
  <span class="chip"><span class="chip-label">Account Value</span><span class="chip-value">${open_val:,.2f}</span></span>
  <span class="chip"><span class="chip-label">Invested</span><span class="chip-value">${total_invested:,.2f}</span></span>
  <span class="chip"><span class="chip-label">Cash</span><span class="chip-value">${cash:,.2f}</span></span>
  <span class="chip"><span class="chip-label">Positions</span><span class="chip-value">{len(picks)}</span></span>
</div>

<h2>Picks &amp; Purchases</h2>
<table>
  <thead><tr>
    <th>#</th><th>Ticker</th><th>Score</th><th>Alloc</th>
    <th>Direction</th><th>Shares</th><th>Buy Price</th><th>Buy Value</th><th>Reason</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Errors / Warnings</h2>
{errors_html}

<div class="footer">
  <a href="{DASHBOARD_URL}">📊 View Dashboard &rarr;</a>
  &nbsp;|&nbsp; Stock Bot &bull; EC2 us-east-1
</div>
</body></html>"""

    return subject, text_body, html_body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Use portfolio_test.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger(__name__)

    session = load_today_session(args.test)
    if not session:
        log.error("No session found for today — skipping email")
        sys.exit(1)

    errors = collect_errors()
    subject, text_body, html_body = build_email(session, errors)

    try:
        boto3.client("ses", region_name=AWS_REGION).send_email(
            Source=SENDER,
            Destination={"ToAddresses": [RECIPIENT]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": text_body}, "Html": {"Data": html_body}},
            },
        )
        log.info("Morning report sent: %s", subject)
    except ClientError as e:
        log.error("SES send failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
