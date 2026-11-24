"""Event bus and models for trading triggers.

This module provides an in-process asyncio.Queue-based EventBus for real-time
trading triggers and TypedDict models for create_token_detected and token_created
events.
"""

from pumptires.app.events.bus import EventBus
from pumptires.app.events.models import CreateTokenDetected, TokenCreated

__all__ = ["EventBus", "CreateTokenDetected", "TokenCreated"]
