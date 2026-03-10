"""
sfm_broker.py — Jupiter v6 aggregator for SFM swaps on Solana.

In PAPER mode: quotes are fetched (real prices) but no transaction is sent.
In LIVE mode: quotes → swap transaction → sign → submit to RPC.

Jupiter API v6 (free, no API key):
  Quote: GET https://quote-api.jup.ag/v6/quote
  Swap:  POST https://quote-api.jup.ag/v6/swap

Token mint addresses:
  USDC (Solana): EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
  SOL:           So11111111111111111111111111111111111111112
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from sfm_settings import (
    SFM_MINT,
    SLIPPAGE_BPS,
    SOLANA_RPC,
    TRADE_MODE,
)

log = logging.getLogger("sfm_broker")

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

# Solana token mints
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT  = "So11111111111111111111111111111111111111112"

# SFM has 9 decimal places (standard SPL)
SFM_DECIMALS = 9
USDC_DECIMALS = 6


@dataclass
class Quote:
    in_mint: str
    out_mint: str
    in_amount: int      # lamports / micro-tokens
    out_amount: int
    price_impact_pct: float
    route_plan: list
    raw: dict           # full Jupiter response (needed for swap tx)


def get_quote(
    input_mint: str,
    output_mint: str,
    amount_in_tokens: float,
    input_decimals: int = USDC_DECIMALS,
    slippage_bps: int = SLIPPAGE_BPS,
) -> Optional[Quote]:
    """
    Fetch a Jupiter swap quote.

    amount_in_tokens: human-readable amount (e.g. 100.0 for $100 USDC)
    Returns Quote or None on failure.
    """
    amount_raw = int(amount_in_tokens * (10 ** input_decimals))
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_raw,
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": "false",
    }
    try:
        resp = requests.get(JUPITER_QUOTE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Jupiter quote failed: %s", exc)
        return None

    if "error" in data:
        log.error("Jupiter quote error: %s", data["error"])
        return None

    return Quote(
        in_mint=input_mint,
        out_mint=output_mint,
        in_amount=int(data.get("inAmount", 0)),
        out_amount=int(data.get("outAmount", 0)),
        price_impact_pct=float(data.get("priceImpactPct", 0)),
        route_plan=data.get("routePlan", []),
        raw=data,
    )


def quote_buy_sfm(usd_amount: float) -> Optional[Quote]:
    """Quote buying SFM with USDC."""
    return get_quote(
        input_mint=USDC_MINT,
        output_mint=SFM_MINT,
        amount_in_tokens=usd_amount,
        input_decimals=USDC_DECIMALS,
    )


def quote_sell_sfm(sfm_amount: float) -> Optional[Quote]:
    """Quote selling SFM for USDC."""
    return get_quote(
        input_mint=SFM_MINT,
        output_mint=USDC_MINT,
        amount_in_tokens=sfm_amount,
        input_decimals=SFM_DECIMALS,
    )


def execute_swap(quote: Quote, wallet_pubkey: str, keypair=None) -> Optional[dict]:
    """
    Execute a swap via Jupiter.

    In PAPER mode: logs the trade, returns a simulated fill dict.
    In LIVE mode: sends the transaction to the Solana RPC.

    Returns dict with keys: status, tx_sig, in_amount, out_amount
    """
    if TRADE_MODE == "PAPER":
        in_human  = quote.in_amount  / (10 ** (USDC_DECIMALS if quote.in_mint == USDC_MINT else SFM_DECIMALS))
        out_human = quote.out_amount / (10 ** (SFM_DECIMALS  if quote.out_mint == SFM_MINT  else USDC_DECIMALS))
        log.info(
            "[PAPER] SWAP %.6f %s → %.6f %s (impact=%.3f%%)",
            in_human, quote.in_mint[-6:], out_human, quote.out_mint[-6:],
            quote.price_impact_pct,
        )
        return {
            "status": "PAPER_FILL",
            "tx_sig": "paper_tx",
            "in_amount": quote.in_amount,
            "out_amount": quote.out_amount,
        }

    # --- LIVE MODE ---
    if keypair is None:
        log.error("keypair required for live swap")
        return None

    # 1. Request swap transaction from Jupiter
    payload = {
        "quoteResponse": quote.raw,
        "userPublicKey": wallet_pubkey,
        "wrapAndUnwrapSol": True,
        "computeUnitPriceMicroLamports": "auto",
    }
    try:
        resp = requests.post(JUPITER_SWAP_URL, json=payload, timeout=20)
        resp.raise_for_status()
        swap_data = resp.json()
    except Exception as exc:
        log.error("Jupiter swap tx request failed: %s", exc)
        return None

    swap_tx_b64 = swap_data.get("swapTransaction")
    if not swap_tx_b64:
        log.error("Jupiter returned no swapTransaction: %s", swap_data)
        return None

    # 2. Sign the transaction
    try:
        from sfm_wallet import sign_transaction
        signed_tx_bytes = sign_transaction(keypair, swap_tx_b64.encode())
        signed_tx_b64 = base64.b64encode(signed_tx_bytes).decode()
    except Exception as exc:
        log.error("Transaction signing failed: %s", exc)
        return None

    # 3. Submit to Solana RPC
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            signed_tx_b64,
            {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"},
        ],
    }
    try:
        rpc_resp = requests.post(SOLANA_RPC, json=rpc_payload, timeout=30)
        rpc_resp.raise_for_status()
        rpc_data = rpc_resp.json()
    except Exception as exc:
        log.error("RPC sendTransaction failed: %s", exc)
        return None

    if "error" in rpc_data:
        log.error("RPC error: %s", rpc_data["error"])
        return None

    tx_sig = rpc_data.get("result", "unknown")
    log.info("Swap submitted: %s", tx_sig)
    return {
        "status": "SUBMITTED",
        "tx_sig": tx_sig,
        "in_amount": quote.in_amount,
        "out_amount": quote.out_amount,
    }


def _paper_fill_buy(usd_amount: float, price_usd: float) -> dict:
    """Simulate a BUY fill in paper mode using live price. No Jupiter needed."""
    sfm_received = usd_amount / price_usd if price_usd > 0 else 0.0
    log.info("[PAPER] BUY $%.2f USDC -> %.0f SFM @ $%.8f (simulated)", usd_amount, sfm_received, price_usd)
    return {
        "status": "PAPER_FILL",
        "tx_sig": "paper_tx",
        "in_amount":  int(usd_amount * 10 ** USDC_DECIMALS),
        "out_amount": int(sfm_received * 10 ** SFM_DECIMALS),
    }


def _paper_fill_sell(sfm_amount: float, price_usd: float) -> dict:
    """Simulate a SELL fill in paper mode using live price. No Jupiter needed."""
    usdc_received = sfm_amount * price_usd
    log.info("[PAPER] SELL %.0f SFM -> $%.2f USDC @ $%.8f (simulated)", sfm_amount, usdc_received, price_usd)
    return {
        "status": "PAPER_FILL",
        "tx_sig": "paper_tx",
        "in_amount":  int(sfm_amount * 10 ** SFM_DECIMALS),
        "out_amount": int(usdc_received * 10 ** USDC_DECIMALS),
    }


def buy_sfm(usd_amount: float, wallet_pubkey: str = "", keypair=None,
            price_usd: float = 0.0) -> Optional[dict]:
    """High-level: BUY usd_amount USDC worth of SFM.
    In PAPER mode: simulates fill using live price — no Jupiter call.
    In LIVE mode: fetches real Jupiter quote and executes swap.
    """
    if TRADE_MODE == "PAPER":
        return _paper_fill_buy(usd_amount, price_usd)

    quote = quote_buy_sfm(usd_amount)
    if quote is None:
        return None
    if quote.price_impact_pct > 2.0:
        log.warning("High price impact %.2f%% — skipping buy", quote.price_impact_pct)
        return None
    return execute_swap(quote, wallet_pubkey, keypair)


def sell_sfm(sfm_amount: float, wallet_pubkey: str = "", keypair=None,
             price_usd: float = 0.0) -> Optional[dict]:
    """High-level: SELL sfm_amount SFM for USDC.
    In PAPER mode: simulates fill using live price — no Jupiter call.
    In LIVE mode: fetches real Jupiter quote and executes swap.
    """
    if TRADE_MODE == "PAPER":
        return _paper_fill_sell(sfm_amount, price_usd)

    quote = quote_sell_sfm(sfm_amount)
    if quote is None:
        return None
    if quote.price_impact_pct > 2.0:
        log.warning("High price impact %.2f%% — skipping sell", quote.price_impact_pct)
        return None
    return execute_swap(quote, wallet_pubkey, keypair)
