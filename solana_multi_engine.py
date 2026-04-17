"""
solana_multi_engine.py — Multi-pair Solana trading engine.

Runs SOL/USDC (primary swing) + SFM/USDC (tactical earned slot) in one loop.
Each pair has independent positions, stops, TP, and outcome tracking.
Uses same Jupiter broker + wallet infrastructure as sfm_engine.

Designed to replace sfm_engine.py once proven. Currently runs alongside it
or as a standalone via: python solana_multi_engine.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][SOLANA] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "solana_engine.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("solana_engine")

from sfm_settings import TRADE_MODE, PHANTOM_PRIVATE_KEY, SOLANA_RPC, CYCLE_SEC
from sfm_broker import get_quote, execute_swap, USDC_MINT, USDC_DECIMALS
from sfm_data import get_candles
from solana_pairs import PAIR_CONFIGS, PairConfig
from solana_state import (
    SolanaState, load_state, save_state, record_buy, record_sell,
)
from solana_strategy import compute_swing_signal


# ── Wallet setup ────────────────────────────────────────────────────
_keypair = None
_wallet_pubkey = ""

def _init_wallet():
    global _keypair, _wallet_pubkey
    if TRADE_MODE != "LIVE" or not PHANTOM_PRIVATE_KEY:
        return
    from sfm_wallet import load_keypair, public_key_str
    _keypair = load_keypair(PHANTOM_PRIVATE_KEY)
    _wallet_pubkey = public_key_str(_keypair)
    log.info("Wallet loaded: %s", _wallet_pubkey)


# ── Data fetching ───────────────────────────────────────────────────

def _fetch_candles(pair_cfg: PairConfig) -> dict:
    """Fetch 15-min candles from DexScreener/GeckoTerminal for a pair."""
    try:
        from sfm_data import get_best_pair

        tick = get_best_pair(pair_cfg.base_mint)
        if not tick:
            return {}

        candles = get_candles(tick.pair_addr, "solana", resolution="15")
        if not candles or len(candles) < 30:
            return {}

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        return {
            "price": tick.price_usd,
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "liquidity": tick.liquidity_usd,
            "volume_24h": tick.volume_24h_usd,
        }
    except Exception as exc:
        log.warning("[%s] Candle fetch failed: %s", pair_cfg.name, exc)
        return {}


# ── Execution ───────────────────────────────────────────────────────

def _execute_buy(pair_cfg: PairConfig, usd_amount: float) -> dict:
    """Buy base token with USDC via Jupiter."""
    from sfm_broker import get_quote as jup_quote
    quote = jup_quote(
        input_mint=pair_cfg.quote_mint,
        output_mint=pair_cfg.base_mint,
        amount_in_tokens=usd_amount,
        input_decimals=pair_cfg.quote_decimals,
        slippage_bps=pair_cfg.slippage_bps,
    )
    if not quote:
        return {}
    if quote.price_impact_pct > 2.0:
        log.warning("[%s] High price impact %.2f%% on BUY — skipping", pair_cfg.name, quote.price_impact_pct)
        return {}
    result = execute_swap(quote, _wallet_pubkey, _keypair)
    return result or {}


def _execute_sell(pair_cfg: PairConfig, base_qty: float) -> dict:
    """Sell base token for USDC via Jupiter."""
    from sfm_broker import get_quote as jup_quote
    quote = jup_quote(
        input_mint=pair_cfg.base_mint,
        output_mint=pair_cfg.quote_mint,
        amount_in_tokens=base_qty,
        input_decimals=pair_cfg.base_decimals,
        slippage_bps=pair_cfg.slippage_bps,
    )
    if not quote:
        return {}
    if quote.price_impact_pct > 3.0:
        log.warning("[%s] High price impact %.2f%% on SELL — skipping", pair_cfg.name, quote.price_impact_pct)
        return {}
    result = execute_swap(quote, _wallet_pubkey, _keypair)
    return result or {}


# ── Supervisor command reading ──────────────────────────────────────

def _read_supervisor_cmd() -> dict:
    try:
        cmd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "supervisor", "commands", "sfm_cmd.json")
        cmd_path = os.path.normpath(cmd_path)
        if os.path.exists(cmd_path):
            with open(cmd_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"mode": "NORMAL", "size_mult": 1.0, "entry_allowed": True, "force_flatten": False}


# ── Earned-slot gating ──────────────────────────────────────────────

def _check_earned_slot(pair_name: str, st: SolanaState) -> bool:
    """Check if a tactical pair has earned its slot based on performance data."""
    cfg = PAIR_CONFIGS.get(pair_name)
    if not cfg or cfg.strategy_type != "tactical":
        return True  # non-tactical pairs always allowed

    ps = st.pair_stats.get(pair_name, {})
    trades = ps.get("trades", 0)
    if trades < 1:
        return True  # no data yet — allow first trade to gather data

    wins = ps.get("wins", 0)
    wr = wins / trades * 100 if trades > 0 else 0
    avg_pnl = ps.get("pnl", 0) / trades if trades > 0 else 0

    if trades >= 5 and wr < 50:
        log.info("[%s] EARNED SLOT BLOCKED: WR=%.0f%% < 50%% over %d trades",
                 pair_name, wr, trades)
        return False
    if trades >= 5 and avg_pnl < 2.0:
        log.info("[%s] EARNED SLOT BLOCKED: avg_pnl=$%.2f < $2.00 over %d trades",
                 pair_name, avg_pnl, trades)
        return False
    return True


# ── Main loop ───────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("SOLANA MULTI-PAIR ENGINE")
    log.info("Mode: %s | Pairs: %s", TRADE_MODE,
             ", ".join(n for n, c in PAIR_CONFIGS.items() if c.enabled))
    log.info("Cycle: %ds", CYCLE_SEC)
    log.info("=" * 60)

    if TRADE_MODE == "LIVE":
        _init_wallet()

    st = load_state()
    log.info("State loaded: USDC=$%.2f | rpnl=$%.2f | trades=%d | positions=%s",
             st.usdc_balance, st.realized_pnl_usd, st.total_trades,
             list(st.positions.keys()) or "none")

    # If no USDC balance set, initialize from wallet
    if st.usdc_balance <= 0 and TRADE_MODE == "LIVE":
        try:
            import requests
            tresp = requests.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [_wallet_pubkey,
                           {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                           {"encoding": "jsonParsed"}]
            }, timeout=15)
            for acct in tresp.json().get("result", {}).get("value", []):
                info = acct["account"]["data"]["parsed"]["info"]
                if info["mint"] == USDC_MINT:
                    st.usdc_balance = float(info["tokenAmount"]["uiAmount"] or 0)
                    log.info("USDC balance from wallet: $%.2f", st.usdc_balance)
        except Exception as exc:
            log.warning("Could not fetch wallet USDC: %s", exc)

    while True:
        try:
            cycle_start = time.time()
            st.cycle += 1
            cycle = st.cycle

            cmd = _read_supervisor_cmd()
            entry_ok = cmd.get("entry_allowed", True)
            size_mult = cmd.get("size_mult", 1.0)
            force_flatten = cmd.get("force_flatten", False)

            # Force flatten all positions
            if force_flatten:
                log.warning("[CYCLE %d] FORCE_FLATTEN — closing all positions", cycle)
                for pair_name in list(st.positions.keys()):
                    pos = st.positions[pair_name]
                    cfg = PAIR_CONFIGS.get(pair_name)
                    if cfg and TRADE_MODE == "LIVE":
                        _execute_sell(cfg, pos.base_qty)
                    pnl = record_sell(st, pair_name, pos.entry_price, "governor_force_flatten")
                    log.info("[%s] Force flattened | pnl=$%.2f", pair_name, pnl)
                save_state(st)
                time.sleep(CYCLE_SEC)
                continue

            # Process each enabled pair
            for pair_name, pair_cfg in PAIR_CONFIGS.items():
                if not pair_cfg.enabled:
                    continue

                # Earned-slot gate for tactical pairs
                if not _check_earned_slot(pair_name, st):
                    continue

                data = _fetch_candles(pair_cfg)
                if not data:
                    continue

                price = data["price"]
                has_position = pair_name in st.positions
                pos = st.positions.get(pair_name)

                signal = compute_swing_signal(
                    price=price,
                    closes=data["closes"],
                    highs=data["highs"],
                    lows=data["lows"],
                    rsi_oversold=pair_cfg.rsi_oversold,
                    rsi_overbought=pair_cfg.rsi_overbought,
                    ema_period=pair_cfg.ema_period,
                    stop_loss_pct=pair_cfg.stop_loss_pct,
                    take_profit_pct=pair_cfg.take_profit_pct,
                    min_score=pair_cfg.min_score,
                    open_position=has_position,
                    entry_price=pos.entry_price if pos else 0,
                    breakeven_armed=pos.breakeven_armed if pos else False,
                    peak_pnl_pct=pos.peak_pnl_pct if pos else 0,
                )

                # Update breakeven arm state
                if pos and not pos.breakeven_armed:
                    pnl_pct = (price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
                    if pnl_pct >= 1.5:
                        pos.breakeven_armed = True
                        log.info("[%s] Breakeven ARMED at pnl=%.1f%%", pair_name, pnl_pct)
                    if pnl_pct > pos.peak_pnl_pct:
                        pos.peak_pnl_pct = pnl_pct

                log.info("[CYCLE %d] [%s] price=$%.6f rsi=%.1f signal=%s reason=%s",
                         cycle, pair_name, price, signal.rsi, signal.action, signal.reason)

                # ── SELL ──
                if signal.action == "SELL" and has_position:
                    log.info("[%s] SELL @ $%.6f | reason=%s", pair_name, price, signal.reason)
                    if TRADE_MODE == "LIVE":
                        fill = _execute_sell(pair_cfg, pos.base_qty)
                        if fill:
                            usdc_received = fill.get("out_amount", 0) / (10 ** pair_cfg.quote_decimals)
                            eff_price = usdc_received / pos.base_qty if pos.base_qty > 0 else price
                        else:
                            log.error("[%s] Sell execution failed — skipping", pair_name)
                            continue
                    else:
                        eff_price = price
                    pnl = record_sell(st, pair_name, eff_price, signal.reason)
                    log.info("[%s] PnL: $%.2f | reason=%s | total_rpnl=$%.2f",
                             pair_name, pnl, signal.reason, st.realized_pnl_usd)
                    # Log to outcome analyzer
                    try:
                        from sfm_outcome_analyzer import log_trade
                        log_trade(
                            entry_signal=pos.entry_signal, exit_reason=signal.reason,
                            pnl_usd=pnl, entry_price=pos.entry_price, exit_price=eff_price,
                            hold_sec=int(time.time()) - pos.entry_ts,
                            rsi_at_exit=signal.rsi, entry_ts=pos.entry_ts,
                        )
                    except Exception:
                        pass

                # ── BUY ──
                elif signal.action == "BUY" and not has_position and entry_ok:
                    max_usd = st.usdc_balance * (pair_cfg.max_allocation_pct / 100)
                    trade_usd = min(max_usd, 300.0) * size_mult  # hard cap
                    if trade_usd < 10:
                        log.info("[%s] Insufficient USDC ($%.2f) — skipping", pair_name, st.usdc_balance)
                        continue
                    log.info("[%s] BUY $%.2f @ $%.6f | score=%.0f reason=%s",
                             pair_name, trade_usd, price, signal.score, signal.reason)
                    if TRADE_MODE == "LIVE":
                        fill = _execute_buy(pair_cfg, trade_usd)
                        if fill:
                            base_received = fill.get("out_amount", 0) / (10 ** pair_cfg.base_decimals)
                            eff_price = trade_usd / base_received if base_received > 0 else price
                        else:
                            log.error("[%s] Buy execution failed — skipping", pair_name)
                            continue
                    else:
                        base_received = trade_usd / price
                        eff_price = price
                    record_buy(st, pair_name, eff_price, base_received, trade_usd, signal.reason)
                    log.info("[%s] Bought %.4f @ $%.6f (cost=$%.2f)",
                             pair_name, base_received, eff_price, trade_usd)

            # Sync USDC balance from wallet each cycle (detect deposits)
            try:
                import requests as _req
                _usdc_resp = _req.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 99, "method": "getTokenAccountsByOwner",
                    "params": [_wallet_pubkey,
                               {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
                               {"encoding": "jsonParsed"}]}, timeout=10)
                for _ua in _usdc_resp.json().get("result", {}).get("value", []):
                    _wallet_usdc = float(_ua["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                    if abs(_wallet_usdc - st.usdc_balance) > 1.0:
                        log.info("[SYNC] USDC wallet=$%.2f state=$%.2f — syncing", _wallet_usdc, st.usdc_balance)
                        st.usdc_balance = _wallet_usdc
            except Exception:
                pass

            # Portfolio summary — include native SOL in equity
            total_deployed = sum(p.cost_usd for p in st.positions.values())
            sol_usd = 0.0
            try:
                _sol_resp = _req.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                    "params": [_wallet_pubkey]}, timeout=10)
                _sol = _sol_resp.json().get("result", {}).get("value", 0) / 1e9
                # Get SOL price from latest tick data (reuse any SOL pair data from this cycle)
                _sol_px = 0
                try:
                    from sfm_data import get_best_pair as _gbp
                    _sol_tick = _gbp("So11111111111111111111111111111111111111112")
                    _sol_px = _sol_tick.price_usd if _sol_tick else 0
                except Exception:
                    _sol_px = 88.0  # fallback
                sol_usd = _sol * _sol_px
                st.sol_balance = _sol
                st.sol_usd = round(sol_usd, 2)
            except Exception:
                pass
            equity = st.usdc_balance + total_deployed + sol_usd
            if equity > st.peak_equity:
                st.peak_equity = equity
            dd = (equity - st.peak_equity) / st.peak_equity * 100 if st.peak_equity > 0 else 0
            wr = st.winning_trades / st.total_trades * 100 if st.total_trades > 0 else 0

            pos_str = ", ".join(f"{p}(${v.cost_usd:.0f})" for p, v in st.positions.items()) or "flat"
            log.info("[CYCLE %d] equity=$%.2f usdc=$%.2f sol=$%.2f deployed=$%.2f dd=%.1f%% rpnl=$%.2f wr=%.0f%% | %s",
                     cycle, equity, st.usdc_balance, sol_usd, total_deployed, dd, st.realized_pnl_usd, wr, pos_str)

            save_state(st)

            # Outcome analyzer every 6 cycles (~30 min)
            if cycle % 6 == 0 and cycle > 0:
                try:
                    from sfm_outcome_analyzer import run_analyzer
                    run_analyzer()
                except Exception:
                    pass

            elapsed = time.time() - cycle_start
            sleep_time = max(1, CYCLE_SEC - elapsed)
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            save_state(st)
            break
        except Exception as exc:
            log.error("Cycle error: %s", exc, exc_info=True)
            save_state(st)
            time.sleep(30)


if __name__ == "__main__":
    run()
