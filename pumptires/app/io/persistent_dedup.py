"""Persistent deduplication storage for transaction hashes.

Maintains a simple JSON-based cache of recently written transaction hashes
to prevent duplicates after restarts or reconnects. Bounded by maximum size.
"""

import asyncio
import json
from pathlib import Path
from typing import Set

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class PersistentDedup:
    """Persistent deduplication tracker for transaction hashes."""

    def __init__(
        self,
        filepath: Path | str,
        max_size: int = 10000,
    ) -> None:
        """Initialize persistent dedup tracker.

        Args:
            filepath: Path to JSON file for storing seen hashes.
            max_size: Maximum number of hashes to keep (FIFO eviction).
        """
        self.filepath = Path(filepath)
        self.max_size = max_size
        self._lock = asyncio.Lock()
        self._seen: Set[str] = set()
        self._ordered_list: list[str] = []  # Track insertion order for FIFO

    async def initialize(self) -> None:
        """Load existing hashes from disk if available."""
        async with self._lock:
            if self.filepath.exists():
                try:
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    hashes = data.get("seen_hashes", [])
                    self._seen = set(hashes)
                    self._ordered_list = list(hashes)
                    logger.info(
                        "persistent_dedup_loaded",
                        extra={
                            "filepath": str(self.filepath),
                            "count": len(self._seen),
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "persistent_dedup_load_failed",
                        extra={
                            "filepath": str(self.filepath),
                            "error": str(e),
                        },
                    )
                    # Start fresh if load fails
                    self._seen = set()
                    self._ordered_list = []
            else:
                logger.info(
                    "persistent_dedup_initialized_empty",
                    extra={"filepath": str(self.filepath)},
                )
                await self._persist()

    async def contains(self, tx_hash: str) -> bool:
        """Check if transaction hash was already seen.

        Args:
            tx_hash: Transaction hash to check.

        Returns:
            True if hash was previously seen.
        """
        async with self._lock:
            return tx_hash in self._seen

    async def add(self, tx_hash: str) -> None:
        """Add transaction hash to seen set and persist.

        Args:
            tx_hash: Transaction hash to add.
        """
        async with self._lock:
            if tx_hash in self._seen:
                return

            # Add to set and ordered list
            self._seen.add(tx_hash)
            self._ordered_list.append(tx_hash)

            # Enforce max size with FIFO eviction
            if len(self._ordered_list) > self.max_size:
                to_remove = self._ordered_list[0]
                self._ordered_list = self._ordered_list[1:]
                self._seen.discard(to_remove)
                logger.debug(
                    "persistent_dedup_evicted_oldest",
                    extra={"tx_hash": to_remove},
                )

            # Persist to disk
            await self._persist()

    async def _persist(self) -> None:
        """Write current seen set to disk."""
        try:
            # Ensure parent directory exists
            self.filepath.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically by writing to temp file then moving
            temp_path = self.filepath.with_suffix(".tmp")
            data = {"seen_hashes": self._ordered_list}

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            # Atomic move
            temp_path.replace(self.filepath)

            logger.debug(
                "persistent_dedup_persisted",
                extra={"filepath": str(self.filepath), "count": len(self._seen)},
            )
        except Exception as e:
            logger.warning(
                "persistent_dedup_persist_failed",
                extra={"filepath": str(self.filepath), "error": str(e)},
            )

    async def close(self) -> None:
        """Final persistence on close."""
        async with self._lock:
            await self._persist()
        logger.info(
            "persistent_dedup_closed",
            extra={"filepath": str(self.filepath)},
        )

