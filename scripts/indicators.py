#!/usr/bin/env python3
"""
Pure, side-effect-free calculation functions for the NSE swing-trading
scanner.

Every function here takes plain lists/floats and returns plain
values — no I/O, no network, no state. That is deliberate: it is what
makes them unit-testable (see tests/test_indicators.py) and safe to
reuse from a future backtest harness without dragging the fetch/
scoring machinery along.

Look-ahead-bias convention used throughout this module: a "prior_*"
function EXCLUDES the most recent bar (index -1). Any calculation that
is used as an entry/breakout CONDITION for "today" must be built from
a prior_* helper, never from a window that includes today's own bar —
otherwise today's high/low would be used to confirm today's own
signal, which cannot happen in live trading.
"""
from __future__ import annotations

from typing import List, Optional, TypedDict


class Candle(TypedDict):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def sma(values: List[float], period: int) -> Optional[float]:
    """Simple trailing mean of the last `period` values, or None if there
    isn't enough history yet. Unchanged from the original script."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    """Exponential moving average, seeded with the SMA of the first
    `period` values and then smoothed forward through the rest of the
    series (the same "use all available history to warm up" approach the
    original script already uses for RSI). Returns only the latest value;
    nothing downstream needs the full EMA series today."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


# ---------------------------------------------------------------------------
# RSI (Wilder) — verified against the original implementation; numerically
# identical, just restructured to expose the full series so a slope can be
# read off without re-seeding the smoothing window from a truncated list
# (recomputing RSI from `closes[:-5]` instead of reading `series[-6]` would
# silently produce a different, less-warmed-up number).
# ---------------------------------------------------------------------------

def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder RSI aligned to `closes` (same length): None until index
    `period`, then the smoothed RSI value at every subsequent index."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi(avg_gain, avg_loss)
    return out


def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """Latest RSI value. Kept as a thin wrapper so `coverage[symbol]["rsi14"]`
    is computed exactly the same way as before — this is a backward-
    compatibility shim, not a behavior change."""
    series = rsi_series(closes, period)
    return series[-1] if series else None


def rsi_slope(closes: List[float], period: int = 14, lookback: int = 5) -> Optional[float]:
    """Change in RSI over the last `lookback` sessions (RSI now minus RSI
    `lookback` sessions ago), using one consistently-seeded series rather
    than two separately-seeded ones."""
    series = rsi_series(closes, period)
    if len(series) <= lookback or series[-1] is None or series[-1 - lookback] is None:
        return None
    return series[-1] - series[-1 - lookback]  # type: ignore[operator]


# ---------------------------------------------------------------------------
# ATR (Wilder true range)
# ---------------------------------------------------------------------------

def true_range_series(rows: List[Candle]) -> List[Optional[float]]:
    """True range aligned to `rows`: None for index 0 (no previous close),
    then max(high-low, |high-prev_close|, |low-prev_close|)."""
    out: List[Optional[float]] = [None] * len(rows)
    for i in range(1, len(rows)):
        high, low = rows[i]["high"], rows[i]["low"]
        prev_close = rows[i - 1]["close"]
        out[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    return out


def atr_series(rows: List[Candle], period: int = 14) -> List[Optional[float]]:
    """Wilder-smoothed ATR aligned to `rows`."""
    tr = true_range_series(rows)
    out: List[Optional[float]] = [None] * len(rows)
    valid_tr = [t for t in tr if t is not None]
    if len(valid_tr) < period:
        return out
    first_idx = tr.index(next(t for t in tr if t is not None))
    seed_end = first_idx + period
    if seed_end > len(tr):
        return out
    atr = sum(tr[first_idx:seed_end]) / period  # type: ignore[arg-type]
    out[seed_end - 1] = atr
    for i in range(seed_end, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period  # type: ignore[operator]
        out[i] = atr
    return out


def atr_latest(rows: List[Candle], period: int = 14) -> Optional[float]:
    series = atr_series(rows, period)
    return series[-1] if series else None


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

def pct_return(closes: List[float], sessions: int) -> Optional[float]:
    """% change from `sessions` trading days ago to the latest close.
    Unchanged from the original script."""
    if len(closes) <= sessions:
        return None
    return (closes[-1] / closes[-1 - sessions] - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Look-ahead-safe structural levels
# ---------------------------------------------------------------------------

def prior_period_high(highs: List[float], n: int) -> Optional[float]:
    """Highest high over the `n` sessions BEFORE today (today's own high at
    index -1 is excluded). This is the level a breakout must clear —
    comparing `close[-1] > prior_period_high(highs, n)` cannot leak today's
    own bar into the level it is being tested against."""
    if len(highs) < n + 1:
        return None
    return max(highs[-n - 1:-1])


def prior_period_low(lows: List[float], n: int) -> Optional[float]:
    if len(lows) < n + 1:
        return None
    return min(lows[-n - 1:-1])


def recent_high(highs: List[float], n: int) -> Optional[float]:
    """Highest high over the last `n` sessions INCLUDING today. Descriptive
    only (e.g. "swing_high_20d" for context) — never use this as a
    breakout condition, only prior_period_high() is look-ahead-safe for that."""
    if len(highs) < n:
        return None
    return max(highs[-n:])


def recent_low(lows: List[float], n: int) -> Optional[float]:
    if len(lows) < n:
        return None
    return min(lows[-n:])


# ---------------------------------------------------------------------------
# Pivot (unchanged — verified correct and look-ahead-safe in the audit:
# it is derived from the PREVIOUS day's H/L/C, not today's)
# ---------------------------------------------------------------------------

def pivot_r1(prev_high: float, prev_low: float, prev_close: float) -> float:
    p = (prev_high + prev_low + prev_close) / 3.0
    return 2 * p - prev_low


# ---------------------------------------------------------------------------
# Volume / turnover
# ---------------------------------------------------------------------------

def avg_volume_excluding_today(vols: List[float], n: int) -> Optional[float]:
    """Average volume over the `n` sessions before today (today excluded).
    Same slice pattern as the original 10-day volume average."""
    if len(vols) < n + 1:
        return None
    return sum(vols[-n - 1:-1]) / n


def turnover_cr_legacy(last_close: float, vols: List[float], n: int = 20) -> Optional[float]:
    """The ORIGINAL turnover approximation: today's close x n-day average
    volume, in crores. Kept unchanged and under its original name/field so
    existing consumers of `turnover_cr` see no change in value or meaning.
    Known limitation (documented, not silently fixed): this overstates
    turnover after a sharp rally and understates it after a sharp fall,
    because it prices every one of the last `n` days' volume at TODAY's
    close rather than each day's own close. See `avg_daily_turnover_cr`
    for the corrected figure."""
    if len(vols) < n:
        return None
    return (last_close * (sum(vols[-n:]) / n)) / 1e7


def avg_daily_turnover_cr(rows: List[Candle], n: int = 20) -> Optional[float]:
    """Corrected turnover: mean of (close_i * volume_i) over the last `n`
    days, in crores — each day priced at its own close rather than
    today's. This is a NEW field (`avg_daily_turnover_cr`); it does not
    replace `turnover_cr`."""
    if len(rows) < n:
        return None
    window = rows[-n:]
    total = sum(r["close"] * r["volume"] for r in window)
    return (total / n) / 1e7


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def dedupe_by_date(rows: List[Candle]) -> List[Candle]:
    """Collapse duplicate-date rows (keeping the last occurrence for a given
    date) and return chronologically sorted. Guards against a rare but
    possible API glitch that would otherwise silently skew every average
    computed over a fixed window."""
    by_date = {r["date"]: r for r in rows}
    return [by_date[d] for d in sorted(by_date.keys())]


def percentile_rank(value: float, population: List[float]) -> Optional[float]:
    """% of `population` that `value` is greater than or equal to (0-100).
    Used for the cross-sectional relative-strength percentile — requires
    the full universe's values for the same night, computed once after
    every symbol has been fetched (see the two-pass design in
    rotation_sweep_v3_full500.py)."""
    if not population:
        return None
    n = len(population)
    below_or_equal = sum(1 for p in population if p <= value)
    return round(below_or_equal / n * 100.0, 1)
