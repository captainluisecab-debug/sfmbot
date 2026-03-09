"""
sfm_data.py — Free price data for SFM.

Price:   DexScreener (free, no API key) → Jupiter fallback
Candles: GeckoTerminal (free, no API key, real OHLCV for Raydium pools)

GeckoTerminal OHLCV: https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

from sfm_settings import SFM_MINT

log = logging.getLogger("sfm_data")

DEXSCREENER_TOKEN_URL  = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
GECKOTERMINAL_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
)

# Fallback: Jupiter price API (real-time spot price, no candles)
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids={mint}"


@dataclass
class Candle:
    ts: int    # unix timestamp seconds
    open: float
    high: float
    low: float
    close: float
    volume: float  # USD volume


@dataclass
class Tick:
    price_usd: float
    price_sol: float
    liquidity_usd: float
    volume_24h_usd: float
    pair_addr: str
    dex: str


def get_best_pair(mint: str = SFM_MINT) -> Optional[Tick]:
    """
    Fetch the best (highest liquidity) Solana pair for mint from DexScreener.
    Returns a Tick with current price + pool metadata.
    """
    url = DEXSCREENER_TOKEN_URL.format(mint=mint)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("DexScreener token fetch failed: %s", exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        log.warning("DexScreener returned no pairs for mint %s", mint)
        return None

    # Filter Solana pairs, sort by liquidity descending
    sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
    if not sol_pairs:
        sol_pairs = pairs  # fallback: use all

    best = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

    price_usd = float(best.get("priceUsd") or 0)
    price_native = float(best.get("priceNative") or 0)
    liq_usd = float((best.get("liquidity") or {}).get("usd") or 0)
    vol_24h = float((best.get("volume") or {}).get("h24") or 0)
    pair_addr = best.get("pairAddress", "")
    dex = best.get("dexId", "unknown")

    return Tick(
        price_usd=price_usd,
        price_sol=price_native,
        liquidity_usd=liq_usd,
        volume_24h_usd=vol_24h,
        pair_addr=pair_addr,
        dex=dex,
    )


def get_candles(pair_addr: str, chain: str = "solana", resolution: str = "15") -> List[Candle]:
    """
    Fetch OHLCV candles from GeckoTerminal for a Raydium pool address.
    resolution: minutes per candle — "1", "5", "15", "60" supported.
    Returns list of Candle sorted oldest→newest (up to 100 candles).
    """
    url = GECKOTERMINAL_OHLCV_URL.format(pool=pair_addr)
    params = {
        "aggregate": int(resolution),  # e.g. 15 → 15-minute candles
        "limit": 100,
        "currency": "usd",
    }
    headers = {"Accept": "application/json"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("GeckoTerminal candles fetch failed: %s", exc)
        return []

    # GeckoTerminal format: [[timestamp, open, high, low, close, volume], ...]
    raw = (data.get("data") or {}).get("attributes", {}).get("ohlcv_list", [])
    candles = []
    for c in raw:
        try:
            candles.append(Candle(
                ts=int(c[0]),
                open=float(c[1]),
                high=float(c[2]),
                low=float(c[3]),
                close=float(c[4]),
                volume=float(c[5]),
            ))
        except (TypeError, ValueError, IndexError):
            continue

    candles.sort(key=lambda c: c.ts)
    return candles


def get_price_jupiter(mint: str = SFM_MINT) -> Optional[float]:
    """
    Fallback: fetch real-time spot price from Jupiter Price API v2.
    Returns USD price or None on failure.
    """
    url = JUPITER_PRICE_URL.format(mint=mint)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = (data.get("data") or {}).get(mint, {}).get("price")
        return float(price) if price is not None else None
    except Exception as exc:
        log.error("Jupiter price fetch failed: %s", exc)
        return None


def get_price(mint: str = SFM_MINT) -> Optional[float]:
    """
    Get current USD price — tries DexScreener first, then Jupiter.
    """
    tick = get_best_pair(mint)
    if tick and tick.price_usd > 0:
        return tick.price_usd
    return get_price_jupiter(mint)
