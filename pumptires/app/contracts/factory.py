"""PumpTires factory contract helper for decoding and interaction.

Provides utilities for loading the contract ABI, decoding createToken transactions,
and interacting with the factory contract.
"""

import json
from pathlib import Path
from typing import Any

from web3 import AsyncWeb3
from web3.contract import AsyncContract

from pumptires.app.config.settings import settings
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


def load_factory_abi() -> list[dict[str, Any]]:
    """Load the PumpTires factory contract ABI from JSON file.

    Returns:
        Contract ABI as a list of dictionaries.

    Raises:
        FileNotFoundError: If the ABI file is not found.
        json.JSONDecodeError: If the ABI file is invalid JSON.
    """
    abi_path = (
        Path(__file__).parent / "abi" / "pumptires" / "PumpTiresFactory.json"
    )
    
    try:
        with open(abi_path, "r", encoding="utf-8") as f:
            abi = json.load(f)
        logger.debug(
            "factory_abi_loaded",
            extra={"abi_path": str(abi_path), "functions": len(abi)},
        )
        return abi
    except FileNotFoundError:
        logger.error(
            "factory_abi_not_found",
            extra={"abi_path": str(abi_path)},
        )
        raise
    except json.JSONDecodeError as e:
        logger.error(
            "factory_abi_invalid_json",
            extra={"abi_path": str(abi_path), "error": str(e)},
        )
        raise


def get_factory_contract(web3: AsyncWeb3) -> AsyncContract:
    """Create a Web3 contract instance for the PumpTires factory.

    Args:
        web3: AsyncWeb3 instance.

    Returns:
        AsyncContract instance for the factory.
    """
    abi = load_factory_abi()
    checksum_address = web3.to_checksum_address(settings.factory_address)
    contract = web3.eth.contract(address=checksum_address, abi=abi)
    
    logger.info(
        "factory_contract_initialized",
        extra={"factory_address": checksum_address},
    )
    return contract


def decode_create_token_input(
    contract: AsyncContract, calldata: str
) -> tuple[str, str] | None:
    """Decode createToken transaction calldata to extract name and symbol.

    Args:
        contract: Factory contract instance with ABI.
        calldata: Transaction calldata (hex string with 0x prefix).

    Returns:
        Tuple of (token_name, token_symbol) if successful, None otherwise.
    """
    try:
        # Decode the function input
        function_obj, decoded_args = contract.decode_function_input(calldata)
        
        # Verify it's the createToken function
        if function_obj.fn_name != "createToken":
            logger.warning(
                "unexpected_function_decoded",
                extra={
                    "expected": "createToken",
                    "actual": function_obj.fn_name,
                },
            )
            return None
        
        # Extract name and symbol
        token_name = decoded_args.get("name", "")
        token_symbol = decoded_args.get("symbol", "")
        
        logger.debug(
            "create_token_decoded",
            extra={
                "token_name": token_name,
                "token_symbol": token_symbol,
            },
        )
        
        return token_name, token_symbol
        
    except Exception as e:
        logger.exception(
            "decode_create_token_failed",
            extra={
                "calldata": calldata[:66] if len(calldata) > 66 else calldata,
                "error": str(e),
            },
        )
        return None


def is_create_token_transaction(tx_data: str | bytes) -> bool:
    """Check if transaction data starts with createToken function selector.

    Args:
        tx_data: Transaction data field (hex string or bytes).

    Returns:
        True if the transaction calls createToken, False otherwise.
    """
    if not tx_data:
        return False
    
    # Convert bytes to hex string if needed
    if isinstance(tx_data, bytes):
        tx_data = '0x' + tx_data.hex()
    
    if len(tx_data) < 10:
        return False
    
    # Normalize to lowercase for comparison and handle with/without 0x prefix
    tx_selector = tx_data[:10].lower()
    expected_selector = settings.create_token_selector.lower()
    
    # Handle case where tx_data doesn't have 0x prefix but our selector does
    if not tx_data.startswith('0x') and expected_selector.startswith('0x'):
        tx_selector = '0x' + tx_data[:8].lower()
    
    # Handle case where tx_data has 0x prefix but our selector doesn't
    if tx_data.startswith('0x') and not expected_selector.startswith('0x'):
        tx_selector = tx_data[2:10].lower()
    
    return tx_selector == expected_selector


def calculate_gas_price_in_beats(tx: dict[str, Any]) -> int:
    """Calculate effective gas price in Beats from transaction.

    For EIP-1559 transactions, this is maxFeePerGas.
    For legacy transactions, this is gasPrice.

    Args:
        tx: Transaction dictionary from web3.

    Returns:
        Gas price in Beats (Wei).
    """
    # EIP-1559 transaction
    if "maxFeePerGas" in tx and tx["maxFeePerGas"] is not None:
        return tx["maxFeePerGas"]
    
    # Legacy transaction
    if "gasPrice" in tx and tx["gasPrice"] is not None:
        return tx["gasPrice"]
    
    return 0


def format_gas_price(beats: int) -> dict[str, Any]:
    """Format gas price in different units for display.

    1 PLS = 1,000,000,000 Beats

    Args:
        beats: Gas price in Beats (Wei).

    Returns:
        Dictionary with formatted values in different units.
    """
    pls = beats / 1_000_000_000
    gwei_equivalent = beats / 1_000_000_000  # Same as PLS
    
    return {
        "beats": beats,
        "pls": round(pls, 9),
        "gwei": round(gwei_equivalent, 9),
    }

