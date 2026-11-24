"""Main entry point for the PumpTires Pending Monitor.

Initializes the mempool listener and runs the monitoring system.
Uses uvloop for improved async performance on Linux.
"""

import asyncio
import signal
import sys

try:
    import uvloop

    # Install uvloop as the default event loop implementation
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    # uvloop not available, continue with default asyncio
    pass

from pumptires.app.config.settings import settings
from pumptires.app.io.jsonl_writer import JSONLWriter
from pumptires.app.listeners.mempool import MempoolListener
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__, level=settings.get_log_level())


class Application:
    """Main application orchestrator."""

    def __init__(self) -> None:
        """Initialize the application."""
        self.listener = MempoolListener()
        self.shutdown_event = asyncio.Event()

    def handle_shutdown_signal(self, signum: int) -> None:
        """Handle shutdown signals gracefully.

        Args:
            signum: Signal number received.
        """
        logger.info(
            "shutdown_signal_received",
            extra={"signal": signal.Signals(signum).name},
        )
        self.shutdown_event.set()

    async def run(self) -> None:
        """Run the application main loop."""
        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda s=sig: self.handle_shutdown_signal(s)  # type: ignore[misc]
            )

        logger.info(
            "application_starting",
            extra={
                "factory_address": settings.factory_address,
                "wss_url": settings.pulsechain_wss_url,
                "log_level": settings.log_level,
                "event_log_path": str(settings.get_event_log_path()),
            },
        )

        # Initialize output file early to ensure it exists
        try:
            jsonl_writer = JSONLWriter(settings.get_event_log_path())
            await jsonl_writer.initialize()
            logger.info(
                "output_file_initialized",
                extra={"filepath": str(settings.get_event_log_path())},
            )
        except Exception as e:
            logger.exception(
                "output_file_initialization_failed",
                extra={
                    "filepath": str(settings.get_event_log_path()),
                    "error": str(e),
                },
            )
            # Continue anyway - the listener will try to initialize it again

        # Start the mempool listener
        listener_task = asyncio.create_task(self.listener.start())

        # Wait for shutdown signal
        await self.shutdown_event.wait()

        # Stop the listener gracefully
        await self.listener.stop()

        # Cancel the listener task
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

        logger.info("application_stopped")

    async def start(self) -> None:
        """Start the application with error handling."""
        try:
            await self.run()
        except KeyboardInterrupt:
            logger.info("application_interrupted_by_user")
        except Exception as e:
            logger.exception(
                "application_fatal_error",
                extra={"error": str(e)},
            )
            sys.exit(1)


async def main() -> None:
    """Main async entry point."""
    app = Application()
    await app.start()


def cli_entry() -> None:
    """CLI entry point for the application."""
    logger.info(
        "pumptires_monitor_initializing",
        extra={
            "version": "0.1.0",
            "python_version": sys.version,
            "event_loop": asyncio.get_event_loop_policy().__class__.__name__,
        },
    )

    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception(
            "application_startup_failed",
            extra={"error": str(e)},
        )
        sys.exit(1)


if __name__ == "__main__":
    cli_entry()
