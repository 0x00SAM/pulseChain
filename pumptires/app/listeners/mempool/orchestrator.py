from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

from hexbytes import HexBytes
from web3.utils.subscriptions import (
    LogsSubscriptionContext,
    PendingTxSubscriptionContext,
)

from pumptires.app.config.settings import settings
from pumptires.app.contracts.factory import get_factory_contract
from pumptires.app.events.bus import EventBus
from pumptires.app.io.persistent_dedup import PersistentDedup
from pumptires.app.services.metrics import MetricsTracker
from pumptires.app.trading import FactoryClient, GasOracle, TradingRunner, TradingWallet
from pumptires.app.utils.logger import get_logger

from .cleanup import CleanupManager
from .connection import ConnectionManager
from .exceptions import SubscriptionStopped
from .pending_processor import PendingTransactionProcessor
from .prediction_store import PredictionStore
from .reconciliation import TokenCreatedReconciler
from .subscriptions import SubscriptionRunner

logger = get_logger(__name__)


class MempoolListener:
    """Asynchronous mempool listener orchestrator.

    Wires together connection management, subscription runner, pending
    transaction processing, reconciliation, metrics, and periodic cleanup.
    Maintains the same public API and logging/event formats.
    """

    def __init__(self) -> None:
        self.connection = ConnectionManager()
        self.metrics_tracker = MetricsTracker(
            log_interval_seconds=settings.metrics_log_interval
        )
        self.store = PredictionStore()
        self.event_bus = EventBus(maxsize=settings.event_queue_maxsize)
        self.pending_processor: PendingTransactionProcessor | None = None
        self.reconciler: TokenCreatedReconciler | None = None
        self.cleanup_manager: CleanupManager | None = None
        self.gas_oracle: GasOracle | None = None
        self.trading_runner: TradingRunner | None = None
        self.trading_task: asyncio.Task | None = None
        self.is_running = False

        logger.info(
            "orchestrator_initialized",
            extra={"event_queue_maxsize": settings.event_queue_maxsize},
        )

    # Lifecycle
    async def connect(self) -> None:
        """Initialize connections and build component instances."""
        await self.connection.connect()
        await self._build_components()

    async def disconnect(self) -> None:
        """Stop background tasks and close connections/IO."""
        # Cancel trading task first
        if self.trading_task and not self.trading_task.done():
            logger.info("Cancelling trading task")
            self.trading_task.cancel()
            try:
                await self.trading_task
            except asyncio.CancelledError:
                logger.info("Trading task cancelled successfully")
            except Exception as e:
                logger.warning(
                    "Trading task cancellation error", extra={"error": str(e)}
                )

        # Stop cleanup
        if self.cleanup_manager:
            await self.cleanup_manager.stop()
        # Cancel pending tx tasks
        if self.pending_processor:
            await self.pending_processor.cancel_all()
        await self.connection.disconnect()

    async def _build_components(self) -> None:
        """Construct component instances based on current connections."""
        self.pending_processor = PendingTransactionProcessor(
            web3=self.connection.web3,
            http_web3=self.connection.http_web3,
            factory_contract=self.connection.factory_contract,
            nonce_tracker=self.connection.nonce_tracker,
            jsonl_writer=self.connection.jsonl_writer,
            persistent_dedup=self.connection.persistent_dedup,
            metrics_tracker=self.metrics_tracker,
            store=self.store,
            event_bus=self.event_bus,
        )
        self.reconciler = TokenCreatedReconciler(
            nonce_tracker=self.connection.nonce_tracker,
            metrics_tracker=self.metrics_tracker,
            store=self.store,
            factory_contract=self.connection.factory_contract,
            jsonl_writer=self.connection.jsonl_writer,
            event_bus=self.event_bus,
        )
        self.cleanup_manager = CleanupManager(
            store=self.store,
            nonce_tracker=self.connection.nonce_tracker,
            interval_seconds=settings.cleanup_interval,
            max_age_seconds=settings.cleanup_max_age,
        )

        # Initialize gas oracle for mempool sampling (used by trading and metrics)
        self.gas_oracle = GasOracle()
        logger.info("Gas oracle initialized for mempool sampling")

        # Build trading components if trading is enabled
        if settings.trading_enabled and settings.trading_private_key:
            try:
                logger.info("Building trading components")

                # Initialize trading wallet
                if not self.connection.http_web3:
                    logger.warning("HTTP Web3 not available, skipping trading setup")
                    return

                if not self.connection.factory_contract:
                    logger.warning(
                        "Factory contract not available, skipping trading setup"
                    )
                    return

                wallet = TradingWallet(
                    private_key=settings.trading_private_key,
                    http_web3=self.connection.http_web3,
                    chain_id=settings.expected_chain_id or 369,
                )

                # Initialize factory client
                factory_client = FactoryClient(
                    factory_contract=self.connection.factory_contract,
                    http_web3=self.connection.http_web3,
                )

                # Initialize buy dedup (persistent idempotency store)
                buy_dedup = PersistentDedup(
                    filepath=Path("data/buy_dedup.json"),
                    max_size=settings.dedup_max_size,
                )
                await buy_dedup.initialize()

                # Initialize trading runner
                self.trading_runner = TradingRunner(
                    event_bus=self.event_bus,
                    wallet=wallet,
                    factory_client=factory_client,
                    gas_oracle=self.gas_oracle,
                    buy_dedup=buy_dedup,
                    http_web3=self.connection.http_web3,
                    settings=settings,
                    metrics=self.metrics_tracker,
                )

                logger.info("Trading components built successfully")
            except Exception as e:
                logger.error(
                    "Failed to build trading components",
                    extra={"error": str(e)},
                    exc_info=True,
                )
                self.trading_runner = None
        else:
            logger.info(
                "Trading disabled",
                extra={
                    "trading_enabled": settings.trading_enabled,
                    "has_private_key": bool(settings.trading_private_key),
                },
            )

    # Subscription handlers
    async def _on_pending_tx(self, context: PendingTxSubscriptionContext) -> None:
        if not self.is_running or not self.pending_processor:
            return
        result = context.result
        tx_obj: dict[str, Any] | None = None
        if isinstance(result, (bytes, HexBytes)):
            tx_hash = HexBytes(result).hex()
        elif hasattr(result, "get"):
            try:
                tx_obj = dict(result)
            except Exception:
                tx_obj = {"hash": getattr(result, "hash", None)}
            tx_hash_value = tx_obj.get("hash")
            tx_hash = (
                HexBytes(tx_hash_value).hex()
                if isinstance(tx_hash_value, (bytes, HexBytes))
                else str(tx_hash_value)
            )
        elif isinstance(result, str):
            tx_hash = result
        else:
            tx_hash = ""
        self.store.inc_pending_received()
        
        # Feed gas oracle with pending tx observations (for mempool sampling)
        if tx_obj is not None and self.gas_oracle:
            self.gas_oracle.observe_from_tx(tx_obj)
        
        if tx_obj is not None:
            await self.pending_processor.handle_pending_transaction_object(tx_obj)
            return
        if tx_hash:
            await self.pending_processor.handle_pending_transaction(tx_hash)

    async def _on_token_created(self, context: LogsSubscriptionContext) -> None:
        if not self.is_running or not self.reconciler:
            return
        await self.reconciler.handle(context)

    # Runner
    async def subscribe_to_pending_transactions(self) -> None:
        """Run subscription runner and start cleanup until stop/timeout."""
        if not self.connection.web3:
            raise RuntimeError("Not connected to PulseChain")
        runner = SubscriptionRunner(
            web3=self.connection.web3,
            http_web3=self.connection.http_web3,
            factory_contract=self.connection.factory_contract,
            on_pending_handler=self._on_pending_tx,
            on_token_created_handler=self._on_token_created,
        )
        self.is_running = True
        if self.cleanup_manager:
            self.cleanup_manager.start()

        # Start trading task if trading is enabled
        if self.trading_runner:
            logger.info("Starting trading runner background task")
            self.trading_task = asyncio.create_task(self.trading_runner.run())

        try:
            await runner.run()
        finally:
            self.is_running = False

    async def run_with_reconnect(self) -> None:
        """Single run cycle with connection, run, and backoff on failure."""
        consecutive_failures = 0
        max_backoff = 300
        try:
            await self.disconnect()
            await self.connect()
            await self.subscribe_to_pending_transactions()
            consecutive_failures = 0
        except SubscriptionStopped as stopped:
            logger.warning(
                "mempool_subscription_stopped", extra={"reason": str(stopped)}
            )
            await self.disconnect()
            consecutive_failures += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("mempool_listener_error", extra={"error": str(e)})
            await self.disconnect()
            consecutive_failures += 1
        if consecutive_failures > 0:
            backoff_seconds = min(
                2**consecutive_failures + random.uniform(0, 1), max_backoff
            )
            logger.info(
                "reconnect_backoff",
                extra={
                    "consecutive_failures": consecutive_failures,
                    "backoff_seconds": backoff_seconds,
                },
            )
            await asyncio.sleep(backoff_seconds)

    async def start(self) -> None:
        """Run the listener forever with reconnect/backoff."""
        logger.info("mempool_listener_starting")
        while True:
            try:
                await self.run_with_reconnect()
                logger.debug("mempool_listener_reconnecting")
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "mempool_listener_reconnect_unexpected_error",
                    extra={"error": str(e)},
                )
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the listener gracefully and release resources."""
        logger.info("mempool_listener_stopping")
        self.is_running = False
        if self.cleanup_manager:
            await self.cleanup_manager.stop()
        if self.pending_processor:
            await self.pending_processor.cancel_all()
        await self.connection.disconnect()
        logger.info("mempool_listener_stopped")
