"""
status.py — Quick paper trade status check.

Usage:
    python status.py
"""
from __future__ import annotations

import json
import os
import time

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfm_state.json")
STARTING_USDC = 2_469.62


def main():
    if not os.path.exists(STATE_FILE):
        print("No state file yet — bot hasn't run a cycle.")
        return

    with open(STATE_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    usdc      = raw.get("usdc_balance", STARTING_USDC)
    pnl       = raw.get("realized_pnl_usd", 0.0)
    total     = raw.get("total_trades", 0)
    wins      = raw.get("winning_trades", 0)
    losses    = raw.get("losing_trades", 0)
    cycle     = raw.get("cycle", 0)
    position  = raw.get("position")

    # Fetch live price for mark-to-market
    try:
        import requests
        from sfm_settings import SFM_MINT
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{SFM_MINT}", timeout=8
        )
        pairs = resp.json().get("pairs") or []
        if pairs:
            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            price = float(best.get("priceUsd") or 0)
        else:
            price = 0.0
    except Exception:
        price = 0.0

    pos_value = 0.0
    if position and price > 0:
        pos_value = position["sfm_qty"] * price

    total_value = usdc + pos_value
    total_pnl   = total_value - STARTING_USDC
    pnl_pct     = total_pnl / STARTING_USDC * 100

    win_rate = (wins / total * 100) if total > 0 else 0.0

    print("=" * 50)
    print("  SFM BOT — PAPER TRADE STATUS")
    print("=" * 50)
    print(f"  Cycle:          {cycle}")
    print(f"  Live Price:     ${price:.8f}" if price else "  Live Price:     (unavailable)")
    print()
    print(f"  USDC Balance:   ${usdc:>10.2f}")
    if position:
        print(f"  Open Position:  {position['sfm_qty']:,.0f} SFM")
        print(f"    Entry Price:  ${position['entry_price']:.8f}")
        print(f"    Cost:         ${position['cost_usd']:.2f}")
        if price > 0:
            unreal = pos_value - position['cost_usd']
            unreal_pct = unreal / position['cost_usd'] * 100
            print(f"    Mark Value:   ${pos_value:.2f}  ({unreal_pct:+.1f}%)")
        print(f"    Scaled Out:   {position.get('scaled_out', False)}")
    else:
        print("  Open Position:  FLAT")
    print()
    print(f"  Realized PnL:   ${pnl:>+.2f}")
    print(f"  Total Value:    ${total_value:>10.2f}")
    print(f"  Total PnL:      ${total_pnl:>+.2f}  ({pnl_pct:+.2f}%)")
    print()
    print(f"  Trades:         {total}  (W={wins} L={losses} WR={win_rate:.0f}%)")
    print("=" * 50)


if __name__ == "__main__":
    main()
