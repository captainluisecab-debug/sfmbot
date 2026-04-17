"""
solana_state.py — Multi-pair state management for the Solana engine.

Tracks per-pair positions, realized PnL, trade counts, peak equity.
Atomic save via tmp+replace.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

log = logging.getLogger("solana_state")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solana_state.json")


@dataclass
class Position:
    pair: str
    base_qty: float
    entry_price: float
    cost_usd: float
    entry_ts: int
    entry_signal: str = ""
    scaled_out: bool = False
    stop_warned: bool = False
    breakeven_armed: bool = False
    peak_pnl_pct: float = 0.0


@dataclass
class SolanaState:
    usdc_balance: float = 0.0
    sol_balance: float = 0.0
    sol_usd: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_usd: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    cycle: int = 0
    peak_equity: float = 0.0
    pair_stats: Dict[str, dict] = field(default_factory=dict)


def load_state() -> SolanaState:
    if not os.path.exists(STATE_FILE):
        return SolanaState()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        st = SolanaState(
            usdc_balance=raw.get("usdc_balance", 0.0),
            realized_pnl_usd=raw.get("realized_pnl_usd", 0.0),
            total_trades=raw.get("total_trades", 0),
            winning_trades=raw.get("winning_trades", 0),
            losing_trades=raw.get("losing_trades", 0),
            cycle=raw.get("cycle", 0),
            peak_equity=raw.get("peak_equity", 0.0),
            pair_stats=raw.get("pair_stats", {}),
        )
        for pair, p in (raw.get("positions") or {}).items():
            st.positions[pair] = Position(**p)
        return st
    except Exception as exc:
        log.error("Failed to load solana_state: %s", exc)
        return SolanaState()


def save_state(st: SolanaState) -> None:
    raw = {
        "usdc_balance": st.usdc_balance,
        "sol_balance": getattr(st, "sol_balance", 0.0),
        "sol_usd": getattr(st, "sol_usd", 0.0),
        "realized_pnl_usd": st.realized_pnl_usd,
        "total_trades": st.total_trades,
        "winning_trades": st.winning_trades,
        "losing_trades": st.losing_trades,
        "cycle": st.cycle,
        "peak_equity": st.peak_equity,
        "positions": {pair: asdict(p) for pair, p in st.positions.items()},
        "pair_stats": st.pair_stats,
    }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log.error("Failed to save solana_state: %s", exc)


def record_buy(st: SolanaState, pair: str, entry_price: float,
               base_qty: float, cost_usd: float, signal: str = "") -> None:
    st.positions[pair] = Position(
        pair=pair, base_qty=base_qty, entry_price=entry_price,
        cost_usd=cost_usd, entry_ts=int(time.time()), entry_signal=signal,
    )
    st.usdc_balance -= cost_usd
    save_state(st)


def record_sell(st: SolanaState, pair: str, exit_price: float,
                reason: str = "") -> float:
    pos = st.positions.pop(pair, None)
    if pos is None:
        return 0.0
    proceeds = pos.base_qty * exit_price
    pnl = proceeds - pos.cost_usd
    st.usdc_balance += proceeds
    st.realized_pnl_usd += pnl
    st.total_trades += 1
    if pnl >= 0:
        st.winning_trades += 1
    else:
        st.losing_trades += 1
    # Track per-pair stats
    ps = st.pair_stats.setdefault(pair, {"trades": 0, "wins": 0, "pnl": 0.0})
    ps["trades"] += 1
    ps["pnl"] += pnl
    if pnl >= 0:
        ps["wins"] += 1
    save_state(st)
    return pnl
