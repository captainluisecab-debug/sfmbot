"""
sfm_settings.py — Load .env, expose typed config for SFM bot.
"""
from __future__ import annotations

import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env(path: str) -> None:
    """Parse .env file and set env vars, .env always wins over system vars."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ[key] = val  # override=True — .env beats system vars


_load_env(_ENV_PATH)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _getf(key: str, default: float) -> float:
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default


def _geti(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


SFM_MINT         = _get("SFM_MINT", "ELPrcU7qRV3DUz8AP6siTE7GkR3gkkBvGmgBRiLnC19Y")
TRADE_SIZE_USD   = _getf("TRADE_SIZE_USD", 100.0)
MAX_TRADE_USD    = _getf("MAX_TRADE_USD", 150.0)
MIN_TRADE_USD    = _getf("MIN_TRADE_USD", 50.0)
STOP_LOSS_PCT    = _getf("STOP_LOSS_PCT", 8.0)
TAKE_PROFIT_PCT  = _getf("TAKE_PROFIT_PCT", 15.0)
MAX_OPEN_USD     = _getf("MAX_OPEN_USD", 300.0)
TRADE_MODE       = _get("TRADE_MODE", "PAPER")   # PAPER | LIVE
SOLANA_RPC       = _get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
PHANTOM_PRIVATE_KEY = _get("PHANTOM_PRIVATE_KEY", "")
SLIPPAGE_BPS     = _geti("SLIPPAGE_BPS", 150)
CYCLE_SEC        = _geti("CYCLE_SEC", 300)
