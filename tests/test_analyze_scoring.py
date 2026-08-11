"""
tests/test_analyze_scoring.py

Unit tests for scripts/analyze_scoring.py -- the score-vs-returns feedback
loop. Pure-logic functions only (no file I/O beyond the temp files the tests
create themselves).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analyze_scoring import (
    _gain_bucket,
    _pearson,
    breakdown_by_catalyst_type,
    calibration_summary,
    classify_catalyst_type,
    load_picks,
    report_to_dict,
    slippage_summary,
    stop_slippage_summary,
)

# ---------------------------------------------------------------------------
# classify_catalyst_type
# ---------------------------------------------------------------------------

class TestClassifyCatalystType:
    def test_native_value_wins(self):
        pick = {"catalyst_type": "earnings_beat", "reason": "acquisition rumor"}
        assert classify_catalyst_type(pick) == "earnings_beat"

    def test_falls_back_to_keyword_when_no_native_value(self):
        pick = {"reason": "Company announces acquisition at $50/share"}
        assert classify_catalyst_type(pick) == "ma_acquisition"

    def test_earnings_beat_keyword(self):
        pick = {"reason": "Stock jumps on earnings beat and raised guidance"}
        assert classify_catalyst_type(pick) == "earnings_beat"

    def test_fda_keyword(self):
        pick = {"reason": "FDA approval for new drug"}
        assert classify_catalyst_type(pick) == "fda_approval"

    def test_analyst_action_keyword(self):
        pick = {"reason": "Analyst raises price target to $100"}
        assert classify_catalyst_type(pick) == "analyst_action"

    def test_no_match_falls_back_to_other(self):
        pick = {"reason": "Stock moves higher on no particular news"}
        assert classify_catalyst_type(pick) == "other"

    def test_empty_native_value_falls_back_to_keyword(self):
        """An empty-string catalyst_type (falsy) should not short-circuit
        the fallback -- only a genuinely present value should."""
        pick = {"catalyst_type": "", "reason": "Acquisition announced"}
        assert classify_catalyst_type(pick) == "ma_acquisition"


# ---------------------------------------------------------------------------
# load_picks -- merge + dedupe across files
# ---------------------------------------------------------------------------

class TestLoadPicks:
    def _write(self, tmp_path, name, sessions):
        path = tmp_path / name
        path.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
        return path

    def test_skips_unrealized_picks(self, tmp_path):
        sessions = [{"date": "2026-01-01", "picks": [
            {"ticker": "AAA", "day_return_pct": None, "reason": "x"},
            {"ticker": "BBB", "day_return_pct": 2.5, "reason": "x"},
        ]}]
        path = self._write(tmp_path, "p.json", sessions)
        picks = load_picks([path])
        assert len(picks) == 1
        assert picks[0]["ticker"] == "BBB"

    def test_dedupes_same_date_ticker_across_files(self, tmp_path):
        sessions = [{"date": "2026-01-01", "picks": [
            {"ticker": "AAA", "day_return_pct": 3.0, "reason": "x"},
        ]}]
        path1 = self._write(tmp_path, "p1.json", sessions)
        path2 = self._write(tmp_path, "p2.json", sessions)  # identical snapshot
        picks = load_picks([path1, path2])
        assert len(picks) == 1

    def test_merges_distinct_dates(self, tmp_path):
        path1 = self._write(tmp_path, "p1.json", [{"date": "2026-01-01", "picks": [
            {"ticker": "AAA", "day_return_pct": 3.0, "reason": "x"},
        ]}])
        path2 = self._write(tmp_path, "p2.json", [{"date": "2026-01-02", "picks": [
            {"ticker": "AAA", "day_return_pct": -1.0, "reason": "x"},
        ]}])
        picks = load_picks([path1, path2])
        assert len(picks) == 2

    def test_annotates_date_and_catalyst_type(self, tmp_path):
        path = self._write(tmp_path, "p.json", [{"date": "2026-01-01", "picks": [
            {"ticker": "AAA", "day_return_pct": 3.0, "reason": "FDA approval granted"},
        ]}])
        picks = load_picks([path])
        assert picks[0]["_date"] == "2026-01-01"
        assert picks[0]["_catalyst_type"] == "fda_approval"


# ---------------------------------------------------------------------------
# _breakdown / breakdown_by_catalyst_type
# ---------------------------------------------------------------------------

class TestBreakdown:
    def test_computes_win_rate_and_avg(self):
        picks = [
            {"day_return_pct": 5.0, "_catalyst_type": "earnings_beat"},
            {"day_return_pct": -3.0, "_catalyst_type": "earnings_beat"},
            {"day_return_pct": 2.0, "_catalyst_type": "earnings_beat"},
        ]
        result = breakdown_by_catalyst_type(picks)
        stats = result["earnings_beat"]
        assert stats["n"] == 3
        assert stats["win_rate"] == pytest.approx(200 / 3)
        assert stats["avg_return_pct"] == pytest.approx((5.0 - 3.0 + 2.0) / 3)
        assert stats["best"] == 5.0
        assert stats["worst"] == -3.0

    def test_groups_are_independent(self):
        picks = [
            {"day_return_pct": 10.0, "_catalyst_type": "ma_acquisition"},
            {"day_return_pct": -10.0, "_catalyst_type": "analyst_action"},
        ]
        result = breakdown_by_catalyst_type(picks)
        assert result["ma_acquisition"]["win_rate"] == 100.0
        assert result["analyst_action"]["win_rate"] == 0.0


# ---------------------------------------------------------------------------
# Calibration: _pearson / _gain_bucket / calibration_summary
# ---------------------------------------------------------------------------

class TestPearson:
    def test_perfect_positive_correlation(self):
        assert _pearson([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self):
        assert _pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)

    def test_no_correlation_returns_near_zero(self):
        # Symmetric spread around the mean with no linear relationship
        r = _pearson([1, 2, 3, 4], [1, -1, 1, -1])
        assert r is not None
        assert abs(r) < 0.5

    def test_insufficient_data_returns_none(self):
        assert _pearson([1.0], [1.0]) is None
        assert _pearson([], []) is None

    def test_zero_variance_returns_none(self):
        assert _pearson([5, 5, 5], [1, 2, 3]) is None


class TestGainBucket:
    def test_buckets_by_expected_gain(self):
        assert _gain_bucket({"expected_gain_pct": 0.5}) == "0-1%"
        assert _gain_bucket({"expected_gain_pct": 2.0}) == "1-3%"
        assert _gain_bucket({"expected_gain_pct": 4.0}) == "3-6%"
        assert _gain_bucket({"expected_gain_pct": 8.0}) == "6-10%"
        assert _gain_bucket({"expected_gain_pct": 15.0}) == "10%+"

    def test_missing_value_is_unknown_bucket(self):
        assert _gain_bucket({}) == "?"
        assert _gain_bucket({"expected_gain_pct": None}) == "?"

    def test_boundary_values(self):
        """Buckets are half-open [min, max) -- boundary values belong to the
        upper bucket, matching the < comparisons in _gain_bucket."""
        assert _gain_bucket({"expected_gain_pct": 1.0}) == "1-3%"
        assert _gain_bucket({"expected_gain_pct": 3.0}) == "3-6%"
        assert _gain_bucket({"expected_gain_pct": 6.0}) == "6-10%"
        assert _gain_bucket({"expected_gain_pct": 10.0}) == "10%+"


class TestCalibrationSummary:
    def test_none_when_no_expected_gain_data(self):
        picks = [{"day_return_pct": 1.0}]
        assert calibration_summary(picks) is None

    def test_none_when_empty(self):
        assert calibration_summary([]) is None

    def test_computes_means_and_correlation(self):
        picks = [
            {"expected_gain_pct": 2.0, "day_return_pct": 3.0},
            {"expected_gain_pct": 4.0, "day_return_pct": 6.0},
            {"expected_gain_pct": 6.0, "day_return_pct": 9.0},
        ]
        result = calibration_summary(picks)
        assert result["n"] == 3
        assert result["correlation"] == pytest.approx(1.0)
        assert result["mean_expected_gain_pct"] == pytest.approx(4.0)
        assert result["mean_actual_return_pct"] == pytest.approx(6.0)

    def test_ignores_picks_missing_expected_gain(self):
        picks = [
            {"expected_gain_pct": 2.0, "day_return_pct": 3.0},
            {"expected_gain_pct": None, "day_return_pct": -5.0},
        ]
        result = calibration_summary(picks)
        assert result["n"] == 1


class TestSlippageSummary:
    def test_none_when_no_slippage_data(self):
        picks = [{"day_return_pct": 1.0}]
        assert slippage_summary(picks) is None

    def test_computes_mean_best_worst(self):
        picks = [
            {"slippage_pct": 1.0},
            {"slippage_pct": -0.5},
            {"slippage_pct": 2.0},
        ]
        result = slippage_summary(picks)
        assert result["n"] == 3
        assert result["mean_slippage_pct"] == pytest.approx((1.0 - 0.5 + 2.0) / 3)
        assert result["best"] == -0.5
        assert result["worst"] == 2.0

    def test_ignores_picks_without_slippage(self):
        picks = [
            {"slippage_pct": 1.0},
            {"slippage_pct": None},
            {},
        ]
        result = slippage_summary(picks)
        assert result["n"] == 1


class TestStopSlippageSummary:
    def test_none_when_no_stop_slippage_data(self):
        picks = [{"day_return_pct": 1.0}]
        assert stop_slippage_summary(picks) is None

    def test_computes_mean_best_worst(self):
        picks = [
            {"stop_slippage_pct": -1.0},
            {"stop_slippage_pct": -3.5},
            {"stop_slippage_pct": 0.2},
        ]
        result = stop_slippage_summary(picks)
        assert result["n"] == 3
        assert result["mean_stop_slippage_pct"] == pytest.approx((-1.0 - 3.5 + 0.2) / 3)
        assert result["best"] == 0.2   # least negative = best
        assert result["worst"] == -3.5  # most negative = worst

    def test_ignores_picks_without_stop_slippage(self):
        picks = [
            {"stop_slippage_pct": -1.0},
            {"stop_slippage_pct": None},
            {},
        ]
        result = stop_slippage_summary(picks)
        assert result["n"] == 1


# ---------------------------------------------------------------------------
# report_to_dict -- JSON-serializable report for automation (run_weekly_report.sh)
# ---------------------------------------------------------------------------

class TestReportToDict:
    def test_empty_picks_returns_zero_n(self):
        assert report_to_dict([]) == {"n": 0}

    def test_includes_all_breakdown_sections(self):
        # report_to_dict expects picks already enriched by load_picks() with
        # "_catalyst_type" (breakdown_by_catalyst_type reads that, not "catalyst_type").
        picks = [
            {
                "score": 8, "risk": 2, "sector": "Technology",
                "catalyst_type": "earnings_beat", "_catalyst_type": "earnings_beat",
                "expected_gain_pct": 3.0, "day_return_pct": 4.1, "reason": "x",
            },
            {
                "score": 7, "risk": 3, "sector": "Healthcare",
                "catalyst_type": "ma_acquisition", "_catalyst_type": "ma_acquisition",
                "expected_gain_pct": 2.0, "day_return_pct": -6.2, "reason": "x",
            },
        ]
        report = report_to_dict(picks)
        assert report["n"] == 2
        assert report["native_catalyst_type_count"] == 2
        assert report["keyword_classified_count"] == 0
        assert report["overall_win_rate_pct"] == 50.0
        for key in ("by_score", "by_catalyst_type", "by_sector", "by_risk", "by_expected_gain_bucket", "calibration", "slippage", "stop_slippage"):
            assert key in report
        assert report["calibration"]["n"] == 2

    def test_is_json_serializable(self):
        import json as _json
        picks = [{
            "score": 8, "risk": 2, "sector": "Technology",
            "catalyst_type": "earnings_beat", "_catalyst_type": "earnings_beat",
            "expected_gain_pct": 3.0, "day_return_pct": 4.1, "reason": "x",
        }]
        _json.dumps(report_to_dict(picks))  # must not raise
