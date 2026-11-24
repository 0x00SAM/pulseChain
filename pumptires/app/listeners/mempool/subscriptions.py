from __future__ import annotations

import asyncio
from typing import Any, Callable, cast

from eth_typing import HexStr
from web3 import AsyncWeb3
from web3.types import FilterParams
from web3.utils.subscriptions import LogsSubscription, PendingTxSubscription

from pumptires.app.config.settings import settings
from pumptires.app.utils.logger import get_logger

from .exceptions import SubscriptionStopped

logger = get_logger(__name__)


class SubscriptionRunner:
    """Sets up and runs WebSocket subscriptions with retry and backfill.

    Creates pending-tx and logs subscriptions, performs a lightweight log
    backfill at startup, and runs the subscription manager with timeout-based
    reconnect signaling via SubscriptionStopped.
    """

    def __init__(
        self,
        web3: AsyncWeb3,
        http_web3: AsyncWeb3 | None,
        factory_contract,
        on_pending_handler: Callable[[Any], Any],
        on_token_created_handler: Callable[[Any], Any],
    ) -> None:
        self.web3 = web3
        self.http_web3 = http_web3
        self.factory_contract = factory_contract
        self.on_pending = on_pending_handler
        self.on_token_created = on_token_created_handler
        self._is_running = False

    async def backfill_recent_token_created_logs(
        self, last_n_blocks: int = 100
    ) -> None:
        """Backfill recent TokenCreated logs before live subscriptions.

        Args:
            last_n_blocks: Number of blocks to scan backward from latest.
        """
        if not self.http_web3 or not self.factory_contract:
            logger.warning("backfill_skipped_missing_clients")
            return
        try:
            latest = await self.http_web3.eth.block_number
            from_block = max(0, latest - last_n_blocks)

            topic = self.factory_contract.events.TokenCreated().topic
            filter_params: FilterParams = {
                "address": self.http_web3.to_checksum_address(settings.factory_address),
                "fromBlock": from_block,
                "toBlock": "latest",
                "topics": [topic],
            }

            logger.info(
                "backfill_start", extra={"from_block": from_block, "to_block": latest}
            )
            logs = await self.http_web3.eth.get_logs(filter_params)
            for log in logs:
                context = type("_Ctx", (), {})()
                setattr(context, "result", log)
                setattr(
                    context,
                    "handler_context",
                    {
                        "token_created_event": self.factory_contract.events.TokenCreated()
                    },
                )
                await self.on_token_created(context)
            logger.info("backfill_complete", extra={"count": len(logs)})
        except Exception as e:  # noqa: BLE001
            logger.exception("backfill_failed", extra={"error": str(e)})

    async def run(self) -> None:
        """Create subscriptions, handle events until timeout/cancel/error."""
        if not self.web3:
            raise RuntimeError("Not connected to PulseChain")

        subscription_ids: list[str] = []
        try:
            logger.info(
                "subscribing_to_pending_transactions",
                extra={
                    "factory_address": settings.factory_address,
                    "function_selector": settings.create_token_selector,
                },
            )

            pending_sub = PendingTxSubscription(
                full_transactions=True,
                handler=self.on_pending,
                label="pending_tx_listener",
            )
            token_created_event = (
                self.factory_contract.events.TokenCreated()
                if self.factory_contract
                else None
            )
            logs_sub = LogsSubscription(
                label="token_created_logs",
                address=self.web3.to_checksum_address(settings.factory_address),
                topics=[token_created_event.topic] if token_created_event else [],
                handler=self.on_token_created,
                handler_context=(
                    {"token_created_event": token_created_event}
                    if token_created_event
                    else {}
                ),
            )

            # Backfill before live subscriptions
            await self.backfill_recent_token_created_logs(last_n_blocks=100)

            logger.debug(
                "attempting_subscription",
                extra={"subscription_type": "newPendingTransactions"},
            )

            for sub in (pending_sub, logs_sub):
                sub_id: str | None = None
                for attempt in range(1, 4):
                    try:
                        sub_id = await self.web3.subscription_manager.subscribe(sub)
                        break
                    except Exception as e:  # noqa: BLE001
                        err_str = str(e)
                        if (
                            "-32602" in err_str
                            or "expected subscription name as first argument" in err_str
                        ):
                            logger.warning(
                                "subscription_retry_on_32602",
                                extra={
                                    "attempt": attempt,
                                    "max_attempts": 3,
                                    "sub_label": getattr(sub, "label", "unknown"),
                                    "error": err_str,
                                },
                            )
                            await asyncio.sleep(0.5 * attempt)
                            continue
                        raise
                if not sub_id:
                    raise RuntimeError("subscription_failed_after_retries")
                subscription_ids.append(sub_id)

            logger.info(
                "subscriptions_started",
                extra={"subscription_ids": [str(i) for i in subscription_ids]},
            )
            logger.info(
                "pending_tx_subscription_active",
                extra={
                    "subscription_id": (
                        str(subscription_ids[0])
                        if len(subscription_ids) > 0
                        else "unknown"
                    ),
                    "subscription_type": "newPendingTransactions",
                },
            )
            logger.info(
                "token_created_logs_subscription_active",
                extra={
                    "subscription_id": (
                        str(subscription_ids[1])
                        if len(subscription_ids) > 1
                        else "unknown"
                    ),
                    "subscription_type": "logs",
                    "factory_address": settings.factory_address,
                },
            )

            self._is_running = True
            try:
                await asyncio.wait_for(
                    self.web3.subscription_manager.handle_subscriptions(
                        run_forever=True
                    ),
                    timeout=settings.subscription_timeout,
                )
                logger.warning(
                    "subscription_handler_stopped",
                    extra={"subscription_ids": [str(i) for i in subscription_ids]},
                )
                raise SubscriptionStopped("Subscription handler stopped without error")
            except asyncio.TimeoutError:
                logger.warning(
                    "subscription_handler_timeout",
                    extra={"subscription_ids": [str(i) for i in subscription_ids]},
                )
                return
            except asyncio.CancelledError as cancelled:
                logger.warning(
                    "subscription_handler_cancelled",
                    extra={"subscription_ids": [str(i) for i in subscription_ids]},
                )
                raise SubscriptionStopped(
                    "Subscription handler cancelled"
                ) from cancelled
            except Exception as e:  # noqa: BLE001
                error_str = str(e)
                logger.exception(
                    "subscription_handler_error",
                    extra={
                        "subscription_ids": [str(i) for i in subscription_ids],
                        "error": error_str,
                        "error_type": type(e).__name__,
                    },
                )
                return
        finally:
            self._is_running = False
            if self.web3 and subscription_ids:
                try:
                    for sub_id in subscription_ids:
                        try:
                            await self.web3.eth._unsubscribe(cast(HexStr, sub_id))
                        except Exception:
                            pass
                except Exception as e:  # noqa: BLE001
                    logger.debug("unsubscribe_cleanup_error", extra={"error": str(e)})
