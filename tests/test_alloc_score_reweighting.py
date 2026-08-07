"""
tests/test_alloc_score_reweighting.py

main.py's _alloc_score() (the score^2 allocation re-weighting applied after
fills, using catalyst_weight + risk_penalty) is a closure defined inside
async main() (same pattern as _effective_trailing_stop in
test_trailing_stop_by_risk.py and the pre-flight budget math in
test_portfolio_writer.py), so these tests replicate its exact logic rather
than importing it directly. Keep this in sync with main.py if that logic
changes.

Locks in the 2026-08-06 fix: risk_penalty.threshold raised 3 -> 5 so that
risk 3-4 picks (which the SIZE RULE now uses for genuine outlier catches,
not just weak ones) are no longer double-penalized on top of their already
-tight trailing_stop_by_risk stop.
"""

import pytest

from stock_bot.ai.catalyst_scorer import catalyst_weight


def _make_alloc_score(score_floor, risk_penalty_enabled, risk_penalty_threshold,
                       risk_penalty_factor, catalyst_weights, genuine_count):
    """Mirrors main.py's _alloc_score()."""
    def _alloc_score(p: dict) -> float:
        s = max(p.get("score", 1), 1)
        w = catalyst_weight(p.get("catalyst_type", "other"), catalyst_weights)
        if genuine_count < 3 and p.get("score", 0) >= score_floor:
            return s * w
        if risk_penalty_enabled and p.get("risk", 1) >= risk_penalty_threshold:
            s = s * risk_penalty_factor
        return s * w
    return _alloc_score


def _pick(score=8, risk=2, catalyst_type="other"):
    return {"score": score, "risk": risk, "catalyst_type": catalyst_type}


class TestRiskPenaltyThreshold5:
    """Current live config: threshold=5, factor=0.5."""

    def _fn(self, genuine_count=5):
        return _make_alloc_score(
            score_floor=7, risk_penalty_enabled=True, risk_penalty_threshold=5,
            risk_penalty_factor=0.5, catalyst_weights=None, genuine_count=genuine_count,
        )

    def test_risk_4_is_not_penalized(self):
        """The whole point of the fix: a genuine outlier catch flagged risk=4
        by the SIZE RULE must not lose half its allocation weight."""
        fn = self._fn()
        assert fn(_pick(score=8, risk=4)) == 8.0

    def test_risk_3_is_not_penalized(self):
        fn = self._fn()
        assert fn(_pick(score=8, risk=3)) == 8.0

    def test_risk_5_is_still_penalized(self):
        """Risk=5 is the one tier the risk rubric itself defines as 'genuine
        catalyst or not' -- caution there still makes sense."""
        fn = self._fn()
        assert fn(_pick(score=8, risk=5)) == 4.0

    def test_risk_1_and_2_unaffected(self):
        fn = self._fn()
        assert fn(_pick(score=8, risk=1)) == 8.0
        assert fn(_pick(score=8, risk=2)) == 8.0

    def test_allocation_no_longer_collapses_at_risk_4(self):
        """Direct regression check for the 2026-08-06 finding: a risk=4
        outlier pick must get the SAME weight as an equally-scored risk=2
        pick, not a quartered one (score^2 halved -> 4x gap)."""
        fn = self._fn()
        low_risk = fn(_pick(score=8, risk=2)) ** 2
        outlier = fn(_pick(score=8, risk=4)) ** 2
        assert low_risk == outlier


class TestOldThreshold3Behavior:
    """Documents the behavior being replaced, so a future reader can see
    exactly what changed and why threshold=5 was chosen instead."""

    def _fn(self, genuine_count=5):
        return _make_alloc_score(
            score_floor=7, risk_penalty_enabled=True, risk_penalty_threshold=3,
            risk_penalty_factor=0.5, catalyst_weights=None, genuine_count=genuine_count,
        )

    def test_old_threshold_penalized_risk_3_and_4(self):
        fn = self._fn()
        assert fn(_pick(score=8, risk=3)) == 4.0
        assert fn(_pick(score=8, risk=4)) == 4.0


class TestGenuinePickProtectionStillWorks:
    """When the pool is thin (fallback fills dominate), genuine picks must
    still be protected from the risk penalty regardless of threshold."""

    def test_genuine_pick_protected_when_pool_thin(self):
        fn = _make_alloc_score(
            score_floor=7, risk_penalty_enabled=True, risk_penalty_threshold=5,
            risk_penalty_factor=0.5, catalyst_weights=None, genuine_count=1,
        )
        # score >= score_floor and pool is thin -> protected even at risk=5
        assert fn(_pick(score=8, risk=5)) == 8.0


class TestCatalystWeightStillApplied:
    def test_catalyst_weight_multiplies_after_risk_penalty(self):
        fn = _make_alloc_score(
            score_floor=7, risk_penalty_enabled=True, risk_penalty_threshold=5,
            risk_penalty_factor=0.5, catalyst_weights={"ma_acquisition": 0.5},
            genuine_count=5,
        )
        # risk=5 -> penalized to score 4, then *0.5 catalyst weight -> 2.0
        assert fn(_pick(score=8, risk=5, catalyst_type="ma_acquisition")) == pytest.approx(2.0)


class TestRiskPenaltyDisabled:
    def test_disabled_never_penalizes(self):
        fn = _make_alloc_score(
            score_floor=7, risk_penalty_enabled=False, risk_penalty_threshold=5,
            risk_penalty_factor=0.5, catalyst_weights=None, genuine_count=5,
        )
        assert fn(_pick(score=8, risk=5)) == 8.0
