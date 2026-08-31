#!/usr/bin/env python3
"""
Nightly NSE full-universe sweep — runs on GitHub Actions, NOT inside Cowork.

Cowork's cloud container blocks/gates outbound requests to market-data hosts,
which is why this runs here instead: GitHub Actions runners have normal,
unrestricted outbound internet, so this script just calls the Upstox endpoint
directly with `requests` — no workaround needed.

=== v4 — swing-trading opportunity engine ===

This replaces the v3 binary-filter sweep with a scored, context-aware
scanner, per the agreed design review. What's new, at a glance:

  * A market-regime layer (NIFTY 50 / NIFTY 500) fetched ONCE per run and
    applied to every stock, instead of scanning with zero market context.
  * Relative strength vs. the benchmark (5/20/60/120d) plus a cross-
    sectional percentile rank within the scanned universe — this needs a
    second pass AFTER every symbol is fetched, which is the one real
    architecture change (see the phase breakdown below).
  * Sector aggregates, computed internally from each night's own stock
    returns (no external sector-index dependency) using an optional
    "sector" field on each `universe` entry.
  * Market breadth (% above SMA50/200, advance/decline, % new 20d highs)
    as a byproduct of the full sweep — effectively free since every stock
    is already fetched every night.
  * Trend (EMA20/50/200 stack), ATR/volatility, look-ahead-safe breakout
    detection (20/50/100/252d, always compared against the PRIOR period's
    high), a consolidation/base detector, a pullback detector, an
    extension/chase classifier, and a 0-100 score with a full breakdown.
  * Structure-first entry/stop/target/risk-reward fields, and optional
    (off-by-default) position-sizing fields.
  * A small daily history snapshot under data/history/ — the one piece of
    infrastructure needed to eventually backtest any of this, since
    `coverage` itself is still a snapshot that gets overwritten nightly.

BACKWARD COMPATIBILITY: every field the v3 script produced (`close`,
`rsi14`, `sma50`, `sma200`, `pivot_r1`, `vol_x_avg`, `turnover_cr`,
`ret_1m`, `passed_all`, `checks`, `candles`) is still produced, with the
same meaning and the same calculation. Nothing existing was silently
redefined. The one field whose original approximation is documented as
known-imprecise (`turnover_cr`) gets a corrected value in a NEW field
(`avg_daily_turnover_cr`) instead of changing in place.

Every symbol now always gets the SAME set of keys in `coverage[symbol]`,
whether the fetch succeeded, the symbol has too little history, or the
fetch failed outright (`status`: "ok" | "insufficient_history" |
"fetch_failed" | "invalid_symbol") — the v3 script emitted a bare
`{"error": ...}` dict for the insufficient-history case, which is a
schema an LLM (or any consumer) shouldn't have to special-case.

On an outright fetch failure, if a previous "ok" record already exists
for that symbol, it is now kept (rather than either silently left
untouched with no indication, which is what the original script did, or
nulled out and losing real data) and marked `stale: true` with a
`last_fetch_error`, so a downstream consumer can tell "fresh tonight"
from "carried over from a prior night because tonight's fetch failed."

Cowork's morning task reads this file over plain HTTPS from
raw.githubusercontent.com — a normal, non-blocked domain — instead of the
old artifact-embedded JSON, and does no scraping itself: it only reasons
over data this script already computed.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators as ind          # noqa: E402
import scoring as sc              # noqa: E402
import risk_management as rm      # noqa: E402

# ---------------------------------------------------------------------------
# Configuration — every threshold that affects a signal is named here, not
# buried inline, so it can be changed in one place and is visible to anyone
# reviewing what the scanner actually does.
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "state.json"
HISTORY_DIR = STATE_PATH.parent / "history"

# Legacy v3 thresholds — kept exactly as-is; `checks`/`passed_all` are
# preserved byte-for-byte in meaning for backward compatibility.
RSI_LO, RSI_HI = 50.0, 68.0
VOL_MULT = 2.0
MIN_TURNOVER_CR = 5.0

# v3 used 340 calendar days (~230 trading sessions). That comfortably covers
# SMA200 but NOT a look-ahead-safe 252-trading-day breakout check, which
# needs 253 prior sessions. Raised to 420 calendar days (~275-285 trading
# sessions with NSE's holiday calendar) so the 252d breakout and its EMA200
# warm-up have real headroom. This only affects how much history is
# fetched, not any existing calculation's definition.
CANDLE_DAYS_BACK = 420
REQUEST_DELAY = 0.4          # seconds between calls — polite to the endpoint
FETCH_RETRIES = 3
FETCH_BASE_DELAY_SEC = 2.0   # exponential backoff: 2s, 4s, 8s

RS_PERIODS = [5, 20, 60, 120]          # relative-strength horizons, trading days
RSI_SLOPE_LOOKBACK = sc.RSI_SLOPE_LOOKBACK
CORPORATE_ACTION_GAP_FLAG_PCT = 15.0   # overnight-gap flag threshold

# Upstox index instrument keys for the market-regime layer. VERIFY these
# against Upstox's published instrument master before relying on this in
# production — index keys are not ISIN-based like the equity fetches below
# and the exact string can change. Fetches are wrapped so a wrong/renamed
# key degrades `market_regime` to "unknown" rather than crashing the run.
NIFTY50_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
NIFTY500_INSTRUMENT_KEY = "NSE_INDEX|Nifty 500"

# Position sizing is OFF by default, per the design review — never
# hard-code the user's account. Set via environment variables in the
# GitHub Actions workflow if/when this is wanted.
ACCOUNT_SIZE: Optional[float] = float(os.environ["SWING_ACCOUNT_SIZE"]) if os.environ.get("SWING_ACCOUNT_SIZE") else None
RISK_PER_TRADE_PCT: Optional[float] = float(os.environ["SWING_RISK_PER_TRADE_PCT"]) if os.environ.get("SWING_RISK_PER_TRADE_PCT") else None


class PermanentFetchError(Exception):
    """The endpoint told us this instrument can't be fetched (bad/delisted
    ISIN, 404/400/410) — retrying will not help, so main() should not spend
    the escalating backoff time on it."""


class TransientFetchError(Exception):
    """Network hiccup, timeout, or 5xx/429 — worth the retry/backoff."""


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def load_state() -> dict:
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=False))


def append_history_snapshot(as_of: str, coverage: Dict[str, dict]) -> None:
    """Write a small, separate, append-only daily record — the one piece of
    infrastructure that makes the backtest plan in the design review
    possible at all. `coverage` itself is a snapshot that gets overwritten
    every night, so without this there is no way to reconstruct what the
    scanner said on any past date. Deliberately condensed (not the full
    per-symbol record) to keep this cheap to accumulate; a retention/
    pruning policy is left as a later addition once there's enough history
    to need one."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date": as_of,
        "symbols": {
            sym: {
                "status": rec.get("status"),
                "score": rec.get("score"),
                "setup_type": rec.get("setup_type"),
                "close": rec.get("close"),
                "entry_zone": rec.get("entry_zone"),
                "stop_loss": rec.get("stop_loss"),
                "target_1": rec.get("target_1"),
                "target_2": rec.get("target_2"),
                "breakout_level": rec.get("breakout_level"),
                "trend_status": rec.get("trend_status"),
            }
            for sym, rec in coverage.items()
            if rec.get("status") == "ok"
        },
    }
    (HISTORY_DIR / f"{as_of}.json").write_text(json.dumps(snapshot, indent=2))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _candle_url(instrument_key: str, to_date: str, from_date: str) -> str:
    return (
        "https://api.upstox.com/v3/historical-candle/"
        f"{quote(instrument_key, safe='')}/days/1/{to_date}/{from_date}"
    )


def fetch_candles(
    instrument_key: str, to_date: str, from_date: str,
    retries: int = FETCH_RETRIES, base_delay: float = FETCH_BASE_DELAY_SEC,
) -> List[ind.Candle]:
    """Upstox v3 historical-candle endpoint. No auth. Returns rows sorted
    chronologically and de-duplicated by date: {date, open, high, low,
    close, volume}.

    Retries only on transient failures (timeouts, connection errors, 5xx,
    429). A 400/404/410 response is treated as permanent — the instrument
    key is wrong or the security is delisted, so retrying wastes the
    escalating backoff for no benefit."""
    url = _candle_url(instrument_key, to_date, from_date)
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code in (400, 404, 410):
                raise PermanentFetchError(f"HTTP {r.status_code} for {instrument_key}")
            r.raise_for_status()
            payload = r.json()
            candles = payload.get("data", {}).get("candles", [])
            rows: List[ind.Candle] = []
            for c in candles:
                # Upstox candle row: [timestamp, open, high, low, close, volume, oi]
                rows.append({
                    "date": c[0][:10],
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                })
            return ind.dedupe_by_date(rows)
        except PermanentFetchError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise TransientFetchError(f"candle fetch failed for {instrument_key}: {last_err}")


# ---------------------------------------------------------------------------
# Phase 1 — market regime (fetched ONCE per run, not per stock)
# ---------------------------------------------------------------------------

def _index_context(rows: List[ind.Candle]) -> dict:
    closes = [r["close"] for r in rows]
    return {
        "close": round(closes[-1], 2),
        "ema20": round(v, 2) if (v := ind.ema(closes, 20)) is not None else None,
        "ema50": round(v, 2) if (v := ind.ema(closes, 50)) is not None else None,
        "sma200": round(v, 2) if (v := ind.sma(closes, 200)) is not None else None,
        "rsi14": round(v, 2) if (v := ind.rsi_wilder(closes)) is not None else None,
        "return_20d": round(v, 2) if (v := ind.pct_return(closes, 20)) is not None else None,
        "return_60d": round(v, 2) if (v := ind.pct_return(closes, 60)) is not None else None,
        "volatility_20d_pct": _daily_return_stdev_pct(closes, 20),
    }


def _daily_return_stdev_pct(closes: List[float], window: int) -> Optional[float]:
    if len(closes) < window + 1:
        return None
    tail = closes[-(window + 1):]
    daily_rets = [(tail[i] / tail[i - 1] - 1.0) * 100.0 for i in range(1, len(tail))]
    if len(daily_rets) < 2:
        return None
    return round(statistics.pstdev(daily_rets), 2)


def fetch_benchmark_context(to_date: str, from_date: str) -> dict:
    """Returns the market_regime block plus the raw benchmark returns per
    RS_PERIODS horizon (needed by every stock's relative-strength calc).
    Degrades to regime="unknown" on failure rather than raising — a bad
    benchmark fetch should not take down the entire 500-stock sweep."""
    result = {
        "as_of": to_date,
        "regime": "unknown",
        "nifty50": None,
        "nifty500": None,
        "benchmark_returns": {str(n): None for n in RS_PERIODS},
    }
    try:
        rows = fetch_candles(NIFTY50_INSTRUMENT_KEY, to_date, from_date)
        closes = [r["close"] for r in rows]
        ctx = _index_context(rows)
        result["nifty50"] = ctx
        result["benchmark_returns"] = {
            str(n): (round(v, 2) if (v := ind.pct_return(closes, n)) is not None else None)
            for n in RS_PERIODS
        }
        result["regime"] = sc.classify_market_regime(
            closes[-1], ind.ema(closes, 20), ind.ema(closes, 50),
            ind.sma(closes, 200), ind.rsi_wilder(closes), ind.pct_return(closes, 20),
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [market regime] NIFTY 50 fetch failed, regime=unknown: {e}")
    time.sleep(REQUEST_DELAY)
    try:
        rows500 = fetch_candles(NIFTY500_INSTRUMENT_KEY, to_date, from_date)
        result["nifty500"] = _index_context(rows500)
    except Exception as e:  # noqa: BLE001
        print(f"  [market regime] NIFTY 500 fetch failed (non-fatal): {e}")
    time.sleep(REQUEST_DELAY)
    return result


# ---------------------------------------------------------------------------
# Phase 2 — per-symbol raw analysis (no cross-sectional info yet)
# ---------------------------------------------------------------------------

_FIELD_KEYS = [
    "status", "last_scanned", "close", "candles",
    # legacy v3 fields — unchanged meaning
    "rsi14", "sma50", "sma200", "pivot_r1", "vol_x_avg", "turnover_cr", "ret_1m",
    "passed_all", "checks",
    # new fields
    "avg_daily_turnover_cr", "ema20", "ema50", "ema200", "trend_status",
    "swing_high_20d", "swing_low_20d", "distance_from_52w_high_pct",
    "atr14", "atr_pct", "volatility_class",
    "return_5d", "return_20d", "return_60d", "return_120d", "return_1d",
    "benchmark_return_20d", "rs_5d", "rs_20d", "rs_60d", "rs_120d",
    "relative_strength_percentile",
    "sector", "sector_return_20d", "sector_rank", "sector_percentile",
    "breakout_level", "breakout_horizon_days", "distance_from_breakout_pct",
    "breakout_strength",
    "consolidation", "rsi_slope", "rsi_regime", "oversold_recovery",
    "overextension_flag", "volume_ratio_10d", "volume_ratio_20d", "volume_status",
    "setup_type", "extension_status",
    "entry_zone", "stop_loss", "target_1", "target_2", "target_method",
    "risk_per_share", "reward_per_share", "risk_reward_ratio",
    "risk_pct_of_entry", "suggested_position_risk_pct", "suggested_qty",
    "score", "score_breakdown", "extension_adjustment_applied",
    "stale", "last_fetch_error", "flags",
]


def _field_template() -> dict:
    """Every coverage[symbol] record — success, insufficient history, or
    fetch failure — starts from this same key set so a consumer never has
    to special-case a record shape. This directly fixes the v3 issue where
    the insufficient-history path returned a bare {"error": ...} dict."""
    return {k: None for k in _FIELD_KEYS}


def analyse_symbol(rows: List[ind.Candle], benchmark_returns: Dict[str, Optional[float]]) -> dict:
    """Raw per-symbol analysis. `benchmark_returns` (keyed by str(period))
    comes from Phase 1 and is the same for every stock in a given run.
    Cross-sectional fields (relative_strength_percentile, sector_percentile,
    score) are intentionally left None here — they require every symbol to
    have been analysed first (Phase 3/4)."""
    result = _field_template()
    if len(rows) < 60:
        result.update({
            "status": "insufficient_history", "candles": len(rows),
            "flags": [f"only {len(rows)} candles — need 60+ (200+ preferred for SMA200/EMA200)"],
        })
        return result

    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r["volume"] for r in rows]
    last, prev = rows[-1], rows[-2]
    close = last["close"]
    flags: List[str] = []

    # --- legacy v3 calculations, unchanged ---
    r1 = ind.pivot_r1(prev["high"], prev["low"], prev["close"])
    sma50, sma200 = ind.sma(closes, 50), ind.sma(closes, 200)
    avg10v = ind.avg_volume_excluding_today(vols, 10)
    rsi14 = ind.rsi_wilder(closes)
    ret1m = ind.pct_return(closes, 21)
    turnover_cr = ind.turnover_cr_legacy(close, vols, 20)
    checks = {
        "above_R1": close > r1,
        "volume_2x_10d": avg10v is not None and avg10v > 0 and last["volume"] > VOL_MULT * avg10v,
        "above_50dma": sma50 is not None and close > sma50,
        "rsi_in_band": rsi14 is not None and RSI_LO <= rsi14 <= RSI_HI,
        "liquid_enough": turnover_cr is not None and turnover_cr >= MIN_TURNOVER_CR,
    }

    # --- trend ---
    ema20, ema50, ema200 = ind.ema(closes, 20), ind.ema(closes, 50), ind.ema(closes, 200)
    trend_status = sc.classify_trend_status(close, ema20, ema50, ema200)

    # --- volatility ---
    atr_series_vals = ind.atr_series(rows, 14)
    atr14 = atr_series_vals[-1]
    atr_pct = (atr14 / close * 100.0) if atr14 else None
    atr_pct_series = [
        (a / rows[i]["close"] * 100.0) if a is not None else None
        for i, a in enumerate(atr_series_vals)
    ]
    volatility_class = sc.classify_volatility(atr_pct)

    # --- momentum / RSI ---
    rsi_series_vals = ind.rsi_series(closes, 14)
    rsi_slope_val = ind.rsi_slope(closes, 14, RSI_SLOPE_LOOKBACK)
    rsi_regime = sc.classify_rsi_regime(rsi14, rsi_slope_val)
    oversold_recovery = sc.detect_oversold_recovery(rsi_series_vals)
    overextension_flag = sc.detect_overextension(rsi14)

    # --- returns & relative strength (raw; percentile comes in Phase 3) ---
    returns = {n: ind.pct_return(closes, n) for n in RS_PERIODS}
    ret_1d = ind.pct_return(closes, 1)
    rs = {}
    for n in RS_PERIODS:
        bench_r = benchmark_returns.get(str(n))
        rs[n] = round(returns[n] - bench_r, 2) if (returns[n] is not None and bench_r is not None) else None

    # --- volume ---
    avg20v = ind.avg_volume_excluding_today(vols, 20)
    vol_ratio_10d = round(last["volume"] / avg10v, 2) if avg10v else None
    vol_ratio_20d = round(last["volume"] / avg20v, 2) if avg20v else None
    volume_status = sc.classify_volume(close, prev["close"], vol_ratio_20d)

    # --- breakout (look-ahead-safe: always the PRIOR period's high) ---
    breakout = sc.classify_breakout(close, highs, vol_ratio_20d, ind.prior_period_high)

    # --- consolidation / base ---
    consolidation = sc.detect_consolidation(atr_pct_series, vols)

    # --- pullback ---
    is_pullback = sc.detect_pullback(trend_status, close, ema20, ema50, rsi_series_vals)

    setup_type = sc.classify_setup_type(
        breakout["breakout_strength"], bool(consolidation["in_base"]), is_pullback, trend_status,
    )
    extension_status = sc.classify_extension(close, ema20, breakout.get("breakout_level"), atr14)

    # --- structure levels ---
    swing_high_20d = ind.recent_high(highs, 20)
    swing_low_20d = ind.recent_low(lows, 20)
    lookback_52w = min(len(highs), 252)
    high_52w = ind.recent_high(highs, lookback_52w) if lookback_52w >= 20 else None
    distance_52w_high_pct = round((close / high_52w - 1.0) * 100.0, 2) if high_52w else None

    avg_daily_turnover = ind.avg_daily_turnover_cr(rows, 20)

    # --- data-quality flags ---
    if prev["close"] > 0:
        gap_pct = abs(last["open"] / prev["close"] - 1.0) * 100.0
        if gap_pct >= CORPORATE_ACTION_GAP_FLAG_PCT:
            flags.append(f"overnight_gap_{round(gap_pct, 1)}pct_check_for_corporate_action")

    # --- risk / reward (structure-first; never fabricated) ---
    entry_zone = rm.compute_entry_zone(setup_type, close, breakout.get("breakout_level"), ema20)
    entry = entry_zone[1] if entry_zone else close
    base_low = swing_low_20d if consolidation["in_base"] else None
    candidate_resistances = [h for h in (swing_high_20d, ind.recent_high(highs, min(len(highs), 100))) if h and h > entry]
    resistance_above = min(candidate_resistances) if candidate_resistances else None
    stop_loss = rm.compute_stop_loss(setup_type, close, swing_low_20d, ema50, atr14)
    targets = rm.compute_targets(close, entry, breakout.get("breakout_level"), base_low, resistance_above, atr14)
    rr = rm.compute_risk_reward(entry, stop_loss, targets["target_1"])
    position = rm.compute_position_sizing(entry, rr["risk_per_share"], ACCOUNT_SIZE, RISK_PER_TRADE_PCT)

    if stop_loss is None:
        flags.append("stop_unavailable")
    if targets["target_method"] == "unavailable":
        flags.append("target_unavailable")
    if rr["risk_reward_ratio"] is None:
        flags.append("risk_reward_unavailable")

    result.update({
        "status": "ok",
        "last_scanned": last["date"],
        "close": round(close, 2),
        "candles": len(rows),
        "rsi14": round(rsi14, 2) if rsi14 is not None else None,
        "sma50": round(sma50, 2) if sma50 is not None else None,
        "sma200": round(sma200, 2) if sma200 is not None else None,
        "pivot_r1": round(r1, 2),
        "vol_x_avg": vol_ratio_10d,
        "turnover_cr": round(turnover_cr, 2) if turnover_cr is not None else None,
        "ret_1m": round(ret1m, 2) if ret1m is not None else None,
        "passed_all": all(checks.values()),
        "checks": checks,

        "avg_daily_turnover_cr": round(avg_daily_turnover, 2) if avg_daily_turnover is not None else None,
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "ema200": round(ema200, 2) if ema200 is not None else None,
        "trend_status": trend_status,
        "swing_high_20d": round(swing_high_20d, 2) if swing_high_20d is not None else None,
        "swing_low_20d": round(swing_low_20d, 2) if swing_low_20d is not None else None,
        "distance_from_52w_high_pct": distance_52w_high_pct,

        "atr14": round(atr14, 2) if atr14 is not None else None,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "volatility_class": volatility_class,

        "return_1d": round(ret_1d, 2) if ret_1d is not None else None,
        "return_5d": round(returns[5], 2) if returns[5] is not None else None,
        "return_20d": round(returns[20], 2) if returns[20] is not None else None,
        "return_60d": round(returns[60], 2) if returns[60] is not None else None,
        "return_120d": round(returns[120], 2) if returns[120] is not None else None,
        "benchmark_return_20d": benchmark_returns.get("20"),
        "rs_5d": rs[5], "rs_20d": rs[20], "rs_60d": rs[60], "rs_120d": rs[120],
        "relative_strength_percentile": None,  # filled in Phase 3

        "sector": None, "sector_return_20d": None, "sector_rank": None, "sector_percentile": None,

        "breakout_level": breakout["breakout_level"],
        "breakout_horizon_days": breakout["breakout_horizon_days"],
        "distance_from_breakout_pct": breakout["distance_from_breakout_pct"],
        "breakout_strength": breakout["breakout_strength"],

        "consolidation": consolidation,
        "rsi_slope": round(rsi_slope_val, 2) if rsi_slope_val is not None else None,
        "rsi_regime": rsi_regime,
        "oversold_recovery": oversold_recovery,
        "overextension_flag": overextension_flag,
        "volume_ratio_10d": vol_ratio_10d,
        "volume_ratio_20d": vol_ratio_20d,
        "volume_status": volume_status,

        "setup_type": setup_type,
        "extension_status": extension_status,

        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "target_1": targets["target_1"], "target_2": targets["target_2"],
        "target_method": targets["target_method"],
        "risk_per_share": rr["risk_per_share"], "reward_per_share": rr["reward_per_share"],
        "risk_reward_ratio": rr["risk_reward_ratio"],
        "risk_pct_of_entry": position["risk_pct_of_entry"],
        "suggested_position_risk_pct": position["suggested_position_risk_pct"],
        "suggested_qty": position["suggested_qty"],

        "score": None, "score_breakdown": None, "extension_adjustment_applied": None,  # filled in Phase 4
        "stale": False, "last_fetch_error": None,
        "flags": flags,
    })
    return result


# ---------------------------------------------------------------------------
# Phase 3 — cross-sectional pass (percentile rank, sector, breadth)
# ---------------------------------------------------------------------------

def cross_sectional_pass(
    coverage: Dict[str, dict], universe_sectors: Dict[str, Optional[str]],
) -> dict:
    """Runs once, after every symbol in the universe has an entry in
    `coverage` for tonight. Needs the whole population for percentile rank,
    sector aggregation, and breadth — none of this can be computed
    per-symbol in isolation."""
    ok_symbols = [s for s, rec in coverage.items() if rec.get("status") == "ok"]

    # relative strength percentile, based on the 20-trading-day horizon
    # (the standard "swing" lookback) — see design review §3.
    rs20_population = [coverage[s]["rs_20d"] for s in ok_symbols if coverage[s]["rs_20d"] is not None]
    for s in ok_symbols:
        rs20 = coverage[s]["rs_20d"]
        coverage[s]["relative_strength_percentile"] = (
            ind.percentile_rank(rs20, rs20_population) if rs20 is not None else None
        )

    # sector aggregation — internal only, no external sector-index fetch.
    sector_returns: Dict[str, List[float]] = {}
    for s in ok_symbols:
        sector = universe_sectors.get(s)
        coverage[s]["sector"] = sector
        r20 = coverage[s]["return_20d"]
        if sector and r20 is not None:
            sector_returns.setdefault(sector, []).append(r20)

    sector_summary = {}
    if sector_returns:
        sector_means = {sec: round(sum(v) / len(v), 2) for sec, v in sector_returns.items()}
        ranked = sorted(sector_means, key=lambda sec: sector_means[sec], reverse=True)
        num_sectors = len(ranked)
        for rank, sec in enumerate(ranked, start=1):
            sector_summary[sec] = {
                "return_20d": sector_means[sec], "rank": rank,
                "member_count": len(sector_returns[sec]),
                "percentile": round((num_sectors - rank + 1) / num_sectors * 100.0, 1),
            }
        for s in ok_symbols:
            sector = coverage[s]["sector"]
            if sector and sector in sector_summary:
                coverage[s]["sector_return_20d"] = sector_summary[sector]["return_20d"]
                coverage[s]["sector_rank"] = sector_summary[sector]["rank"]
                coverage[s]["sector_percentile"] = sector_summary[sector]["percentile"]
            else:
                coverage[s]["flags"] = (coverage[s].get("flags") or []) + ["sector_unknown"]

    # breadth — descriptive market-wide stats, free byproduct of the sweep.
    above_sma50 = [s for s in ok_symbols if coverage[s]["sma50"] and coverage[s]["close"] > coverage[s]["sma50"]]
    have_sma50 = [s for s in ok_symbols if coverage[s]["sma50"] is not None]
    above_sma200 = [s for s in ok_symbols if coverage[s]["sma200"] and coverage[s]["close"] > coverage[s]["sma200"]]
    have_sma200 = [s for s in ok_symbols if coverage[s]["sma200"] is not None]
    advancing = [s for s in ok_symbols if (coverage[s]["return_1d"] or 0) > 0]
    declining = [s for s in ok_symbols if (coverage[s]["return_1d"] or 0) < 0]
    new_20d_highs = [
        s for s in ok_symbols
        if coverage[s]["swing_high_20d"] is not None and coverage[s]["close"] >= coverage[s]["swing_high_20d"]
    ]
    breadth = {
        "scanned_ok": len(ok_symbols),
        "pct_above_sma50": round(len(above_sma50) / len(have_sma50) * 100.0, 1) if have_sma50 else None,
        "pct_above_sma200": round(len(above_sma200) / len(have_sma200) * 100.0, 1) if have_sma200 else None,
        "advance_decline": len(advancing) - len(declining),
        "pct_new_20d_highs": round(len(new_20d_highs) / len(ok_symbols) * 100.0, 1) if ok_symbols else None,
    }
    return {"sector_summary": sector_summary, "breadth": breadth}


# ---------------------------------------------------------------------------
# Phase 4 — scoring pass
# ---------------------------------------------------------------------------

def scoring_pass(coverage: Dict[str, dict], regime: str) -> None:
    for rec in coverage.values():
        if rec.get("status") != "ok":
            continue
        scored = sc.compute_score(
            regime=regime,
            sector_percentile=rec["sector_percentile"],
            rs_percentile=rec["relative_strength_percentile"],
            trend_status=rec["trend_status"],
            setup_type=rec["setup_type"],
            volume_status=rec["volume_status"],
            rsi_regime=rec["rsi_regime"],
            risk_reward_ratio=rec["risk_reward_ratio"],
            extension_status=rec["extension_status"],
        )
        rec["score"] = scored["score"]
        rec["score_breakdown"] = scored["score_breakdown"]
        rec["extension_adjustment_applied"] = scored["extension_adjustment_applied"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    state = load_state()
    universe = state["universe"]
    universe_sectors = {sym["symbol"]: sym.get("sector") for sym in universe}

    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=CANDLE_DAYS_BACK)).isoformat()

    print(f"Full nightly sweep: {len(universe)} symbols")

    print("Phase 1: market regime (NIFTY 50 / NIFTY 500)...")
    benchmark_ctx = fetch_benchmark_context(to_date, from_date)
    print(f"  regime={benchmark_ctx['regime']}")

    coverage: Dict[str, dict] = state.setdefault("coverage", {})
    fetch_failed: List[str] = []
    invalid_symbol: List[str] = []

    print("Phase 2: per-symbol fetch + raw analysis...")
    for i, sym in enumerate(universe):
        symbol, isin = sym["symbol"], sym["isin"]
        try:
            rows = fetch_candles(f"NSE_EQ|{isin}", to_date, from_date)
            result = analyse_symbol(rows, benchmark_ctx["benchmark_returns"])
            coverage[symbol] = result
            if (i + 1) % 25 == 0:
                print(f"  ...{i + 1}/{len(universe)} done")
        except PermanentFetchError as e:
            invalid_symbol.append(symbol)
            _mark_failed(coverage, symbol, "invalid_symbol")
            print(f"  {symbol:<16} INVALID (not retried): {e}")
        except Exception as e:  # noqa: BLE001
            fetch_failed.append(symbol)
            _mark_failed(coverage, symbol, "fetch_failed")
            print(f"  {symbol:<16} FETCH FAILED: {e}")
        time.sleep(REQUEST_DELAY)

    print("Phase 3: cross-sectional pass (relative strength %ile, sector, breadth)...")
    cross_sectional = cross_sectional_pass(coverage, universe_sectors)

    print("Phase 4: scoring pass...")
    scoring_pass(coverage, benchmark_ctx["regime"])

    ok_count = sum(1 for rec in coverage.values() if rec.get("status") == "ok")
    scored = [(s, rec["score"]) for s, rec in coverage.items() if rec.get("status") == "ok" and rec["score"] is not None]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_candidates = [s for s, _ in scored[:25]]
    passed_legacy = [s for s, rec in coverage.items() if rec.get("status") == "ok" and rec.get("passed_all")]

    state["market_regime"] = benchmark_ctx
    state["breadth"] = cross_sectional["breadth"]
    state["sector_summary"] = cross_sectional["sector_summary"]
    state["last_sweep_run_at"] = datetime.now(timezone.utc).isoformat()
    state["last_sweep_summary"] = {
        "date": to_date,
        "scanned": len(universe),
        "ok": ok_count,
        "fetch_failed": fetch_failed,
        "invalid_symbol": invalid_symbol,
        "passed_all_count": len(passed_legacy),      # legacy binary gate, preserved
        "passed_all_names": passed_legacy,
        "top_candidates_by_score": top_candidates,
    }
    # rotation-era fields no longer meaningful — drop them if present
    state.pop("rotation_cursor", None)
    state.pop("batch_size", None)
    state.pop("last_full_pass_completed", None)
    state.pop("watch_always", None)
    save_state(state)
    append_history_snapshot(to_date, coverage)

    print(
        f"\nDone. scanned={len(universe)} ok={ok_count} "
        f"fetch_failed={len(fetch_failed)} invalid_symbol={len(invalid_symbol)} "
        f"passed_all={len(passed_legacy)} top_by_score={top_candidates[:10]}"
    )
    if (len(fetch_failed) + len(invalid_symbol)) == len(universe):
        sys.exit(1)  # every fetch failed — flag the run as red so you notice


def _mark_failed(coverage: Dict[str, dict], symbol: str, error: str) -> None:
    """On a fetch failure, keep a prior 'ok' record if one exists (real,
    slightly-stale data beats no data) but mark it explicitly stale rather
    than leaving it silently indistinguishable from tonight's fresh scan.
    If there is no prior record at all, store a fully-templated placeholder
    so the schema stays consistent for every symbol, every night."""
    existing = coverage.get(symbol)
    if existing and existing.get("status") == "ok":
        existing["stale"] = True
        existing["last_fetch_error"] = error
        return
    placeholder = _field_template()
    placeholder["status"] = error
    placeholder["stale"] = True
    placeholder["last_fetch_error"] = error
    coverage[symbol] = placeholder


if __name__ == "__main__":
    main()
