"""
tests/test_filter_and_rank.py

Tests for catalyst_scorer.filter_and_rank — the pick selection logic that
applies score/gain/risk filters and computes allocations.
"""

import pytest
from stock_bot.ai.catalyst_scorer import filter_and_rank


def _pick(ticker, score, direction="bullish", gain=3.0, risk=2):
    return {
        "ticker": ticker,
        "score": score,
        "direction": direction,
        "expected_gain_pct": gain,
        "risk": risk,
        "reason": "test",
        "trend_summary": "",
    }


class TestScoreFiltering:
    def test_min_score_7_excludes_score_6(self):
        scored = [_pick("AAA", 7), _pick("BBB", 6), _pick("CCC", 8)]
        picks = filter_and_rank(scored, 10, min_score=7)
        tickers = [p["ticker"] for p in picks]
        assert "BBB" not in tickers

    def test_min_score_7_includes_7_and_above(self):
        scored = [_pick("AAA", 7), _pick("BBB", 8), _pick("CCC", 9)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) == 3

    def test_score_floor_equals_min_score_no_gap(self):
        """With score_floor == min_score == 7, no extra candidates slip through."""
        scored = [_pick("A", 7), _pick("B", 6), _pick("C", 5)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert all(p["score"] >= 7 for p in picks)

    def test_empty_when_nothing_qualifies(self):
        scored = [_pick("A", 5), _pick("B", 6)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert picks == []

    def test_bearish_excluded(self):
        scored = [_pick("AAA", 9, direction="bearish"), _pick("BBB", 8)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) == 1
        assert picks[0]["ticker"] == "BBB"

    def test_neutral_excluded(self):
        scored = [_pick("AAA", 9, direction="neutral"), _pick("BBB", 7)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) == 1

    def test_high_risk_low_score_excluded(self):
        """risk >= 4 and score < 8 should be filtered out."""
        scored = [_pick("RISKY", 7, risk=4), _pick("SAFE", 7, risk=2)]
        picks = filter_and_rank(scored, 10, min_score=7)
        tickers = [p["ticker"] for p in picks]
        assert "RISKY" not in tickers
        assert "SAFE" in tickers

    def test_high_risk_high_score_allowed(self):
        """risk >= 4 is allowed when score >= 8."""
        scored = [_pick("RISKY_HIGH", 8, risk=4)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) == 1


class TestGainFilter:
    def test_min_gain_2_5_excludes_low_gain(self):
        scored = [_pick("A", 8, gain=2.0), _pick("B", 8, gain=2.5), _pick("C", 8, gain=3.0)]
        picks = filter_and_rank(scored, 10, min_score=7, min_expected_gain_pct=2.5)
        tickers = [p["ticker"] for p in picks]
        assert "A" not in tickers
        assert "B" in tickers
        assert "C" in tickers

    def test_gain_exactly_at_threshold_included(self):
        scored = [_pick("A", 8, gain=2.5)]
        picks = filter_and_rank(scored, 10, min_score=7, min_expected_gain_pct=2.5)
        assert len(picks) == 1


class TestNumStocksLimit:
    def test_returns_at_most_num_stocks(self):
        scored = [_pick(f"T{i}", 8) for i in range(20)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) <= 10

    def test_returns_fewer_when_not_enough_qualify(self):
        scored = [_pick("A", 8), _pick("B", 7)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) == 2

    def test_returns_exactly_num_stocks_when_enough(self):
        scored = [_pick(f"T{i}", 8) for i in range(15)]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert len(picks) == 10


class TestConvictionRanking:
    def test_higher_conviction_ranked_first(self):
        """score=9, gain=5, risk=2 → conviction=22.5 beats score=7, gain=3, risk=2 → 10.5"""
        scored = [
            _pick("LOW", 7, gain=3.0, risk=2),
            _pick("HIGH", 9, gain=5.0, risk=2),
        ]
        picks = filter_and_rank(scored, 10, min_score=7)
        assert picks[0]["ticker"] == "HIGH"

    def test_allocations_present_and_sum_to_100(self):
        scored = [_pick(f"T{i}", 8) for i in range(5)]
        picks = filter_and_rank(scored, 10, min_score=7)
        total = sum(p["allocation_pct"] for p in picks)
        assert abs(total - 100.0) < 1.0  # allow small floating-point rounding


class TestThresholdLoweringSimulation:
    """Simulate the while-loop in main.py that lowers threshold until score_floor."""

    def _simulate_pick_selection(self, all_scored, num_stocks, start_threshold, score_floor, min_gain):
        threshold = start_threshold
        picks = []
        while threshold >= score_floor:
            picks = filter_and_rank(all_scored, num_stocks, min_score=threshold, min_expected_gain_pct=min_gain)
            if len(picks) >= num_stocks:
                break
            threshold -= 1
        return picks, threshold

    def test_fills_to_num_stocks_by_lowering_threshold(self):
        """If only 5 candidates score 8+, lowering to 7 should add more."""
        scored = (
            [_pick(f"HIGH{i}", 8) for i in range(5)] +
            [_pick(f"MED{i}", 7) for i in range(7)]
        )
        picks, final_threshold = self._simulate_pick_selection(
            scored, num_stocks=10, start_threshold=8, score_floor=7, min_gain=2.5
        )
        assert len(picks) == 10
        assert final_threshold == 7

    def test_stops_at_score_floor_even_if_short(self):
        """If not enough candidates exist even at floor, returns what it has.
        The loop decrements threshold after the last iteration, so final value
        is score_floor - 1 when the loop exhausts without reaching num_stocks."""
        scored = [_pick(f"T{i}", 7) for i in range(3)]
        picks, final_threshold = self._simulate_pick_selection(
            scored, num_stocks=10, start_threshold=8, score_floor=7, min_gain=2.5
        )
        assert len(picks) == 3
        # threshold is decremented after the last attempt, landing at floor-1
        assert final_threshold == 6

    def test_does_not_pick_below_score_floor(self):
        """Even though threshold decrements past floor, the last filter run at
        floor still excludes stocks below score_floor."""
        scored = [_pick("A", 5), _pick("B", 4)]  # All below floor=7
        picks, _ = self._simulate_pick_selection(
            scored, num_stocks=10, start_threshold=8, score_floor=7, min_gain=0.0
        )
        # filter_and_rank was last called with threshold=7, so score 5/4 don't qualify
        assert picks == []
