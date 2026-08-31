# Changelog — v3 → v4 (swing-trading opportunity engine)

This implements Stages 1–4 of the agreed implementation plan (design
review delivered earlier in this conversation). Stage 5 (a standalone
backtest harness, and any weight re-tuning based on it) is intentionally
**not** included — it needs the history log this version starts writing
tonight, and isn't meaningful until that log has real sessions in it.

## Drop-in compatibility

- Same entry point: `python scripts/rotation_sweep_v3_full500.py`.
- Same `STATE_PATH` resolution (`<repo>/data/state.json`), so the script
  can be dropped into the same location in the repo.
- Same universe/coverage top-level keys; `universe` entries gain an
  **optional** `"sector"` field (see "Sector mapping" below) — absence of
  it degrades sector fields to `null`, it does not break anything.
- Same GitHub Actions usage pattern: full nightly sweep, no rotation, no
  new secrets required for the scanner itself to run (Upstox is still
  public/unauthenticated). `requirements.txt` is unchanged in spirit
  (`requests` only).

## Fields that are unchanged in meaning and calculation (verified in the audit)

`close`, `rsi14`, `sma50`, `sma200`, `pivot_r1`, `vol_x_avg`, `turnover_cr`,
`ret_1m`, `passed_all`, `checks`, `candles`. None of these were touched.

## Breaking / behavior changes (read this before deploying)

1. **`coverage[symbol]` now always has the same key set**, whether the
   symbol scanned cleanly, had too little history, or failed to fetch.
   The old "insufficient history" path returned a bare `{"error": "..."}`
   dict; that shape no longer exists. If anything downstream pattern-
   matches on `"error" in coverage[symbol]`, switch it to
   `coverage[symbol]["status"] != "ok"`.
2. **A fetch failure no longer silently leaves `coverage[symbol]`
   untouched with no signal.** If a prior "ok" record exists, it's kept
   (so real data isn't lost) but now carries `"stale": true` and
   `"last_fetch_error"`. If there's no prior record, a fully-null
   templated record with `"status": "fetch_failed"` (or
   `"invalid_symbol"` for a permanent 4xx) is stored instead of the
   symbol being absent from `coverage` entirely.
3. **`CANDLE_DAYS_BACK` increased from 340 to 420 calendar days.** Needed
   so a look-ahead-safe 252-trading-day breakout check (and EMA200's
   warm-up) has enough history to ever return a non-`null` value. This
   only changes how much history is *fetched*; it does not change any
   existing field's definition. It does mean slightly larger API
   responses per symbol.
4. **`last_sweep_summary.failed` is replaced by two lists**,
   `fetch_failed` and `invalid_symbol`, so a permanently bad ISIN (won't
   fix itself on retry) is distinguishable from a transient network
   failure (might succeed tomorrow). `passed_all_count` /
   `passed_all_names` are unchanged and still reflect the original
   binary five-check gate.
5. **New top-level state keys**: `market_regime`, `breadth`,
   `sector_summary`. None of these existed before; nothing reads them
   unless it's updated to.
6. **New file location**: `data/history/<YYYY-MM-DD>.json`, one small
   snapshot per run. This is what makes the backtest plan in the design
   review possible — `coverage` itself is still overwritten nightly and
   was never meant to double as a time series.

## New fields, additive only (see the design review for the full list and rationale)

Trend (`ema20/50/200`, `trend_status`), volatility (`atr14`, `atr_pct`,
`volatility_class`), returns & relative strength (`return_5d/20d/60d/120d`,
`rs_5d/20d/60d/120d`, `relative_strength_percentile`), sector
(`sector`, `sector_return_20d`, `sector_rank`, `sector_percentile`),
breakout (`breakout_level`, `breakout_horizon_days`,
`distance_from_breakout_pct`, `breakout_strength`), `consolidation`,
momentum (`rsi_slope`, `rsi_regime`, `oversold_recovery`,
`overextension_flag`), volume (`volume_ratio_10d/20d`, `volume_status`),
`setup_type`, `extension_status`, risk/reward (`entry_zone`, `stop_loss`,
`target_1`, `target_2`, `target_method`, `risk_per_share`,
`reward_per_share`, `risk_reward_ratio`), position sizing
(`risk_pct_of_entry`, `suggested_position_risk_pct`, `suggested_qty` —
all `null` unless `SWING_ACCOUNT_SIZE`/`SWING_RISK_PER_TRADE_PCT` are set),
`score` + `score_breakdown`, and the corrected turnover figure
`avg_daily_turnover_cr` (kept separate from the legacy `turnover_cr`).

## Before this runs against real data

- **Verify `NIFTY50_INSTRUMENT_KEY` / `NIFTY500_INSTRUMENT_KEY`** in
  `scripts/rotation_sweep_v3_full500.py` against Upstox's current
  instrument master. They're set to `"NSE_INDEX|Nifty 50"` /
  `"NSE_INDEX|Nifty 500"` as a reasonable guess based on the endpoint's
  existing `NSE_EQ|<isin>` convention, but this sandbox has no network
  access to Upstox to confirm the exact index key format. If it's wrong,
  the scanner degrades gracefully (`market_regime.regime = "unknown"`,
  every stock's `score` still computes using the "unknown" regime
  midpoint) rather than crashing — but it should still be confirmed
  before relying on the regime output.
- **Sector mapping** is optional and additive: add a `"sector"` string to
  each `universe` entry whenever convenient; unmapped stocks simply get
  `null` sector fields and a `"sector_unknown"` flag rather than a
  fabricated sector.
- Run `pytest tests/ -v` (76 tests, all passing against synthetic data —
  this sandbox cannot reach Upstox to test live) before deploying.
