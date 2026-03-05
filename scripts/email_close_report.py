#!/usr/bin/env python3
"""
scripts/email_close_report.py

Send a close-of-day report via AWS SES after close_of_day.py runs.
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
SENDER = "patrickcarroll.it@outlook.com"
RECIPIENT = "patrickcarroll.it@outlook.com"
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


def build_email(session: dict, portfolio: dict, errors: list[str]) -> tuple[str, str, str]:
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

    # Sign helpers
    def signed(val: float, fmt: str = ".2f") -> str:
        return f"+${val:{fmt}}" if val >= 0 else f"-${abs(val):{fmt}}"

    def signed_pct(val: float) -> str:
        return f"+{val:.2f}%" if val >= 0 else f"{val:.2f}%"

    day_color = "#27ae60" if day_return_usd >= 0 else "#e74c3c"
    total_color = "#27ae60" if total_return_usd >= 0 else "#e74c3c"

    subject = (
        f"[Stock Bot] Close Report — "
        f"{signed_pct(day_return_pct)} ({signed(day_return_usd)}) | {today}"
    )

    # ── Plain text ────────────────────────────────────────────────────────────
    col = "{:<6}  {:>5}  {:>6}  {:>6}  {:>10}  {:>10}  {:>9}  {:>10}"
    header = col.format("TICKER", "SCORE", "ALLOC", "SHARES", "BUY", "CLOSE", "DAY RTN", "DAY P/L")
    divider = "-" * len(header)
    rows_txt = "\n".join(
        col.format(
            p["ticker"],
            p["score"],
            f"{p['allocation_pct']:.1f}%",
            p["shares"],
            f"${p.get('buy_price', 0):.2f}",
            f"${p.get('close_price', 0):.2f}" if p.get("close_price") else "N/A",
            f"{signed_pct(p.get('day_return_pct', 0))}",
            f"{signed(p.get('day_return_usd', 0))}",
        )
        for p in picks
    )
    errors_txt = "\n".join(errors) if errors else "None"

    text_body = f"""STOCK BOT — CLOSE OF DAY REPORT
{'='*60}
Date: {today}  |  Mode: {mode}

RESULTS
{header}
{divider}
{rows_txt}
{divider}

ACCOUNT SUMMARY
  Open value    : ${open_val:>12,.2f}
  Close value   : ${close_val:>12,.2f}
  Day return    : {signed(day_return_usd):>12}  ({signed_pct(day_return_pct)})
  vs QQQ        : {signed_pct(qqq_pct):>12}

TOTAL RETURN (since inception)
  Initial invest: ${initial:>12,.2f}
  Current value : ${close_val:>12,.2f}
  Total return  : {signed(total_return_usd):>12}  ({signed_pct(total_return_pct)})

ERRORS / WARNINGS
{errors_txt}

Dashboard: {DASHBOARD_URL}
"""

    # ── HTML ──────────────────────────────────────────────────────────────────
    rows_html = ""
    for i, p in enumerate(picks, 1):
        ret_pct = p.get("day_return_pct", 0)
        ret_usd = p.get("day_return_usd", 0)
        ret_color = "#27ae60" if ret_usd >= 0 else "#e74c3c"
        close_px = f"${p['close_price']:.2f}" if p.get("close_price") else "N/A"
        rows_html += f"""
        <tr>
          <td style="color:#888">{i}</td>
          <td><strong>{p['ticker']}</strong></td>
          <td style="text-align:center">{p['score']}</td>
          <td style="text-align:right">{p['allocation_pct']:.1f}%</td>
          <td style="text-align:right">{p['shares']:,}</td>
          <td style="text-align:right">${p.get('buy_price',0):.2f}</td>
          <td style="text-align:right">{close_px}</td>
          <td style="text-align:right;color:{ret_color}">{signed_pct(ret_pct)}</td>
          <td style="text-align:right;color:{ret_color};font-weight:600">{signed(ret_usd)}</td>
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
  th{{background:#2c3e50;color:#fff;padding:9px 12px;text-align:left;font-size:13px;}}
  td{{padding:8px 12px;border-bottom:1px solid #eee;}}
  tr:hover td{{background:#f5f9ff;}}
  .chip{{display:inline-block;background:#eaf4ff;border-radius:6px;padding:8px 14px;margin:4px;}}
  .chip-label{{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;display:block;}}
  .chip-value{{font-size:18px;font-weight:700;color:#2c3e50;}}
  .summary-table td{{border:none;padding:5px 14px 5px 0;font-size:14px;}}
  .footer{{margin-top:30px;padding-top:14px;border-top:1px solid #ddd;color:#999;font-size:12px;}}
  a{{color:#3498db;text-decoration:none;}}
</style>
</head>
<body>
<h1>🏁 Stock Bot &mdash; Close of Day Report</h1>
<p style="color:#666;margin-top:0"><strong>Date:</strong> {today} &nbsp;|&nbsp; <strong>Mode:</strong> {mode}</p>

<div style="margin:16px 0">
  <span class="chip"><span class="chip-label">Open Value</span><span class="chip-value">${open_val:,.2f}</span></span>
  <span class="chip"><span class="chip-label">Close Value</span><span class="chip-value">${close_val:,.2f}</span></span>
  <span class="chip"><span class="chip-label">Day Return</span>
    <span class="chip-value" style="color:{day_color}">{signed_pct(day_return_pct)}</span></span>
  <span class="chip"><span class="chip-label">Day P&amp;L</span>
    <span class="chip-value" style="color:{day_color}">{signed(day_return_usd)}</span></span>
  <span class="chip"><span class="chip-label">vs QQQ</span>
    <span class="chip-value" style="color:#888">{signed_pct(qqq_pct)}</span></span>
</div>

<h2>Position Results</h2>
<table>
  <thead><tr>
    <th>#</th><th>Ticker</th><th>Score</th><th>Alloc</th>
    <th>Shares</th><th>Buy Price</th><th>Close Price</th><th>Day Return</th><th>Day P/L</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Account Summary</h2>
<table class="summary-table">
  <tr><td style="color:#888">Open value</td><td><strong>${open_val:,.2f}</strong></td></tr>
  <tr><td style="color:#888">Close value</td><td><strong>${close_val:,.2f}</strong></td></tr>
  <tr><td style="color:#888">Day return</td>
      <td><strong style="color:{day_color}">{signed(day_return_usd)} ({signed_pct(day_return_pct)})</strong></td></tr>
  <tr><td style="color:#888">vs QQQ</td><td><strong>{signed_pct(qqq_pct)}</strong></td></tr>
</table>

<h2>Total Return (since inception)</h2>
<table class="summary-table">
  <tr><td style="color:#888">Initial investment</td><td><strong>${initial:,.2f}</strong></td></tr>
  <tr><td style="color:#888">Current value</td><td><strong>${close_val:,.2f}</strong></td></tr>
  <tr><td style="color:#888">Total return</td>
      <td><strong style="color:{total_color};font-size:16px">{signed(total_return_usd)} ({signed_pct(total_return_pct)})</strong></td></tr>
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

    session, portfolio = load_today_session(args.test)
    if not session:
        log.error("No session found for today — skipping email")
        sys.exit(1)

    errors = collect_errors()
    subject, text_body, html_body = build_email(session, portfolio, errors)

    try:
        boto3.client("ses", region_name=AWS_REGION).send_email(
            Source=SENDER,
            Destination={"ToAddresses": [RECIPIENT]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": text_body}, "Html": {"Data": html_body}},
            },
        )
        log.info("Close report sent: %s", subject)
    except ClientError as e:
        log.error("SES send failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
