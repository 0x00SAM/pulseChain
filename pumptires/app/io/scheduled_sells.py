"""Persistent storage for scheduled sell tasks.

Maintains a JSON-based store of scheduled sells to enable recovery after
process restarts. Follows the same patterns as persistent_dedup.py with
async operations, atomic writes, and FIFO eviction.
"""

import asyncio
import json
import time
from pathlib import Path

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class ScheduledSells:
    """Persistent tracker for scheduled sell tasks."""

    def __init__(
        self,
        filepath: Path | str,
        max_size: int = 10000,
    ) -> None:
        """Initialize scheduled sells tracker.

        Args:
            filepath: Path to JSON file for storing scheduled sells.
            max_size: Maximum number of entries to keep (FIFO eviction).
        """
        self.filepath = Path(filepath)
        self.max_size = max_size
        self._lock = asyncio.Lock()
        self._scheduled: dict[str, dict] = {}  # token_address -> entry
        self._ordered_list: list[str] = []  # Track insertion order for FIFO

    async def initialize(self) -> None:
        """Load existing scheduled sells from disk if available."""
        async with self._lock:
            if self.filepath.exists():
                try:
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    
                    scheduled_list = data.get("scheduled_sells", [])
                    
                    # Rebuild internal structures
                    self._scheduled = {}
                    self._ordered_list = []
                    
                    for entry in scheduled_list:
                        token_address = entry["token_address"]
                        self._scheduled[token_address] = entry
                        self._ordered_list.append(token_address)
                    
                    logger.info(
                        "scheduled_sells_loaded",
                        extra={
                            "filepath": str(self.filepath),
                            "count": len(self._scheduled),
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "scheduled_sells_load_failed",
                        extra={
                            "filepath": str(self.filepath),
                            "error": str(e),
                        },
                    )
                    # Start fresh if load fails
                    self._scheduled = {}
                    self._ordered_list = []
            else:
                logger.info(
                    "scheduled_sells_initialized_empty",
                    extra={"filepath": str(self.filepath)},
                )
                await self._persist()

    async def add(
        self,
        token_address: str,
        buy_tx_hash: str,
        execute_at: int,
    ) -> None:
        """Add or update a scheduled sell entry and persist.

        Args:
            token_address: Token address for the scheduled sell.
            buy_tx_hash: Buy transaction hash for correlation.
            execute_at: Unix timestamp when sell should execute.
        """
        async with self._lock:
            # Check if entry already exists with same data (idempotency)
            if token_address in self._scheduled:
                existing = self._scheduled[token_address]
                if (
                    existing["buy_tx_hash"] == buy_tx_hash
                    and existing["execute_at"] == execute_at
                ):
                    # Identical entry, no-op
                    return
                
                # Update existing entry (remove from ordered list, will re-add)
                self._ordered_list.remove(token_address)

            # Add/update entry
            entry = {
                "token_address": token_address,
                "buy_tx_hash": buy_tx_hash,
                "execute_at": execute_at,
            }
            self._scheduled[token_address] = entry
            self._ordered_list.append(token_address)

            # Enforce max size with FIFO eviction
            if len(self._ordered_list) > self.max_size:
                to_remove = self._ordered_list[0]
                self._ordered_list = self._ordered_list[1:]
                del self._scheduled[to_remove]
                logger.debug(
                    "scheduled_sells_evicted_oldest",
                    extra={"token_address": to_remove},
                )

            # Persist to disk
            await self._persist()
            
            logger.debug(
                "scheduled_sell_added",
                extra={
                    "token_address": token_address,
                    "buy_tx_hash": buy_tx_hash,
                    "execute_at": execute_at,
                },
            )

    async def remove(self, token_address: str) -> None:
        """Remove a scheduled sell entry and persist.

        Args:
            token_address: Token address to remove.
        """
        async with self._lock:
            if token_address in self._scheduled:
                del self._scheduled[token_address]
                self._ordered_list.remove(token_address)
                
                # Persist to disk
                await self._persist()
                
                logger.debug(
                    "scheduled_sell_removed",
                    extra={"token_address": token_address},
                )

    async def list(self) -> list[dict]:
        """Return list of all scheduled sell entries.

        Returns:
            List of entries, each with token_address, buy_tx_hash, execute_at.
        """
        async with self._lock:
            # Return ordered list of entries
            return [self._scheduled[addr] for addr in self._ordered_list]

    async def close(self) -> None:
        """Final persistence and cleanup on shutdown."""
        async with self._lock:
            await self._persist()
        logger.info(
            "scheduled_sells_closed",
            extra={"filepath": str(self.filepath)},
        )

    async def _persist(self) -> None:
        """Write current scheduled sells to disk atomically."""
        try:
            # Ensure parent directory exists
            self.filepath.parent.mkdir(parents=True, exist_ok=True)

            # Build ordered list of entries for persistence
            scheduled_list = [self._scheduled[addr] for addr in self._ordered_list]

            # Write atomically by writing to temp file then moving
            temp_path = self.filepath.with_suffix(".tmp")
            data = {"scheduled_sells": scheduled_list}

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            # Atomic move
            temp_path.replace(self.filepath)

            logger.debug(
                "scheduled_sells_persisted",
                extra={
                    "filepath": str(self.filepath),
                    "count": len(self._scheduled),
                },
            )
        except Exception as e:
            logger.warning(
                "scheduled_sells_persist_failed",
                extra={
                    "filepath": str(self.filepath),
                    "error": str(e),
                },
            )
