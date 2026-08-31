"""Unit tests for scripts/risk_management.py."""
import conftest  # noqa: F401
import risk_management as rm


def test_entry_zone_breakout_anchors_to_breakout_level():
    zone = rm.compute_entry_zone("breakout", close=105.0, breakout_level=100.0, ema20=90.0)
    assert zone == [100.0, 105.0]


def test_entry_zone_none_for_momentum_continuation():
    zone = rm.compute_entry_zone("momentum_continuation", close=105.0, breakout_level=100.0, ema20=90.0)
    assert zone is None


def test_entry_zone_pullback_anchors_to_ema20():
    zone = rm.compute_entry_zone("pullback", close=97.0, breakout_level=None, ema20=100.0)
    assert zone == [97.0, 100.0]


def test_stop_loss_uses_structure_when_close_enough():
    stop = rm.compute_stop_loss("breakout", close=110.0, swing_low=105.0, ema50=100.0, atr14=2.0)
    assert stop == 105.0  # structural swing low, well within MAX_STOP_DISTANCE_ATR


def test_stop_loss_falls_back_to_atr_when_structure_too_far():
    # swing low is 50 points away vs ATR=2 -> 25 ATRs, way beyond the cap
    stop = rm.compute_stop_loss("breakout", close=110.0, swing_low=60.0, ema50=100.0, atr14=2.0)
    expected = round(110.0 - rm.ATR_STOP_MULT * 2.0, 2)
    assert stop == expected


def test_stop_loss_none_without_atr():
    assert rm.compute_stop_loss("breakout", 110.0, 105.0, 100.0, None) is None


def test_targets_prefers_measured_move():
    result = rm.compute_targets(
        close=101.0, entry=100.0, breakout_level=100.0, base_low=90.0,
        resistance_above=150.0, atr14=1.0,
    )
    assert result["target_method"] == "measured_move"
    assert result["target_1"] == 105.0  # 100 + (100-90)*0.5
    assert result["target_2"] == 110.0  # 100 + (100-90)


def test_targets_falls_back_to_swing_high():
    result = rm.compute_targets(
        close=101.0, entry=100.0, breakout_level=None, base_low=None,
        resistance_above=120.0, atr14=1.0,
    )
    assert result["target_method"] == "swing_high"
    assert result["target_1"] == 120.0


def test_targets_falls_back_to_atr_projection():
    result = rm.compute_targets(
        close=101.0, entry=100.0, breakout_level=None, base_low=None,
        resistance_above=None, atr14=2.0,
    )
    assert result["target_method"] == "atr_projection"
    assert result["target_1"] == round(100.0 + rm.ATR_TARGET_MULT_1 * 2.0, 2)


def test_targets_unavailable_when_nothing_computable():
    result = rm.compute_targets(close=100, entry=100, breakout_level=None, base_low=None, resistance_above=None, atr14=None)
    assert result["target_method"] == "unavailable"
    assert result["target_1"] is None


def test_risk_reward_basic():
    result = rm.compute_risk_reward(entry=100.0, stop_loss=95.0, target_1=110.0)
    assert result["risk_per_share"] == 5.0
    assert result["reward_per_share"] == 10.0
    assert result["risk_reward_ratio"] == 2.0


def test_risk_reward_none_when_inputs_missing():
    result = rm.compute_risk_reward(None, 95.0, 110.0)
    assert result["risk_reward_ratio"] is None


def test_risk_reward_none_ratio_when_risk_non_positive():
    # stop above entry -> non-positive risk, ratio should not be computed
    result = rm.compute_risk_reward(entry=100.0, stop_loss=105.0, target_1=110.0)
    assert result["risk_reward_ratio"] is None


def test_position_sizing_disabled_by_default():
    result = rm.compute_position_sizing(entry=100.0, risk_per_share=5.0, account_size=None, risk_per_trade_pct=None)
    assert result["suggested_position_risk_pct"] is None
    assert result["suggested_qty"] is None
    assert result["risk_pct_of_entry"] == 5.0  # this part is always computed


def test_position_sizing_when_configured():
    result = rm.compute_position_sizing(entry=100.0, risk_per_share=5.0, account_size=100_000.0, risk_per_trade_pct=1.0)
    # risk budget = 100000 * 1% = 1000; qty = 1000 // 5 = 200
    assert result["suggested_qty"] == 200
    assert result["suggested_position_risk_pct"] == 1.0
