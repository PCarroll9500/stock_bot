#!/usr/bin/env python3
"""
scripts/backtest_prompt.py

Offline backtest harness: replay historical LLM scoring inputs (logged by
catalyst_scorer to logs/llm_inputs/{date}/{ticker}_input.json) through
whatever catalyst_prompt.txt + picker_config.json is currently checked out,
and compare the new score/direction against:
  1. the ORIGINAL logged output (what the bot actually decided that day), and
  2. the REALIZED day_return_pct from docs/data/portfolio.json (what
     actually happened), when that ticker/date was picked and later closed.

This exists so a prompt or scoring-config change can be validated against
real historical inputs before it ships live, instead of the only feedback
loop being "wait a few weeks and see if scripts/analyze_scoring.py looks
better or worse."

KNOWN LIMITATION: spy_context and volume_context are not currently logged as
separate fields by core/llm_input_logger.py (only baked into the original
gpt_prompt_sent text), so replays use fixed placeholder values for both.
Score deltas caused by SPY-down-day or volume-modifier logic in
catalyst_prompt.txt will not replay accurately until log_llm_input() is
extended to store those two fields directly.

Usage:
    # Plumbing check only -- no OpenAI calls, no API key required:
    python scripts/backtest_prompt.py --start 2026-07-01 --end 2026-07-31 --dry-run

    # Real replay -- calls the OpenAI API via catalyst_scorer.score_candidates,
    # same as a live run, so OPENAI_API_KEY must be set:
    python scripts/backtest_prompt.py --start 2026-07-01 --end 2026-07-31
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_DEFAULT_LOG_DIR = _REPO_ROOT / "logs" / "llm_inputs"
_DEFAULT_PORTFOLIO = _REPO_ROOT / "docs" / "data" / "portfolio.json"

_PLACEHOLDER_SPY_CONTEXT = "SPY return unavailable."
_PLACEHOLDER_VOLUME_CONTEXT = "unavailable"


# ---------------------------------------------------------------------------
# Loading logged inputs/outputs
# ---------------------------------------------------------------------------

def _parse_article_dates(articles: list[dict]) -> list[dict]:
    """Logged articles have published_dt serialized to an ISO string (via
    json.dump's default=str). Parse it back to a datetime so catalyst_scorer's
    _age_label/_filter_stale_articles behave the same as they did live.
    Unparseable/missing values are left as None -- fail open, matching
    catalyst_scorer's own convention of never dropping news it can't date."""
    parsed = []
    for a in articles:
        a = dict(a)
        raw = a.get("published_dt")
        if isinstance(raw, str) and raw:
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                a["published_dt"] = dt
            except ValueError:
                a["published_dt"] = None
        elif not raw:
            a["published_dt"] = None
        parsed.append(a)
    return parsed


def iter_logged_inputs(
    log_dir: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[tuple[str, str, dict]]:
    """Discover every logged {ticker}_input.json under log_dir/{date}/,
    filtered to dates in [start_date, end_date] (inclusive, ISO format).

    Returns a list of (date, ticker, input_dict) sorted by (date, ticker).
    input_dict's "news" articles have published_dt parsed back to datetime.
    """
    if not log_dir.exists():
        return []

    results: list[tuple[str, str, dict]] = []
    for date_dir in sorted(log_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        date = date_dir.name
        if start_date and date < start_date:
            continue
        if end_date and date > end_date:
            continue
        for input_file in sorted(date_dir.glob("*_input.json")):
            ticker = input_file.stem.removesuffix("_input")
            try:
                data = json.loads(input_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            data["news"] = _parse_article_dates(data.get("news", []))
            results.append((date, ticker, data))
    return results


def load_original_output(log_dir: Path, date: str, ticker: str) -> dict | None:
    """Load the ORIGINAL gpt_response for a given date/ticker, if logged."""
    output_file = log_dir / date / f"{ticker}_output.json"
    if not output_file.exists():
        return None
    try:
        data = json.loads(output_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("gpt_response")


# ---------------------------------------------------------------------------
# Realized outcomes -- reuse portfolio.json, keyed for O(1) lookup by replay
# ---------------------------------------------------------------------------

def load_realized_outcomes(portfolio_paths: list[Path]) -> dict[tuple[str, str], float | None]:
    """Return {(date, ticker): day_return_pct} for every pick ever recorded
    (picked or not is irrelevant here -- only picks that were actually bought
    appear in portfolio.json at all). day_return_pct is None for picks whose
    session hasn't closed yet."""
    outcomes: dict[tuple[str, str], float | None] = {}
    for path in portfolio_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for session in data.get("sessions", []):
            date = session.get("date", "")
            for pick in session.get("picks", []):
                outcomes[(date, pick["ticker"])] = pick.get("day_return_pct")
    return outcomes


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def rescore_one(ticker: str, input_dict: dict, score_candidates_fn=None) -> dict:
    """Re-score a single logged input through the CURRENT prompt/config.

    score_candidates_fn defaults to catalyst_scorer.score_candidates (a real
    OpenAI call) -- injectable so tests/--dry-run can substitute a stub
    without a network call or API key.
    """
    if score_candidates_fn is None:
        from stock_bot.ai.catalyst_scorer import score_candidates as score_candidates_fn

    news_by_ticker = {ticker: input_dict.get("news", [])}
    trend_by_ticker = {ticker: input_dict.get("trends", "unavailable")}
    earnings = input_dict.get("earnings") or None
    sentiment = input_dict.get("sentiment") or None
    sec_filings = input_dict.get("sec_filings") or None

    results = score_candidates_fn(
        news_by_ticker, excluded=set(), trend_by_ticker=trend_by_ticker,
        spy_context=_PLACEHOLDER_SPY_CONTEXT,
        volume_by_ticker={ticker: _PLACEHOLDER_VOLUME_CONTEXT},
        earnings_by_ticker={ticker: earnings} if earnings else {},
        sentiment_by_ticker={ticker: sentiment} if sentiment else {},
        sec_by_ticker={ticker: sec_filings} if sec_filings else {},
    )
    return results[0] if results else {}


def run_backtest(
    log_dir: Path,
    start_date: str | None,
    end_date: str | None,
    portfolio_paths: list[Path],
    dry_run: bool = False,
    score_candidates_fn=None,
) -> list[dict]:
    """Replay every logged input in [start_date, end_date] and compare
    original vs. new score/direction, joined against realized returns.

    In dry-run mode, no scoring call is made -- new_score/new_direction are
    left None so the plumbing (discovery, parsing, joining) can be verified
    without an API key or network access.
    """
    logged = iter_logged_inputs(log_dir, start_date, end_date)
    outcomes = load_realized_outcomes(portfolio_paths)

    rows: list[dict] = []
    for date, ticker, input_dict in logged:
        original = load_original_output(log_dir, date, ticker) or {}
        row: dict = {
            "date": date,
            "ticker": ticker,
            "original_score": original.get("score"),
            "original_direction": original.get("direction"),
            "realized_return_pct": outcomes.get((date, ticker)),
        }
        if dry_run:
            row["new_score"] = None
            row["new_direction"] = None
        else:
            new = rescore_one(ticker, input_dict, score_candidates_fn=score_candidates_fn)
            row["new_score"] = new.get("score")
            row["new_direction"] = new.get("direction")
        if row.get("new_score") is not None and row.get("original_score") is not None:
            row["score_delta"] = row["new_score"] - row["original_score"]
            row["direction_flipped"] = row["new_direction"] != row["original_direction"]
        rows.append(row)
    return rows


def print_report(rows: list[dict]) -> None:
    if not rows:
        print("No logged inputs found in the given date range.")
        return

    print(f"Replayed {len(rows)} logged input(s)")
    flips = [r for r in rows if r.get("direction_flipped")]
    if flips:
        print(f"\n{len(flips)} direction flip(s):")
        for r in flips:
            outcome = (
                f"realized {r['realized_return_pct']:+.2f}%"
                if r.get("realized_return_pct") is not None else "not picked / not yet closed"
            )
            print(
                f"  {r['date']} {r['ticker']:<8} "
                f"{r['original_direction']}(score={r['original_score']}) -> "
                f"{r['new_direction']}(score={r['new_score']})  [{outcome}]"
            )

    scored = [r for r in rows if r.get("score_delta") is not None]
    if scored:
        avg_delta = sum(r["score_delta"] for r in scored) / len(scored)
        print(f"\nAverage score delta (new - original): {avg_delta:+.2f} over {len(scored)} replayed picks")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-dir", type=Path, default=_DEFAULT_LOG_DIR,
                         help="Directory of logged LLM inputs/outputs (default: logs/llm_inputs)")
    parser.add_argument("--start", type=str, default=None, help="Start date, ISO format (inclusive)")
    parser.add_argument("--end", type=str, default=None, help="End date, ISO format (inclusive)")
    parser.add_argument("--portfolio", type=Path, action="append", default=None,
                         help="portfolio.json path(s) to join realized returns against "
                              "(default: docs/data/portfolio.json). Repeatable.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Skip actual OpenAI calls -- verify discovery/joining plumbing only.")
    args = parser.parse_args()

    portfolio_paths = args.portfolio or [_DEFAULT_PORTFOLIO]

    if not args.dry_run:
        import os
        if not os.getenv("OPENAI_API_KEY"):
            print(
                "OPENAI_API_KEY is not set. Re-scoring requires a real API key "
                "(same as a live run). Use --dry-run to check plumbing without one.",
                file=sys.stderr,
            )
            sys.exit(1)

    rows = run_backtest(args.log_dir, args.start, args.end, portfolio_paths, dry_run=args.dry_run)
    print_report(rows)


if __name__ == "__main__":
    main()
