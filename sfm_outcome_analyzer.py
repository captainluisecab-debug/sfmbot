"""
sfm_outcome_analyzer.py — Closed feedback loop for SFM trading.

Reads sfm_trade_log.jsonl (real trade data).
Computes what entry signals and exit types are working.
Writes sfm_score_adjustments.json that the engine reads each cycle.

Runs every 30 minutes from sfm_engine.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List

log = logging.getLogger("sfm_outcome_analyzer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG = os.path.join(BASE_DIR, "sfm_trade_log.jsonl")
OUTPUT_FILE = os.path.join(BASE_DIR, "sfm_score_adjustments.json")
MIN_TRADES = 3


def log_trade(entry_signal: str, exit_reason: str, pnl_usd: float,
              entry_price: float, exit_price: float, hold_sec: int,
              rsi_at_entry: float = 0, rsi_at_exit: float = 0,
              entry_ts: int = 0) -> None:
    record = {
        "type": "trade",
        "ts": time.time(),
        "entry_signal": entry_signal,
        "exit_reason": exit_reason,
        "pnl_usd": round(pnl_usd, 4),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "hold_sec": hold_sec,
        "rsi_at_entry": round(rsi_at_entry, 1),
        "rsi_at_exit": round(rsi_at_exit, 1),
        "entry_ts": entry_ts,
    }
    try:
        with open(TRADE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        log.error("[ANALYZER] Failed to log trade: %s", exc)


def _read_trades(lookback_days: int = 14) -> List[dict]:
    if not os.path.exists(TRADE_LOG):
        return []
    cutoff = time.time() - (lookback_days * 86400)
    trades = []
    try:
        with open(TRADE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("type") != "trade":
                        continue
                    if r.get("ts", 0) < cutoff:
                        continue
                    trades.append(r)
                except Exception:
                    continue
    except Exception as exc:
        log.error("[ANALYZER] Failed to read trades: %s", exc)
    return trades


def _signal_quality(trades: List[dict]) -> Dict[str, dict]:
    signals: Dict[str, dict] = {}
    for t in trades:
        sig = t.get("entry_signal", "unknown")
        if sig not in signals:
            signals[sig] = {"count": 0, "pnl": 0.0, "wins": 0, "hold_sec_sum": 0}
        signals[sig]["count"] += 1
        signals[sig]["pnl"] += t.get("pnl_usd", 0)
        signals[sig]["hold_sec_sum"] += t.get("hold_sec", 0)
        if t.get("pnl_usd", 0) > 0:
            signals[sig]["wins"] += 1

    result = {}
    for sig, s in signals.items():
        result[sig] = {
            "count": s["count"],
            "avg_pnl": round(s["pnl"] / max(1, s["count"]), 2),
            "win_rate": round(s["wins"] / max(1, s["count"]) * 100, 1),
            "avg_hold_min": round(s["hold_sec_sum"] / max(1, s["count"]) / 60, 1),
        }
    return result


def _exit_quality(trades: List[dict]) -> Dict[str, dict]:
    exits: Dict[str, dict] = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        if r not in exits:
            exits[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        exits[r]["count"] += 1
        exits[r]["pnl"] += t.get("pnl_usd", 0)
        if t.get("pnl_usd", 0) > 0:
            exits[r]["wins"] += 1

    result = {}
    for r, s in exits.items():
        result[r] = {
            "count": s["count"],
            "avg_pnl": round(s["pnl"] / max(1, s["count"]), 2),
            "win_rate": round(s["wins"] / max(1, s["count"]) * 100, 1),
        }
    return result


def _compute_recommendations(trades: List[dict], signal_q: dict) -> dict:
    recs = {}
    if len(trades) < MIN_TRADES:
        return recs

    total_wr = sum(1 for t in trades if t.get("pnl_usd", 0) > 0) / len(trades)

    for sig, q in signal_q.items():
        if q["count"] < MIN_TRADES:
            continue
        if q["win_rate"] < 25:
            recs[f"disable_{sig}"] = f"win_rate {q['win_rate']:.0f}% < 25% over {q['count']} trades"
        elif q["win_rate"] > 65 and q["avg_pnl"] > 0:
            recs[f"boost_{sig}"] = f"win_rate {q['win_rate']:.0f}% with avg_pnl ${q['avg_pnl']:.2f}"

    losing_rsi_entries = [t for t in trades if t.get("rsi_at_entry", 0) > 55 and t.get("pnl_usd", 0) < 0]
    if len(losing_rsi_entries) >= 3:
        loss_rate = len(losing_rsi_entries) / max(1, len([t for t in trades if t.get("rsi_at_entry", 0) > 55]))
        if loss_rate > 0.6:
            recs["tighten_rsi"] = f"{loss_rate*100:.0f}% of RSI>55 entries lost money"

    return recs


def run_analyzer() -> dict:
    trades = _read_trades(lookback_days=14)
    if len(trades) < MIN_TRADES:
        log.info("[ANALYZER] Only %d trades in 14 days -- waiting for data", len(trades))
        return {}

    signal_q = _signal_quality(trades)
    exit_q = _exit_quality(trades)
    recs = _compute_recommendations(trades, signal_q)

    total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
    total_wins = sum(1 for t in trades if t.get("pnl_usd", 0) > 0)

    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "lookback_days": 14,
        "total_trades": len(trades),
        "total_pnl": round(total_pnl, 2),
        "overall_win_rate": round(total_wins / len(trades) * 100, 1),
        "entry_signal_quality": signal_q,
        "exit_reason_quality": exit_q,
        "recommendations": recs,
    }

    try:
        tmp = OUTPUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        os.replace(tmp, OUTPUT_FILE)
        log.info("[ANALYZER] Updated: %d trades, WR=%.0f%%, recs=%s",
                 len(trades), result["overall_win_rate"], list(recs.keys()) or "none")
    except Exception as exc:
        log.error("[ANALYZER] Failed to write: %s", exc)

    return result
