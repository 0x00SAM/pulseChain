"""JSONL file writer for event logging.

Provides async file writing for logging detected createToken events
to a JSONL (JSON Lines) file for later analysis or processing.
"""

import asyncio
from pathlib import Path
from typing import Any

import aiofiles  # type: ignore[import-untyped]
import orjson

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class JSONLWriter:
    """Asynchronous JSONL file writer for event logging."""

    def __init__(self, filepath: Path | str) -> None:
        """Initialize the JSONL writer.

        Args:
            filepath: Path to the JSONL file.
        """
        self.filepath = Path(filepath)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the writer and create directories and file if needed."""
        async with self._lock:
            if not self._initialized:
                # Create parent directories if they don't exist
                self.filepath.parent.mkdir(parents=True, exist_ok=True)

                # Create the file if it doesn't exist
                if not self.filepath.exists():
                    # Create empty file
                    async with aiofiles.open(
                        self.filepath, mode="w", encoding="utf-8"
                    ) as f:
                        await f.write("")  # Create empty file
                    logger.info(
                        "jsonl_writer_initialized_new",
                        extra={"filepath": str(self.filepath)},
                    )
                else:
                    logger.info(
                        "jsonl_writer_initialized_existing",
                        extra={"filepath": str(self.filepath)},
                    )

                self._initialized = True

    async def write_event(self, event: dict[str, Any]) -> None:
        """Write an event to the JSONL file.

        Args:
            event: Event dictionary to write.
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Serialize to JSON bytes
            json_bytes = orjson.dumps(event)
            json_line = json_bytes.decode("utf-8") + "\n"

            # Write to file asynchronously
            async with self._lock:
                async with aiofiles.open(
                    self.filepath, mode="a", encoding="utf-8"
                ) as f:
                    await f.write(json_line)

            logger.debug(
                "event_written_to_jsonl",
                extra={
                    "filepath": str(self.filepath),
                    "event_type": event.get("event_type", "unknown"),
                },
            )

        except Exception as e:
            logger.exception(
                "jsonl_write_failed",
                extra={
                    "filepath": str(self.filepath),
                    "error": str(e),
                    "event": event,
                },
            )

    async def write_events_batch(self, events: list[dict[str, Any]]) -> None:
        """Write multiple events to the JSONL file in a batch.

        Args:
            events: List of event dictionaries to write.
        """
        if not self._initialized:
            await self.initialize()

        if not events:
            return

        try:
            # Serialize all events
            lines = []
            for event in events:
                json_bytes = orjson.dumps(event)
                lines.append(json_bytes.decode("utf-8"))

            # Write all lines at once
            async with self._lock:
                async with aiofiles.open(
                    self.filepath, mode="a", encoding="utf-8"
                ) as f:
                    await f.write("\n".join(lines) + "\n")

            logger.debug(
                "events_batch_written_to_jsonl",
                extra={
                    "filepath": str(self.filepath),
                    "count": len(events),
                },
            )

        except Exception as e:
            logger.exception(
                "jsonl_batch_write_failed",
                extra={
                    "filepath": str(self.filepath),
                    "error": str(e),
                    "event_count": len(events),
                },
            )

    async def close(self) -> None:
        """Close the writer (placeholder for future cleanup)."""
        logger.info(
            "jsonl_writer_closed",
            extra={"filepath": str(self.filepath)},
        )

