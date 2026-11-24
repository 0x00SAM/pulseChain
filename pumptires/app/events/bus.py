"""In-process EventBus for real-time trading triggers.

This module provides an asyncio.Queue-based EventBus that serves as the primary
trigger channel for the TradingRunner. Producers emit events after JSONL writes,
and the TradingRunner consumes events to trigger Buy/Sell operations.
"""

import asyncio
from typing import Any


class EventBus:
    """Async queue-based event bus for trading triggers.

    Wraps asyncio.Queue and provides emit/get methods with statistics tracking.
    In-memory only; events are lost on restart (JSONL persists for audit).

    Attributes:
        maxsize: Maximum queue size (0 = unbounded).
        high_watermark: Maximum queue size reached since instantiation.
        emit_count: Total number of events emitted since instantiation.
    """

    def __init__(self, maxsize: int = 0) -> None:
        """Initialize EventBus with specified queue size.

        Args:
            maxsize: Maximum number of events in queue. 0 means unbounded.
        """
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._emit_count: int = 0
        self._high_watermark: int = 0

    async def emit(self, event: dict[str, Any]) -> None:
        """Enqueue an event to the bus.

        Args:
            event: Event dictionary to enqueue (typically CreateTokenDetected
                or TokenCreated).

        Raises:
            asyncio.QueueFull: If queue is full and maxsize > 0.
        """
        await self._queue.put(event)
        self._emit_count += 1
        current_size = self._queue.qsize()
        if current_size > self._high_watermark:
            self._high_watermark = current_size

    async def get(self) -> dict[str, Any]:
        """Dequeue an event from the bus (FIFO).

        Returns:
            Event dictionary.

        Raises:
            asyncio.CancelledError: If task is cancelled while waiting.
        """
        return await self._queue.get()

    @property
    def high_watermark(self) -> int:
        """Maximum queue size reached since instantiation.

        Returns:
            Integer representing peak queue depth.
        """
        return self._high_watermark

    @property
    def emit_count(self) -> int:
        """Total number of events emitted since instantiation.

        Returns:
            Integer count of emitted events.
        """
        return self._emit_count

    @property
    def qsize(self) -> int:
        """Current number of events in the queue.

        Returns:
            Integer representing current queue size.
        """
        return self._queue.qsize()
