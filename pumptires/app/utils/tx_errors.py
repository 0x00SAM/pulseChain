"""Transaction error utilities for decoding and handling revert reasons.

Provides utilities to decode custom error selectors and determine if
errors are retriable in the context of mempool warfare trading.
"""

import logging
from typing import Any, cast

from web3 import AsyncWeb3
from web3.exceptions import ContractLogicError
from web3.types import TxParams, Wei

logger = logging.getLogger(__name__)

# Known custom error selectors from PumpTires contracts
ERROR_SELECTORS = {
    "0xceea21b6": "TokenNotGraduated",  # Token deployed but not graduated yet
    "0x3d693ada": "InsufficientInput",  # Input amount too low
    "0x3b79867e": "InsufficientLiquidity",  # Not enough liquidity
    "0x7939f424": "TransferFromFailed",  # ERC20 transfer failed
}

# Errors that are retriable (may succeed on subsequent attempts)
RETRIABLE_ERRORS = {
    "TokenNotGraduated",  # Token may graduate in next block
}


def decode_revert_reason(error_data: str) -> str | None:
    """Decode revert reason from error data.

    Supports:
    - Custom error selectors (4-byte function signatures)
    - String revert reasons
    - Raw hex data

    Args:
        error_data: Hex-encoded error data from failed transaction.

    Returns:
        Human-readable error name or None if unknown.
    """
    if not error_data or not isinstance(error_data, str):
        return None

    # Normalize to lowercase hex
    error_data = error_data.lower()
    if error_data.startswith("0x"):
        error_data = error_data[2:]

    # Extract first 4 bytes (8 hex chars) as selector
    if len(error_data) >= 8:
        selector = "0x" + error_data[:8]
        if selector in ERROR_SELECTORS:
            return ERROR_SELECTORS[selector]

    return None


def is_retriable_error(error_name: str | None) -> bool:
    """Check if an error is retriable.

    Args:
        error_name: Decoded error name from decode_revert_reason.

    Returns:
        True if error is known to be retriable, False otherwise.
    """
    return error_name in RETRIABLE_ERRORS if error_name else False


async def get_transaction_revert_reason(
    web3: AsyncWeb3, tx_hash: str
) -> str | None:
    """Get revert reason from a failed transaction.

    Fetches transaction receipt and attempts to decode revert reason
    from the transaction data.

    Args:
        web3: AsyncWeb3 instance for blockchain queries.
        tx_hash: Transaction hash to query.

    Returns:
        Decoded error name or None if not found/unknown.
    """
    try:
        # Get transaction receipt
        receipt = await web3.eth.get_transaction_receipt(cast(Any, tx_hash))
        
        # Check if transaction succeeded
        if receipt.get("status") == 1:
            return None  # Transaction succeeded
        
        # Get transaction details
        tx = await web3.eth.get_transaction(cast(Any, tx_hash))
        
        # Try to call the transaction to get revert reason
        try:
            # Build transaction parameters, ensuring all required fields are present
            tx_from = tx.get("from")
            tx_to = tx.get("to")
            tx_input = tx.get("input")
            tx_value = tx.get("value", 0)
            
            if not tx_from or not tx_to or not tx_input:
                return None
            
            tx_params: TxParams = {
                "from": tx_from,
                "to": tx_to,
                "data": tx_input,
                "value": Wei(tx_value) if isinstance(tx_value, int) else tx_value,
            }
            await web3.eth.call(
                tx_params,
                receipt["blockNumber"] - 1,  # Call on block before execution
            )
        except ContractLogicError as e:
            # Extract revert reason from error message
            error_str = str(e)
            # Parse for custom error or revert string
            if "execution reverted" in error_str.lower():
                # Try to extract hex data
                if "0x" in error_str:
                    hex_start = error_str.index("0x")
                    hex_end = hex_start + 10  # Get at least selector
                    error_data = error_str[hex_start:hex_end]
                    return decode_revert_reason(error_data)
            return None
        except Exception as e:
            logger.debug(f"Could not replay transaction call: {e}")
            return None
            
    except Exception as e:
        logger.warning(f"Failed to get revert reason for {tx_hash}: {e}")
        return None

    return None


def extract_error_from_exception(exception: Exception) -> str | None:
    """Extract error information from an exception.

    Parses Web3 exceptions to find custom error selectors or revert reasons.

    Args:
        exception: Exception from Web3 transaction.

    Returns:
        Decoded error name or None if not found.
    """
    error_str = str(exception)
    
    # Look for hex data in error message
    if "0x" in error_str:
        # Find all potential hex sequences
        parts = error_str.split("0x")
        for part in parts[1:]:  # Skip first part before any "0x"
            # Take up to 8 chars for selector
            potential_selector = "0x" + part[:8]
            if len(part) >= 8:
                decoded = decode_revert_reason(potential_selector)
                if decoded:
                    return decoded
    
    # Check for known error patterns in exception type/message
    if isinstance(exception, ContractLogicError):
        return extract_error_from_exception(exception)
    
    return None
