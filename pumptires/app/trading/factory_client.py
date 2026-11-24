"""Factory client for encoding buy/sell/approve calldata and quoting.

This module provides the FactoryClient class for interacting with the PumpTires
Factory contract, including encoding calldata for buy/sell/approve operations
and calling quote functions.
"""

from typing import Any

from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.contract import AsyncContract

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)

# Minimal ERC-20 ABI for approve function
ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]


class FactoryClient:
    """Client for encoding transactions and calling view functions on the Factory.

    Provides methods to encode calldata for buy/sell/approve operations and
    to call quote functions (getBuyAmountOut, getSellAmountOut) on the
    PumpTires Factory contract.

    Attributes:
        factory_contract: The AsyncContract instance for the Factory.
        http_web3: AsyncWeb3 instance for HTTP RPC calls.
    """

    def __init__(
        self,
        factory_contract: AsyncContract,
        http_web3: AsyncWeb3,
    ) -> None:
        """Initialize FactoryClient with factory contract and Web3 instance.

        Args:
            factory_contract: AsyncContract instance for PumpTires Factory.
            http_web3: AsyncWeb3 instance for HTTP RPC calls.
        """
        self._factory_contract = factory_contract
        self._http_web3 = http_web3

        logger.info(
            "factory_client_initialized",
            extra={"factory_address": factory_contract.address},
        )

    @property
    def factory_address(self) -> str:
        """Get factory contract address.

        Returns:
            Factory contract address as checksummed string.
        """
        return self._factory_contract.address

    def encode_buy(self, token: str, min_tokens: int) -> HexBytes:
        """Encode calldata for buy function (0x58bbe38e).

        Args:
            token: Token address to buy (checksummed or not).
            min_tokens: Minimum tokens to receive (slippage protection).

        Returns:
            Encoded calldata as HexBytes with 0x58bbe38e prefix.
        """
        checksum_token = self._http_web3.to_checksum_address(token)
        
        # Manual encoding since ABI has function name as "0x58bbe38e" (hex string)
        # which web3.py would incorrectly treat as a literal function name
        selector = bytes.fromhex("58bbe38e")
        
        # Encode parameters: address (32 bytes) + uint256 (32 bytes)
        token_bytes = bytes.fromhex(checksum_token[2:].zfill(64))
        min_tokens_bytes = min_tokens.to_bytes(32, byteorder="big")
        
        calldata = HexBytes(selector + token_bytes + min_tokens_bytes)

        logger.debug(
            "factory_client_buy_encoded",
            extra={
                "token": token,
                "min_tokens": min_tokens,
                "calldata_length": len(calldata),
            },
        )

        return calldata

    def encode_sell(self, token: str, amount: int, min_eth: int) -> HexBytes:
        """Encode calldata for sellToken function.

        Args:
            token: Token address to sell (checksummed or not).
            amount: Amount of tokens to sell.
            min_eth: Minimum ETH/PLS to receive (slippage protection).

        Returns:
            Encoded calldata as HexBytes.
        """
        checksum_token = self._http_web3.to_checksum_address(token)
        
        sell_function = self._factory_contract.functions.sellToken(
            checksum_token,
            amount,
            min_eth,
        )
        calldata = HexBytes(sell_function._encode_transaction_data())

        logger.debug(
            "factory_client_sell_encoded",
            extra={
                "token": token,
                "amount": amount,
                "min_eth": min_eth,
                "calldata_length": len(calldata),
            },
        )

        return calldata

    def encode_approve(self, token_addr: str, spender: str, amount: int) -> HexBytes:
        """Encode calldata for ERC-20 approve function.

        Args:
            token_addr: Token contract address (checksummed or not).
            spender: Spender address (typically factory).
            amount: Amount to approve (typically MAX_UINT256).

        Returns:
            Encoded calldata as HexBytes with 0x095ea7b3 prefix.
        """
        # Create a minimal ERC-20 contract instance for encoding
        token_contract = self._http_web3.eth.contract(
            address=self._http_web3.to_checksum_address(token_addr),
            abi=ERC20_APPROVE_ABI,
        )

        checksum_spender = self._http_web3.to_checksum_address(spender)
        approve_function = token_contract.functions.approve(checksum_spender, amount)
        calldata = HexBytes(approve_function._encode_transaction_data())

        logger.debug(
            "factory_client_approve_encoded",
            extra={
                "token": token_addr,
                "spender": spender,
                "amount": amount,
                "calldata_length": len(calldata),
            },
        )

        return calldata

    async def quote_buy(self, token: str, amount_in: int) -> tuple[int, int] | None:
        """Call getBuyAmountOut to quote a buy operation.

        Best-effort call; returns None on any error or revert. Fast-path
        trading can proceed without quotes if enabled.

        Args:
            token: Token address to query (checksummed or not).
            amount_in: Amount of PLS to spend (in Wei).

        Returns:
            Tuple of (amountOut, aux) if successful, None otherwise.
        """
        try:
            checksum_token = self._http_web3.to_checksum_address(token)
            result = await self._factory_contract.functions.getBuyAmountOut(
                checksum_token, amount_in
            ).call()

            # Result should be tuple of (uint256, uint256)
            amount_out = int(result[0])
            aux = int(result[1])

            logger.debug(
                "factory_client_quote_buy_success",
                extra={
                    "token": token,
                    "amount_in": amount_in,
                    "amount_out": amount_out,
                    "aux": aux,
                },
            )

            return (amount_out, aux)

        except Exception as e:
            logger.warning(
                "factory_client_quote_buy_failed",
                extra={
                    "token": token,
                    "amount_in": amount_in,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return None

    async def quote_sell(self, token: str, amount_in: int) -> tuple[int, int] | None:
        """Call getSellAmountOut to quote a sell operation.

        Best-effort call; returns None on any error or revert. Fast-path
        trading can proceed without quotes if enabled.

        Args:
            token: Token address to query (checksummed or not).
            amount_in: Amount of tokens to sell.

        Returns:
            Tuple of (amountOut, aux) if successful, None otherwise.
        """
        try:
            checksum_token = self._http_web3.to_checksum_address(token)
            result = await self._factory_contract.functions.getSellAmountOut(
                checksum_token, amount_in
            ).call()

            # Result should be tuple of (uint256, uint256)
            amount_out = int(result[0])
            aux = int(result[1])

            logger.debug(
                "factory_client_quote_sell_success",
                extra={
                    "token": token,
                    "amount_in": amount_in,
                    "amount_out": amount_out,
                    "aux": aux,
                },
            )

            return (amount_out, aux)

        except Exception as e:
            logger.warning(
                "factory_client_quote_sell_failed",
                extra={
                    "token": token,
                    "amount_in": amount_in,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return None
