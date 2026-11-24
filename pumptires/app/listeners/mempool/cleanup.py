from __future__ import annotations

import asyncio
import time
from typing import Optional

from pumptires.app.config.settings import settings
from pumptires.app.services.nonce_tracker import NonceTracker
from pumptires.app.utils.logger import get_logger
from .prediction_store import PredictionStore

logger = get_logger(__name__)


class CleanupManager:
    """Manages periodic cleanup of stale prediction correlations."""

    def __init__(
        self,
        store: PredictionStore,
        nonce_tracker: Optional[NonceTracker],
        interval_seconds: float = 300.0,
        max_age_seconds: float = 3600.0,
    ) -> None:
        self.store = store
        self.nonce_tracker = nonce_tracker
        self.interval_seconds = interval_seconds
        self.max_age_seconds = max_age_seconds
        self._task: asyncio.Task | None = None
        self._running = False

    async def cleanup_once(self) -> None:
        """Clean one cycle of stale prediction correlations and release nonces."""
        current_time = time.time()
        to_remove: list[str] = []

        for tx_hash, (predicted_addr, timestamp, nonce) in list(
            self.store.prediction_correlations.items()
        ):
            age = current_time - timestamp
            if age > self.max_age_seconds:
                to_remove.append(tx_hash)
                if self.nonce_tracker:
                    try:
                        await self.nonce_tracker.release(settings.factory_address, tx_hash)
                        logger.debug(
                            "stale_prediction_cleaned_nonce_released",
                            extra={
                                "tx_hash": tx_hash,
                                "age_seconds": age,
                                "predicted_address": predicted_addr,
                                "factory_nonce": nonce,
                            },
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "nonce_release_failed_during_cleanup",
                            extra={"tx_hash": tx_hash, "error": str(e)},
                        )

        for tx_hash in to_remove:
            self.store.prediction_correlations.pop(tx_hash, None)

        if to_remove:
            logger.info(
                "old_predictions_cleaned",
                extra={"count": len(to_remove), "max_age_seconds": self.max_age_seconds},
            )

    async def _loop(self) -> None:
        """Background loop that periodically runs cleanup_once()."""
        logger.info(
            "periodic_cleanup_started", extra={"interval_seconds": self.interval_seconds}
        )
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(self.interval_seconds)
                await self.cleanup_once()
        except asyncio.CancelledError:
            logger.info("periodic_cleanup_cancelled")
        except Exception as e:  # noqa: BLE001
            logger.exception("periodic_cleanup_error", extra={"error": str(e)})
        finally:
            self._running = False

    def start(self) -> None:
        """Start the periodic cleanup task if not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.debug("cleanup_task_started")

    async def stop(self) -> None:
        """Cancel and await the periodic cleanup task if running."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
