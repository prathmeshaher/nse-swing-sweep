# NSE Swing Trading Opportunity Engine

Nightly full-Nifty-500 scanner. See `CHANGELOG.md` for what changed from
the v3 binary-filter sweep, and the design review delivered earlier in
this conversation for the full rationale, prioritization, schema, and
backtest plan behind every field.

## Layout

```
scripts/
  rotation_sweep_v3_full500.py   # orchestrator / entry point (unchanged CLI)
  indicators.py                  # pure calculation functions (RSI, EMA, ATR, ...)
  scoring.py                     # classification + 0-100 scoring engine
  risk_management.py             # entry/stop/target/risk-reward + position sizing
data/
  state.json                     # unchanged location; richer schema (see CHANGELOG)
  history/YYYY-MM-DD.json         # NEW — daily snapshot log for future backtesting
tests/
  test_indicators.py, test_scoring.py, test_risk_management.py,
  test_pipeline.py, test_main_dry_run.py
```

Drop `scripts/` and (if not already present) `data/` into the existing
repo at the same relative paths the v3 script used
(`STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "state.json"`
is unchanged).

## Running

```
pip install -r requirements.txt
python scripts/rotation_sweep_v3_full500.py
```

Requires outbound access to `api.upstox.com` (works from GitHub Actions;
does not work from this Cowork sandbox, which is why everything here was
validated against synthetic data instead — see `tests/`).

## Optional configuration (environment variables)

Both are unset/disabled by default — position sizing fields stay `null`
until you opt in:

```
SWING_ACCOUNT_SIZE=100000        # your account size, in the same currency as prices
SWING_RISK_PER_TRADE_PCT=1.0     # % of account to risk per trade
```

## Tests

```
pip install -r requirements-dev.txt
pytest tests/ -v
```

76 tests, all against synthetic candle data (no network dependency) —
covering the RSI/EMA/ATR/pivot/turnover math, look-ahead-safety of the
breakout and volume-average calculations, every classification function,
the scoring engine, and a full `main()` dry run with a mocked fetch layer
exercising the ok / insufficient-history / invalid-symbol /
transient-failure paths together.

## Before deploying against real data

See the "Before this runs against real data" section of `CHANGELOG.md` —
in particular, the NIFTY 50 / NIFTY 500 instrument keys used for the
market-regime layer should be confirmed against Upstox's instrument
master before relying on `market_regime` in production.
