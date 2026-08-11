#!/usr/bin/env python3
"""
scripts/analyze_scoring.py

The score-vs-returns feedback loop: joins every historical pick's GPT score,
sector, and catalyst_type against its actual realized day_return_pct, so we
can see what's actually working instead of guessing.

Usage:
    python scripts/analyze_scoring.py [portfolio.json ...]

With no arguments, reads docs/data/portfolio.json. Multiple files are merged
(useful for combining today's live portfolio.json with an archived snapshot
pulled from git history, e.g. via `git show <ref>:docs/data/portfolio.json`).

Picks recorded before the catalyst_type field existed (2026-08 and earlier)
fall back to a keyword classifier over the `reason` text — approximate, but
good enough to spot broad patterns until enough native catalyst_type data
accumulates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Keyword fallback classifier for picks recorded before catalyst_type existed
# ---------------------------------------------------------------------------

# Order matters: first matching pattern wins. Mirrors the priority guidance
# in catalyst_prompt.txt's catalyst_type field description.
_KEYWORD_RULES: list[tuple[str, re.Pattern]] = [
    ("earnings_beat", re.compile(r"earnings beat|revenue beat|record (revenue|quarter)|raised guidance|beat.*guidance", re.IGNORECASE)),
    ("ma_acquisition", re.compile(r"acquisition|acquire[ds]?|merger|buyout|all-cash deal|deal at \$", re.IGNORECASE)),
    ("fda_approval", re.compile(r"\bFDA\b|drug approval|clinical trial", re.IGNORECASE)),
    ("insider_buying", re.compile(r"insider (buy|buying)|Form 4|CEO buy|CFO buy", re.IGNORECASE)),
    ("contract_win", re.compile(r"contract win|new contract|major contract|disclosed revenue", re.IGNORECASE)),
    ("short_squeeze", re.compile(r"short squeeze|short interest|squeeze", re.IGNORECASE)),
    ("guidance_raise", re.compile(r"guidance raise|raised (full-year|fy) guidance|guidance hike", re.IGNORECASE)),
    ("analyst_action", re.compile(r"analyst|price target|upgrade|initiat(ed|es).*buy|target raise", re.IGNORECASE)),
    ("sector_tailwind", re.compile(r"sector tailwind|industry-wide|broad.*(rally|move|tailwind)|sympathy move", re.IGNORECASE)),
]


def classify_catalyst_type(pick: dict) -> str:
    """Return pick['catalyst_type'] if present (native data), else classify
    the reason text with keyword rules, else 'other'."""
    native = pick.get("catalyst_type")
    if native:
        return native
    reason = pick.get("reason", "") or ""
    for label, pattern in _KEYWORD_RULES:
        if pattern.search(reason):
            return label
    return "other"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_picks(paths: list[Path]) -> list[dict]:
    """Load and merge all realized picks (day_return_pct is not None) from
    one or more portfolio.json files."""
    picks: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (date, ticker) — dedupe across overlapping files
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for session in data.get("sessions", []):
            date = session.get("date", "")
            for pick in session.get("picks", []):
                if pick.get("day_return_pct") is None:
                    continue
                key = (date, pick["ticker"])
                if key in seen:
                    continue
                seen.add(key)
                enriched = dict(pick)
                enriched["_date"] = date
                enriched["_catalyst_type"] = classify_catalyst_type(pick)
                picks.append(enriched)
    return picks


# ---------------------------------------------------------------------------
# Breakdown computation
# ---------------------------------------------------------------------------

def _breakdown(picks: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        groups[key_fn(p)].append(p)

    result: dict[str, dict] = {}
    for key, group in groups.items():
        returns = [p["day_return_pct"] for p in group]
        wins = [r for r in returns if r > 0]
        result[key] = {
            "n": len(group),
            "win_rate": len(wins) / len(group) * 100,
            "avg_return_pct": sum(returns) / len(group),
            "total_return_pct": sum(returns),
            "best": max(returns),
            "worst": min(returns),
        }
    return result


def breakdown_by_score(picks: list[dict]) -> dict[str, dict]:
    return _breakdown(picks, lambda p: str(p.get("score", "?")))


def breakdown_by_sector(picks: list[dict]) -> dict[str, dict]:
    return _breakdown(picks, lambda p: p.get("sector") or "Unknown")


def breakdown_by_catalyst_type(picks: list[dict]) -> dict[str, dict]:
    return _breakdown(picks, lambda p: p["_catalyst_type"])


def breakdown_by_risk(picks: list[dict]) -> dict[str, dict]:
    return _breakdown(picks, lambda p: str(p.get("risk", "?")))


# ---------------------------------------------------------------------------
# Calibration: does GPT's stated expected_gain_pct actually predict the
# realized day_return_pct, or is it just a plausible-sounding number?
# ---------------------------------------------------------------------------

_GAIN_BUCKET_ORDER = ["0-1%", "1-3%", "3-6%", "6-10%", "10%+", "?"]


def _gain_bucket(pick: dict) -> str:
    gain = pick.get("expected_gain_pct")
    if gain is None:
        return "?"
    if gain < 1:
        return "0-1%"
    if gain < 3:
        return "1-3%"
    if gain < 6:
        return "3-6%"
    if gain < 10:
        return "6-10%"
    return "10%+"


def breakdown_by_expected_gain_bucket(picks: list[dict]) -> dict[str, dict]:
    return _breakdown(picks, _gain_bucket)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient. Returns None if undefined (n<2 or
    zero variance in either series)."""
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def calibration_summary(picks: list[dict]) -> dict | None:
    """Compare GPT's expected_gain_pct against the realized day_return_pct.

    A well-calibrated model should show a positive correlation and a mean
    expected value in the same ballpark as the mean realized return. If the
    correlation is near zero, expected_gain_pct is decorative — the number
    GPT prints is not actually informative about outcomes, regardless of how
    well score/catalyst_type predict direction.
    """
    pairs = [
        (p["expected_gain_pct"], p["day_return_pct"])
        for p in picks
        if p.get("expected_gain_pct") is not None
    ]
    if not pairs:
        return None
    xs, ys = [x for x, _ in pairs], [y for _, y in pairs]
    return {
        "n": len(pairs),
        "correlation": _pearson(xs, ys),
        "mean_expected_gain_pct": sum(xs) / len(xs),
        "mean_actual_return_pct": sum(ys) / len(ys),
    }


# ---------------------------------------------------------------------------
# Slippage: is the ~0% average edge per pick (see overall stats) actually
# being eaten by execution cost rather than bad scoring? slippage_pct is
# populated by portfolio_writer.write_session only for confirmed real fills
# (pre-flight quote vs actual avgFillPrice) -- see main.py's
# preflight_price_by_ticker. Picks recorded before this field existed, or
# whose order was never confirmed filled, simply have no slippage_pct.
# ---------------------------------------------------------------------------

def slippage_summary(picks: list[dict]) -> dict | None:
    """Mean/best/worst execution slippage across picks that have a recorded
    slippage_pct. A consistently positive mean (paying more than the
    pre-flight quote) directly explains part of any near-zero overall edge,
    independent of whether the scoring itself has predictive power."""
    values = [p["slippage_pct"] for p in picks if p.get("slippage_pct") is not None]
    if not values:
        return None
    return {
        "n": len(values),
        "mean_slippage_pct": sum(values) / len(values),
        "best": min(values),  # most negative = best (paid less than quoted)
        "worst": max(values),  # most positive = worst (paid more than quoted)
    }


def stop_slippage_summary(picks: list[dict]) -> dict | None:
    """Mean/best/worst EXIT slippage on stop-loss-triggered picks --
    stop_slippage_pct (added 2026-08-10 in close_of_day.py) compares the
    actual exit fill against buy_price * (1 - trail_pct/100), the hard floor
    a trailing stop's trigger can never go below. A negative mean means
    exits are landing worse than that guaranteed floor on average -- the
    trailing-stop order becomes a market order once triggered, so a fast
    decline can blow through the intended trail width. Use this to judge
    whether trailing_stop_limit_offset_pct (the TRAIL LIMIT fix) is actually
    closing the gap once enough post-fix fills accumulate."""
    values = [p["stop_slippage_pct"] for p in picks if p.get("stop_slippage_pct") is not None]
    if not values:
        return None
    return {
        "n": len(values),
        "mean_stop_slippage_pct": sum(values) / len(values),
        "best": max(values),  # least negative/most positive = best
        "worst": min(values),  # most negative = worst (blew furthest through the floor)
    }


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

def _print_table(title: str, breakdown: dict[str, dict], sort_by_n: bool = True, order: list[str] | None = None) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if order is not None:
        rows = [(k, breakdown[k]) for k in order if k in breakdown]
        rows += [(k, v) for k, v in breakdown.items() if k not in order]  # unexpected keys, keep visible
    else:
        rows = sorted(breakdown.items(), key=lambda kv: -kv[1]["n"] if sort_by_n else kv[0])
    print(f"{'key':<18} {'n':>4} {'win%':>7} {'avg%':>8} {'total%':>8} {'best%':>7} {'worst%':>7}")
    for key, stats in rows:
        print(
            f"{key:<18} {stats['n']:>4} {stats['win_rate']:>6.1f}% "
            f"{stats['avg_return_pct']:>7.2f}% {stats['total_return_pct']:>7.2f}% "
            f"{stats['best']:>6.2f}% {stats['worst']:>6.2f}%"
        )


def print_report(picks: list[dict]) -> None:
    if not picks:
        print("No realized picks found (no sessions with day_return_pct set).")
        return

    returns = [p["day_return_pct"] for p in picks]
    wins = [r for r in returns if r > 0]
    native_count = sum(1 for p in picks if p.get("catalyst_type"))

    print(f"Total realized picks: {len(picks)}")
    print(f"  ({native_count} with native catalyst_type, {len(picks) - native_count} keyword-classified)")
    print(f"Overall win rate: {len(wins) / len(picks) * 100:.1f}%")
    print(f"Overall avg return: {sum(returns) / len(picks):.3f}%")

    _print_table("By score", breakdown_by_score(picks), sort_by_n=False)
    _print_table("By catalyst_type", breakdown_by_catalyst_type(picks))
    _print_table("By sector", breakdown_by_sector(picks))
    _print_table("By risk", breakdown_by_risk(picks), sort_by_n=False)
    _print_table("By expected_gain_pct bucket", breakdown_by_expected_gain_bucket(picks), order=_GAIN_BUCKET_ORDER)

    calib = calibration_summary(picks)
    print("\nCalibration: expected_gain_pct vs. realized day_return_pct")
    print("-" * 58)
    if calib is None:
        print("  Not enough data (need >=2 picks with expected_gain_pct set).")
    else:
        corr = calib["correlation"]
        corr_str = f"{corr:+.3f}" if corr is not None else "undefined (zero variance)"
        print(f"  n = {calib['n']}")
        print(f"  correlation(expected_gain_pct, day_return_pct) = {corr_str}")
        print(f"  mean expected_gain_pct = {calib['mean_expected_gain_pct']:.2f}%")
        print(f"  mean actual day_return_pct = {calib['mean_actual_return_pct']:.2f}%")
        if corr is not None and corr < 0.1:
            print(
                "  -> Near-zero or negative correlation: expected_gain_pct is not currently "
                "predictive of realized returns. Treat it as descriptive color in the prompt "
                "output, not a sizing input, until this improves."
            )

    slip = slippage_summary(picks)
    print("\nExecution slippage: pre-flight quote vs. actual fill")
    print("-" * 53)
    if slip is None:
        print("  No slippage_pct data yet (picks predate preflight_price tracking, "
              "or none had a confirmed fill).")
    else:
        print(f"  n = {slip['n']}")
        print(f"  mean slippage = {slip['mean_slippage_pct']:+.3f}%  (positive = paid more than quoted)")
        print(f"  best = {slip['best']:+.3f}%   worst = {slip['worst']:+.3f}%")
        if slip["mean_slippage_pct"] > 0.05:
            print(
                "  -> Positive mean slippage is directly eating into any scoring edge -- "
                "compare against the overall avg return above to see how much of the gap it explains."
            )

    stop_slip = stop_slippage_summary(picks)
    print("\nStop-loss exit slippage: guaranteed floor vs. actual fill")
    print("-" * 59)
    if stop_slip is None:
        print("  No stop_slippage_pct data yet (picks predate the 2026-08-10 tracking, "
              "or none were stop-loss-triggered).")
    else:
        print(f"  n = {stop_slip['n']}")
        print(f"  mean = {stop_slip['mean_stop_slippage_pct']:+.3f}%  (negative = exit priced worse than the guaranteed floor)")
        print(f"  best = {stop_slip['best']:+.3f}%   worst = {stop_slip['worst']:+.3f}%")
        if stop_slip["mean_stop_slippage_pct"] < -0.05:
            print(
                "  -> Stop-loss exits are landing worse than their configured trail width on average -- "
                "trailing_stop_limit_offset_pct in picker_config.json caps this; revisit its value against this data."
            )


def report_to_dict(picks: list[dict]) -> dict:
    """Assemble every breakdown + the calibration summary into one JSON-serializable
    dict, for scripts/run_weekly_report.sh / email_scoring_report.py to consume
    without re-parsing the printed text report."""
    if not picks:
        return {"n": 0}

    returns = [p["day_return_pct"] for p in picks]
    wins = [r for r in returns if r > 0]
    native_count = sum(1 for p in picks if p.get("catalyst_type"))

    return {
        "n": len(picks),
        "native_catalyst_type_count": native_count,
        "keyword_classified_count": len(picks) - native_count,
        "overall_win_rate_pct": len(wins) / len(picks) * 100,
        "overall_avg_return_pct": sum(returns) / len(picks),
        "by_score": breakdown_by_score(picks),
        "by_catalyst_type": breakdown_by_catalyst_type(picks),
        "by_sector": breakdown_by_sector(picks),
        "by_risk": breakdown_by_risk(picks),
        "by_expected_gain_bucket": breakdown_by_expected_gain_bucket(picks),
        "calibration": calibration_summary(picks),
        "slippage": slippage_summary(picks),
        "stop_slippage": stop_slippage_summary(picks),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="portfolio.json file(s) to analyze (default: docs/data/portfolio.json)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write the full report as JSON to this path (e.g. docs/data/scoring_report.json), "
             "for scripts/run_weekly_report.sh / a future dashboard panel to consume.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the human-readable stdout report (useful with --output in cron).",
    )
    args = parser.parse_args()

    paths = args.paths or [Path(__file__).resolve().parents[1] / "docs" / "data" / "portfolio.json"]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"File(s) not found: {', '.join(str(m) for m in missing)}", file=sys.stderr)
        sys.exit(1)

    picks = load_picks(paths)

    if not args.quiet:
        print_report(picks)

    if args.output:
        from datetime import datetime
        report = report_to_dict(picks)
        report["generated_at"] = datetime.now().isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote JSON report -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
