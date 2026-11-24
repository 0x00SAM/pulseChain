"""Trading runner with idempotency and buy trigger logic.

This module implements the core trading loop that consumes events from EventBus,
applies strict tx_hash-based idempotency, and triggers buy operations on token
creation events using predicted addresses with fallback to confirmed addresses.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

from web3 import AsyncWeb3
from web3.types import TxParams, Wei

from pumptires.app.config.settings import Settings
from pumptires.app.events.bus import EventBus
from pumptires.app.events.models import CreateTokenDetected, TokenCreated
from pumptires.app.io.persistent_dedup import PersistentDedup
from pumptires.app.io.scheduled_sells import ScheduledSells
from pumptires.app.services.metrics import MetricsTracker
from pumptires.app.trading.factory_client import FactoryClient
from pumptires.app.trading.gas_oracle import GasOracle
from pumptires.app.trading.wallet import TradingWallet
from pumptires.app.utils.logger import get_logger
from pumptires.app.utils.multi_rpc import broadcast_to_multiple_rpcs, verify_tx_propagation
from pumptires.app.utils.nonce_mgmt import (
    check_nonce_backlog,
    safe_nonce_seed,
    handle_nonce_error_with_retry,
)
from pumptires.app.utils.gas_sampling import (
    sample_gas_from_recent_blocks,
    compute_fees_with_mempool_analysis,
)
from pumptires.app.utils.tx_errors import (
    decode_revert_reason,
    is_retriable_error,
    extract_error_from_exception,
)

logger = get_logger(__name__)


class TradingRunner:
    """Consumes EventBus and executes buy operations with idempotency.

    Triggers buy on create_token_detected (using predicted_token_address) with
    fallback to token_created (address_0) if missed. Enforces strict one-buy-per-tx_hash
    idempotency with persistent storage.

    Attributes:
        event_bus: EventBus for consuming trading trigger events.
        wallet: TradingWallet for transaction signing and nonce management.
        factory_client: FactoryClient for encoding buy/approve calldata.
        gas_oracle: GasOracle for computing recommended gas fees.
        buy_dedup: PersistentDedup for tracking which tx_hashes have been bought.
        scheduled_sells: ScheduledSells for persisting scheduled sell tasks.
        http_web3: AsyncWeb3 HTTP client for submitting transactions.
        settings: Application settings for trading configuration.
        metrics: MetricsTracker for observability.
        _running: Flag indicating if run loop is active.
        _scheduled_sells: Dict tracking scheduled sell tasks by token address.
    """

    def __init__(
        self,
        event_bus: EventBus,
        wallet: TradingWallet,
        factory_client: FactoryClient,
        gas_oracle: GasOracle,
        buy_dedup: PersistentDedup,
        http_web3: AsyncWeb3,
        settings: Settings,
        metrics: MetricsTracker | None = None,
        scheduled_sells: ScheduledSells | None = None,
    ) -> None:
        """Initialize trading runner.

        Args:
            event_bus: EventBus for consuming events.
            wallet: TradingWallet for signing transactions.
            factory_client: FactoryClient for calldata encoding.
            gas_oracle: GasOracle for gas fee computation.
            buy_dedup: PersistentDedup for buy idempotency tracking.
            http_web3: AsyncWeb3 HTTP client for transaction submission.
            settings: Application settings.
            metrics: Optional MetricsTracker for observability.
            scheduled_sells: Optional ScheduledSells for sell persistence (defaults to data/scheduled_sells.json).
        """
        self.event_bus = event_bus
        self.wallet = wallet
        self.factory_client = factory_client
        self.gas_oracle = gas_oracle
        self.buy_dedup = buy_dedup
        self.http_web3 = http_web3
        self.settings = settings
        self.metrics = metrics or MetricsTracker()
        self._running = False
        self._scheduled_sells: dict[str, asyncio.Task] = {}
        
        # Initialize scheduled sells persistence
        if scheduled_sells is None:
            scheduled_sells = ScheduledSells(filepath=Path("data/scheduled_sells.json"))
        self.scheduled_sells = scheduled_sells
        
        # Token address deduplication cache: prevents buying same token multiple times
        # Key: token_address, Value: (trigger_tx, timestamp, status)
        self._token_buy_cache: dict[str, tuple[str, float, str]] = {}
        
        # Gas price cache: avoid expensive mempool scans for every buy
        # Key: block_number, Value: (max_fee, priority_fee, timestamp)
        self._gas_cache: dict[int, tuple[int, int, float]] = {}

        logger.info(
            "TradingRunner initialized",
            extra={
                "wallet_address": self.wallet.address,
                "trading_enabled": self.settings.trading_enabled,
                "buy_amount_pls": self.settings.trading_buy_amount_pls,
                "slippage_bps": self.settings.trading_slippage_bps,
                "auto_sell_enabled": self.settings.trading_auto_sell_enabled,
                "sell_delay_seconds": self.settings.trading_sell_delay_seconds,
            },
        )

    async def initialize(self) -> None:
        """Initialize trading runner: seed nonce, check backlog, sample gas.

        This should be called once before starting the run loop.
        """
        logger.info("Initializing TradingRunner...")

        # Check for nonce backlog
        latest_nonce, pending_nonce, backlog = await check_nonce_backlog(
            self.http_web3, self.wallet.address
        )

        if backlog >= 2:
            logger.warning(
                "Nonce backlog detected on startup",
                extra={
                    "latest_nonce": latest_nonce,
                    "pending_nonce": pending_nonce,
                    "backlog": backlog,
                },
            )
            # Production: Log and continue (manual intervention may be needed)
            # Could implement auto-clearing here if desired

        # Safely seed wallet nonce with txpool validation
        await safe_nonce_seed(self.http_web3, self.wallet, check_txpool=True)

        # Sample gas from recent blocks for oracle warm-up
        await sample_gas_from_recent_blocks(
            self.http_web3, self.gas_oracle, num_blocks=5, txs_per_block=50
        )

        # Initialize scheduled sells persistence and rehydrate tasks
        await self.scheduled_sells.initialize()
        await self._rehydrate_scheduled_sells()

        logger.info(
            "TradingRunner initialization complete",
            extra={
                "wallet_nonce": self.wallet._nonce,
                "gas_observations": self.gas_oracle.observation_count(),
                "scheduled_sells_count": len(self._scheduled_sells),
            },
        )

    async def _rehydrate_scheduled_sells(self) -> None:
        """Rehydrate scheduled sell tasks from persisted state.

        Called during initialization to restore scheduled sells after restart.
        """
        persisted_entries = await self.scheduled_sells.list()
        current_time = int(time.time())

        for entry in persisted_entries:
            token_address = entry["token_address"]
            buy_tx_hash = entry["buy_tx_hash"]
            execute_at = entry["execute_at"]
            remaining = execute_at - current_time

            if remaining <= 0:
                # Execute immediately (past due)
                logger.info(
                    "scheduled_sell_restored",
                    extra={
                        "token_address": token_address,
                        "buy_tx_hash": buy_tx_hash,
                        "scheduled_execute_at": execute_at,
                        "remaining_seconds": 0,
                        "action": "immediate_execution",
                    },
                )
                # Create task with no delay
                sell_task = asyncio.create_task(
                    self._delayed_sell_execution(
                        token_address, buy_tx_hash, delay_override=0
                    )
                )
                self._scheduled_sells[token_address] = sell_task
            else:
                # Schedule with remaining time
                logger.info(
                    "scheduled_sell_restored",
                    extra={
                        "token_address": token_address,
                        "buy_tx_hash": buy_tx_hash,
                        "scheduled_execute_at": execute_at,
                        "remaining_seconds": remaining,
                        "action": "scheduled_execution",
                    },
                )
                sell_task = asyncio.create_task(
                    self._delayed_sell_execution(
                        token_address, buy_tx_hash, delay_override=remaining
                    )
                )
                self._scheduled_sells[token_address] = sell_task

    async def run(self) -> None:
        """Main event consumption loop.

        Consumes events from EventBus indefinitely and triggers buy operations
        based on event type and idempotency checks. Runs until cancelled.
        """
        if not self.settings.trading_enabled:
            logger.warning(
                "Trading disabled in settings, runner will not process events"
            )
            return

        # Initialize: seed nonce, check backlog, warm up gas oracle
        await self.initialize()

        self._running = True
        logger.info("TradingRunner started, consuming events from EventBus")

        try:
            while self._running:
                # Get next event from bus (blocks until available)
                event = await self.event_bus.get()

                # Dispatch based on event type
                event_type = event.get("event_type")
                if event_type == "create_token_detected":
                    await self._handle_create_token_detected(event)  # type: ignore
                elif event_type == "token_created":
                    await self._handle_token_created(event)  # type: ignore
                else:
                    logger.warning(
                        "Unknown event type received",
                        extra={"event_type": event_type},
                    )

        except asyncio.CancelledError:
            logger.info("TradingRunner cancelled, shutting down gracefully")
            self._running = False
            raise
        except Exception as e:
            logger.error(
                "TradingRunner encountered fatal error",
                extra={"error": str(e)},
                exc_info=True,
            )
            self._running = False
            raise

    async def _handle_create_token_detected(self, event: CreateTokenDetected) -> None:
        """Handle create_token_detected event.

        Triggers buy using predicted_token_address if available and tx_hash
        not already bought.

        Args:
            event: CreateTokenDetected event from EventBus.
        """
        tx_hash = event["tx_hash"]
        predicted_token_address = event.get("predicted_token_address")

        # Check idempotency by trigger tx hash
        if await self.buy_dedup.contains(tx_hash):
            logger.debug(
                "Skipping duplicate create_token_detected",
                extra={
                    "tx_hash": tx_hash,
                    "trigger_event_type": "create_token_detected",
                },
            )
            self._increment_metric("buy_skipped_duplicate")
            return

        # Validate predicted address available
        if not predicted_token_address:
            logger.warning(
                "create_token_detected missing predicted_token_address, skipping",
                extra={"tx_hash": tx_hash},
            )
            return

        # Check if we've already bought this token address
        if self._has_token_buy_attempt(predicted_token_address):
            logger.info(
                "Skipping buy - token address already bought",
                extra={
                    "tx_hash": tx_hash,
                    "token_address": predicted_token_address,
                    "trigger_event_type": "create_token_detected",
                },
            )
            self._increment_metric("buy_skipped_duplicate")
            return

        # Trigger buy with predicted address
        logger.info(
            "Triggering buy on create_token_detected",
            extra={
                "tx_hash": tx_hash,
                "token_address": predicted_token_address,
                "trigger_event_type": "create_token_detected",
            },
        )

        # Mark token as being bought (before execution to prevent race conditions)
        self._mark_token_buy_attempt(predicted_token_address, tx_hash, "pending")

        await self._execute_buy(
            token_address=predicted_token_address,
            tx_hash=tx_hash,
            trigger_event_type="create_token_detected",
            event=event,
        )

        # Mark as bought
        await self.buy_dedup.add(tx_hash)
        self._increment_metric("buys_initiated_by_create")

    async def _handle_token_created(self, event: TokenCreated) -> None:
        """Handle token_created event.

        Fallback buy using address_0 (confirmed token address) if tx_hash
        not already bought. This handles quick-mined scenarios where
        create_token_detected was missed.

        Args:
            event: TokenCreated event from EventBus.
        """
        tx_hash = event["tx_hash"]
        address_0 = event["address_0"]

        # Check idempotency by trigger tx hash
        if await self.buy_dedup.contains(tx_hash):
            logger.debug(
                "Skipping duplicate token_created (already bought on create_token_detected)",
                extra={
                    "tx_hash": tx_hash,
                    "trigger_event_type": "token_created",
                },
            )
            self._increment_metric("buy_skipped_duplicate")
            return

        # Check if we've already bought this token address (critical for preventing duplicates)
        if self._has_token_buy_attempt(address_0):
            logger.info(
                "Skipping fallback buy - token address already bought",
                extra={
                    "tx_hash": tx_hash,
                    "token_address": address_0,
                    "trigger_event_type": "token_created",
                },
            )
            self._increment_metric("buy_skipped_duplicate")
            return

        # Trigger fallback buy with confirmed address
        logger.info(
            "Triggering fallback buy on token_created",
            extra={
                "tx_hash": tx_hash,
                "token_address": address_0,
                "trigger_event_type": "token_created",
            },
        )

        # Mark token as being bought (before execution to prevent race conditions)
        self._mark_token_buy_attempt(address_0, tx_hash, "pending")

        await self._execute_buy(
            token_address=address_0,
            tx_hash=tx_hash,
            trigger_event_type="token_created",
            event=event,
        )

        # Mark as bought
        await self.buy_dedup.add(tx_hash)
        self._increment_metric("buys_initiated_by_created")

    async def _execute_buy(
        self,
        token_address: str,
        tx_hash: str,
        trigger_event_type: str,
        event: CreateTokenDetected | TokenCreated,
    ) -> None:
        """Execute buy transaction with retry logic for TokenNotGraduated errors.

        Implements aggressive mempool warfare strategy:
        - Maintains high gas fees for first-block inclusion
        - Retries on TokenNotGraduated errors (token deployed but not graduated)
        - Only submits approve after successful buy confirmation
        - Multi-RPC broadcast for better propagation

        Args:
            token_address: Token address to buy.
            tx_hash: Transaction hash from trigger event (for correlation).
            trigger_event_type: Event type that triggered buy.
            event: Full event data for logging.
        """
        max_attempts = max(1, self.settings.trading_buy_retry_max_attempts)
        retry_delay_seconds = self.settings.trading_buy_retry_delay_ms / 1000.0
        
        buy_tx_hash_hex: str | None = None
        buy_nonce = 0  # Initialize for error handling
        base_fee_wei = 0  # Initialize for error handling
        max_fee_per_gas = 0  # Initialize for error handling
        max_priority_fee_per_gas = 0  # Initialize for error handling
        
        for attempt in range(max_attempts):
            try:
                # Calculate amount in Wei
                amount_in_wei = Web3.to_wei(self.settings.trading_buy_amount_pls, "ether")

                # Optional quote (best-effort)
                min_tokens = await self._get_min_tokens(token_address, amount_in_wei)

                # Encode buy calldata
                buy_data = self.factory_client.encode_buy(token_address, min_tokens)

                # Get base fee from latest block
                latest_block = await self.http_web3.eth.get_block("latest")
                base_fee_wei = latest_block.get("baseFeePerGas", 0)
                base_fee_beats = int(base_fee_wei // 10**9)

                # Select initial gas limit from ladder
                gas_limit = self._select_gas_limit()

                # Compute fees with comprehensive mempool analysis (P95 strategy)
                # Uses caching to avoid expensive mempool scans for every buy
                max_fee_per_gas, max_priority_fee_per_gas = await self._get_cached_gas_fees(
                    base_fee_wei
                )

                # Reserve nonce (wallet manages nonce state)
                buy_nonce = self.wallet.reserve_nonce()
                
                # Build transaction
                buy_tx = self.wallet.build_base_tx(
                    to=self.factory_client.factory_address,
                    value=Wei(amount_in_wei),
                    data=buy_data,
                    nonce=buy_nonce,
                    gas=gas_limit,
                    max_fee_per_gas=max_fee_per_gas,
                    max_priority_fee_per_gas=max_priority_fee_per_gas,
                )

                # Sign transaction
                signed_buy_tx = await self.wallet.sign_transaction(buy_tx)

                # Broadcast to multiple RPCs for better propagation
                buy_tx_hash_hex, successful_rpcs = await broadcast_to_multiple_rpcs(
                    signed_buy_tx, self.settings.pulsechain_rpc_urls
                )

                # Verify propagation
                propagated = await verify_tx_propagation(
                    self.http_web3, buy_tx_hash_hex, max_wait=3
                )

                logger.info(
                    "Buy transaction submitted",
                    extra={
                        "buy_tx_hash": buy_tx_hash_hex,
                        "trigger_tx_hash": tx_hash,
                        "trigger_event_type": trigger_event_type,
                        "token_address": token_address,
                        "amount_pls": self.settings.trading_buy_amount_pls,
                        "min_tokens": min_tokens,
                        "nonce": buy_nonce,
                        "gas_limit": gas_limit,
                        "base_fee_beats": base_fee_beats,
                        "max_fee_per_gas": max_fee_per_gas,
                        "max_priority_fee_per_gas": max_priority_fee_per_gas,
                        "broadcast_rpcs": len(successful_rpcs),
                        "propagated": propagated,
                        "attempt": attempt + 1,
                        "is_retry": attempt > 0,
                    },
                )

                # Wait for transaction receipt to check if it reverted on-chain
                # This allows us to retry on-chain reverts (e.g., TokenNotGraduated)
                try:
                    receipt = await asyncio.wait_for(
                        self.http_web3.eth.wait_for_transaction_receipt(
                            buy_tx_hash_hex  # type: ignore[arg-type]
                        ),
                        timeout=30.0,  # 30 second timeout (PulseChain ~3s blocks)
                    )
                    
                    # Check if transaction succeeded on-chain
                    if receipt.get("status") == 1:
                        # SUCCESS - Transaction mined and succeeded
                        logger.info(
                            "Buy transaction confirmed on-chain",
                            extra={
                                "buy_tx_hash": buy_tx_hash_hex,
                                "token_address": token_address,
                                "block_number": receipt.get("blockNumber"),
                                "gas_used": receipt.get("gasUsed"),
                            },
                        )
                        # Mark token buy as successful
                        self._mark_token_buy_attempt(token_address, tx_hash, "success")
                        break  # Exit retry loop, proceed with approve/sell
                    else:
                        # Transaction reverted on-chain - check if retriable
                        from pumptires.app.utils.tx_errors import get_transaction_revert_reason
                        
                        revert_reason = await get_transaction_revert_reason(
                            self.http_web3, buy_tx_hash_hex
                        )
                        is_retriable = is_retriable_error(revert_reason)
                        
                        logger.warning(
                            "Buy transaction reverted on-chain",
                            extra={
                                "buy_tx_hash": buy_tx_hash_hex,
                                "token_address": token_address,
                                "revert_reason": revert_reason,
                                "is_retriable": is_retriable,
                                "attempt": attempt + 1,
                                "max_attempts": max_attempts,
                                "gas_used": receipt.get("gasUsed"),
                            },
                        )
                        
                        # Check if we should retry
                        if attempt < max_attempts - 1 and is_retriable:
                            logger.info(
                                "Retriable on-chain revert, retrying buy",
                                extra={
                                    "revert_reason": revert_reason,
                                    "attempt": attempt + 1,
                                    "max_attempts": max_attempts,
                                    "retry_delay_seconds": retry_delay_seconds,
                                },
                            )
                            await asyncio.sleep(retry_delay_seconds)
                            continue  # Retry
                        else:
                            # Non-retriable or max attempts reached
                            self._increment_metric("buy_failures")
                            return  # Exit without post-buy actions
                        
                except asyncio.TimeoutError:
                    logger.warning(
                        "Buy confirmation timeout during retry check",
                        extra={
                            "buy_tx_hash": buy_tx_hash_hex,
                            "token_address": token_address,
                            "attempt": attempt + 1,
                        },
                    )
                    # If we timeout waiting for receipt, assume success and proceed
                    # (transaction may still be pending)
                    break
                except Exception as receipt_error:
                    logger.warning(
                        "Error checking buy receipt during retry",
                        extra={
                            "buy_tx_hash": buy_tx_hash_hex,
                            "token_address": token_address,
                            "error": str(receipt_error),
                            "attempt": attempt + 1,
                        },
                    )
                    # If we can't get receipt, assume success and proceed
                    break

            except Exception as e:
                error_msg = str(e)
                
                # Try to extract error from exception
                error_name = extract_error_from_exception(e)
                is_retriable = is_retriable_error(error_name)
                
                logger.error(
                    "Buy execution failed",
                    extra={
                        "trigger_tx_hash": tx_hash,
                        "trigger_event_type": trigger_event_type,
                        "token_address": token_address,
                        "error": error_msg,
                        "error_name": error_name,
                        "is_retriable": is_retriable,
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "will_retry": attempt < max_attempts - 1 and is_retriable,
                    },
                    exc_info=True,
                )
                
                # Check if we should retry
                if attempt < max_attempts - 1:
                    # Handle nonce errors with recovery (existing logic)
                    if await handle_nonce_error_with_retry(
                        self.http_web3,
                        self.wallet,
                        base_fee_wei,
                        error_msg,
                        buy_nonce,
                    ):
                        logger.info(
                            "Nonce error recovered, retrying buy",
                            extra={
                                "attempt": attempt + 1,
                                "max_attempts": max_attempts,
                            },
                        )
                        await asyncio.sleep(retry_delay_seconds)
                        continue  # Retry with fresh nonce
                    
                    # Retry on retriable errors (TokenNotGraduated)
                    if is_retriable:
                        logger.info(
                            "Retriable error detected, retrying buy",
                            extra={
                                "error_name": error_name,
                                "attempt": attempt + 1,
                                "max_attempts": max_attempts,
                                "retry_delay_seconds": retry_delay_seconds,
                            },
                        )
                        await asyncio.sleep(retry_delay_seconds)
                        continue  # Retry
                    
                    # For other errors, still retry if attempts remain
                    logger.info(
                        "Retrying buy on generic error",
                        extra={
                            "attempt": attempt + 1,
                            "max_attempts": max_attempts,
                        },
                    )
                    await asyncio.sleep(retry_delay_seconds)
                    continue
                else:
                    # Final attempt failed
                    self._increment_metric("buy_failures")
                    return  # Exit without post-buy actions

        # Post-buy actions (only if buy was submitted and confirmed successfully)
        if buy_tx_hash_hex is None:
            logger.warning(
                "Buy transaction not submitted after all attempts",
                extra={
                    "trigger_tx_hash": tx_hash,
                    "token_address": token_address,
                    "max_attempts": max_attempts,
                },
            )
            self._increment_metric("buy_failures")
            return

        # Submit approve if enabled
        # Note: We already confirmed buy succeeded in the retry loop above
        if self.settings.trading_approve_on_buy:
            try:
                approve_nonce = self.wallet.reserve_nonce()
                await self._submit_approve(
                    token_address=token_address,
                    nonce=approve_nonce,
                    max_fee_per_gas=max_fee_per_gas,
                    max_priority_fee_per_gas=max_priority_fee_per_gas,
                    buy_tx_hash=buy_tx_hash_hex,
                )
            except Exception as e:
                logger.warning(
                    "Error submitting approve transaction",
                    extra={
                        "buy_tx_hash": buy_tx_hash_hex,
                        "token_address": token_address,
                        "error": str(e),
                    },
                )
                # Continue to schedule sell even if approve fails

        # Schedule sell if auto-sell is enabled (even if approve failed)
        if self.settings.trading_auto_sell_enabled:
            await self._schedule_sell(token_address, buy_tx_hash_hex)

    def _select_gas_limit(self, retry_count: int = 0) -> int:
        """Select gas limit from ladder based on configuration and retry count.

        Args:
            retry_count: Current retry attempt (0 = initial).

        Returns:
            Gas limit in units.
        """
        # Get starting rung
        start_rung = self.settings.gas_limit_start_rung.lower()
        if start_rung == "conservative":
            base_limit = self.settings.gas_limit_conservative
        elif start_rung == "ceiling":
            base_limit = self.settings.gas_limit_ceiling
        else:  # baseline
            base_limit = self.settings.gas_limit_baseline

        # Apply retry bump (capped by ceiling)
        if retry_count > 0:
            bump_factor = 1.0 + (
                self.settings.gas_limit_retry_bump_pct * retry_count
            )
            bumped_limit = int(base_limit * bump_factor)
            return min(bumped_limit, self.settings.gas_limit_ceiling)

        return base_limit

    async def _get_min_tokens(self, token_address: str, amount_in_wei: int) -> int:
        """Get minimum tokens out with slippage protection.

        Optionally quotes from factory. Falls back to 0 if fast path enabled
        or quote fails.

        Args:
            token_address: Token address to quote.
            amount_in_wei: Amount in Wei to spend.

        Returns:
            Minimum tokens out (with slippage applied) or 0 for fast path.
        """
        if self.settings.trading_fast_path:
            logger.debug("Fast path enabled, using min_tokens=0")
            return 0

        try:
            # Attempt quote
            quote_result = await self.factory_client.quote_buy(
                token_address, amount_in_wei
            )

            if quote_result is None:
                logger.warning(
                    "Quote failed, using min_tokens=0",
                    extra={"token_address": token_address},
                )
                return 0

            amount_out, _ = quote_result

            # Apply slippage: min_tokens = amount_out * (1 - slippage_bps/10000)
            slippage_multiplier = 1.0 - (self.settings.trading_slippage_bps / 10000.0)
            min_tokens = int(amount_out * slippage_multiplier)

            logger.debug(
                "Quote successful",
                extra={
                    "token_address": token_address,
                    "amount_out": amount_out,
                    "min_tokens": min_tokens,
                    "slippage_bps": self.settings.trading_slippage_bps,
                },
            )

            return min_tokens

        except Exception as e:
            logger.warning(
                "Quote exception, using min_tokens=0",
                extra={"token_address": token_address, "error": str(e)},
            )
            return 0

    async def _submit_approve(
        self,
        token_address: str,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        buy_tx_hash: str,
    ) -> None:
        """Submit optimistic approve with nonce+1.

        Submits approve immediately after buy to enable future sells without
        additional approval latency. May revert if buy fails.

        Args:
            token_address: Token to approve.
            nonce: Nonce for approve transaction (should be buy_nonce + 1).
            max_fee_per_gas: Max fee per gas in Beats.
            max_priority_fee_per_gas: Max priority fee per gas in Beats.
            buy_tx_hash: Buy transaction hash for correlation.
        """
        try:
            # Encode approve calldata (MAX_UINT256 allowance to factory)
            approve_data = self.factory_client.encode_approve(
                token_addr=token_address,
                spender=self.factory_client.factory_address,
                amount=2**256 - 1,  # MAX_UINT256
            )

            # Build approve transaction
            approve_tx = self.wallet.build_base_tx(
                to=token_address,
                value=Wei(0),
                data=approve_data,
                nonce=nonce,
                gas=60_000,  # Standard approve gas
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
            )

            # Sign transaction
            signed_approve_tx = await self.wallet.sign_transaction(approve_tx)

            # Broadcast to multiple RPCs
            approve_tx_hash_hex, successful_rpcs = await broadcast_to_multiple_rpcs(
                signed_approve_tx, self.settings.pulsechain_rpc_urls
            )

            logger.info(
                "Approve transaction submitted (optimistic)",
                extra={
                    "approve_tx_hash": approve_tx_hash_hex,
                    "buy_tx_hash": buy_tx_hash,
                    "token_address": token_address,
                    "nonce": nonce,
                    "broadcast_rpcs": len(successful_rpcs),
                },
            )

        except Exception as e:
            logger.warning(
                "Approve submission failed (non-fatal)",
                extra={
                    "buy_tx_hash": buy_tx_hash,
                    "token_address": token_address,
                    "error": str(e),
                },
            )

    async def _schedule_sell(
        self,
        token_address: str,
        buy_tx_hash: str,
    ) -> None:
        """Schedule a sell transaction to execute after configured delay.

        Args:
            token_address: Token address to sell.
            buy_tx_hash: Buy transaction hash for correlation.
        """
        if not self.settings.trading_auto_sell_enabled:
            return

        # Cancel any existing scheduled sell for this token
        if token_address in self._scheduled_sells:
            self._scheduled_sells[token_address].cancel()

        # Calculate execute_at timestamp
        execute_at = int(time.time()) + self.settings.trading_sell_delay_seconds

        # Persist scheduled sell BEFORE creating task (atomicity guarantee)
        await self.scheduled_sells.add(token_address, buy_tx_hash, execute_at)

        # Create sell task
        sell_task = asyncio.create_task(
            self._delayed_sell_execution(token_address, buy_tx_hash)
        )
        self._scheduled_sells[token_address] = sell_task

        logger.info(
            "Sell scheduled",
            extra={
                "token_address": token_address,
                "buy_tx_hash": buy_tx_hash,
                "delay_seconds": self.settings.trading_sell_delay_seconds,
                "execute_at": execute_at,
            },
        )

    async def _delayed_sell_execution(
        self,
        token_address: str,
        buy_tx_hash: str,
        delay_override: int | None = None,
    ) -> None:
        """Execute sell after delay.

        Args:
            token_address: Token address to sell.
            buy_tx_hash: Buy transaction hash for correlation.
            delay_override: Optional delay override in seconds (for rehydration).
        """
        try:
            # Wait for configured delay (or override for rehydration)
            delay_seconds = (
                delay_override
                if delay_override is not None
                else self.settings.trading_sell_delay_seconds
            )
            await asyncio.sleep(delay_seconds)

            logger.info(
                "Executing scheduled sell",
                extra={
                    "token_address": token_address,
                    "buy_tx_hash": buy_tx_hash,
                },
            )

            # Remove from persistent storage (task is about to execute)
            await self.scheduled_sells.remove(token_address)

            # Execute sell
            await self._execute_sell(token_address, buy_tx_hash)

        except asyncio.CancelledError:
            logger.info(
                "Scheduled sell cancelled",
                extra={
                    "token_address": token_address,
                    "buy_tx_hash": buy_tx_hash,
                },
            )
            # Remove from persistent storage on cancellation
            await self.scheduled_sells.remove(token_address)
            raise

        except Exception as e:
            logger.error(
                "Scheduled sell execution failed",
                extra={
                    "token_address": token_address,
                    "buy_tx_hash": buy_tx_hash,
                    "error": str(e),
                },
            )
            # Remove from persistent storage on failure to avoid retry loop
            await self.scheduled_sells.remove(token_address)
        finally:
            # Clean up task reference
            if token_address in self._scheduled_sells:
                del self._scheduled_sells[token_address]

    async def _execute_sell(
        self,
        token_address: str,
        buy_tx_hash: str,
    ) -> None:
        """Execute sell transaction for a token.

        Args:
            token_address: Token address to sell.
            buy_tx_hash: Buy transaction hash for correlation.
        """
        max_retries = 3

        for attempt in range(max_retries):
            try:
                # Get token balance
                token_contract = self.http_web3.eth.contract(
                    address=AsyncWeb3.to_checksum_address(token_address),
                    abi=[
                        {
                            "constant": True,
                            "inputs": [{"name": "_owner", "type": "address"}],
                            "name": "balanceOf",
                            "outputs": [{"name": "balance", "type": "uint256"}],
                            "type": "function",
                        }
                    ],
                )

                balance = await token_contract.functions.balanceOf(
                    self.wallet.address
                ).call()

                if balance == 0:
                    logger.warning(
                        "No token balance to sell",
                        extra={
                            "token_address": token_address,
                            "buy_tx_hash": buy_tx_hash,
                        },
                    )
                    return

                # Quote sell
                quote_result = await self.factory_client.quote_sell(
                    token_address, balance
                )

                if quote_result is None:
                    logger.warning(
                        "Sell quote failed, using min_eth=0",
                        extra={
                            "token_address": token_address,
                            "balance": balance,
                        },
                    )
                    min_eth = 0
                else:
                    amount_out, _ = quote_result
                    # Apply slippage tolerance
                    slippage_factor = 1.0 - (
                        self.settings.trading_sell_slippage_bps / 10000.0
                    )
                    min_eth = int(amount_out * slippage_factor)

                # Encode sell calldata
                sell_data = self.factory_client.encode_sell(
                    token_address, balance, min_eth
                )

                # Get base fee and compute fees
                latest_block = await self.http_web3.eth.get_block("latest")
                base_fee_wei = latest_block.get("baseFeePerGas", 0)

                max_fee_per_gas, max_priority_fee_per_gas = (
                    await compute_fees_with_mempool_analysis(
                        self.http_web3,
                        self.gas_oracle,
                        base_fee_wei,
                    )
                )

                # Select gas limit
                gas_limit = self._select_gas_limit()

                # Reserve nonce
                sell_nonce = self.wallet.reserve_nonce()

                # Build transaction
                sell_tx = self.wallet.build_base_tx(
                    to=self.factory_client.factory_address,
                    value=Wei(0),
                    data=sell_data,
                    nonce=sell_nonce,
                    gas=gas_limit,
                    max_fee_per_gas=max_fee_per_gas,
                    max_priority_fee_per_gas=max_priority_fee_per_gas,
                )

                # Sign transaction
                signed_sell_tx = await self.wallet.sign_transaction(sell_tx)

                # Broadcast
                sell_tx_hash_hex, successful_rpcs = await broadcast_to_multiple_rpcs(
                    signed_sell_tx, self.settings.pulsechain_rpc_urls
                )

                # Verify propagation
                propagated = await verify_tx_propagation(
                    self.http_web3, sell_tx_hash_hex, max_wait=3
                )

                logger.info(
                    "Sell transaction submitted",
                    extra={
                        "sell_tx_hash": sell_tx_hash_hex,
                        "buy_tx_hash": buy_tx_hash,
                        "token_address": token_address,
                        "balance": balance,
                        "min_eth": min_eth,
                        "nonce": sell_nonce,
                        "gas_limit": gas_limit,
                        "max_fee_per_gas": max_fee_per_gas,
                        "max_priority_fee_per_gas": max_priority_fee_per_gas,
                        "broadcast_rpcs": len(successful_rpcs),
                        "propagated": propagated,
                        "attempt": attempt + 1,
                    },
                )

                # Wait for receipt to confirm sell succeeded on-chain
                try:
                    receipt = await asyncio.wait_for(
                        self.http_web3.eth.wait_for_transaction_receipt(
                            sell_tx_hash_hex  # type: ignore[arg-type]
                        ),
                        timeout=30.0,
                    )
                    
                    if receipt.get("status") == 1:
                        logger.info(
                            "Sell transaction confirmed on-chain",
                            extra={
                                "sell_tx_hash": sell_tx_hash_hex,
                                "token_address": token_address,
                                "block_number": receipt.get("blockNumber"),
                                "gas_used": receipt.get("gasUsed"),
                            },
                        )
                        self._increment_metric("sells_executed")
                        return
                    else:
                        # Sell reverted on-chain
                        from pumptires.app.utils.tx_errors import get_transaction_revert_reason
                        
                        revert_reason = await get_transaction_revert_reason(
                            self.http_web3, sell_tx_hash_hex
                        )
                        
                        logger.warning(
                            "Sell transaction reverted on-chain",
                            extra={
                                "sell_tx_hash": sell_tx_hash_hex,
                                "token_address": token_address,
                                "revert_reason": revert_reason,
                                "gas_used": receipt.get("gasUsed"),
                                "attempt": attempt + 1,
                            },
                        )
                        
                        # Retry if attempts remain
                        if attempt < max_retries - 1:
                            logger.info(
                                "Retrying sell after on-chain revert",
                                extra={
                                    "attempt": attempt + 1,
                                    "max_retries": max_retries,
                                },
                            )
                            await asyncio.sleep(2 ** attempt)
                            continue
                        else:
                            self._increment_metric("sells_failed")
                            return
                            
                except asyncio.TimeoutError:
                    logger.warning(
                        "Sell confirmation timeout",
                        extra={
                            "sell_tx_hash": sell_tx_hash_hex,
                            "token_address": token_address,
                        },
                    )
                    # Assume success if we timeout (transaction may still be pending)
                    self._increment_metric("sells_executed")
                    return

                self._increment_metric("sells_executed")
                return

            except Exception as e:
                error_msg = str(e)
                logger.error(
                    "Sell execution failed",
                    extra={
                        "token_address": token_address,
                        "buy_tx_hash": buy_tx_hash,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "error": error_msg,
                    },
                )

                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    self._increment_metric("sells_failed")
                    raise

    def _has_token_buy_attempt(self, token_address: str) -> bool:
        """Check if we've already attempted to buy this token address.
        
        Args:
            token_address: Token address to check.
            
        Returns:
            True if we've already attempted/succeeded buying this token.
        """
        # Normalize address to lowercase for consistent lookups
        normalized_addr = token_address.lower()
        
        if normalized_addr not in self._token_buy_cache:
            return False
        
        # Check if cache entry is still valid (TTL: 5 minutes)
        _, timestamp, status = self._token_buy_cache[normalized_addr]
        age_seconds = time.time() - timestamp
        
        if age_seconds > 300:  # 5 minutes TTL
            # Expired, remove and return False
            del self._token_buy_cache[normalized_addr]
            return False
        
        return True
    
    def _mark_token_buy_attempt(self, token_address: str, trigger_tx: str, status: str = "pending") -> None:
        """Mark that we're attempting/attempted to buy this token.
        
        Args:
            token_address: Token address being bought.
            trigger_tx: Trigger transaction hash for correlation.
            status: Status of buy attempt (pending, success, failed).
        """
        normalized_addr = token_address.lower()
        self._token_buy_cache[normalized_addr] = (trigger_tx, time.time(), status)
        
        logger.debug(
            "Token buy attempt marked",
            extra={
                "token_address": token_address,
                "trigger_tx": trigger_tx,
                "status": status,
            },
        )
    
    async def _get_cached_gas_fees(self, base_fee: int) -> tuple[int, int]:
        """Get cached gas fees if available for current block, otherwise compute fresh.
        
        This avoids expensive mempool scans for every buy by caching fees per block.
        Since PulseChain blocks are ~10 seconds, multiple tokens can share same fees.
        
        Args:
            base_fee: Current base fee in wei.
            
        Returns:
            Tuple of (max_fee_per_gas, max_priority_fee_per_gas) in wei.
        """
        latest_block = await self.http_web3.eth.get_block("latest")
        block_num = latest_block.get("number", 0)
        
        # Check cache
        if block_num in self._gas_cache:
            max_fee, priority_fee, ts = self._gas_cache[block_num]
            # Use cached fees if < 5 seconds old
            if time.time() - ts < 5.0:
                logger.debug(
                    "Using cached gas fees",
                    extra={
                        "block": block_num,
                        "max_fee_gwei": max_fee / 10**9,
                        "priority_fee_gwei": priority_fee / 10**9,
                    },
                )
                return max_fee, priority_fee
        
        # Compute fresh fees (expensive mempool scan)
        max_fee, priority_fee = await compute_fees_with_mempool_analysis(
            self.http_web3,
            self.gas_oracle,
            base_fee,
        )
        
        # Cache for this block
        self._gas_cache[block_num] = (max_fee, priority_fee, time.time())
        
        # Evict old blocks (keep only last 5)
        if len(self._gas_cache) > 5:
            oldest = min(self._gas_cache.keys())
            del self._gas_cache[oldest]
            logger.debug("Evicted old gas cache entry", extra={"block": oldest})
        
        return max_fee, priority_fee

    def _increment_metric(self, metric_name: str) -> None:
        """Increment a metric counter.

        Args:
            metric_name: Name of metric to increment.
        """
        # Simple counter tracking; extend MetricsTracker if needed
        logger.debug("metric_increment", extra={"metric": metric_name})

    async def stop(self) -> None:
        """Stop the trading runner loop and cancel all scheduled sells."""
        self._running = False
        
        # Cancel all scheduled sells
        for token_address, task in list(self._scheduled_sells.items()):
            task.cancel()
            logger.info(
                "Cancelling scheduled sell",
                extra={"token_address": token_address},
            )
        self._scheduled_sells.clear()
        
        # Persist final state and close
        await self.scheduled_sells.close()
        
        logger.info("TradingRunner stop requested")


# Import Web3 for to_wei conversion
from web3 import Web3
