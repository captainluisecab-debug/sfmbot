"""
solana_pairs.py — Pair definitions for the multi-pair Solana engine.

Each pair has: mints, decimals, pool address for candles, strategy type,
sizing rules, and slippage tolerance.
"""
from __future__ import annotations
from dataclasses import dataclass

# Solana token mints
SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SFM_MINT  = "ELPrcU7qRV3DUz8AP6siTE7GkR3gkkBvGmgBRiLnC19Y"
JITOSOL_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
JUP_MINT     = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
PYTH_MINT    = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
BONK_MINT    = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@dataclass
class PairConfig:
    name: str
    base_mint: str
    quote_mint: str
    base_decimals: int
    quote_decimals: int
    strategy_type: str       # "swing" | "yield" | "tactical"
    max_allocation_pct: float  # max % of total capital
    slippage_bps: int
    stop_loss_pct: float
    take_profit_pct: float
    rsi_oversold: float
    rsi_overbought: float
    ema_period: int
    min_score: float         # minimum signal score to enter
    enabled: bool


JITOSOL_POOL = "AxHPCGeEMXfZMqGdUWSiGiGMdR9sCKJFhWDnBmqy5K6L"  # JitoSOL/SOL Orca pool

PAIR_CONFIGS = {
    "SOL/USDC": PairConfig(
        name="SOL/USDC",
        base_mint=SOL_MINT,
        quote_mint=USDC_MINT,
        base_decimals=9,
        quote_decimals=6,
        strategy_type="swing",
        max_allocation_pct=60.0,
        slippage_bps=50,
        stop_loss_pct=4.0,
        take_profit_pct=8.0,
        rsi_oversold=30.0,
        rsi_overbought=72.0,
        ema_period=20,
        min_score=60.0,
        enabled=True,
    ),
    "JITOSOL/USDC": PairConfig(
        name="JITOSOL/USDC",
        base_mint=JITOSOL_MINT,
        quote_mint=USDC_MINT,
        base_decimals=9,
        quote_decimals=6,
        strategy_type="swing",
        max_allocation_pct=15.0,
        slippage_bps=50,
        stop_loss_pct=4.0,
        take_profit_pct=8.0,
        rsi_oversold=30.0,
        rsi_overbought=72.0,
        ema_period=20,
        min_score=60.0,
        enabled=True,
    ),
    "JUP/USDC": PairConfig(
        name="JUP/USDC",
        base_mint=JUP_MINT,
        quote_mint=USDC_MINT,
        base_decimals=6,
        quote_decimals=6,
        strategy_type="swing",
        max_allocation_pct=15.0,
        slippage_bps=80,
        stop_loss_pct=4.0,
        take_profit_pct=8.0,
        rsi_oversold=30.0,
        rsi_overbought=72.0,
        ema_period=20,
        min_score=60.0,
        enabled=True,
    ),
    "PYTH/USDC": PairConfig(
        name="PYTH/USDC",
        base_mint=PYTH_MINT,
        quote_mint=USDC_MINT,
        base_decimals=6,
        quote_decimals=6,
        strategy_type="swing",
        max_allocation_pct=15.0,
        slippage_bps=80,
        stop_loss_pct=4.0,
        take_profit_pct=8.0,
        rsi_oversold=30.0,
        rsi_overbought=72.0,
        ema_period=20,
        min_score=60.0,
        enabled=True,
    ),
    "BONK/USDC": PairConfig(
        name="BONK/USDC",
        base_mint=BONK_MINT,
        quote_mint=USDC_MINT,
        base_decimals=5,
        quote_decimals=6,
        strategy_type="swing",
        max_allocation_pct=15.0,
        slippage_bps=100,
        stop_loss_pct=5.0,
        take_profit_pct=8.0,
        rsi_oversold=30.0,
        rsi_overbought=72.0,
        ema_period=20,
        min_score=60.0,
        enabled=True,
    ),
    "SFM/USDC": PairConfig(
        name="SFM/USDC",
        base_mint=SFM_MINT,
        quote_mint=USDC_MINT,
        base_decimals=6,
        quote_decimals=6,
        strategy_type="tactical",
        max_allocation_pct=15.0,
        slippage_bps=150,
        stop_loss_pct=5.0,
        take_profit_pct=8.0,
        rsi_oversold=30.0,
        rsi_overbought=75.0,
        ema_period=20,
        min_score=50.0,
        enabled=True,
    ),
}
