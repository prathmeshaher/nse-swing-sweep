#!/usr/bin/env python3
"""
Structure-aware entry/stop/target/risk-reward calculation.

Per the design review: ATR informs risk sizing but must not be the sole
or automatic source of the stop, and targets are never manufactured —
every target carries a `target_method` tag so a downstream consumer can
tell a measured-move target from an ATR-projection fallback, and `None`
is returned (with a flag) when no reliable target can be derived at all.

Pure functions, no I/O — see tests/test_risk_management.py.
"""
from __future__ import annotations

from typing import Dict, Optional

from scoring import ATR_STOP_MULT, ATR_TARGET_MULT_1, ATR_TARGET_MULT_2, MAX_STOP_DISTANCE_ATR


def compute_entry_zone(
    setup_type: str, close: float, breakout_level: Optional[float],
    ema20: Optional[float],
) -> Optional[list]:
    """Entry zone as [low, high]. Breakout/emerging setups anchor to the
    breakout level; pullback setups anchor to EMA20-to-current-price
    (buying the pullback, not chasing); momentum_continuation and
    no_setup get no defined zone (None) rather than an invented one."""
    if setup_type in ("breakout", "emerging_breakout") and breakout_level:
        lo, hi = sorted([breakout_level, close])
        return [round(lo, 2), round(max(hi, breakout_level * 1.0), 2)]
    if setup_type == "pullback" and ema20:
        lo, hi = sorted([ema20, close])
        return [round(lo, 2), round(hi, 2)]
    return None


def compute_stop_loss(
    setup_type: str, close: float, swing_low: Optional[float],
    ema50: Optional[float], atr14: Optional[float],
) -> Optional[float]:
    """Structure-first stop: prefer the relevant swing low / EMA as the
    invalidation point. Only fall back to a pure ATR-multiple stop when no
    structural level is available, or when the structural level is
    farther away than MAX_STOP_DISTANCE_ATR (at which point it's no
    longer a useful risk-defining level for this trade)."""
    if atr14 is None or atr14 <= 0:
        return None

    structural_candidate = None
    if setup_type in ("breakout", "emerging_breakout", "momentum_continuation") and swing_low is not None:
        structural_candidate = swing_low
    elif setup_type == "pullback" and ema50 is not None:
        structural_candidate = ema50 * 0.99  # small buffer below EMA50

    if structural_candidate is not None:
        distance_atr = abs(close - structural_candidate) / atr14
        if distance_atr <= MAX_STOP_DISTANCE_ATR:
            return round(structural_candidate, 2)

    return round(close - ATR_STOP_MULT * atr14, 2)


def compute_targets(
    close: float, entry: float, breakout_level: Optional[float],
    base_low: Optional[float], resistance_above: Optional[float], atr14: Optional[float],
) -> Dict[str, object]:
    """Target priority, per the design review: measured move (base height
    projected above the breakout) > next known resistance above entry >
    ATR projection (explicitly whitelisted by the brief as a valid
    method, not a fabrication) > unavailable."""
    if breakout_level is not None and base_low is not None and base_low < breakout_level:
        base_height = breakout_level - base_low
        target_1 = breakout_level + base_height * 0.5
        target_2 = breakout_level + base_height
        if target_1 > entry:
            return {
                "target_1": round(target_1, 2), "target_2": round(target_2, 2),
                "target_method": "measured_move",
            }

    if resistance_above is not None and resistance_above > entry:
        target_1 = resistance_above
        target_2 = resistance_above + (resistance_above - entry)
        return {
            "target_1": round(target_1, 2), "target_2": round(target_2, 2),
            "target_method": "swing_high",
        }

    if atr14 is not None and atr14 > 0:
        return {
            "target_1": round(entry + ATR_TARGET_MULT_1 * atr14, 2),
            "target_2": round(entry + ATR_TARGET_MULT_2 * atr14, 2),
            "target_method": "atr_projection",
        }

    return {"target_1": None, "target_2": None, "target_method": "unavailable"}


def compute_risk_reward(
    entry: Optional[float], stop_loss: Optional[float], target_1: Optional[float],
) -> Dict[str, Optional[float]]:
    if entry is None or stop_loss is None or target_1 is None:
        return {"risk_per_share": None, "reward_per_share": None, "risk_reward_ratio": None}
    risk = entry - stop_loss
    reward = target_1 - entry
    if risk <= 0:
        return {"risk_per_share": round(risk, 2), "reward_per_share": round(reward, 2), "risk_reward_ratio": None}
    return {
        "risk_per_share": round(risk, 2),
        "reward_per_share": round(reward, 2),
        "risk_reward_ratio": round(reward / risk, 2),
    }


def compute_position_sizing(
    entry: Optional[float], risk_per_share: Optional[float],
    account_size: Optional[float], risk_per_trade_pct: Optional[float],
) -> Dict[str, Optional[float]]:
    """Never hard-codes an account size. `suggested_position_risk_pct`
    (and a suggested share quantity) are only populated when the caller
    has explicitly configured ACCOUNT_SIZE and RISK_PER_TRADE_PCT — both
    None/disabled by default, per the design review."""
    result: Dict[str, Optional[float]] = {
        "risk_pct_of_entry": None,
        "suggested_position_risk_pct": None,
        "suggested_qty": None,
    }
    if entry and risk_per_share and entry > 0:
        result["risk_pct_of_entry"] = round(risk_per_share / entry * 100.0, 2)
    if account_size and risk_per_trade_pct and risk_per_share and risk_per_share > 0:
        risk_budget = account_size * (risk_per_trade_pct / 100.0)
        result["suggested_position_risk_pct"] = risk_per_trade_pct
        result["suggested_qty"] = int(risk_budget // risk_per_share)
    return result
