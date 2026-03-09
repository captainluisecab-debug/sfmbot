"""
sfm_wallet.py — Phantom wallet keypair loader using solders.

Supports two import formats:
  1. Base58 string (Phantom "Export Private Key" → copy string)
  2. JSON array of bytes (Solana CLI / Phantom backup format)

The keypair is used only for signing Jupiter swap transactions.
In PAPER mode this module is never called.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("sfm_wallet")


def load_keypair(private_key_b58: str):
    """
    Load a solders Keypair from a base58-encoded private key string.
    Returns a solders.keypair.Keypair instance.
    Raises RuntimeError if solders is not installed or key is invalid.
    """
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "solders not installed — run: pip install solders"
        ) from exc

    if not private_key_b58:
        raise RuntimeError("PHANTOM_PRIVATE_KEY is empty — cannot load keypair")

    # Try base58 string (64 bytes / 88 chars typically)
    try:
        import base58  # type: ignore
        key_bytes = base58.b58decode(private_key_b58)
        if len(key_bytes) == 64:
            return Keypair.from_bytes(key_bytes)
        if len(key_bytes) == 32:
            return Keypair.from_seed(key_bytes)
    except Exception:
        pass

    # Try JSON array of ints
    try:
        arr = json.loads(private_key_b58)
        if isinstance(arr, list) and len(arr) == 64:
            return Keypair.from_bytes(bytes(arr))
    except Exception:
        pass

    raise RuntimeError(
        "Could not decode PHANTOM_PRIVATE_KEY — "
        "expected base58 string or JSON array of 64 ints"
    )


def public_key_str(keypair) -> str:
    """Return the public key as a base58 string."""
    return str(keypair.pubkey())


def sign_transaction(keypair, transaction_bytes: bytes) -> bytes:
    """
    Sign a raw transaction message with the keypair.
    Returns signed transaction bytes ready to submit.

    Note: Jupiter returns a base64-encoded transaction that needs to be
    decoded, signed, and re-serialized. This helper is called by sfm_broker.
    """
    try:
        from solders.transaction import Transaction  # type: ignore
        from solders.message import Message  # type: ignore
        import base64

        tx = Transaction.from_bytes(base64.b64decode(transaction_bytes))
        tx.sign([keypair], tx.message.recent_blockhash)
        return bytes(tx)
    except Exception as exc:
        raise RuntimeError(f"Transaction signing failed: {exc}") from exc
