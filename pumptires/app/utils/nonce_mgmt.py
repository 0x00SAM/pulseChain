"""Nonce management and stuck transaction handling utilities.

This module provides production-ready nonce management including:
- Backlog detection and clearing
- txpool validation for phantom nonces
- Stuck transaction replacement
- Safe nonce calculation
"""

import asyncio
from typing import Any

from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.types import Wei

from pumptires.app.trading.wallet import TradingWallet
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


async def check_nonce_backlog(
    web3: AsyncWeb3, wallet_address: str
) -> tuple[int, int, int]:
    """Check for nonce backlog (stuck/pending transactions).

    Args:
        web3: AsyncWeb3 instance.
        wallet_address: Wallet address to check.

    Returns:
        Tuple of (latest_nonce, pending_nonce, backlog_count).
    """
    latest_nonce = await web3.eth.get_transaction_count(wallet_address, "latest")  # type: ignore
    pending_nonce = await web3.eth.get_transaction_count(wallet_address, "pending")  # type: ignore
    backlog = pending_nonce - latest_nonce

    logger.info(
        "Nonce backlog check",
        extra={
            "wallet": wallet_address,
            "latest_nonce": latest_nonce,
            "pending_nonce": pending_nonce,
            "backlog": backlog,
        },
    )

    return latest_nonce, pending_nonce, backlog


async def validate_pending_nonce_with_txpool(
    web3: AsyncWeb3, wallet_address: str, pending_nonce: int, latest_nonce: int
) -> bool:
    """Validate pending nonce using txpool to detect phantom increments.

    Some RPCs incorrectly return pending = latest + 1 even when no txs exist.
    This checks txpool_content to verify actual pending transactions.

    Args:
        web3: AsyncWeb3 instance.
        wallet_address: Wallet address to check.
        pending_nonce: Pending nonce from RPC.
        latest_nonce: Latest nonce from RPC.

    Returns:
        True if should seed from latest (phantom detected), False otherwise.
    """
    try:
        # Only check if pending is exactly latest + 1 (suspicious)
        if (pending_nonce - latest_nonce) != 1:
            return False

        txpool = await web3.provider.make_request("txpool_content", [])
        pending_map = txpool.get("result", {}).get("pending", {}) or {}

        # Check if our address has actual pending transactions
        addr_lower = wallet_address.lower()
        has_txpool_entries = (
            addr_lower in pending_map
            and isinstance(pending_map.get(addr_lower), dict)
            and len(pending_map.get(addr_lower, {})) > 0
        )

        if not has_txpool_entries:
            logger.warning(
                "Phantom pending nonce detected",
                extra={
                    "wallet": wallet_address,
                    "pending_nonce": pending_nonce,
                    "latest_nonce": latest_nonce,
                    "txpool_has_entries": False,
                },
            )
            return True  # Seed from latest

        logger.debug(
            "Pending nonce validated via txpool",
            extra={
                "wallet": wallet_address,
                "txpool_entries": len(pending_map.get(addr_lower, {})),
            },
        )
        return False

    except Exception as e:
        logger.warning(
            "txpool validation failed, assuming pending nonce is valid",
            extra={"wallet": wallet_address, "error": str(e)},
        )
        return False


async def clear_stuck_nonce(
    web3: AsyncWeb3,
    wallet: TradingWallet,
    stuck_nonce: int,
    base_fee: int,
) -> bool:
    """Clear a stuck transaction by replacing it with a high-gas no-op transaction.

    Args:
        web3: AsyncWeb3 instance.
        wallet: TradingWallet instance.
        stuck_nonce: The nonce to clear.
        base_fee: Current base fee in beats.

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Build aggressive replacement: send 0 PLS to self with high gas
        # Must use HIGHER gas than stuck tx to replace it
        aggressive_priority_fee = max(int(base_fee * 2.0), int(10000 * 10**12))  # At least 10,000 Gwei
        aggressive_max_fee = int(base_fee * 5.0) + aggressive_priority_fee

        clear_tx = wallet.build_base_tx(
            to=wallet.address,  # Send to self
            value=Wei(0),
            data=HexBytes("0x"),
            nonce=stuck_nonce,
            gas=21_000,  # Minimum gas for transfer
            max_fee_per_gas=aggressive_max_fee,
            max_priority_fee_per_gas=aggressive_priority_fee,
        )

        signed_clear = await wallet.sign_transaction(clear_tx)
        clear_hash = await web3.eth.send_raw_transaction(signed_clear)

        logger.info(
            "Stuck nonce replacement submitted",
            extra={
                "stuck_nonce": stuck_nonce,
                "clear_tx_hash": clear_hash.hex(),
                "aggressive_priority_fee_gwei": aggressive_priority_fee / 10**12,
                "aggressive_max_fee_gwei": aggressive_max_fee / 10**12,
            },
        )

        # Wait briefly for propagation
        await asyncio.sleep(2)

        return True

    except Exception as e:
        logger.error(
            "Failed to clear stuck nonce",
            extra={"stuck_nonce": stuck_nonce, "error": str(e)},
            exc_info=True,
        )
        return False


async def safe_nonce_seed(
    web3: AsyncWeb3, wallet: TradingWallet, check_txpool: bool = True
) -> None:
    """Safely seed wallet nonce with backlog and phantom detection.

    This is the production-ready nonce seeding that should be called:
    1. On startup
    2. After clearing stuck transactions
    3. After any nonce-related errors

    Args:
        web3: AsyncWeb3 instance.
        wallet: TradingWallet instance.
        check_txpool: Whether to validate pending nonce with txpool.
    """
    latest_nonce, pending_nonce, backlog = await check_nonce_backlog(
        web3, wallet.address
    )

    # Detect phantom pending nonce
    seed_from_latest = False
    if check_txpool:
        seed_from_latest = await validate_pending_nonce_with_txpool(
            web3, wallet.address, pending_nonce, latest_nonce
        )

    # Seed wallet
    await wallet.seed_nonce(use_latest=seed_from_latest)

    logger.info(
        "Wallet nonce seeded",
        extra={
            "wallet": wallet.address,
            "seed_source": "latest" if seed_from_latest else "pending",
            "nonce": wallet._nonce,
            "backlog": backlog,
        },
    )


async def handle_nonce_error_with_retry(
    web3: AsyncWeb3,
    wallet: TradingWallet,
    base_fee: int,
    error_msg: str,
    current_nonce: int,
) -> bool:
    """Handle nonce-related errors by clearing stuck nonce and reseeding.

    Args:
        web3: AsyncWeb3 instance.
        wallet: TradingWallet instance.
        base_fee: Current base fee in beats.
        error_msg: Error message from transaction submission.
        current_nonce: The nonce that failed.

    Returns:
        True if nonce was cleared and wallet reseeded, False otherwise.
    """
    error_lower = error_msg.lower()

    # Check for nonce-related errors
    if "nonce too low" in error_lower or "replacement transaction underpriced" in error_lower:
        logger.warning(
            "Nonce error detected, attempting recovery",
            extra={
                "error": error_msg,
                "failed_nonce": current_nonce,
            },
        )

        # Clear stuck nonce
        if await clear_stuck_nonce(web3, wallet, current_nonce, base_fee):
            # Reseed wallet (use pending after clearing)
            await safe_nonce_seed(web3, wallet, check_txpool=True)
            return True
        else:
            logger.error("Failed to recover from nonce error")
            return False

    return False
