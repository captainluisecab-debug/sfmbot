"""
sfm_brain.py — Local self-adaptation brain for sfmbot.
Runs every BRAIN_EVERY_CYCLES cycles. Uses Claude to tune strategy parameters.
Writes overrides to sfm_brain_overrides.json which engine reads each cycle.
"""
from __future__ import annotations
import json, os, logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("sfm_brain")

BRAIN_EVERY_CYCLES = 10
OVERRIDES_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfm_brain_overrides.json")
DECISIONS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfm_brain_decisions.jsonl")

PARAM_BOUNDS = {
    "STOP_LOSS_PCT":   (3.0, 12.0),
    "TAKE_PROFIT_PCT": (5.0, 20.0),
    "TRADE_SIZE_USD":  (100.0, 500.0),
}


def load_overrides() -> dict:
    """Load current parameter overrides. Returns {} if none."""
    if not os.path.exists(OVERRIDES_FILE):
        return {}
    try:
        with open(OVERRIDES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("[BRAIN] Could not load overrides: %s", exc)
        return {}


def save_overrides(overrides: dict) -> None:
    try:
        with open(OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)
    except Exception as exc:
        log.warning("[BRAIN] Could not save overrides: %s", exc)


def run_brain(state, cycle: int, position_summary: str) -> Optional[dict]:
    """
    Run brain cycle. Returns new overrides dict or None if no changes.
    Only runs every BRAIN_EVERY_CYCLES cycles.
    """
    if cycle % BRAIN_EVERY_CYCLES != 0:
        return None

    current = load_overrides()

    # Build performance summary from state
    win_rate = (state.winning_trades / state.total_trades * 100) if state.total_trades > 0 else 0
    portfolio_val = getattr(state, "portfolio_val", state.usdc_balance)
    dd_pct = getattr(state, "dd_pct", 0.0)

    prompt = f"""You are the local brain for an SFM Solana token swing trading bot.

## CURRENT STATE
- USDC balance: ${state.usdc_balance:.2f} | Portfolio value: ${portfolio_val:.2f} | DD: {dd_pct:.1f}%
- Realized PnL: ${state.realized_pnl_usd:+.2f} | Trades: {state.total_trades} | Win rate: {win_rate:.0f}%
- Open position: {position_summary}
- Current params: {json.dumps(current or {"STOP_LOSS_PCT": 8.0, "TAKE_PROFIT_PCT": 15.0, "TRADE_SIZE_USD": 100.0})}

## ASSET: SFM (SafeMoon token on Solana) — high-volatility micro-cap

## TASK
Analyze performance. Tune parameters to maximize returns while protecting capital.
If win_rate < 40%: tighten STOP_LOSS_PCT and reduce TRADE_SIZE_USD
If dd > 10%: reduce TRADE_SIZE_USD significantly
If win_rate > 60% and dd < 5%: can increase TRADE_SIZE_USD and TAKE_PROFIT_PCT slightly
For high-volatility Solana tokens, wider stops and targets are generally appropriate.

Respond ONLY with valid JSON: {{"changes":{{}}, "reasoning":"one sentence"}}
Only include parameters that need changing. Empty changes={{}} means no change needed."""

    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if Claude wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        if not raw:
            log.warning("[BRAIN] cycle=%d empty response from Claude", cycle)
            return None
        data = json.loads(raw)
        changes = data.get("changes", {})

        # Apply bounds to any proposed changes
        new_overrides = dict(current or {})
        for k, v in changes.items():
            if k in PARAM_BOUNDS:
                lo, hi = PARAM_BOUNDS[k]
                new_overrides[k] = max(lo, min(hi, float(v)))

        # Audit trail — record every brain decision, including no-change
        try:
            with open(DECISIONS_FILE, "a", encoding="utf-8") as _f:
                _f.write(json.dumps({
                    "ts":         datetime.now(timezone.utc).isoformat(),
                    "cycle":      cycle,
                    "old_params": current or {},
                    "new_params": new_overrides,
                    "reasoning":  data.get("reasoning", ""),
                }) + "\n")
        except Exception as _e:
            log.warning("sfm_brain_decisions write failed: %s", _e)

        if not changes:
            log.info("[BRAIN] cycle=%d no parameter changes needed | %s", cycle, data.get("reasoning", ""))
            return None

        save_overrides(new_overrides)
        log.info("[BRAIN] cycle=%d overrides updated: %s | %s", cycle, new_overrides, data.get("reasoning", ""))
        return new_overrides

    except Exception as e:
        log.warning("[BRAIN] cycle=%d error: %s", cycle, e)
        return None


def check_escalations(state, cycle: int):
    """Check for Opus responses and escalate roadblocks to Opus."""
    try:
        from escalation_client import RoadblockDetector, write_escalation, read_response, apply_response
        if not hasattr(check_escalations, "_detector"):
            check_escalations._detector = RoadblockDetector("sfmbot")
        det = check_escalations._detector

        win_rate = (state.winning_trades / state.total_trades * 100) if state.total_trades > 0 else 0
        context  = {
            "cycle":     cycle,
            "usdc":      state.usdc_balance,
            "win_rate":  win_rate,
            "trades":    state.total_trades,
            "position":  bool(getattr(state, "position", None)),
        }

        if state.total_trades > 0:
            if win_rate < 40:
                det.tick_loss()
            else:
                det.tick_win()

        roadblock = det.detect(context)
        if roadblock:
            write_escalation("sfmbot", roadblock)

        resp = read_response("sfmbot")
        if resp:
            overrides = load_overrides()
            new_ov = apply_response(resp, overrides, PARAM_BOUNDS)
            if new_ov != overrides:
                save_overrides(new_ov)
                log.info("[ESCALATION] Applied Opus response: %s", resp.get("decision","")[:80])
    except Exception as e:
        log.debug("[ESCALATION] sfmbot client error: %s", e)
