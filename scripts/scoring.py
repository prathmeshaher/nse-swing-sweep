#!/usr/bin/env python3
"""
Classification and scoring logic for the NSE swing-trading scanner.

Every threshold in this module is a named constant, not a magic number,
specifically so it can be revisited once the daily history log (written
by rotation_sweep_v3_full500.py) has enough sessions in it to backtest
against. Nothing here has been tuned against today's watchlist — these
are documented, provisional starting points, per the design review's
overfitting-avoidance rule ("do not optimize weights blindly").

All functions are pure (no I/O) so they can be unit-tested directly
against synthetic inputs (see tests/test_scoring.py).
"""
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict

# ---------------------------------------------------------------------------
# Configurable thresholds (documented, provisional — see module docstring)
# ---------------------------------------------------------------------------

BREAKOUT_LOOKBACKS: List[int] = [20, 50, 100, 252]
BREAKOUT_APPROACH_PCT = 3.0          # "approaching" if within 3% below a level
BREAKOUT_VOLUME_CONFIRM_RATIO = 1.5  # need >=1.5x 20d avg volume to call it "confirmed"

CONSOLIDATION_RECENT_SESSIONS = 15
CONSOLIDATION_BASELINE_SESSIONS = 45
CONSOLIDATION_ATR_COMPRESSION_MAX_PCT = 75.0   # recent ATR% <= 75% of baseline ATR% -> compressed
CONSOLIDATION_VOLUME_CONTRACTION_MAX_PCT = 90.0  # recent avg vol <= 90% of baseline avg vol

PULLBACK_LOOKBACK_SESSIONS = 10
PULLBACK_MAX_DISTANCE_BELOW_EMA20_PCT = 8.0   # don't call it a "controlled" pullback beyond this
PULLBACK_RSI_RESET_MAX = 48.0                 # RSI must have dipped to/below this during the pullback

RSI_OVERBOUGHT = 72.0
RSI_OVERSOLD = 32.0
RSI_SLOPE_LOOKBACK = 5

EXTENSION_ATR_EARLY_MAX = 1.0
EXTENSION_ATR_HEALTHY_MAX = 2.5
EXTENSION_ATR_EXTENDED_MAX = 4.0
# beyond EXTENSION_ATR_EXTENDED_MAX -> "severely_extended"

VOLATILITY_ATR_PCT_LOW_MAX = 2.0
VOLATILITY_ATR_PCT_MODERATE_MAX = 4.0
VOLATILITY_ATR_PCT_HIGH_MAX = 7.0
# beyond that -> "extreme"

ATR_STOP_MULT = 1.5          # fallback stop distance in ATR when structure alone is unusable
ATR_TARGET_MULT_1 = 2.0      # fallback target-1 distance in ATR
ATR_TARGET_MULT_2 = 4.0      # fallback target-2 distance in ATR
MAX_STOP_DISTANCE_ATR = 3.0  # if the structural swing low is farther than this, fall back to ATR stop

SCORE_WEIGHTS: Dict[str, int] = {
    "market": 15,
    "sector": 15,
    "relative_strength": 15,
    "trend": 15,
    "setup": 15,
    "volume": 10,
    "momentum": 5,
    "risk_reward": 10,
}
assert sum(SCORE_WEIGHTS.values()) == 100

EXTENSION_SCORE_ADJUSTMENT = {
    "early": 1.0,
    "healthy": 1.0,
    "extended": 0.95,
    "severely_extended": 0.85,
}


class ScoreBreakdown(TypedDict):
    market: float
    sector: float
    relative_strength: float
    trend: float
    setup: float
    volume: float
    momentum: float
    risk_reward: float


# ---------------------------------------------------------------------------
# Ordinal classifications — each built the same way (count how many of a
# small set of documented conditions hold, map the count to a label) so the
# logic is easy to read, easy to test, and easy to re-derive by hand from
# raw numbers when auditing a specific call.
# ---------------------------------------------------------------------------

def classify_market_regime(
    close: float, ema20: Optional[float], ema50: Optional[float],
    sma200: Optional[float], rsi14: Optional[float], return_20d: Optional[float],
) -> str:
    """5-way market regime from 6 boolean conditions on the benchmark index.
    bullish >=5, cautiously_bullish 4, neutral 3, cautiously_bearish 2,
    bearish <=1. Returns "unknown" if there isn't enough index data yet —
    the scanner must degrade gracefully here rather than guess."""
    if None in (ema20, ema50, sma200, rsi14, return_20d):
        return "unknown"
    score = sum([
        close > ema20,
        ema20 > ema50,           # type: ignore[operator]
        ema50 > sma200,          # type: ignore[operator]
        close > sma200,
        rsi14 >= 50.0,
        return_20d > 0.0,
    ])
    if score >= 5:
        return "bullish"
    if score == 4:
        return "cautiously_bullish"
    if score == 3:
        return "neutral"
    if score == 2:
        return "cautiously_bearish"
    return "bearish"


def classify_trend_status(
    close: float, ema20: Optional[float], ema50: Optional[float], ema200: Optional[float],
) -> str:
    """5-way per-stock trend classification from 5 boolean EMA-stack
    conditions. strong_uptrend 5, uptrend 4, weak_uptrend 3, sideways 2,
    downtrend <=1. Returns "unknown" if EMA200 isn't available yet
    (recently-listed stock) rather than guessing a trend."""
    if None in (ema20, ema50, ema200):
        return "unknown"
    score = sum([
        close > ema20,
        ema20 > ema50,     # type: ignore[operator]
        ema50 > ema200,    # type: ignore[operator]
        close > ema50,
        close > ema200,
    ])
    if score == 5:
        return "strong_uptrend"
    if score == 4:
        return "uptrend"
    if score == 3:
        return "weak_uptrend"
    if score == 2:
        return "sideways"
    return "downtrend"


def classify_volatility(atr_pct: Optional[float]) -> str:
    if atr_pct is None:
        return "unknown"
    if atr_pct <= VOLATILITY_ATR_PCT_LOW_MAX:
        return "low"
    if atr_pct <= VOLATILITY_ATR_PCT_MODERATE_MAX:
        return "moderate"
    if atr_pct <= VOLATILITY_ATR_PCT_HIGH_MAX:
        return "high"
    return "extreme"


def classify_rsi_regime(rsi14: Optional[float], slope: Optional[float]) -> str:
    if rsi14 is None:
        return "unknown"
    if rsi14 >= RSI_OVERBOUGHT:
        return "overbought"
    if rsi14 <= RSI_OVERSOLD:
        return "oversold"
    if slope is not None and slope > 0:
        return "rising_momentum" if rsi14 >= 50 else "recovering"
    if slope is not None and slope < 0:
        return "weakening"
    return "neutral"


def detect_oversold_recovery(rsi_history: List[Optional[float]], lookback: int = 10) -> bool:
    """True if RSI dipped to/below RSI_OVERSOLD within the last `lookback`
    sessions and has since recovered above it. Reads off the already-
    computed rsi_series so it's consistent with rsi14/rsi_slope rather than
    a separately re-seeded calculation."""
    recent = [r for r in rsi_history[-lookback:] if r is not None]
    if not recent or recent[-1] is None:
        return False
    return min(recent) <= RSI_OVERSOLD and recent[-1] > RSI_OVERSOLD


def detect_overextension(rsi14: Optional[float]) -> bool:
    return rsi14 is not None and rsi14 >= RSI_OVERBOUGHT


def classify_breakout(
    close: float, highs: List[float], vol_ratio_20d: Optional[float],
    prior_high_fn,
) -> Dict[str, object]:
    """Look-ahead-safe breakout detection across BREAKOUT_LOOKBACKS.

    `prior_high_fn` is injected (rather than imported directly) purely so
    tests can pass a stub; in production it is
    indicators.prior_period_high.

    Returns level/horizon/strength/distance — never uses today's own high
    as part of the level it's compared against.
    """
    levels = {n: prior_high_fn(highs, n) for n in BREAKOUT_LOOKBACKS}
    broken = {n: (lvl is not None and close > lvl) for n, lvl in levels.items()}

    confirmed_horizons = [n for n, is_broken in broken.items() if is_broken]
    if confirmed_horizons:
        horizon = max(confirmed_horizons)
        level = levels[horizon]
        distance_pct = round((close / level - 1.0) * 100.0, 2)  # type: ignore[operator]
        strong_volume = vol_ratio_20d is not None and vol_ratio_20d >= BREAKOUT_VOLUME_CONFIRM_RATIO
        strength = "confirmed" if (len(confirmed_horizons) >= 2 or strong_volume) else "moderate"
        return {
            "breakout_level": round(level, 2),  # type: ignore[arg-type]
            "breakout_horizon_days": horizon,
            "distance_from_breakout_pct": distance_pct,
            "breakout_strength": strength,
        }

    # Nothing broken yet — report the nearest (shortest) unbroken level so
    # "approaching" can be evaluated.
    available = {n: lvl for n, lvl in levels.items() if lvl is not None}
    if not available:
        return {
            "breakout_level": None, "breakout_horizon_days": None,
            "distance_from_breakout_pct": None, "breakout_strength": "unknown",
        }
    nearest_horizon = min(available, key=lambda n: available[n])
    nearest_level = available[nearest_horizon]
    distance_pct = round((close / nearest_level - 1.0) * 100.0, 2)
    strength = "approaching" if distance_pct >= -BREAKOUT_APPROACH_PCT else "none"
    return {
        "breakout_level": round(nearest_level, 2),
        "breakout_horizon_days": nearest_horizon,
        "distance_from_breakout_pct": distance_pct,
        "breakout_strength": strength,
    }


def detect_consolidation(
    atr_pct_series: List[Optional[float]], vols: List[float],
) -> Dict[str, object]:
    """Base/consolidation detector: compares a recent window's ATR% and
    average volume against the window immediately before it. Both must
    contract for `in_base` to be True. Thresholds are named constants at
    the top of this module, not inline magic numbers, so they can be
    swept in a future backtest."""
    recent_n, baseline_n = CONSOLIDATION_RECENT_SESSIONS, CONSOLIDATION_BASELINE_SESSIONS
    total_needed = recent_n + baseline_n
    valid_atr = [a for a in atr_pct_series if a is not None]
    if len(valid_atr) < total_needed or len(vols) < total_needed:
        return {"in_base": False, "base_sessions": 0, "atr_compression_pct": None}

    recent_atr = valid_atr[-recent_n:]
    baseline_atr = valid_atr[-(recent_n + baseline_n):-recent_n]
    recent_vol = vols[-recent_n:]
    baseline_vol = vols[-(recent_n + baseline_n):-recent_n]

    baseline_atr_avg = sum(baseline_atr) / len(baseline_atr)
    recent_atr_avg = sum(recent_atr) / len(recent_atr)
    baseline_vol_avg = sum(baseline_vol) / len(baseline_vol)
    recent_vol_avg = sum(recent_vol) / len(recent_vol)

    if baseline_atr_avg <= 0:
        return {"in_base": False, "base_sessions": 0, "atr_compression_pct": None}

    atr_compression_pct = round(recent_atr_avg / baseline_atr_avg * 100.0, 1)
    vol_compression_pct = (recent_vol_avg / baseline_vol_avg * 100.0) if baseline_vol_avg else 100.0

    in_base = (
        atr_compression_pct <= CONSOLIDATION_ATR_COMPRESSION_MAX_PCT
        and vol_compression_pct <= CONSOLIDATION_VOLUME_CONTRACTION_MAX_PCT
    )
    return {
        "in_base": in_base,
        "base_sessions": recent_n if in_base else 0,
        "atr_compression_pct": atr_compression_pct,
    }


def detect_pullback(
    trend_status: str, close: float, ema20: Optional[float], ema50: Optional[float],
    rsi_history: List[Optional[float]],
) -> bool:
    """Pullback setup: requires an established uptrend, price sitting a
    controlled distance below EMA20 (between EMA20 and EMA50, or slightly
    under EMA20 — not a breakdown), and RSI having reset down toward
    PULLBACK_RSI_RESET_MAX within the recent window before recovering.
    Volume-contraction-during-the-decline is intentionally left as a
    documented future refinement (P1 follow-up) rather than added here
    with an untested threshold — see design review §4."""
    if trend_status not in ("strong_uptrend", "uptrend"):
        return False
    if ema20 is None or ema50 is None:
        return False
    distance_from_ema20_pct = (close - ema20) / ema20 * 100.0
    controlled_pullback = -PULLBACK_MAX_DISTANCE_BELOW_EMA20_PCT <= distance_from_ema20_pct <= 0.5
    if not controlled_pullback:
        return False
    if close < ema50 * 0.97:  # broke meaningfully below EMA50 -> not a "controlled" pullback anymore
        return False
    recent_rsi = [r for r in rsi_history[-PULLBACK_LOOKBACK_SESSIONS:] if r is not None]
    if not recent_rsi:
        return False
    return min(recent_rsi) <= PULLBACK_RSI_RESET_MAX


def classify_setup_type(
    breakout_strength: str, in_base: bool, is_pullback: bool, trend_status: str,
) -> str:
    """Single categorical setup label — deliberately rule-based (not a
    fitted model) so it stays auditable and backtestable, per the design
    review. Priority order documents the intent: a confirmed breakout out
    of a real base is the highest-conviction setup; an unconfirmed
    breakout out of a base is the "emerging" version; anything else falls
    through to pullback, then plain momentum continuation, then nothing."""
    if breakout_strength == "confirmed" and in_base:
        return "breakout"
    if breakout_strength in ("confirmed", "moderate") and trend_status in (
        "strong_uptrend", "uptrend", "weak_uptrend",
    ):
        return "momentum_continuation"
    if breakout_strength == "approaching" and in_base:
        return "emerging_breakout"
    if is_pullback:
        return "pullback"
    return "no_setup"


def classify_extension(
    close: float, ema20: Optional[float], breakout_level: Optional[float], atr14: Optional[float],
) -> str:
    """How far price has run from its nearest anchor (EMA20, or the
    breakout level if one exists and is closer), measured in ATR
    multiples. This exists specifically so a technically strong but
    already-run-up name doesn't automatically outrank an earlier-stage
    setup — see design review §16."""
    if atr14 is None or atr14 <= 0 or ema20 is None:
        return "unknown"
    anchors = [ema20]
    if breakout_level is not None:
        anchors.append(breakout_level)
    nearest_anchor = min(anchors, key=lambda a: abs(close - a))
    atr_multiple = abs(close - nearest_anchor) / atr14
    if atr_multiple <= EXTENSION_ATR_EARLY_MAX:
        return "early"
    if atr_multiple <= EXTENSION_ATR_HEALTHY_MAX:
        return "healthy"
    if atr_multiple <= EXTENSION_ATR_EXTENDED_MAX:
        return "extended"
    return "severely_extended"


def classify_volume(
    close: float, prev_close: Optional[float], vol_ratio_20d: Optional[float],
) -> str:
    """Volume trend, deliberately combined with price direction rather
    than treating high volume as inherently bullish (per design review
    §12): high volume on a down day is flagged separately, not scored the
    same as high volume on an up day."""
    if vol_ratio_20d is None:
        return "unknown"
    price_up = prev_close is not None and close > prev_close
    if vol_ratio_20d >= 1.3:
        return "expanding_on_strength" if price_up else "expanding_on_weakness"
    if vol_ratio_20d <= 0.7:
        return "contracting"
    return "average"


# ---------------------------------------------------------------------------
# Scoring engine — sums to 100 across the 8 documented components. Weights
# are the design review's suggested provisional defaults, unchanged; they
# are explicitly NOT tuned against any specific watchlist.
# ---------------------------------------------------------------------------

def score_market(regime: str) -> float:
    table = {
        "bullish": 15.0, "cautiously_bullish": 11.0, "neutral": 8.0,
        "cautiously_bearish": 4.0, "bearish": 0.0, "unknown": 7.5,
    }
    return table.get(regime, 7.5)


def score_sector(sector_percentile: Optional[float]) -> float:
    """sector_percentile: 0-100, where 100 = strongest sector that night.
    None (no sector mapping available for this stock) scores at the
    midpoint rather than zero or full marks — absence of data should be
    neutral, not penalized or rewarded."""
    if sector_percentile is None:
        return SCORE_WEIGHTS["sector"] / 2.0
    return round(sector_percentile / 100.0 * SCORE_WEIGHTS["sector"], 2)


def score_relative_strength(rs_percentile: Optional[float]) -> float:
    if rs_percentile is None:
        return 0.0
    return round(rs_percentile / 100.0 * SCORE_WEIGHTS["relative_strength"], 2)


def score_trend(trend_status: str) -> float:
    table = {
        "strong_uptrend": 15.0, "uptrend": 12.0, "weak_uptrend": 7.0,
        "sideways": 3.0, "downtrend": 0.0, "unknown": 0.0,
    }
    return table.get(trend_status, 0.0)


def score_setup(setup_type: str) -> float:
    table = {
        "breakout": 15.0, "pullback": 12.0, "emerging_breakout": 10.0,
        "momentum_continuation": 8.0, "no_setup": 0.0,
    }
    return table.get(setup_type, 0.0)


def score_volume(volume_status: str) -> float:
    table = {
        "expanding_on_strength": 10.0, "average": 5.0,
        "contracting": 4.0, "expanding_on_weakness": 1.0, "unknown": 3.0,
    }
    return table.get(volume_status, 3.0)


def score_momentum(rsi_regime: str) -> float:
    table = {
        "rising_momentum": 5.0, "recovering": 4.0, "neutral": 2.5,
        "weakening": 1.0, "overbought": 2.0, "oversold": 1.0, "unknown": 2.0,
    }
    return table.get(rsi_regime, 2.0)


def score_risk_reward(risk_reward_ratio: Optional[float]) -> float:
    if risk_reward_ratio is None:
        return 0.0
    if risk_reward_ratio >= 3.0:
        return 10.0
    if risk_reward_ratio >= 2.0:
        return 8.0
    if risk_reward_ratio >= 1.5:
        return 6.0
    if risk_reward_ratio >= 1.0:
        return 3.0
    return 1.0


def compute_score(
    *, regime: str, sector_percentile: Optional[float], rs_percentile: Optional[float],
    trend_status: str, setup_type: str, volume_status: str, rsi_regime: str,
    risk_reward_ratio: Optional[float], extension_status: str,
) -> Dict[str, object]:
    """Combine every component into the final 0-100 score + breakdown.
    Returns both so the LLM consumer can see WHY a stock scored the way
    it did, per design review §14."""
    breakdown = {
        "market": score_market(regime),
        "sector": score_sector(sector_percentile),
        "relative_strength": score_relative_strength(rs_percentile),
        "trend": score_trend(trend_status),
        "setup": score_setup(setup_type),
        "volume": score_volume(volume_status),
        "momentum": score_momentum(rsi_regime),
        "risk_reward": score_risk_reward(risk_reward_ratio),
    }
    raw_total = sum(breakdown.values())
    adjustment = EXTENSION_SCORE_ADJUSTMENT.get(extension_status, 1.0)
    final_score = round(raw_total * adjustment)
    return {
        "score": max(0, min(100, final_score)),
        "score_breakdown": {k: round(v, 1) for k, v in breakdown.items()},
        "extension_adjustment_applied": adjustment != 1.0,
    }
