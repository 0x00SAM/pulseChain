from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from hexbytes import HexBytes
from web3.contract import AsyncContract
from web3.types import LogReceipt
from web3.utils.subscriptions import LogsSubscriptionContext

from pumptires.app.config.settings import settings
from pumptires.app.services.metrics import MetricsTracker
from pumptires.app.services.nonce_tracker import NonceTracker
from pumptires.app.utils.logger import get_logger

from .prediction_store import PredictionStore

logger = get_logger(__name__)


class TokenCreatedReconciler:
    """Handles TokenCreated log reconciliation and reorg recovery.

    Decodes TokenCreated logs, commits nonce reservations, matches predictions
    to actual token addresses, records metrics, writes events, and restores
    tracker state on reorg removals.
    """

    def __init__(
        self,
        nonce_tracker: NonceTracker | None,
        metrics_tracker: MetricsTracker,
        store: PredictionStore,
        factory_contract: AsyncContract | None,
        jsonl_writer,
        event_bus: Any | None = None,
    ) -> None:
        self.nonce_tracker = nonce_tracker
        self.metrics_tracker = metrics_tracker
        self.store = store
        self.factory_contract = factory_contract
        self.jsonl_writer = jsonl_writer
        self.event_bus = event_bus

    async def handle(self, context: LogsSubscriptionContext) -> None:
        """Process a TokenCreated log or removal event.

        Args:
            context: Subscription context carrying the log result and optional
                handler_context with a prebound token_created_event.
        """
        try:
            log_receipt = context.result
            if isinstance(log_receipt, (bytes, HexBytes)):
                return
            log_data = cast(LogReceipt, log_receipt)
            raw_hash = HexBytes(log_data["transactionHash"]).hex()
            # Normalize to best-match any existing store keys
            tx_hash = raw_hash if raw_hash.startswith("0x") else f"0x{raw_hash}"
            tx_hash = tx_hash.lower()
            # Prefer trimmed leading zero variant if present in store
            if len(tx_hash) > 3 and tx_hash[2] == "0":
                trimmed = "0x" + tx_hash[3:]
                if (
                    trimmed in self.store.prediction_correlations
                    or trimmed in self.store.committed_nonces
                ):
                    tx_hash = trimmed
            log_index = int(log_data["logIndex"])
            block_number = log_data.get("blockNumber")
            is_removed = log_data.get("removed", False)

            if is_removed:
                await self._handle_removed(tx_hash, log_index, block_number)
                return

            key = (tx_hash, log_index)
            if key in self.store.processed_log_keys:
                logger.debug(
                    "token_created_already_processed",
                    extra={"tx_hash": tx_hash, "log_index": log_index},
                )
                return
            self.store.processed_log_keys.add(key)

            token_created_event = getattr(context, "token_created_event", None)
            if token_created_event is None and hasattr(context, "handler_context"):
                token_created_event = context.handler_context.get("token_created_event")

            if token_created_event is None:
                logger.warning("token_created_event_object_missing")
                return

            decoded = token_created_event.process_log(log_data)
            args = (
                decoded["args"]
                if isinstance(decoded, dict)
                else getattr(decoded, "args", {})
            )

            token_name = args.get("name")
            token_symbol = args.get("symbol")
            address_0 = args.get("arg0")
            address_1 = args.get("arg1")

            # Commit nonce reservation and track for reorg recovery
            if self.nonce_tracker:
                try:
                    if tx_hash in self.store.prediction_correlations:
                        _, _, factory_nonce = self.store.prediction_correlations[
                            tx_hash
                        ]
                        self.store.committed_nonces[tx_hash] = factory_nonce
                        logger.debug(
                            "committed_nonce_tracked_for_reorg_recovery",
                            extra={"tx_hash": tx_hash, "factory_nonce": factory_nonce},
                        )
                    await self.nonce_tracker.commit(settings.factory_address, tx_hash)
                    logger.debug(
                        "nonce_committed",
                        extra={"tx_hash": tx_hash, "token_address": address_0},
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "nonce_commit_failed",
                        extra={"tx_hash": tx_hash, "error": str(e)},
                    )

            # Reconcile prediction
            prediction_match = None
            predicted_token_address = None
            if tx_hash in self.store.prediction_correlations:
                predicted_addr, _timestamp, _nonce = self.store.prediction_correlations[
                    tx_hash
                ]
                predicted_token_address = predicted_addr
                if address_0:
                    prediction_match = predicted_addr.lower() == address_0.lower()
                    logger.info(
                        "prediction_reconciled",
                        extra={
                            "tx_hash": tx_hash,
                            "predicted": predicted_addr,
                            "actual": address_0,
                            "match": prediction_match,
                        },
                    )
                    self.metrics_tracker.record_reconciliation(
                        predicted_address=predicted_addr,
                        actual_address=address_0,
                        match=bool(prediction_match),
                    )
                # Clean up correlation after use
                del self.store.prediction_correlations[tx_hash]

            event_data = {
                "event_type": "token_created",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tx_hash": tx_hash,
                "block_number": block_number,
                "log_index": log_index,
                "factory": settings.factory_address,
                "token_name": token_name,
                "token_symbol": token_symbol,
                "address_0": address_0,
                "address_1": address_1,
                "predicted_token_address": predicted_token_address,
                "prediction_match": prediction_match,
            }

            logger.info("token_created_event", extra=event_data)

            event_key = (tx_hash, log_index)
            if self.jsonl_writer and event_key not in self.store.event_written_keys:
                await self.jsonl_writer.write_event(event_data)
                self.store.event_written_keys.add(event_key)

                # Emit to EventBus for trading triggers
                if self.event_bus:
                    await self.event_bus.emit(event_data)

        except Exception as e:  # noqa: BLE001
            logger.exception("token_created_handler_failed", extra={"error": str(e)})

    async def _handle_removed(
        self, tx_hash: str, log_index: int, block_number: Any
    ) -> None:
        """Handle a log removal due to chain reorg.

        Restores the nonce reservation when previously committed and clears
        internal dedup state so future re-added logs can be processed.

        Args:
            tx_hash: Transaction hash for the removed log.
            log_index: Log index within the block.
            block_number: The block number from which the log was removed.
        """
        logger.warning(
            "token_created_log_removed_reorg_detected",
            extra={
                "tx_hash": tx_hash,
                "log_index": log_index,
                "block_number": block_number,
            },
        )
        if self.nonce_tracker and tx_hash in self.store.committed_nonces:
            reserved_nonce = self.store.committed_nonces[tx_hash]
            try:
                await self.nonce_tracker.handle_reorg_removal(
                    settings.factory_address, tx_hash, reserved_nonce
                )
                logger.info(
                    "reorg_removal_nonce_restored",
                    extra={"tx_hash": tx_hash, "reserved_nonce": reserved_nonce},
                )
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "reorg_removal_handling_failed",
                    extra={"tx_hash": tx_hash, "error": str(e)},
                )
        key = (tx_hash, log_index)
        self.store.processed_log_keys.discard(key)
        self.store.event_written_keys.discard(key)
        self.store.committed_nonces.pop(tx_hash, None)
        logger.info(
            "reorg_removal_cleanup_complete",
            extra={"tx_hash": tx_hash, "log_index": log_index},
        )
