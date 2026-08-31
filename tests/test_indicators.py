"""Unit tests for the pure calculation functions in scripts/indicators.py.

Run with: pytest tests/ -v   (from the repo root)
"""
import conftest  # noqa: F401  (adds scripts/ to sys.path)
import indicators as ind


def make_rows(closes, highs=None, lows=None, vols=None, opens=None):
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1_000_000.0] * n
    opens = opens or closes
    return [
        {"date": f"2026-01-{i+1:02d}", "open": opens[i], "high": highs[i],
         "low": lows[i], "close": closes[i], "volume": vols[i]}
        for i in range(n)
    ]


# --- SMA / EMA -------------------------------------------------------------

def test_sma_basic():
    assert ind.sma([1, 2, 3, 4, 5], 5) == 3.0
    assert ind.sma([1, 2, 3], 5) is None  # not enough data


def test_sma_uses_trailing_window_only():
    vals = [100] * 10 + [10] * 5
    # last 5 values are the 10s -> mean should be 10, not blended with the 100s
    assert ind.sma(vals, 5) == 10.0


def test_ema_converges_toward_a_flat_series():
    vals = [100.0] * 60
    e = ind.ema(vals, 20)
    assert e is not None
    assert abs(e - 100.0) < 1e-6


def test_ema_none_when_insufficient_history():
    assert ind.ema([1.0, 2.0], 20) is None


# --- RSI ---------------------------------------------------------------

def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 30)]  # strictly increasing
    rsi = ind.rsi_wilder(closes, period=14)
    assert rsi == 100.0


def test_rsi_all_losses_is_0():
    closes = [float(i) for i in range(30, 1, -1)]  # strictly decreasing
    rsi = ind.rsi_wilder(closes, period=14)
    assert rsi == 0.0


def test_rsi_flat_series_is_none_or_100_not_crash():
    # Wilder RSI is mathematically 100 when there are literally zero losses,
    # even on a flat series (avg_loss == 0) -- verifying no ZeroDivisionError.
    closes = [100.0] * 30
    rsi = ind.rsi_wilder(closes, period=14)
    assert rsi == 100.0


def test_rsi_none_when_insufficient_history():
    assert ind.rsi_wilder([1.0, 2.0, 3.0], period=14) is None


def test_rsi_series_last_value_matches_rsi_wilder():
    closes = [100 + (i % 7) - 3 for i in range(60)]
    series = ind.rsi_series(closes, 14)
    assert series[-1] == ind.rsi_wilder(closes, 14)


def test_rsi_slope_positive_when_rsi_rising():
    # A dip followed by a strong recovery run should show a positive slope
    closes = [100.0] * 20 + [90.0] * 5 + [95, 100, 105, 110, 115, 120]
    slope = ind.rsi_slope(closes, period=14, lookback=5)
    assert slope is not None
    assert slope > 0


# --- ATR -----------------------------------------------------------------

def test_atr_constant_true_range():
    # high-low always exactly 2.0, close never gaps beyond the prior range
    n = 30
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n
    rows = make_rows(closes, highs=highs, lows=lows)
    atr = ind.atr_latest(rows, period=14)
    assert atr is not None
    assert abs(atr - 2.0) < 1e-6


def test_atr_none_when_insufficient_history():
    rows = make_rows([100.0, 101.0, 102.0])
    assert ind.atr_latest(rows, period=14) is None


# --- returns ---------------------------------------------------------------

def test_pct_return_basic():
    closes = [100.0] * 10 + [110.0]
    # 10 sessions back from the last close (index -11) is 100.0
    assert abs(ind.pct_return(closes, 10) - 10.0) < 1e-9


def test_pct_return_none_when_not_enough_history():
    assert ind.pct_return([100.0, 101.0], 21) is None


# --- look-ahead-safety of prior_period_high/low -----------------------------

def test_prior_period_high_excludes_today():
    highs = [10.0] * 20 + [1000.0]  # today's own high is a huge spike
    # prior_period_high must NOT be influenced by today's spike
    level = ind.prior_period_high(highs, 20)
    assert level == 10.0


def test_prior_period_high_none_when_insufficient_history():
    assert ind.prior_period_high([1.0, 2.0], 20) is None


def test_recent_high_includes_today_by_design():
    highs = [10.0] * 20 + [1000.0]
    assert ind.recent_high(highs, 21) == 1000.0


# --- pivot R1 (verified correct in the audit; regression-test it) ----------

def test_pivot_r1_known_values():
    # Classic floor-trader pivot: P=(H+L+C)/3, R1=2P-L
    r1 = ind.pivot_r1(prev_high=110.0, prev_low=90.0, prev_close=100.0)
    p = (110.0 + 90.0 + 100.0) / 3.0
    assert abs(r1 - (2 * p - 90.0)) < 1e-9


# --- volume / turnover -------------------------------------------------

def test_avg_volume_excludes_today():
    vols = [100.0] * 10 + [1_000_000.0]  # today's volume is a huge spike
    avg = ind.avg_volume_excluding_today(vols, 10)
    assert avg == 100.0  # must not be pulled in by today's own spike


def test_turnover_legacy_matches_original_formula():
    vols = [1000.0] * 20
    last_close = 50.0
    result = ind.turnover_cr_legacy(last_close, vols, 20)
    expected = (last_close * (sum(vols[-20:]) / 20.0)) / 1e7
    assert abs(result - expected) < 1e-9


def test_avg_daily_turnover_uses_each_days_own_close():
    # Legacy formula would overstate this: today's close is 10x the
    # historical prices, but volume never changed.
    rows = make_rows(closes=[10.0] * 19 + [100.0], vols=[1000.0] * 20)
    legacy = ind.turnover_cr_legacy(rows[-1]["close"], [r["volume"] for r in rows], 20)
    corrected = ind.avg_daily_turnover_cr(rows, 20)
    assert corrected < legacy  # corrected figure should NOT be inflated by today's price


# --- dedupe -----------------------------------------------------------------

def test_dedupe_by_date_keeps_last_and_sorts():
    rows = [
        {"date": "2026-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": "2026-01-01", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
        {"date": "2026-01-01", "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3},
    ]
    result = ind.dedupe_by_date(rows)
    assert [r["date"] for r in result] == ["2026-01-01", "2026-01-02"]
    assert result[0]["close"] == 3  # last occurrence wins


# --- percentile rank ---------------------------------------------------

def test_percentile_rank_basic():
    population = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert ind.percentile_rank(5.0, population) == 100.0
    assert ind.percentile_rank(1.0, population) == 20.0


def test_percentile_rank_empty_population():
    assert ind.percentile_rank(5.0, []) is None
