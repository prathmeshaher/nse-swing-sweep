"""End-to-end tests of the analysis pipeline using synthetic candle data —
no network access, so these run anywhere (including this Cowork sandbox,
which cannot reach Upstox). They exercise analyse_symbol() and the Phase
3/4 cross-sectional + scoring passes together.
"""
import conftest  # noqa: F401
import rotation_sweep_v3_full500 as scanner


def _synthetic_uptrend_with_breakout(n_flat=280, breakout_pct=8.0):
    """n_flat sessions of a mild, steady uptrend, then a volume-backed
    breakout above the prior 20-day high on the final day."""
    rows = []
    price = 100.0
    for i in range(n_flat):
        price *= 1.0015  # ~0.15%/day drift
        rows.append({
            "date": f"2025-{(i // 28) % 12 + 1:02d}-{(i % 28) + 1:02d}",
            "open": price * 0.999, "high": price * 1.006, "low": price * 0.994,
            "close": price, "volume": 500_000.0,
        })
    breakout_close = price * (1 + breakout_pct / 100.0)
    rows.append({
        "date": "2026-08-29", "open": price * 1.001, "high": breakout_close * 1.002,
        "low": price * 1.0, "close": breakout_close, "volume": 500_000.0 * 3.0,
    })
    # de-duplicate synthetic dates deterministically for the test
    for i, r in enumerate(rows):
        r["date"] = f"D{i:04d}"
    return rows


BENCHMARK_RETURNS_FLAT = {"5": 0.0, "20": 1.0, "60": 3.0, "120": 6.0}


def test_analyse_symbol_ok_status_and_full_schema():
    rows = _synthetic_uptrend_with_breakout()
    result = scanner.analyse_symbol(rows, BENCHMARK_RETURNS_FLAT)
    assert result["status"] == "ok"
    # every key from the shared template must be present regardless of path
    assert set(result.keys()) == set(scanner._FIELD_KEYS)


def test_analyse_symbol_insufficient_history_same_schema():
    rows = _synthetic_uptrend_with_breakout(n_flat=10)[:20]  # well under 60
    result = scanner.analyse_symbol(rows, BENCHMARK_RETURNS_FLAT)
    assert result["status"] == "insufficient_history"
    assert set(result.keys()) == set(scanner._FIELD_KEYS)
    assert result["close"] is None  # no fabricated values on the error path


def test_breakout_scenario_detects_breakout_setup():
    rows = _synthetic_uptrend_with_breakout(breakout_pct=8.0)
    result = scanner.analyse_symbol(rows, BENCHMARK_RETURNS_FLAT)
    assert result["breakout_strength"] in ("confirmed", "moderate")
    assert result["trend_status"] in ("strong_uptrend", "uptrend")
    # legacy fields must still be populated
    assert result["rsi14"] is not None
    assert result["sma50"] is not None
    assert result["passed_all"] in (True, False)


def test_no_breakout_scenario_is_no_setup_or_pullback():
    rows = _synthetic_uptrend_with_breakout(breakout_pct=0.0)  # flat finish, no breakout
    result = scanner.analyse_symbol(rows, BENCHMARK_RETURNS_FLAT)
    assert result["setup_type"] in ("no_setup", "pullback", "momentum_continuation")


def test_cross_sectional_pass_assigns_percentile_and_breadth():
    strong = scanner.analyse_symbol(_synthetic_uptrend_with_breakout(breakout_pct=15.0), BENCHMARK_RETURNS_FLAT)
    weak = scanner.analyse_symbol(_synthetic_uptrend_with_breakout(breakout_pct=-5.0), BENCHMARK_RETURNS_FLAT)
    coverage = {"STRONG": strong, "WEAK": weak}
    result = scanner.cross_sectional_pass(coverage, {"STRONG": "IT", "WEAK": "IT"})

    assert coverage["STRONG"]["relative_strength_percentile"] >= coverage["WEAK"]["relative_strength_percentile"]
    assert result["breadth"]["scanned_ok"] == 2
    assert result["breadth"]["pct_above_sma50"] is not None


def test_scoring_pass_ranks_strong_setup_above_weak():
    strong = scanner.analyse_symbol(_synthetic_uptrend_with_breakout(breakout_pct=15.0), BENCHMARK_RETURNS_FLAT)
    weak = scanner.analyse_symbol(_synthetic_uptrend_with_breakout(breakout_pct=-5.0), BENCHMARK_RETURNS_FLAT)
    coverage = {"STRONG": strong, "WEAK": weak}
    scanner.cross_sectional_pass(coverage, {"STRONG": "IT", "WEAK": "IT"})
    scanner.scoring_pass(coverage, regime="bullish")

    assert coverage["STRONG"]["score"] is not None
    assert coverage["WEAK"]["score"] is not None
    assert coverage["STRONG"]["score"] > coverage["WEAK"]["score"]


def test_field_template_and_mark_failed_keep_schema_consistent():
    coverage = {}
    scanner._mark_failed(coverage, "GHOST", "invalid_symbol")
    assert coverage["GHOST"]["status"] == "invalid_symbol"
    assert set(coverage["GHOST"].keys()) == set(scanner._FIELD_KEYS)


def test_mark_failed_preserves_prior_ok_record_as_stale():
    coverage = {"AAA": {"status": "ok", "close": 123.4}}
    scanner._mark_failed(coverage, "AAA", "fetch_failed")
    assert coverage["AAA"]["status"] == "ok"     # untouched, real data kept
    assert coverage["AAA"]["close"] == 123.4
    assert coverage["AAA"]["stale"] is True
    assert coverage["AAA"]["last_fetch_error"] == "fetch_failed"
