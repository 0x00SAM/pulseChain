"""Address prediction service for CREATE opcode-based contract deployments.

Implements predictive address calculation using RLP encoding and Keccak256
hashing for contracts deployed via the CREATE opcode (not CREATE2).

The formula is: address = keccak256(rlp([sender_address, nonce]))[-20:]
"""

import rlp
from eth_utils import keccak, to_checksum_address  # type: ignore[attr-defined]

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


def calculate_contract_address(sender_address: str, nonce: int) -> str:
    """Calculate the contract address that will be created by a transaction.

    Uses the standard CREATE opcode formula:
    address = keccak256(rlp.encode([sender_address, nonce]))[-20:]

    Args:
        sender_address: Address of the contract creator (factory).
        nonce: Transaction nonce of the creator.

    Returns:
        Predicted contract address (checksummed).

    Example:
        >>> calculate_contract_address(
        ...     "0x6538A83a81d855B965983161AF6a83e616D16fD5",
        ...     42
        ... )
        '0x...'
    """
    # Remove 0x prefix and convert to bytes
    sender_bytes = bytes.fromhex(sender_address[2:] if sender_address.startswith("0x") else sender_address)
    
    # RLP encode the sender address and nonce
    # rlp.encode expects a list/tuple
    encoded = rlp.encode([sender_bytes, nonce])
    
    # Calculate keccak256 hash
    hash_bytes = keccak(encoded)
    
    # Take the last 20 bytes as the address
    address_bytes = hash_bytes[-20:]
    
    # Convert to hex string with 0x prefix and checksum
    address = to_checksum_address("0x" + address_bytes.hex())
    
    logger.debug(
        "contract_address_calculated",
        extra={
            "sender": sender_address,
            "nonce": nonce,
            "predicted_address": address,
        },
    )
    
    return address

