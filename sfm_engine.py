"""
sfm_engine.py — Main trading loop for SFM swing bot.

Cycle every CYCLE_SEC seconds:
  1. Fetch current price + candles from DexScreener
  2. Compute signal from sfm_strategy
  3. Execute BUY / SELL via sfm_broker (paper or live)
  4. Update + save state

Run:
    python sfm_engine.py

Mode is controlled by TRADE_MODE in .env:
    PAPER = simulate trades, no real transactions
    LIVE  = execute real swaps via Jupiter (requires PHANTOM_PRIVATE_KEY)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

# Create logs directory BEFORE setting up file handler
os.makedirs(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
    exist_ok=True,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][SFM] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sfm_engine.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("sfm_engine")

from sfm_settings import (
    CYCLE_SEC,
    MAX_OPEN_USD,
    PHANTOM_PRIVATE_KEY,
    SFM_MINT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRADE_MODE,
    TRADE_SIZE_USD,
)
from sfm_data import get_best_pair, get_candles
from sfm_strategy import compute_signal
from sfm_broker import buy_sfm, sell_sfm
from sfm_state import (
    SFMState,
    close_position,
    load_state,
    open_position,
    portfolio_value,
    save_state,
)
from sfm_brain import run_brain as brain_run, load_overrides as brain_overrides


def _read_supervisor_cmd() -> dict:
    """Read supervisor command file if present. Returns defaults if missing."""
    cmd_path = r"C:\Projects\supervisor\commands\sfm_cmd.json"
    defaults = {"mode": "NORMAL", "size_mult": 1.0, "entry_allowed": True}
    try:
        if os.path.exists(cmd_path):
            with open(cmd_path, encoding="utf-8") as f:
                return {**defaults, **json.load(f)}
    except Exception:
        pass
    return defaults


def _load_wallet():
    """Load keypair for live trading. Returns None in paper mode."""
    if TRADE_MODE != "LIVE":
        return None, ""
    if not PHANTOM_PRIVATE_KEY:
        log.error("[FATAL] TRADE_MODE=LIVE but PHANTOM_PRIVATE_KEY is empty")
        sys.exit(1)
    from sfm_wallet import load_keypair, public_key_str
    kp = load_keypair(PHANTOM_PRIVATE_KEY)
    pk = public_key_str(kp)
    log.info("Wallet loaded: %s", pk)
    return kp, pk


def _run_cycle(st: SFMState, keypair, pubkey: str, cycle: int) -> None:
    st.cycle = cycle

    # ── Brain overrides — load every cycle, run brain every 10 ─────
    overrides   = brain_overrides()
    stop_loss   = overrides.get("STOP_LOSS_PCT",   STOP_LOSS_PCT)
    take_profit = overrides.get("TAKE_PROFIT_PCT", TAKE_PROFIT_PCT)
    trade_size  = overrides.get("TRADE_SIZE_USD",  TRADE_SIZE_USD)

    # ── 0. Supervisor command ───────────────────────────────────────
    cmd = _read_supervisor_cmd()
    sup_mode    = cmd.get("mode", "NORMAL")
    size_mult   = float(cmd.get("size_mult", 1.0))
    entry_ok    = bool(cmd.get("entry_allowed", True))

    if sup_mode == "DEFENSE":
        log.info("[CYCLE %d] Supervisor: DEFENSE — no new entries", cycle)
        entry_ok = False
    elif sup_mode == "SCOUT":
        size_mult = min(size_mult, 0.5)

    # ── 1. Fetch market data ────────────────────────────────────────
    tick = get_best_pair(SFM_MINT)
    if tick is None or tick.price_usd <= 0:
        log.warning("[CYCLE %d] Could not fetch price — skipping", cycle)
        return

    price = tick.price_usd
    pair_addr = tick.pair_addr

    # Fetch 15-min candles (last ~50 candles = ~12 hours)
    candles = get_candles(pair_addr, chain="solana", resolution="15")
    if len(candles) < 20:
        log.warning(
            "[CYCLE %d] Only %d candles available — using spot price only",
            cycle, len(candles),
        )

    # ── 2. Compute signal ───────────────────────────────────────────
    has_position = st.position is not None
    entry_price  = st.position.entry_price if has_position else 0.0
    scaled_out   = st.position.scaled_out  if has_position else False

    last_buy_candle_idx = getattr(st, 'last_buy_candle_idx', -1)
    signal = compute_signal(
        candles=candles,
        open_position=has_position,
        entry_price=entry_price,
        stop_loss_pct=stop_loss,
        take_profit_pct=take_profit,
        scaled_out=scaled_out,
        last_buy_candle_idx=last_buy_candle_idx,
        cooldown_candles=3,
    )

    # Override signal.price with live tick (more accurate than candle close)
    signal = signal.__class__(
        action=signal.action,
        rsi=signal.rsi,
        ema=signal.ema,
        atr=signal.atr,
        price=price,
        volume=signal.volume,
        vol_avg=signal.vol_avg,
        reason=signal.reason,
    )

    pv = portfolio_value(st, price)
    pnl_pct = ((pv - 2_469.62) / 2_469.62 * 100) if pv else 0.0

    log.info(
        "[CYCLE %d] price=$%.8f rsi=%.1f ema=$%.8f signal=%s reason=%s | "
        "pos=%s pv=$%.2f pnl=%.2f%% usdc=$%.2f",
        cycle, price, signal.rsi, signal.ema, signal.action, signal.reason,
        "OPEN" if has_position else "FLAT", pv, pnl_pct, st.usdc_balance,
    )

    # ── 3. Execute ──────────────────────────────────────────────────
    if signal.action == "BUY" and not has_position and entry_ok:
        # Don't buy if already at max open exposure
        trade_usd_adj = trade_size * size_mult
        if st.usdc_balance < trade_usd_adj:
            log.warning("[CYCLE %d] Insufficient USDC (%.2f) — skipping buy", cycle, st.usdc_balance)
        elif portfolio_value(st, price) - st.usdc_balance >= MAX_OPEN_USD:
            log.warning("[CYCLE %d] Max open exposure reached — skipping buy", cycle)
        else:
            trade_usd = min(trade_usd_adj, st.usdc_balance)
            log.info("[CYCLE %d] BUY $%.2f of SFM @ $%.8f", cycle, trade_usd, price)
            fill = buy_sfm(trade_usd, pubkey, keypair, price_usd=price)
            if fill:
                # In live mode, out_amount is SFM lamports; convert to tokens
                if TRADE_MODE == "LIVE":
                    sfm_received = fill["out_amount"] / 1e9
                    effective_price = trade_usd / sfm_received if sfm_received > 0 else price
                else:
                    effective_price = price
                open_position(st, effective_price, trade_usd)
                st.last_buy_candle_idx = len(candles)
                try:
                    import sys as _sys
                    if r"C:\Projects\supervisor" not in _sys.path:
                        _sys.path.insert(0, r"C:\Projects\supervisor")
                    from supervisor_execution import log_execution
                    log_execution("sfm", "SFM", "BUY", trade_usd, effective_price, 0.0, signal.reason)
                except Exception:
                    pass

    elif signal.action == "SELL" and has_position:
        is_partial = "scale_out" in signal.reason
        sfm_to_sell = st.position.sfm_qty * (0.5 if is_partial else 1.0)
        proceeds_usd = sfm_to_sell * price

        log.info(
            "[CYCLE %d] SELL %.0f SFM @ $%.8f ($%.2f) | reason=%s",
            cycle, sfm_to_sell, price, proceeds_usd, signal.reason,
        )
        fill = sell_sfm(sfm_to_sell, pubkey, keypair, price_usd=price)
        if fill:
            if TRADE_MODE == "LIVE":
                usdc_received = fill["out_amount"] / 1e6
                effective_price = usdc_received / sfm_to_sell if sfm_to_sell > 0 else price
            else:
                effective_price = price
            pnl = close_position(st, effective_price, sfm_to_sell, signal.reason)
            log.info("[CYCLE %d] PnL this trade: $%.2f", cycle, pnl)
            st.last_buy_candle_idx = -1
            try:
                import sys as _sys
                if r"C:\Projects\supervisor" not in _sys.path:
                    _sys.path.insert(0, r"C:\Projects\supervisor")
                from supervisor_execution import log_execution
                log_execution("sfm", "SFM", "SELL", proceeds_usd, effective_price, pnl, signal.reason)
            except Exception:
                pass

    # ── Brain — self-tune parameters every 10 cycles ────────────────
    if cycle % 10 == 0:
        pv = portfolio_value(st, price)
        peak_pv = getattr(st, "peak_portfolio_val", pv)
        if pv > peak_pv:
            peak_pv = pv
        st.peak_portfolio_val = peak_pv
        st.portfolio_val = pv
        st.dd_pct = ((peak_pv - pv) / peak_pv * 100) if peak_pv > 0 else 0.0
        if st.position:
            pos_summary = (
                f"{st.position.sfm_qty:.0f} SFM @ entry=${st.position.entry_price:.8f}"
                f" (cost=${st.position.cost_usd:.2f})"
            )
        else:
            pos_summary = "none"
        brain_run(st, cycle, pos_summary)

    save_state(st)


def main() -> None:
    log.info("=" * 60)
    log.info("SFM BOT — Swing Trader")
    log.info("Mode: %s | Mint: %s", TRADE_MODE, SFM_MINT)
    log.info("Trade size: $%.0f | Stop: %.1f%% | TP: %.1f%%",
             TRADE_SIZE_USD, STOP_LOSS_PCT, TAKE_PROFIT_PCT)
    log.info("Cycle interval: %ds", CYCLE_SEC)
    log.info("=" * 60)

    keypair, pubkey = _load_wallet()
    st = load_state()

    log.info(
        "State loaded — USDC=$%.2f | realized_pnl=$%.2f | trades=%d",
        st.usdc_balance, st.realized_pnl_usd, st.total_trades,
    )
    if st.position:
        log.info(
            "Open position: %.0f SFM @ entry=$%.8f (cost=$%.2f)",
            st.position.sfm_qty, st.position.entry_price, st.position.cost_usd,
        )

    cycle = st.cycle
    while True:
        cycle += 1
        try:
            _run_cycle(st, keypair, pubkey, cycle)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.error("[CYCLE %d] Unhandled error: %s", cycle, exc, exc_info=True)
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("SFM bot stopped.")
