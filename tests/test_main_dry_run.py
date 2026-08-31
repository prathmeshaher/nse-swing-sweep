"""Full main() orchestration dry run with fetch_candles monkeypatched —
proves Phase 1-6 wire together correctly (benchmark fetch, per-symbol loop
with a mix of ok/insufficient/invalid/transient-failure outcomes, the
cross-sectional pass, scoring, state.json + history snapshot writes)
without ever touching the network, which this sandbox can't reach anyway.
"""
import json

import conftest  # noqa: F401
import rotation_sweep_v3_full500 as scanner
from test_pipeline import _synthetic_uptrend_with_breakout  # noqa: E402


def _fake_fetch_candles(instrument_key, to_date, from_date, retries=3, base_delay=2.0):
    if instrument_key == scanner.NIFTY50_INSTRUMENT_KEY:
        return _synthetic_uptrend_with_breakout(breakout_pct=2.0)
    if instrument_key == scanner.NIFTY500_INSTRUMENT_KEY:
        return _synthetic_uptrend_with_breakout(breakout_pct=1.0)
    # equity fetches are keyed by ISIN (NSE_EQ|<isin>), not by symbol name
    if instrument_key == "NSE_EQ|INE000A00001":  # GOODSTOCK
        return _synthetic_uptrend_with_breakout(breakout_pct=15.0)
    if instrument_key == "NSE_EQ|INE000A00002":  # THINSTOCK
        return _synthetic_uptrend_with_breakout(n_flat=5)[:20]  # insufficient history
    if instrument_key == "NSE_EQ|INE000A00003":  # DEADSTOCK
        raise scanner.PermanentFetchError("HTTP 404")
    if instrument_key == "NSE_EQ|INE000A00004":  # FLAKYSTOCK
        raise scanner.TransientFetchError("timeout")
    raise AssertionError(f"unexpected instrument key in test: {instrument_key}")


def test_main_end_to_end_dry_run(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = data_dir / "state.json"
    history_dir = data_dir / "history"

    universe = [
        {"symbol": "GOODSTOCK", "isin": "INE000A00001", "sector": "IT"},
        {"symbol": "THINSTOCK", "isin": "INE000A00002", "sector": "IT"},
        {"symbol": "DEADSTOCK", "isin": "INE000A00003", "sector": "AUTO"},
        {"symbol": "FLAKYSTOCK", "isin": "INE000A00004", "sector": "AUTO"},
    ]
    state_path.write_text(json.dumps({"universe": universe, "coverage": {}}))

    monkeypatch.setattr(scanner, "STATE_PATH", state_path)
    monkeypatch.setattr(scanner, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(scanner, "fetch_candles", _fake_fetch_candles)
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)  # skip real delays in the test

    scanner.main()

    saved = json.loads(state_path.read_text())
    coverage = saved["coverage"]

    assert coverage["GOODSTOCK"]["status"] == "ok"
    assert coverage["GOODSTOCK"]["score"] is not None
    assert coverage["THINSTOCK"]["status"] == "insufficient_history"
    assert coverage["DEADSTOCK"]["status"] == "invalid_symbol"
    assert coverage["FLAKYSTOCK"]["status"] == "fetch_failed"

    # every record, regardless of outcome, has the identical key set
    key_sets = {frozenset(rec.keys()) for rec in coverage.values()}
    assert len(key_sets) == 1

    assert saved["market_regime"]["regime"] != "unknown"
    assert "breadth" in saved
    assert "sector_summary" in saved
    assert saved["last_sweep_summary"]["scanned"] == 4
    assert saved["last_sweep_summary"]["ok"] == 1
    assert "GOODSTOCK" in saved["last_sweep_summary"]["top_candidates_by_score"]

    # rotation-era fields are still stripped
    assert "rotation_cursor" not in saved

    # history snapshot got written
    history_files = list(history_dir.glob("*.json"))
    assert len(history_files) == 1
    snapshot = json.loads(history_files[0].read_text())
    assert "GOODSTOCK" in snapshot["symbols"]
    assert "THINSTOCK" not in snapshot["symbols"]  # only "ok" symbols go into the snapshot


def test_main_exits_nonzero_when_every_fetch_fails(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = data_dir / "state.json"
    universe = [{"symbol": "DEADSTOCK", "isin": "INE000A00003", "sector": "AUTO"}]
    state_path.write_text(json.dumps({"universe": universe, "coverage": {}}))

    monkeypatch.setattr(scanner, "STATE_PATH", state_path)
    monkeypatch.setattr(scanner, "HISTORY_DIR", data_dir / "history")
    monkeypatch.setattr(scanner, "fetch_candles", _fake_fetch_candles)
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)

    try:
        scanner.main()
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 1
