"""Trading wallet for transaction signing and nonce management.

This module provides the TradingWallet class for managing a trading account,
including loading private keys, tracking nonces locally, signing transactions,
and building EIP-1559 transaction dictionaries.
"""

from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import AsyncWeb3

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class TradingWallet:
    """Manages trading wallet operations including signing and nonce tracking.

    Loads an Ethereum account from a private key and maintains local nonce state
    for rapid back-to-back transaction submissions without HTTP round trips.

    Attributes:
        account: The eth_account LocalAccount instance.
        address: Checksummed Ethereum address.
        chain_id: PulseChain chain ID (default 369).
    """

    def __init__(
        self,
        private_key: str,
        http_web3: AsyncWeb3,
        chain_id: int = 369,
    ) -> None:
        """Initialize trading wallet with private key.

        Args:
            private_key: Hex-encoded private key (with or without 0x prefix).
            http_web3: AsyncWeb3 instance for HTTP RPC calls.
            chain_id: Chain ID for transaction signing (default 369 for PulseChain).

        Raises:
            ValueError: If private key is invalid.
        """
        self._http_web3 = http_web3
        self._chain_id = chain_id
        self._nonce: int | None = None

        # Load account from private key
        # Account.from_key handles both 0x-prefixed and non-prefixed keys
        try:
            self._account: LocalAccount = Account.from_key(private_key)
        except Exception as e:
            logger.error(
                "trading_wallet_init_failed",
                extra={"error": str(e), "reason": "invalid_private_key"},
            )
            raise ValueError(f"Invalid private key: {e}") from e

        logger.info(
            "trading_wallet_initialized",
            extra={"address": self._account.address, "chain_id": chain_id},
        )

    @property
    def account(self) -> LocalAccount:
        """Get the LocalAccount instance.

        Returns:
            The eth_account LocalAccount instance.
        """
        return self._account

    @property
    def address(self) -> str:
        """Get the checksummed Ethereum address.

        Returns:
            Checksummed address string.
        """
        return self._account.address

    @property
    def chain_id(self) -> int:
        """Get the chain ID.

        Returns:
            Chain ID integer.
        """
        return self._chain_id

    async def seed_nonce(self, use_latest: bool = False) -> None:
        """Seed nonce from pending transaction count.

        Fetches the current pending nonce from the network and initializes
        local nonce tracking. Should be called once at startup or after restart.

        Args:
            use_latest: If True, use "latest" (confirmed) instead of "pending".
                       Default False (uses "pending" for real-time state).

        Raises:
            Exception: If RPC call fails.
        """
        try:
            block_param = "latest" if use_latest else "pending"
            pending_nonce = await self._http_web3.eth.get_transaction_count(
                self._account.address, block_param  # type: ignore
            )
            self._nonce = pending_nonce
            logger.info(
                "trading_wallet_nonce_seeded",
                extra={
                    "address": self._account.address,
                    "nonce": pending_nonce,
                    "block_param": block_param,
                },
            )
        except Exception as e:
            logger.exception(
                "trading_wallet_seed_nonce_failed",
                extra={"address": self._account.address, "error": str(e)},
            )
            raise

    def reserve_nonce(self) -> int:
        """Reserve and return the next nonce for transaction submission.

        Increments the local nonce counter and returns the reserved value.
        Must call seed_nonce() before first use.

        Returns:
            Next nonce value as integer.

        Raises:
            RuntimeError: If nonce not seeded yet.
        """
        if self._nonce is None:
            raise RuntimeError(
                "Nonce not seeded. Call await wallet.seed_nonce() first."
            )

        current = self._nonce
        self._nonce += 1

        logger.debug(
            "trading_wallet_nonce_reserved",
            extra={"address": self._account.address, "nonce": current},
        )

        return current

    def build_base_tx(
        self,
        *,
        to: str,
        value: int = 0,
        gas: int = 200_000,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        data: bytes = b"",
        nonce: int | None = None,
    ) -> dict[str, Any]:
        """Build an EIP-1559 transaction dictionary.

        Constructs a transaction dict ready for signing. If nonce is not provided,
        uses reserve_nonce() to get the next sequential nonce.

        Args:
            to: Recipient address (checksummed or not).
            value: Amount in Wei to send (default 0).
            gas: Gas limit (default 200,000).
            max_fee_per_gas: Maximum fee per gas in Wei.
            max_priority_fee_per_gas: Maximum priority fee (tip) in Wei.
            data: Transaction calldata as bytes (default empty).
            nonce: Transaction nonce (if None, calls reserve_nonce()).

        Returns:
            EIP-1559 transaction dictionary with fields: chainId, from, to, value,
            gas, maxFeePerGas, maxPriorityFeePerGas, data, nonce, type.

        Raises:
            RuntimeError: If nonce not seeded and nonce parameter is None.
        """
        if nonce is None:
            nonce = self.reserve_nonce()

        tx: dict[str, Any] = {
            "chainId": self._chain_id,
            "from": self._account.address,
            "to": to,
            "value": value,
            "gas": gas,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
            "data": data,
            "nonce": nonce,
            "type": 2,  # EIP-1559
        }

        return tx

    async def sign_transaction(self, tx: dict[str, Any]) -> HexBytes:
        """Sign a transaction dictionary and return raw transaction bytes.

        Args:
            tx: Transaction dictionary (must include all required EIP-1559 fields).

        Returns:
            Signed raw transaction as HexBytes ready for eth_sendRawTransaction.

        Raises:
            Exception: If signing fails.
        """
        try:
            # Remove 'from' field if present (not needed for signing)
            signing_tx = {k: v for k, v in tx.items() if k != "from"}
            
            signed = self._account.sign_transaction(signing_tx)
            logger.debug(
                "trading_wallet_transaction_signed",
                extra={
                    "address": self._account.address,
                    "nonce": tx.get("nonce"),
                    "to": tx.get("to"),
                },
            )
            # SignedTransaction has raw_transaction attribute
            return HexBytes(signed.raw_transaction)
        except Exception as e:
            logger.exception(
                "trading_wallet_sign_transaction_failed",
                extra={
                    "address": self._account.address,
                    "nonce": tx.get("nonce"),
                    "error": str(e),
                },
            )
            raise
