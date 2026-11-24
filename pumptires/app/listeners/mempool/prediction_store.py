from __future__ import annotations

import time
from typing import Dict, Set, Tuple

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class PredictionStore:
    """Holds prediction/reconciliation state and processing counters.

    Encapsulates in-memory state for deduplication and reconciliation along with
    lightweight counters and periodic logging of gate metrics.
    """

    def __init__(self) -> None:
        # Reconciliation/dedup state
        self.processed_log_keys: Set[tuple[str, int]] = set()
        self.event_written_keys: Set[tuple[str, int]] = set()
        self.pending_written_tx_hashes: Set[str] = set()
        # tx_hash -> (predicted_address, timestamp, factory_nonce)
        self.prediction_correlations: Dict[str, tuple[str, float, int]] = {}
        # tx_hash -> committed factory nonce (for reorg recovery)
        self.committed_nonces: Dict[str, int] = {}

        # Processing counters (pending tx path)
        self.counter_pending_received: int = 0
        self.counter_fetch_success: int = 0
        self.counter_fetch_failed: int = 0
        self.counter_to_factory_match: int = 0
        self.counter_selector_match: int = 0
        self.counter_create_token_written: int = 0

        self._last_counter_log_time: float = 0.0

    # Counter helpers
    def inc_pending_received(self) -> None:
        self.counter_pending_received += 1
        self._log_if_needed()

    def inc_fetch_success(self) -> None:
        self.counter_fetch_success += 1
        self._log_if_needed()

    def inc_fetch_failed(self) -> None:
        self.counter_fetch_failed += 1
        self._log_if_needed()

    def inc_to_factory_match(self) -> None:
        self.counter_to_factory_match += 1
        self._log_if_needed()

    def inc_selector_match(self) -> None:
        self.counter_selector_match += 1
        self._log_if_needed()

    def inc_create_token_written(self) -> None:
        self.counter_create_token_written += 1
        self._log_if_needed()

    # Prediction correlation helpers
    def set_correlation(self, tx_hash: str, predicted: str, factory_nonce: int) -> None:
        self.prediction_correlations[tx_hash] = (predicted, time.time(), factory_nonce)

    def pop_correlation(self, tx_hash: str) -> tuple[str, float, int] | None:
        return self.prediction_correlations.pop(tx_hash, None)

    def _log_if_needed(self) -> None:
        now = time.time()
        if now - self._last_counter_log_time >= 60.0:
            logger.info(
                "pending_tx_processing_counters",
                extra={
                    "received": self.counter_pending_received,
                    "fetch_success": self.counter_fetch_success,
                    "fetch_failed": self.counter_fetch_failed,
                    "to_factory_match": self.counter_to_factory_match,
                    "selector_match": self.counter_selector_match,
                    "create_token_written": self.counter_create_token_written,
                },
            )
            self._last_counter_log_time = now
