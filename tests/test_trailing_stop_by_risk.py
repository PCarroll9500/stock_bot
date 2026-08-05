"""
tests/test_trailing_stop_by_risk.py

main.py's _effective_trailing_stop() is a closure defined inside async
main() (same pattern as the pre-flight budget math tested in
test_portfolio_writer.py's TestPreflightBudgetMath), so these tests replicate
its exact logic rather than importing it directly. Keep this in sync with
main.py if that logic changes.
"""



def _make_effective_trailing_stop(trailing_stop_pct, trailing_by_risk_cfg):
    """Mirrors main.py's _trailing_by_risk_cfg / _effective_trailing_stop."""
    enabled = trailing_stop_pct is not None and trailing_by_risk_cfg.get("enabled", False)

    def _effective_trailing_stop(pick: dict) -> float | None:
        if not enabled:
            return trailing_stop_pct
        risk_key = str(pick.get("risk", ""))
        return trailing_by_risk_cfg.get(risk_key, trailing_stop_pct)

    return _effective_trailing_stop


_RISK_MAP = {"enabled": True, "1": 5.0, "2": 4.0, "3": 3.0, "4": 2.0, "5": 1.5}


class TestEffectiveTrailingStop:
    def test_disabled_config_returns_flat_value(self):
        fn = _make_effective_trailing_stop(3.0, {"enabled": False, "1": 5.0})
        assert fn({"risk": 1}) == 3.0
        assert fn({"risk": 5}) == 3.0

    def test_missing_config_key_defaults_disabled(self):
        fn = _make_effective_trailing_stop(3.0, {})
        assert fn({"risk": 5}) == 3.0

    def test_enabled_uses_per_risk_value(self):
        fn = _make_effective_trailing_stop(3.0, _RISK_MAP)
        assert fn({"risk": 1}) == 5.0
        assert fn({"risk": 5}) == 1.5

    def test_high_risk_gets_tighter_stop_than_low_risk(self):
        """The core intent: higher risk = tighter stop = cut losers faster."""
        fn = _make_effective_trailing_stop(3.0, _RISK_MAP)
        assert fn({"risk": 5}) < fn({"risk": 1})

    def test_risk_not_in_map_falls_back_to_flat(self):
        partial_map = {"enabled": True, "3": 3.0}
        fn = _make_effective_trailing_stop(3.0, partial_map)
        assert fn({"risk": 1}) == 3.0  # not in map -> flat fallback

    def test_missing_risk_key_falls_back_to_flat(self):
        fn = _make_effective_trailing_stop(3.0, _RISK_MAP)
        assert fn({}) == 3.0  # str("") not in map -> flat fallback

    def test_none_trailing_stop_pct_disables_feature_entirely(self):
        """trailing_stop_pct=None means trailing stops aren't in use at all
        (e.g. stop_loss/take_profit bracket mode instead) -- risk map must
        not activate a feature that wasn't requested."""
        fn = _make_effective_trailing_stop(None, _RISK_MAP)
        assert fn({"risk": 1}) is None
        assert fn({"risk": 5}) is None
