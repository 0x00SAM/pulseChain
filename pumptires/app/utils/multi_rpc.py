"""Multi-RPC broadcasting utilities for transaction propagation.

This module provides functions to broadcast signed transactions to multiple RPC
endpoints for better propagation and reliability. Also includes verification helpers.
"""

import asyncio
from typing import Any

from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


async def broadcast_to_multiple_rpcs(
    signed_tx: HexBytes, rpc_urls: list[str]
) -> tuple[str, list[str]]:
    """Broadcast signed transaction to multiple RPC endpoints for better propagation.

    Args:
        signed_tx: Signed raw transaction bytes.
        rpc_urls: List of RPC endpoint URLs to broadcast to.

    Returns:
        Tuple of (tx_hash_hex, list_of_successful_endpoints).

    Raises:
        Exception: If transaction fails to broadcast to all endpoints.
    """
    tx_hash_hex = None
    successful_rpcs = []
    errors = []  # Track errors for debugging

    for rpc_url in rpc_urls:
        web3_instance = None
        try:
            provider = AsyncHTTPProvider(rpc_url)
            web3_instance = AsyncWeb3(provider)
            tx_hash = await web3_instance.eth.send_raw_transaction(signed_tx)
            if tx_hash_hex is None:
                tx_hash_hex = tx_hash.hex()
            successful_rpcs.append(rpc_url)
            logger.debug(
                "Transaction broadcast successful",
                extra={"rpc_url": rpc_url, "tx_hash": tx_hash.hex()},
            )
        except Exception as e:
            # Ignore errors from individual RPCs (already sent or nonce used is OK)
            error_msg = str(e).lower()
            if "already known" in error_msg or "nonce too low" in error_msg:
                # Transaction already propagated, count as success
                successful_rpcs.append(rpc_url)
                logger.debug(
                    "Transaction already known at RPC",
                    extra={"rpc_url": rpc_url, "error": str(e)},
                )
            else:
                # Track actual errors for debugging
                errors.append(f"{rpc_url}: {e}")
                logger.warning(
                    "Transaction broadcast failed to RPC",
                    extra={"rpc_url": rpc_url, "error": str(e)},
                )
        finally:
            # Cleanup web3 provider to close aiohttp sessions
            if web3_instance:
                try:
                    await web3_instance.provider.disconnect()
                except Exception:
                    pass

    if tx_hash_hex is None:
        error_details = (
            "\n  ".join(errors) if errors else "No specific errors captured"
        )
        raise Exception(
            f"Failed to broadcast to any RPC endpoint. Errors:\n  {error_details}"
        )

    logger.info(
        "Multi-RPC broadcast completed",
        extra={
            "tx_hash": tx_hash_hex,
            "successful_rpcs": len(successful_rpcs),
            "total_rpcs": len(rpc_urls),
            "rpcs": successful_rpcs,
        },
    )

    return tx_hash_hex, successful_rpcs


async def verify_tx_propagation(
    web3: AsyncWeb3, tx_hash: str, max_wait: int = 5
) -> bool:
    """Verify transaction is visible in mempool after submission.

    Args:
        web3: Web3 instance to query.
        tx_hash: Transaction hash to verify.
        max_wait: Maximum seconds to wait for propagation.

    Returns:
        True if tx found in mempool, False otherwise.
    """
    for attempt in range(max_wait):
        try:
            tx = await web3.eth.get_transaction(HexBytes(tx_hash))
            if tx:
                logger.debug(
                    "Transaction propagation verified",
                    extra={"tx_hash": tx_hash, "attempt": attempt + 1},
                )
                return True
        except Exception as e:
            logger.debug(
                "Transaction not yet propagated",
                extra={"tx_hash": tx_hash, "attempt": attempt + 1, "error": str(e)},
            )
        await asyncio.sleep(1)

    logger.warning(
        "Transaction propagation verification timeout",
        extra={"tx_hash": tx_hash, "max_wait": max_wait},
    )
    return False


async def check_tx_status(web3: AsyncWeb3, tx_hash: str) -> dict[str, Any]:
    """Check transaction status: confirmed, pending, or not found.

    Args:
        web3: Web3 instance to query.
        tx_hash: Transaction hash to check.

    Returns:
        Dictionary with transaction status information.
    """
    try:
        # Try to get receipt (confirmed tx)
        receipt = await web3.eth.get_transaction_receipt(HexBytes(tx_hash))
        if receipt:
            status = receipt.get("status")
            return {
                "state": "confirmed",
                "success": status == 1,
                "block": receipt.get("blockNumber"),
                "gas_used": receipt.get("gasUsed"),
            }
    except Exception:
        pass

    try:
        # Try to get pending tx
        tx = await web3.eth.get_transaction(HexBytes(tx_hash))
        if tx:
            return {"state": "pending", "tx": tx}
    except Exception:
        pass

    return {"state": "not_found"}
