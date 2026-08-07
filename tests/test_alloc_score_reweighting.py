"""
tests/test_alloc_score_reweighting.py

main.py's _alloc_score() (the score^2 allocation re-weighting applied after
fills, using catalyst_weight + risk_penalty) is a closure defined inside
async main() (same pattern as _effective_trailing_stop in
test_trailing_stop_by_risk.py and the pre-flight budget math in
test_portfolio_writer.py), so these tests replicate its exact logic rather
than importing it directly. Keep this in sync with main.py if that logic
changes.

Locks in two 2026-08-06 fixes:
  1. risk_penalty.threshold raised 3 -> 5 so that risk 3-4 picks (which the
     SIZE RULE now uses for genuine outlier catches, not just weak ones)
     are no longer double-penalized on top of their already-tight
     trailing_stop_by_risk stop.
  2. A 25% position cap added to the initial score^2 allocation step,
     matching the cap the smart-reallocation rounds already enforced on
     top-ups -- the initial step had no ceiling at all before this.
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


# ---------------------------------------------------------------------------
# Position cap (added 2026-08-06): the score^2 -> percentage normalization had
# no ceiling, unlike the smart-reallocation rounds later in main.py which cap
# any single stock at _MAX_POSITION_PCT (25%) of the portfolio. A lopsided
# score distribution could size one initial position arbitrarily large while
# every top-up after it was held to 25% -- inconsistent risk discipline
# between the two mechanisms. Mirrors main.py's compute-then-clamp step.
# ---------------------------------------------------------------------------

def _compute_and_cap(picks, alloc_score_fn, max_pct=25.0):
    """Mirrors main.py: score^2-normalize to percentages, then clamp any
    pick above max_pct down to it (excess is left undeployed, matching the
    real code -- no redistribution pass)."""
    sq_total = sum(alloc_score_fn(p) ** 2 for p in picks)
    for p in picks:
        p["allocation_pct"] = round(alloc_score_fn(p) ** 2 / sq_total * 100, 1)
    for p in picks:
        if p["allocation_pct"] > max_pct:
            p["allocation_pct"] = max_pct
    return picks


class TestPositionCap:
    def _uniform_fn(self):
        """No risk penalty / catalyst weighting in play -- isolates the cap."""
        return _make_alloc_score(
            score_floor=7, risk_penalty_enabled=False, risk_penalty_threshold=5,
            risk_penalty_factor=0.5, catalyst_weights=None, genuine_count=5,
        )

    def test_dominant_score_gets_capped(self):
        """A single much-higher-scored pick among otherwise-similar picks
        would naturally exceed 25% under pure score^2 weighting -- the cap
        must bring it back down."""
        picks = [_pick(score=10) for _ in range(1)] + [_pick(score=5) for _ in range(4)]
        # Without a cap: 10^2 / (10^2 + 4*5^2) * 100 = 100/200*100 = 50% -- well over 25%.
        result = _compute_and_cap(picks, self._uniform_fn())
        assert result[0]["allocation_pct"] == 25.0

    def test_uncapped_picks_are_unaffected(self):
        picks = [_pick(score=10)] + [_pick(score=5) for _ in range(4)]
        result = _compute_and_cap(picks, self._uniform_fn())
        # The four score=5 picks were never near the cap -- untouched by the clamp.
        for p in result[1:]:
            assert p["allocation_pct"] < 25.0

    def test_no_pick_ever_exceeds_the_cap(self):
        picks = [_pick(score=s) for s in (10, 9, 8, 7, 7, 6, 6, 5, 5, 5)]
        result = _compute_and_cap(picks, self._uniform_fn())
        assert all(p["allocation_pct"] <= 25.0 for p in result)

    def test_evenly_scored_picks_stay_under_cap_uncapped(self):
        """A reasonably-sized, evenly-scored slate (like a normal 12-15 pick
        day) shouldn't trip the cap at all -- it's a tail-risk guard, not a
        change to normal-day sizing."""
        picks = [_pick(score=8) for _ in range(13)]
        result = _compute_and_cap(picks, self._uniform_fn())
        assert all(p["allocation_pct"] < 25.0 for p in result)
        # Even split: 100/13 ~= 7.7%, nowhere near the 25% ceiling.
        assert result[0]["allocation_pct"] == pytest.approx(100 / 13, abs=0.1)

    def test_few_picks_can_legitimately_leave_capital_undeployed(self):
        """With very few picks (e.g. near min_picks=3), an even split alone
        exceeds 25% each -- the cap correctly leaves the remainder
        undeployed here rather than over-concentrating; main.py's smart
        reallocation rounds redeploy it later under the same cap."""
        picks = [_pick(score=8) for _ in range(3)]
        result = _compute_and_cap(picks, self._uniform_fn())
        assert all(p["allocation_pct"] == 25.0 for p in picks)  # would be ~33.3% uncapped
        assert sum(p["allocation_pct"] for p in result) == pytest.approx(75.0)
