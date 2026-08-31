"""Unit tests for scripts/scoring.py — classification and scoring logic."""
import conftest  # noqa: F401
import scoring as sc


# --- market regime -----------------------------------------------------

def test_market_regime_bullish_when_all_conditions_hold():
    regime = sc.classify_market_regime(
        close=110, ema20=108, ema50=105, sma200=100, rsi14=60, return_20d=5.0,
    )
    assert regime == "bullish"


def test_market_regime_bearish_when_all_conditions_fail():
    regime = sc.classify_market_regime(
        close=90, ema20=95, ema50=100, sma200=105, rsi14=35, return_20d=-5.0,
    )
    assert regime == "bearish"


def test_market_regime_unknown_when_data_missing():
    assert sc.classify_market_regime(100, None, None, None, None, None) == "unknown"


# --- trend status --------------------------------------------------------

def test_trend_strong_uptrend():
    assert sc.classify_trend_status(close=120, ema20=115, ema50=110, ema200=100) == "strong_uptrend"


def test_trend_downtrend():
    assert sc.classify_trend_status(close=80, ema20=90, ema50=95, ema200=100) == "downtrend"


def test_trend_unknown_without_ema200():
    assert sc.classify_trend_status(100, 100, 100, None) == "unknown"


# --- breakout (look-ahead safety is the important property here) ----------

def test_breakout_uses_prior_high_stub_not_todays_high():
    # stub prior_high_fn that ignores the injected highs entirely and
    # returns a fixed level, to prove classify_breakout never reaches into
    # `highs[-1]` (today's own bar) itself -- it only ever calls the
    # injected function.
    calls = []

    def stub_prior_high(highs, n):
        calls.append((tuple(highs), n))
        return 100.0  # pretend the level is fixed regardless of today's bar

    result = sc.classify_breakout(close=105.0, highs=[1, 2, 3, 999], vol_ratio_20d=2.0, prior_high_fn=stub_prior_high)
    assert result["breakout_strength"] in ("confirmed", "moderate")
    assert result["breakout_level"] == 100.0
    # confirm it was actually called for each configured lookback
    assert len(calls) == len(sc.BREAKOUT_LOOKBACKS)


def test_breakout_approaching_when_close_but_not_broken():
    def stub(highs, n):
        return 100.0
    result = sc.classify_breakout(close=98.5, highs=[], vol_ratio_20d=1.0, prior_high_fn=stub)
    assert result["breakout_strength"] == "approaching"


def test_breakout_none_when_far_below():
    def stub(highs, n):
        return 100.0
    result = sc.classify_breakout(close=50.0, highs=[], vol_ratio_20d=1.0, prior_high_fn=stub)
    assert result["breakout_strength"] == "none"


def test_breakout_unknown_when_no_levels_available():
    def stub(highs, n):
        return None
    result = sc.classify_breakout(close=100.0, highs=[], vol_ratio_20d=None, prior_high_fn=stub)
    assert result["breakout_strength"] == "unknown"
    assert result["breakout_level"] is None


# --- consolidation ----------------------------------------------------

def test_consolidation_detects_compression():
    baseline_n, recent_n = sc.CONSOLIDATION_BASELINE_SESSIONS, sc.CONSOLIDATION_RECENT_SESSIONS
    atr_series = [2.0] * baseline_n + [0.5] * recent_n  # sharp compression
    vols = [1_000_000.0] * baseline_n + [500_000.0] * recent_n  # volume also contracts
    result = sc.detect_consolidation(atr_series, vols)
    assert result["in_base"] is True
    assert result["base_sessions"] == recent_n


def test_consolidation_false_when_no_compression():
    baseline_n, recent_n = sc.CONSOLIDATION_BASELINE_SESSIONS, sc.CONSOLIDATION_RECENT_SESSIONS
    atr_series = [1.0] * (baseline_n + recent_n)  # flat, no compression
    vols = [1_000_000.0] * (baseline_n + recent_n)
    result = sc.detect_consolidation(atr_series, vols)
    assert result["in_base"] is False


def test_consolidation_false_when_insufficient_history():
    result = sc.detect_consolidation([1.0, 2.0], [100.0, 200.0])
    assert result["in_base"] is False
    assert result["base_sessions"] == 0


# --- pullback ------------------------------------------------------------

def test_pullback_requires_uptrend():
    rsi_hist = [40.0] * 10
    assert sc.detect_pullback("downtrend", close=95, ema20=100, ema50=105, rsi_history=rsi_hist) is False


def test_pullback_detected_in_valid_scenario():
    rsi_hist = [55.0] * 5 + [45.0] * 3 + [52.0] * 2  # dipped to 45, recovering
    result = sc.detect_pullback("uptrend", close=97.0, ema20=100.0, ema50=95.0, rsi_history=rsi_hist)
    assert result is True


def test_pullback_false_when_broken_below_ema50():
    rsi_hist = [40.0] * 10
    result = sc.detect_pullback("uptrend", close=90.0, ema20=100.0, ema50=95.0, rsi_history=rsi_hist)
    assert result is False


# --- setup type ---------------------------------------------------------

def test_setup_type_breakout_priority():
    assert sc.classify_setup_type("confirmed", in_base=True, is_pullback=True, trend_status="uptrend") == "breakout"


def test_setup_type_pullback_when_no_breakout():
    assert sc.classify_setup_type("none", in_base=False, is_pullback=True, trend_status="uptrend") == "pullback"


def test_setup_type_no_setup_fallback():
    assert sc.classify_setup_type("none", in_base=False, is_pullback=False, trend_status="sideways") == "no_setup"


# --- extension -----------------------------------------------------------

def test_extension_early_when_close_to_anchor():
    status = sc.classify_extension(close=101.0, ema20=100.0, breakout_level=None, atr14=2.0)
    assert status == "early"


def test_extension_severely_extended_when_far_from_anchor():
    status = sc.classify_extension(close=130.0, ema20=100.0, breakout_level=None, atr14=2.0)
    assert status == "severely_extended"


def test_extension_unknown_without_atr():
    assert sc.classify_extension(100.0, 100.0, None, None) == "unknown"


# --- scoring engine ------------------------------------------------------

def test_score_weights_sum_to_100():
    assert sum(sc.SCORE_WEIGHTS.values()) == 100


def test_compute_score_breakdown_sums_close_to_raw_score_before_adjustment():
    result = sc.compute_score(
        regime="bullish", sector_percentile=90.0, rs_percentile=95.0,
        trend_status="strong_uptrend", setup_type="breakout", volume_status="expanding_on_strength",
        rsi_regime="rising_momentum", risk_reward_ratio=3.0, extension_status="healthy",
    )
    breakdown_sum = sum(result["score_breakdown"].values())
    assert abs(result["score"] - round(breakdown_sum)) < 1  # no adjustment applied at "healthy"
    assert result["extension_adjustment_applied"] is False
    assert result["score"] > 90  # should be a near-max-quality setup


def test_compute_score_penalizes_severe_extension():
    kwargs = dict(
        regime="bullish", sector_percentile=90.0, rs_percentile=95.0,
        trend_status="strong_uptrend", setup_type="breakout", volume_status="expanding_on_strength",
        rsi_regime="rising_momentum", risk_reward_ratio=3.0,
    )
    healthy = sc.compute_score(extension_status="healthy", **kwargs)
    extended = sc.compute_score(extension_status="severely_extended", **kwargs)
    assert extended["score"] < healthy["score"]
    assert extended["extension_adjustment_applied"] is True


def test_compute_score_worst_case_is_low():
    result = sc.compute_score(
        regime="bearish", sector_percentile=0.0, rs_percentile=0.0,
        trend_status="downtrend", setup_type="no_setup", volume_status="expanding_on_weakness",
        rsi_regime="weakening", risk_reward_ratio=None, extension_status="unknown",
    )
    assert result["score"] < 10


def test_compute_score_bounded_0_to_100():
    result = sc.compute_score(
        regime="bullish", sector_percentile=100.0, rs_percentile=100.0,
        trend_status="strong_uptrend", setup_type="breakout", volume_status="expanding_on_strength",
        rsi_regime="rising_momentum", risk_reward_ratio=10.0, extension_status="early",
    )
    assert 0 <= result["score"] <= 100
