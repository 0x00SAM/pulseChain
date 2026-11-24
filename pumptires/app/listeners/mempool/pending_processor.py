from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping, cast

from eth_typing import HexStr
from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.contract import AsyncContract

from pumptires.app.config.settings import settings
from pumptires.app.contracts.factory import (
    calculate_gas_price_in_beats,
    decode_create_token_input,
    format_gas_price,
    is_create_token_transaction,
)
from pumptires.app.io.jsonl_writer import JSONLWriter
from pumptires.app.io.persistent_dedup import PersistentDedup
from pumptires.app.services.address_predictor import calculate_contract_address
from pumptires.app.services.metrics import MetricsTracker
from pumptires.app.services.nonce_tracker import NonceTracker
from pumptires.app.utils.logger import get_logger

from .prediction_store import PredictionStore

logger = get_logger(__name__, level=settings.get_log_level())


class PendingTransactionProcessor:
    """Processes pending transactions for createToken detection and prediction."""

    def __init__(
        self,
        web3: AsyncWeb3 | None,
        http_web3: AsyncWeb3 | None,
        factory_contract: AsyncContract | None,
        nonce_tracker: NonceTracker | None,
        jsonl_writer: JSONLWriter | None,
        persistent_dedup: PersistentDedup | None,
        metrics_tracker: MetricsTracker,
        store: PredictionStore,
        event_bus: Any | None = None,
    ) -> None:
        self.web3 = web3
        self.http_web3 = http_web3
        self.factory_contract = factory_contract
        self.nonce_tracker = nonce_tracker
        self.jsonl_writer = jsonl_writer
        self.persistent_dedup = persistent_dedup
        self.metrics_tracker = metrics_tracker
        self.store = store
        self.event_bus = event_bus
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_tx_fetches)
        self._pending_tx_tasks: set[asyncio.Task] = set()

    async def fetch_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        """Fetch full transaction by hash using WS client with concurrency guard.

        Args:
            tx_hash: Transaction hash to fetch.

        Returns:
            Transaction dict or None if not found/failed.
        """
        async with self._semaphore:
            if not self.web3:
                self.store.inc_fetch_failed()
                logger.warning(
                    "fetch_transaction_failed",
                    extra={
                        "tx_hash": tx_hash,
                        "error": "websocket_unavailable",
                        "transport": "ws",
                    },
                )
                return None
            try:
                tx = await self.web3.eth.get_transaction(HexStr(tx_hash))
                self.store.inc_fetch_success()
                return dict(tx)
            except Exception as e:  # noqa: BLE001
                self.store.inc_fetch_failed()
                logger.warning(
                    "fetch_transaction_failed",
                    extra={"tx_hash": tx_hash, "error": str(e), "transport": "ws"},
                )
                return None

    async def process_pending_transaction(self, tx_hash: str) -> None:
        """End-to-end processing path for a pending tx hash."""
        # Normalize tx hash to a canonical form for consistent correlation keys
        if tx_hash:
            if not tx_hash.startswith("0x"):
                tx_hash = f"0x{tx_hash}"
            tx_hash = tx_hash.lower()
        try:
            tx = await self.fetch_transaction(tx_hash)
            if not tx:
                logger.debug("tx_fetch_failed", extra={"tx_hash": tx_hash})
                return
            await self._process_common(tx_hash, tx)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "process_pending_transaction_failed",
                extra={"tx_hash": tx_hash, "error": str(e)},
            )

    async def process_pending_transaction_object(self, tx: dict[str, Any]) -> None:
        """Processing path when subscription delivers a full tx object."""
        try:
            tx_hash_value = tx.get("hash")
            tx_hash = (
                HexBytes(tx_hash_value).hex()
                if isinstance(tx_hash_value, (bytes, HexBytes))
                else str(tx_hash_value)
            )
            # Canonicalize to ensure consistent store/reconciliation keys
            if tx_hash and not tx_hash.startswith("0x"):
                tx_hash = f"0x{tx_hash}"
            tx_hash = tx_hash.lower()
            await self._process_common(tx_hash, tx)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "process_pending_transaction_failed",
                extra={"tx_hash": tx.get("hash"), "error": str(e)},
            )

    async def handle_pending_transaction(self, tx_hash: str) -> None:
        """Create isolated task to process a tx hash without blocking."""

        async def safe() -> None:
            try:
                await self.process_pending_transaction(tx_hash)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "transaction_processing_error_isolated",
                    extra={"tx_hash": tx_hash, "error": str(e)},
                )

        task = asyncio.create_task(safe())
        self._pending_tx_tasks.add(task)
        task.add_done_callback(self._pending_tx_tasks.discard)

    async def handle_pending_transaction_object(self, tx: dict[str, Any]) -> None:
        """Create isolated task to process a tx object without blocking."""

        async def safe() -> None:
            try:
                await self.process_pending_transaction_object(tx)
            except Exception as e:  # noqa: BLE001
                tx_hash_value = tx.get("hash")
                tx_hash = (
                    HexBytes(tx_hash_value).hex()
                    if isinstance(tx_hash_value, (bytes, HexBytes))
                    else str(tx_hash_value)
                )
                logger.exception(
                    "transaction_processing_error_isolated",
                    extra={"tx_hash": tx_hash, "error": str(e)},
                )

        task = asyncio.create_task(safe())
        self._pending_tx_tasks.add(task)
        task.add_done_callback(self._pending_tx_tasks.discard)

    async def cancel_all(self) -> None:
        """Cancel all in-flight processing tasks and await their completion."""
        for task in list(self._pending_tx_tasks):
            if not task.done():
                task.cancel()
        if self._pending_tx_tasks:
            await asyncio.gather(*self._pending_tx_tasks, return_exceptions=True)

    async def _process_common(self, tx_hash: str, tx: dict[str, Any]) -> None:
        """Shared processing logic for both hash- and object-based paths.

        Applies factory-address and selector filters, decodes calldata, handles
        dedup, coordinates nonce reservation, emits event, and updates metrics.
        """
        # Gate 1: to == factory
        tx_to = tx.get("to")
        if not tx_to:
            return
        if self.http_web3:
            tx_to_checksum = self.http_web3.to_checksum_address(tx_to)
            factory_checksum = self.http_web3.to_checksum_address(
                settings.factory_address
            )
            logger.debug(
                "address_comparison",
                extra={
                    "tx_to": tx_to_checksum,
                    "factory": factory_checksum,
                    "match": tx_to_checksum == factory_checksum,
                },
            )
            if tx_to_checksum != factory_checksum:
                return
            self.store.inc_to_factory_match()

        # Gate 2: function selector
        tx_data = tx.get("input", "") or tx.get("data", "")
        if isinstance(tx_data, (bytes, HexBytes)):
            tx_data = HexBytes(tx_data).hex()
        # WS full_transactions=True should provide complete input/data field.
        # HTTP cannot reliably serve pending tx bodies (probe findings).
        if not tx_data or len(tx_data) < 10:
            logger.debug(
                "tx_data_missing_or_short_skipped",
                extra={
                    "tx_hash": tx_hash,
                    "tx_data_len": len(tx_data) if tx_data else 0,
                },
            )
            return
        logger.debug(
            "function_selector_check",
            extra={
                "tx_data_prefix": tx_data[:10] if tx_data else "None",
                "expected_selector": settings.create_token_selector,
                "match": is_create_token_transaction(tx_data),
            },
        )
        if not is_create_token_transaction(tx_data):
            return
        self.store.inc_selector_match()

        # Decode
        if not self.factory_contract:
            logger.error("process_tx_no_contract")
            return
        decoded = decode_create_token_input(self.factory_contract, tx_data)
        if not decoded:
            return
        token_name, token_symbol = decoded

        # Gas price
        gas_price_beats = calculate_gas_price_in_beats(tx)
        gas_price_formatted = format_gas_price(gas_price_beats)

        # Dedup
        if tx_hash in self.store.pending_written_tx_hashes:
            logger.debug(
                "create_token_detected_duplicate_skipped_in_memory",
                extra={"tx_hash": tx_hash},
            )
            return
        if self.persistent_dedup and await self.persistent_dedup.contains(tx_hash):
            logger.debug(
                "create_token_detected_duplicate_skipped_persistent",
                extra={"tx_hash": tx_hash},
            )
            self.store.pending_written_tx_hashes.add(tx_hash)
            return

        # Nonce reservation & prediction
        tx_nonce = tx.get("nonce", 0)  # kept for compatibility
        factory_creation_nonce = None
        predicted_address = None
        prediction_source = "none"

        if self.nonce_tracker:
            try:
                factory_creation_nonce = await self.nonce_tracker.reserve_next(
                    settings.factory_address, tx_hash
                )
                predicted_address = calculate_contract_address(
                    settings.factory_address, factory_creation_nonce
                )
                prediction_source = "tracker_reserved"
                logger.debug(
                    "nonce_reserved_for_prediction",
                    extra={
                        "tx_hash": tx_hash,
                        "factory_creation_nonce": factory_creation_nonce,
                        "predicted_address": predicted_address,
                    },
                )
                # Store correlation immediately to avoid race with logs reconciliation
                if predicted_address and factory_creation_nonce is not None:
                    self.store.set_correlation(
                        tx_hash, predicted_address, factory_creation_nonce
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "nonce_reservation_failed",
                    extra={"tx_hash": tx_hash, "error": str(e)},
                )

        # Event data
        tx_value = tx.get("value")
        block_number = tx.get("blockNumber")
        timestamp = datetime.now(timezone.utc).isoformat()

        event_data = {
            "event_type": "create_token_detected",
            "timestamp": timestamp,
            "tx_hash": tx_hash,
            "sender": tx.get("from"),
            "token_name": token_name,
            "token_symbol": token_symbol,
            "gas_price_beats": str(gas_price_beats),
            "gas_price_pls": gas_price_formatted["pls"],
            "gas_price_gwei": gas_price_formatted["gwei"],
            "predicted_token_address": predicted_address,
            "factory_creation_nonce": factory_creation_nonce,
            "prediction_source": prediction_source,
            "tx_value": str(tx_value) if tx_value is not None else None,
            "block_number": block_number,
        }

        if self.jsonl_writer:
            await self.jsonl_writer.write_event(event_data)
            self.store.pending_written_tx_hashes.add(tx_hash)
            self.store.inc_create_token_written()

            # Emit to EventBus for trading triggers
            if self.event_bus:
                await self.event_bus.emit(event_data)

            if self.persistent_dedup:
                await self.persistent_dedup.add(tx_hash)
            if predicted_address and factory_creation_nonce is not None:
                self.metrics_tracker.record_prediction(
                    predicted_address, factory_creation_nonce
                )
                if self.nonce_tracker:
                    tracker_state = await self.nonce_tracker.get_state(
                        settings.factory_address
                    )
                    if "factory_creation_nonce" in tracker_state:
                        self.metrics_tracker.update_tracker_state(
                            active_reservations=tracker_state["factory_creation_nonce"][
                                "reservation_count"
                            ],
                            confirmed_nonce=tracker_state["factory_creation_nonce"][
                                "confirmed"
                            ],
                        )
