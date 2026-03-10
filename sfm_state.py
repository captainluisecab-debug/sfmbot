"""
sfm_state.py — Persistent position and P&L state for SFM bot.

State is saved to sfm_state.json after every change.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger("sfm_state")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfm_state.json")


@dataclass
class Position:
    entry_price: float = 0.0
    sfm_qty: float     = 0.0       # SFM tokens held
    cost_usd: float    = 0.0       # USD spent on entry
    entry_ts: int      = 0         # unix timestamp
    scaled_out: bool   = False     # True after 50% scale-out


@dataclass
class SFMState:
    # Wallet balances (USD equivalent, tracked in paper mode)
    usdc_balance: float  = 1_000.0  # starting paper USDC

    # Open position (None if flat)
    position: Optional[Position] = None

    # P&L tracking
    realized_pnl_usd: float = 0.0
    total_trades: int        = 0
    winning_trades: int      = 0
    losing_trades: int       = 0

    # Cycle counter
    cycle: int = 0

    # Cooldown tracking: candle index of last buy (-1 = none)
    last_buy_candle_idx: int = -1


def load_state() -> SFMState:
    if not os.path.exists(STATE_FILE):
        log.info("No state file found — starting fresh")
        return SFMState()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        st = SFMState(
            usdc_balance=raw.get("usdc_balance", 1_000.0),
            realized_pnl_usd=raw.get("realized_pnl_usd", 0.0),
            total_trades=raw.get("total_trades", 0),
            winning_trades=raw.get("winning_trades", 0),
            losing_trades=raw.get("losing_trades", 0),
            cycle=raw.get("cycle", 0),
            last_buy_candle_idx=raw.get("last_buy_candle_idx", -1),
        )
        pos_raw = raw.get("position")
        if pos_raw:
            st.position = Position(**pos_raw)
        return st
    except Exception as exc:
        log.error("Failed to load state: %s — starting fresh", exc)
        return SFMState()


def save_state(st: SFMState) -> None:
    raw = {
        "usdc_balance": st.usdc_balance,
        "realized_pnl_usd": st.realized_pnl_usd,
        "total_trades": st.total_trades,
        "winning_trades": st.winning_trades,
        "losing_trades": st.losing_trades,
        "cycle": st.cycle,
        "last_buy_candle_idx": st.last_buy_candle_idx,
        "position": asdict(st.position) if st.position else None,
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
    except Exception as exc:
        log.error("Failed to save state: %s", exc)


def open_position(st: SFMState, entry_price: float, usd_spent: float) -> None:
    sfm_qty = usd_spent / entry_price
    st.position = Position(
        entry_price=entry_price,
        sfm_qty=sfm_qty,
        cost_usd=usd_spent,
        entry_ts=int(time.time()),
    )
    st.usdc_balance -= usd_spent
    save_state(st)
    log.info("Opened position: %.0f SFM @ $%.8f (cost=$%.2f)", sfm_qty, entry_price, usd_spent)


def close_position(st: SFMState, exit_price: float, sfm_qty: float, reason: str = "") -> float:
    """
    Close (or partially close) a position.
    Returns realized PnL in USD for this exit.
    """
    if st.position is None:
        return 0.0

    proceeds_usd = sfm_qty * exit_price
    cost_basis_per_token = st.position.cost_usd / max(st.position.sfm_qty, 1e-12)
    cost_basis = sfm_qty * cost_basis_per_token
    pnl = proceeds_usd - cost_basis

    st.usdc_balance    += proceeds_usd
    st.realized_pnl_usd += pnl
    st.total_trades    += 1
    if pnl >= 0:
        st.winning_trades += 1
    else:
        st.losing_trades += 1

    remaining_qty = st.position.sfm_qty - sfm_qty
    if remaining_qty <= 1.0:  # fully closed (allow dust)
        log.info(
            "Closed position: sold %.0f SFM @ $%.8f | pnl=$%.2f | reason=%s",
            sfm_qty, exit_price, pnl, reason,
        )
        st.position = None
    else:
        # Partial close — update qty and cost
        st.position.sfm_qty  = remaining_qty
        st.position.cost_usd = remaining_qty * cost_basis_per_token
        st.position.scaled_out = True
        log.info(
            "Partial close: sold %.0f SFM @ $%.8f | pnl=$%.2f | remaining=%.0f SFM | reason=%s",
            sfm_qty, exit_price, pnl, remaining_qty, reason,
        )

    save_state(st)
    return pnl


def portfolio_value(st: SFMState, current_price: float) -> float:
    """Total portfolio value in USD (USDC balance + open position mark-to-market)."""
    pos_value = st.position.sfm_qty * current_price if st.position else 0.0
    return st.usdc_balance + pos_value
