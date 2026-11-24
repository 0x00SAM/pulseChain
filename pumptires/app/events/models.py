"""TypedDict event models for trading triggers.

This module defines the event schemas emitted by PendingTransactionProcessor
(create_token_detected) and TokenCreatedReconciler (token_created) for
consumption by the TradingRunner.
"""

from typing import TypedDict


class CreateTokenDetected(TypedDict):
    """Event emitted when a pending createToken transaction is detected.

    Attributes:
        event_type: Always "create_token_detected".
        timestamp: ISO 8601 UTC timestamp.
        tx_hash: Transaction hash (0x-prefixed hex string).
        sender: Sender address (0x-prefixed hex string).
        token_name: Token name from decoded calldata.
        token_symbol: Token symbol from decoded calldata.
        gas_price_beats: Gas price in Beats (string representation).
        gas_price_pls: Gas price in PLS (human-readable string).
        gas_price_gwei: Gas price in Gwei (human-readable string).
        predicted_token_address: Predicted token address (0x-prefixed) or None.
        factory_creation_nonce: Factory nonce used for prediction or None.
        prediction_source: Source of prediction ("RPC" or "tracker") or None.
        tx_value: Transaction value in Wei (string representation) or None.
        block_number: Block number if already mined, otherwise None.
    """

    event_type: str
    timestamp: str
    tx_hash: str
    sender: str | None
    token_name: str
    token_symbol: str
    gas_price_beats: str
    gas_price_pls: str
    gas_price_gwei: str
    predicted_token_address: str | None
    factory_creation_nonce: int | None
    prediction_source: str | None
    tx_value: str | None
    block_number: int | None


class TokenCreated(TypedDict):
    """Event emitted when a TokenCreated log is confirmed on-chain.

    Attributes:
        event_type: Always "token_created".
        timestamp: ISO 8601 UTC timestamp.
        tx_hash: Transaction hash (0x-prefixed hex string).
        block_number: Block number where log was emitted.
        log_index: Log index within the block.
        factory: Factory contract address (0x-prefixed hex string).
        token_name: Token name from TokenCreated log.
        token_symbol: Token symbol from TokenCreated log.
        address_0: First address from TokenCreated log (0x-prefixed hex string).
        address_1: Second address from TokenCreated log (0x-prefixed hex string).
        predicted_token_address: Predicted token address (0x-prefixed) or None.
        prediction_match: Boolean indicating if prediction matched address_0.
    """

    event_type: str
    timestamp: str
    tx_hash: str
    block_number: int
    log_index: int
    factory: str
    token_name: str
    token_symbol: str
    address_0: str
    address_1: str
    predicted_token_address: str | None
    prediction_match: bool | None
