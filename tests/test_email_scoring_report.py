"""
tests/test_email_scoring_report.py

Unit tests for scripts/email_scoring_report.py's build_message() -- pure
formatting logic, no AWS calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from email_scoring_report import build_message


class TestBuildMessage:
    def test_zero_picks_short_circuits(self):
        subject, body = build_message({"n": 0})
        assert "0 realized picks" in subject
        assert "No realized picks" in body

    def test_includes_core_stats(self):
        report = {
            "n": 5, "native_catalyst_type_count": 5, "keyword_classified_count": 0,
            "overall_win_rate_pct": 60.0, "overall_avg_return_pct": 0.68,
            "by_catalyst_type": {"earnings_beat": {"n": 3, "win_rate": 66.7, "avg_return_pct": 1.2}},
            "by_score": {"8": {"n": 3, "win_rate": 66.7, "avg_return_pct": 1.5}},
            "calibration": {"n": 5, "correlation": 0.65, "mean_expected_gain_pct": 2.8, "mean_actual_return_pct": 0.68},
        }
        subject, body = build_message(report)
        assert "5 realized picks" in subject
        assert "60.0%" in body
        assert "earnings_beat" in body
        assert "+0.650" in body  # correlation

    def test_handles_missing_calibration_gracefully(self):
        report = {
            "n": 2, "native_catalyst_type_count": 2, "keyword_classified_count": 0,
            "overall_win_rate_pct": 50.0, "overall_avg_return_pct": -1.0,
            "by_catalyst_type": {}, "by_score": {}, "calibration": None,
        }
        _subject, body = build_message(report)
        assert "undefined" in body

    def test_no_exception_on_missing_optional_keys(self):
        """A minimal report dict (e.g. from an older report format) must not
        raise KeyError -- everything optional falls back to a safe default."""
        subject, body = build_message({"n": 1})
        assert subject and body
