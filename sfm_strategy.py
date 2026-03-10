"""
sfm_strategy.py — Swing trading signals for SFM meme coin.

Strategy: Option C — trade both directions (buy dips, sell strength).

Signals used:
  1. RSI(14)  — oversold <35 → buy signal; overbought >65 → sell signal
  2. Momentum — price vs EMA(20); acceleration check
  3. Volume   — volume spike (>2x 20-bar avg) confirms signal
  4. ATR      — dynamic stop loss and take profit levels

Entry conditions (BUY):
  - RSI < 35 (oversold)
  - Price < EMA(20) AND last 2 candles trending down (dip detected)
  - Volume > 1.5x avg (interest returning)

Entry conditions (SELL / take profit):
  - RSI > 65 (overbought)
  - Price > EMA(20) by ≥ 2% (extended)
  - OR: open position profit ≥ TAKE_PROFIT_PCT

Exit (stop loss):
  - Position price drops stop_pct below entry (e.g. 8%)
  - Or price drops below trailing stop (entry - 1.5 * ATR)

Returns: "BUY", "SELL", or "HOLD"
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import List, Optional

from sfm_data import Candle

log = logging.getLogger("sfm_strategy")

# ── Signal thresholds ───────────────────────────────────────────────
RSI_OVERSOLD   = 45.0   # loosened: fires on smaller dips
RSI_OVERBOUGHT = 58.0   # loosened: exits on smaller bounces
EMA_PERIOD     = 20
RSI_PERIOD     = 14
ATR_PERIOD     = 14
VOL_AVG_PERIOD = 20
VOL_SPIKE_MULT = 1.0    # no volume requirement — low-volume token


@dataclass
class Signal:
    action: str          # "BUY" | "SELL" | "HOLD"
    rsi: float
    ema: float
    atr: float
    price: float
    volume: float
    vol_avg: float
    reason: str


def _ema(values: List[float], period: int) -> float:
    """Exponential moving average of last `period` values."""
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k = 2 / (period + 1)
    ema = values[-period]
    for v in values[-period + 1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: List[float], period: int = RSI_PERIOD) -> float:
    """RSI from close prices."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [-min(d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(candles: List[Candle], period: int = ATR_PERIOD) -> float:
    """Average True Range."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low  = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else 0.0


def compute_signal(
    candles: List[Candle],
    open_position: bool = False,
    entry_price: float = 0.0,
    stop_loss_pct: float = 8.0,
    take_profit_pct: float = 15.0,
    scaled_out: bool = False,
) -> Signal:
    """
    Compute the current trading signal from recent candles.

    candles: list of Candle (oldest → newest), at least 30 recommended
    open_position: True if we currently hold SFM
    entry_price: price at which we entered (used for exit checks)
    """
    if len(candles) < max(EMA_PERIOD, RSI_PERIOD) + 2:
        return Signal("HOLD", 50.0, 0.0, 0.0, 0.0, 0.0, 0.0, "insufficient_data")

    closes  = [c.close  for c in candles]
    volumes = [c.volume for c in candles]
    price   = closes[-1]
    volume  = volumes[-1]

    ema   = _ema(closes, EMA_PERIOD)
    rsi   = _rsi(closes, RSI_PERIOD)
    atr   = _atr(candles, ATR_PERIOD)
    vol_avg = sum(volumes[-VOL_AVG_PERIOD:]) / min(VOL_AVG_PERIOD, len(volumes))

    # ── Exit checks (position management) ──────────────────────────
    if open_position and entry_price > 0:
        pnl_pct = (price - entry_price) / entry_price * 100

        # Stop loss
        if pnl_pct <= -stop_loss_pct:
            return Signal("SELL", rsi, ema, atr, price, volume, vol_avg,
                          f"stop_loss ({pnl_pct:.1f}%)")

        # Take profit (full exit if already scaled, or RSI overbought)
        if pnl_pct >= take_profit_pct:
            return Signal("SELL", rsi, ema, atr, price, volume, vol_avg,
                          f"take_profit ({pnl_pct:.1f}%)")

        # Scale-out trigger: at 50% of take_profit, sell 50%
        if not scaled_out and pnl_pct >= take_profit_pct * 0.5:
            return Signal("SELL", rsi, ema, atr, price, volume, vol_avg,
                          f"scale_out_50pct ({pnl_pct:.1f}%)")

        # Trailing stop: if price falls back below EMA after being above it
        # (only relevant after a good run)
        if pnl_pct > 5.0 and rsi > RSI_OVERBOUGHT and price < ema:
            return Signal("SELL", rsi, ema, atr, price, volume, vol_avg,
                          f"trail_exit rsi={rsi:.1f}")

    # ── Entry signals ───────────────────────────────────────────────
    if not open_position:
        # ENTRY 1 — Dip buy: RSI oversold + price at or below EMA
        price_below_ema = price <= ema * 1.002
        dip = len(closes) >= 3 and closes[-1] <= closes[-2]

        if rsi < RSI_OVERSOLD and (price_below_ema or dip):
            reason = f"oversold rsi={rsi:.1f} ema_gap={((price-ema)/ema*100):.1f}%"
            return Signal("BUY", rsi, ema, atr, price, volume, vol_avg, reason)

        # ENTRY 2 — Momentum cross: price just crossed above EMA + RSI building
        # Catches early breakouts before RSI reaches overbought territory.
        # Condition: prev close was below EMA, current close is above EMA,
        #            RSI in 42–56 range (not already overbought).
        if len(closes) >= 2 and ema > 0:
            prev_ema = _ema(closes[:-1], EMA_PERIOD)
            ema_cross_up = closes[-2] < prev_ema and price > ema
            momentum_rsi = 42.0 <= rsi <= 56.0
            if ema_cross_up and momentum_rsi:
                reason = f"ema_cross_up rsi={rsi:.1f} gap={((price-ema)/ema*100):.1f}%"
                return Signal("BUY", rsi, ema, atr, price, volume, vol_avg, reason)

        # Overbought avoid: RSI extended + price stretched above EMA
        if rsi > RSI_OVERBOUGHT:
            ema_gap_pct = (price - ema) / ema * 100 if ema > 0 else 0
            if ema_gap_pct >= 2.0:
                return Signal("HOLD", rsi, ema, atr, price, volume, vol_avg,
                              f"overbought_avoid rsi={rsi:.1f} gap={ema_gap_pct:.1f}%")

    return Signal("HOLD", rsi, ema, atr, price, volume, vol_avg, "no_signal")
