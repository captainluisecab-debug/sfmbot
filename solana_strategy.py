"""
solana_strategy.py — Multi-pair signal computation for the Solana engine.

Supports two strategy types:
  - "swing": RSI dip-buy + trend-ride + EMA crossover (for SOL/USDC, tactical tokens)
  - "yield": premium/discount capture (for JitoSOL/SOL, future Phase 2)

Each pair config provides its own RSI/EMA/stop/TP thresholds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("solana_strategy")


@dataclass
class Signal:
    action: str       # "BUY" | "SELL" | "HOLD"
    reason: str
    price: float
    rsi: float
    ema: float
    atr: float
    score: float = 0.0


def compute_indicators(closes: List[float], highs: List[float] = None,
                       lows: List[float] = None,
                       rsi_period: int = 14, ema_period: int = 20,
                       atr_period: int = 14) -> dict:
    if len(closes) < max(rsi_period, ema_period, atr_period) + 2:
        return {}

    # EMA
    ema_vals = [closes[0]]
    mult = 2.0 / (ema_period + 1)
    for c in closes[1:]:
        ema_vals.append(c * mult + ema_vals[-1] * (1 - mult))
    ema = ema_vals[-1]

    # RSI
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    if len(gains) < rsi_period:
        return {}
    avg_gain = sum(gains[-rsi_period:]) / rsi_period
    avg_loss = sum(losses[-rsi_period:]) / rsi_period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # ATR
    atr = 0.0
    if highs and lows and len(highs) >= atr_period + 1:
        trs = []
        for i in range(1, len(highs)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        if len(trs) >= atr_period:
            atr = sum(trs[-atr_period:]) / atr_period

    # Volume average (if provided via closes proxy — actual vol passed separately)
    return {"ema": ema, "rsi": rsi, "atr": atr}


def compute_swing_signal(
    price: float,
    closes: List[float],
    highs: List[float],
    lows: List[float],
    rsi_oversold: float,
    rsi_overbought: float,
    ema_period: int,
    stop_loss_pct: float,
    take_profit_pct: float,
    min_score: float,
    open_position: bool = False,
    entry_price: float = 0.0,
    breakeven_armed: bool = False,
    peak_pnl_pct: float = 0.0,
) -> Signal:
    ind = compute_indicators(closes, highs, lows, ema_period=ema_period)
    if not ind:
        return Signal("HOLD", "insufficient_data", price, 0, 0, 0)

    rsi = ind["rsi"]
    ema = ind["ema"]
    atr = ind["atr"]

    if not open_position:
        # ── ENTRY SIGNALS ──────────────────────────────────────
        gap_pct = (price - ema) / ema * 100 if ema > 0 else 0

        # Score: 0-100 composite
        score = 0.0
        # Trend component (40 points)
        if price > ema:
            score += 25 + min(15, gap_pct * 5)
        # Momentum component (30 points)
        if rsi < rsi_oversold:
            score += 30
        elif rsi < 45:
            score += 20
        elif rsi < 55:
            score += 10
        # Volatility component (20 points)
        if atr > 0 and price > 0:
            vol_pct = atr / price * 100
            if vol_pct > 1.0:
                score += 20
            elif vol_pct > 0.5:
                score += 10
        # Green candle momentum (10 points)
        if len(closes) >= 3 and closes[-1] > closes[-2] > closes[-3]:
            score += 10

        # Signal 1: Dip buy (oversold + not in freefall)
        if rsi < rsi_oversold and gap_pct > -3.0 and score >= min_score:
            return Signal("BUY", f"oversold rsi={rsi:.1f} gap={gap_pct:.1f}%",
                         price, rsi, ema, atr, score)

        # Signal 2: Trend ride (2 green candles + above EMA + controlled RSI)
        if (len(closes) >= 3 and closes[-1] > closes[-2] > closes[-3]
                and price > ema and rsi_oversold < rsi < 65
                and gap_pct < 3.0 and score >= min_score):
            return Signal("BUY", f"trend_ride rsi={rsi:.1f} gap={gap_pct:.1f}%",
                         price, rsi, ema, atr, score)

        return Signal("HOLD", "no_signal", price, rsi, ema, atr, score)

    else:
        # ── EXIT SIGNALS ───────────────────────────────────────
        if entry_price <= 0:
            return Signal("HOLD", "no_entry_price", price, rsi, ema, atr)

        pnl_pct = (price - entry_price) / entry_price * 100

        # Breakeven arm: once +1.5%, protect at breakeven
        if pnl_pct >= 1.5 and not breakeven_armed:
            return Signal("HOLD", f"breakeven_arm pnl={pnl_pct:.1f}%",
                         price, rsi, ema, atr)

        # Breakeven stop: armed and price dropped to entry
        if breakeven_armed and pnl_pct <= 0.1:
            return Signal("SELL", f"breakeven_stop pnl={pnl_pct:.1f}%",
                         price, rsi, ema, atr)

        # Stop loss
        if pnl_pct <= -stop_loss_pct:
            return Signal("SELL", f"stop_loss pnl={pnl_pct:.1f}%",
                         price, rsi, ema, atr)

        # Take profit
        if pnl_pct >= take_profit_pct:
            return Signal("SELL", f"take_profit pnl={pnl_pct:.1f}%",
                         price, rsi, ema, atr)

        # Trail exit: overbought + below EMA
        if rsi > rsi_overbought and price < ema and pnl_pct > 0:
            return Signal("SELL", f"trail_exit rsi={rsi:.1f} below_ema",
                         price, rsi, ema, atr)

        # Score-based exit: if score drops below exit floor
        score = 0.0
        if price > ema:
            score += 25
        if rsi > 40:
            score += 15
        if score < 15 and pnl_pct < 0:
            return Signal("SELL", f"weak_score score={score:.0f} pnl={pnl_pct:.1f}%",
                         price, rsi, ema, atr)

        return Signal("HOLD", f"holding pnl={pnl_pct:.1f}%", price, rsi, ema, atr)
