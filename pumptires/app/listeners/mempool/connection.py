from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from web3 import AsyncWeb3
from web3.contract import AsyncContract
from web3.providers import AsyncHTTPProvider, WebSocketProvider

from pumptires.app.config.settings import settings
from pumptires.app.contracts.factory import get_factory_contract
from pumptires.app.io.jsonl_writer import JSONLWriter
from pumptires.app.io.persistent_dedup import PersistentDedup
from pumptires.app.services.nonce_tracker import NonceTracker
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class ConnectionManager:
    """Manages RPC clients and provider lifecycle."""

    def __init__(self) -> None:
        self.web3: AsyncWeb3 | None = None
        self.provider: WebSocketProvider | None = None
        self.http_web3: AsyncWeb3 | None = None
        self.factory_contract: AsyncContract | None = None
        self.nonce_tracker: NonceTracker | None = None
        self.jsonl_writer: JSONLWriter | None = None
        self.persistent_dedup: PersistentDedup | None = None
        self.chain_id: int | None = None

    async def connect(self) -> None:
        """Establish RPC clients, verify chain ID, and initialize IO/services.

        Raises:
            ConnectionError: If the WebSocket connection or chain ID check fails.
        """
        try:
            self.provider = WebSocketProvider(
                settings.pulsechain_wss_url,
                websocket_kwargs={
                    "ping_interval": settings.websocket_ping_interval,
                    "ping_timeout": settings.websocket_ping_timeout,
                    "close_timeout": 10,
                },
            )
            self.web3 = AsyncWeb3(self.provider)
            await self.provider.connect()

            # Give the websocket a moment to establish
            await asyncio.sleep(1)

            is_connected = await self.web3.is_connected()
            if not is_connected:
                logger.warning(
                    "websocket_connection_check_failed_retrying",
                    extra={"wss_url": settings.pulsechain_wss_url},
                )
                await asyncio.sleep(3)
                is_connected = await self.web3.is_connected()

            if not is_connected:
                try:
                    chain_id = await self.web3.eth.chain_id
                    logger.info(
                        "connection_established_via_chain_id_check",
                        extra={"chain_id": chain_id},
                    )
                except Exception as chain_error:  # noqa: BLE001
                    logger.exception(
                        "connection_verification_failed",
                        extra={"error": str(chain_error)},
                    )
                    raise ConnectionError(
                        f"Failed to connect to PulseChain WebSocket: {chain_error}"
                    )
            else:
                chain_id = await self.web3.eth.chain_id

            expected_chain_id = settings.expected_chain_id
            if expected_chain_id is not None and chain_id != expected_chain_id:
                raise ConnectionError(
                    f"Unexpected chain ID {chain_id}; expected {expected_chain_id}"
                )

            self.chain_id = chain_id

            # Separate HTTP client
            http_provider = AsyncHTTPProvider(settings.pulsechain_http_url)
            self.http_web3 = AsyncWeb3(http_provider)

            # Initialize factory contract
            self.factory_contract = get_factory_contract(self.web3)

            # Nonce tracker seeded from HTTP client
            self.nonce_tracker = NonceTracker(self.http_web3)
            try:
                factory_checksum = self.web3.to_checksum_address(
                    settings.factory_address
                )
                factory_nonce = await self.http_web3.eth.get_transaction_count(
                    factory_checksum, "latest"
                )
                await self.nonce_tracker.seed(factory_checksum, factory_nonce)
                logger.info(
                    "nonce_tracker_seeded",
                    extra={
                        "factory_address": factory_checksum,
                        "initial_nonce": factory_nonce,
                    },
                )
            except Exception as seed_error:  # noqa: BLE001
                logger.exception(
                    "nonce_tracker_seed_failed",
                    extra={
                        "factory_address": settings.factory_address,
                        "error": str(seed_error),
                    },
                )

            # IO components
            self.jsonl_writer = JSONLWriter(settings.get_event_log_path())
            await self.jsonl_writer.initialize()

            dedup_path = Path("data/seen_pending_tx.json")
            self.persistent_dedup = PersistentDedup(
                dedup_path, max_size=settings.dedup_max_size
            )
            await self.persistent_dedup.initialize()

            logger.info(
                "pulsechain_connected",
                extra={
                    "wss_url": settings.pulsechain_wss_url,
                    "chain_id": chain_id,
                    "expected_chain_id": expected_chain_id,
                    "factory_address": settings.factory_address,
                    "event_log_path": str(settings.get_event_log_path()),
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "pulsechain_connection_failed",
                extra={"wss_url": settings.pulsechain_wss_url, "error": str(e)},
            )
            raise

    async def disconnect(self) -> None:
        """Close IO components and providers, cleaning up references."""
        # Close JSONL writer first
        if self.jsonl_writer:
            try:
                await self.jsonl_writer.close()
            except Exception as e:  # noqa: BLE001
                logger.exception("jsonl_writer_close_failed", extra={"error": str(e)})

        # Close persistent dedup
        if self.persistent_dedup:
            try:
                await self.persistent_dedup.close()
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "persistent_dedup_close_failed", extra={"error": str(e)}
                )

        # Provider
        if self.provider:
            try:
                await asyncio.wait_for(
                    self._safe_disconnect_provider(),
                    timeout=settings.disconnect_timeout,
                )
                logger.info("pulsechain_disconnected")
            except asyncio.TimeoutError:
                logger.warning("pulsechain_disconnect_timeout")
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "pulsechain_disconnect_failed", extra={"error": str(e)}
                )

        # Clean references
        self.provider = None
        self.web3 = None
        self.chain_id = None
        if self.http_web3 and hasattr(self.http_web3.provider, "disconnect"):
            try:
                await self.http_web3.provider.disconnect()
            except Exception as e:  # noqa: BLE001
                logger.debug("http_client_close_failed", extra={"error": str(e)})
        self.http_web3 = None
        self.factory_contract = None
        self.nonce_tracker = None
        self.jsonl_writer = None
        self.persistent_dedup = None

    async def _safe_disconnect_provider(self) -> None:
        """Safely disconnect the WebSocket provider.

        Cancels the internal message listener task if present and suppresses
        any disconnect errors to avoid masking shutdown paths.
        """
        try:
            if self.provider and hasattr(self.provider, "_message_listener_task"):
                try:
                    self.provider._message_listener_task.cancel()
                except Exception:
                    pass
            if self.provider:
                await self.provider.disconnect()
        except Exception:
            pass
