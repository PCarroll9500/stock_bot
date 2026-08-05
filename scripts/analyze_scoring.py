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
    ("earnings_beat", re.compile(r"earnings beat|revenue beat|record (revenue|quarter)|raised guidance|beat.*guidance", re.I)),
    ("ma_acquisition", re.compile(r"acquisition|acquire[ds]?|merger|buyout|all-cash deal|deal at \$", re.I)),
    ("fda_approval", re.compile(r"\bFDA\b|drug approval|clinical trial", re.I)),
    ("insider_buying", re.compile(r"insider (buy|buying)|Form 4|CEO buy|CFO buy", re.I)),
    ("contract_win", re.compile(r"contract win|new contract|major contract|disclosed revenue", re.I)),
    ("short_squeeze", re.compile(r"short squeeze|short interest|squeeze", re.I)),
    ("guidance_raise", re.compile(r"guidance raise|raised (full-year|fy) guidance|guidance hike", re.I)),
    ("analyst_action", re.compile(r"analyst|price target|upgrade|initiat(ed|es).*buy|target raise", re.I)),
    ("sector_tailwind", re.compile(r"sector tailwind|industry-wide|broad.*(rally|move|tailwind)|sympathy move", re.I)),
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
# Report printing
# ---------------------------------------------------------------------------

def _print_table(title: str, breakdown: dict[str, dict], sort_by_n: bool = True) -> None:
    print(f"\n{title}")
    print("-" * len(title))
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="portfolio.json file(s) to analyze (default: docs/data/portfolio.json)",
    )
    args = parser.parse_args()

    paths = args.paths or [Path(__file__).resolve().parents[1] / "docs" / "data" / "portfolio.json"]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"File(s) not found: {', '.join(str(m) for m in missing)}", file=sys.stderr)
        sys.exit(1)

    picks = load_picks(paths)
    print_report(picks)


if __name__ == "__main__":
    main()
