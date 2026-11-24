"""Advanced gas estimation with mempool sampling.

This module provides production-ready gas estimation including:
- Multi-block historical sampling
- Mempool tip analysis with interpolated percentiles
- Dynamic fallback strategies
- Congestion detection
"""

import math
from typing import Any

from web3 import AsyncWeb3

from pumptires.app.trading.gas_oracle import GasOracle
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


async def sample_gas_from_recent_blocks(
    web3: AsyncWeb3, gas_oracle: GasOracle, num_blocks: int = 5, txs_per_block: int = 50
) -> None:
    """Sample gas prices from recent blocks for better estimation.

    Args:
        web3: AsyncWeb3 instance.
        gas_oracle: GasOracle to populate with observations.
        num_blocks: Number of recent blocks to sample.
        txs_per_block: Maximum transactions to sample per block.
    """
    try:
        latest_block = await web3.eth.get_block("latest")
        current_block_number = latest_block.get("number", 0)

        sampled_count = 0
        for i in range(num_blocks):
            block_num = current_block_number - i
            if block_num < 0:
                break

            try:
                block = await web3.eth.get_block(block_num, full_transactions=True)
                tx_list = block.get("transactions", [])

                for tx in tx_list[:txs_per_block]:
                    if isinstance(tx, dict):
                        priority_fee = tx.get("maxPriorityFeePerGas")
                        if priority_fee:
                            gas_oracle.observe(priority_fee)
                            sampled_count += 1

            except Exception as e:
                logger.warning(
                    "Failed to sample block",
                    extra={"block_num": block_num, "error": str(e)},
                )
                continue

        logger.info(
            "Multi-block gas sampling completed",
            extra={
                "blocks_sampled": num_blocks,
                "observations": sampled_count,
                "oracle_window": gas_oracle.observation_count(),
            },
        )

    except Exception as e:
        logger.error(
            "Multi-block gas sampling failed",
            extra={"error": str(e)},
            exc_info=True,
        )


def _compute_interpolated_percentile(sorted_vals: list[int], p: float) -> int:
    """Compute interpolated percentile for stability with small samples.

    Uses linear interpolation between floor and ceiling indices.

    Args:
        sorted_vals: Sorted list of values.
        p: Percentile (0.0 to 1.0).

    Returns:
        Interpolated percentile value.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0
    if n == 1:
        return sorted_vals[0]

    k = (n - 1) * p
    f = math.floor(k)
    c = math.ceil(k)

    if f == c:
        return sorted_vals[int(k)]

    low = sorted_vals[f]
    high = sorted_vals[c]
    return int(low + (high - low) * (k - f))


def _infer_tip_from_tx(tx: dict[str, Any], base_fee: int) -> int | None:
    """Infer tip from transaction data (handles EIP-1559 and legacy).

    Args:
        tx: Transaction dictionary from txpool.
        base_fee: Current base fee in wei.

    Returns:
        Inferred tip in wei, or None if unable to infer.
    """
    # Prefer explicit EIP-1559 priority tip
    v = tx.get("maxPriorityFeePerGas")
    if isinstance(v, str):
        try:
            return int(v, 16)
        except Exception:
            pass

    # Legacy gasPrice: infer tip as gasPrice - base_fee
    v = tx.get("gasPrice")
    if isinstance(v, str):
        try:
            gp = int(v, 16)
            return max(gp - base_fee, 0)
        except Exception:
            pass

    # EIP-1559 without explicit priority (rare): infer from maxFeePerGas
    v = tx.get("maxFeePerGas")
    if isinstance(v, str):
        try:
            mfg = int(v, 16)
            return max(mfg - base_fee, 0)
        except Exception:
            pass

    return None


async def sample_mempool_tips(
    web3: AsyncWeb3, base_fee: int, max_addresses: int = 200, txs_per_address: int = 5
) -> tuple[list[int], dict[str, int]]:
    """Sample tips from pending transactions in mempool via txpool_content.

    Args:
        web3: AsyncWeb3 instance.
        base_fee: Current base fee in wei.
        max_addresses: Maximum addresses to sample from txpool.
        txs_per_address: Maximum transactions per address to sample.

    Returns:
        Tuple of (list of tips in wei, stats dict with percentiles).
    """
    try:
        txpool = await web3.provider.make_request("txpool_content", [])
        pending_txs = txpool.get("result", {}).get("pending", {}) or {}

        mempool_tips: list[int] = []

        # Sample transactions from multiple addresses
        for addr_txs in list(pending_txs.values())[:max_addresses]:
            if not isinstance(addr_txs, dict):
                continue

            for tx in list(addr_txs.values())[:txs_per_address]:
                if isinstance(tx, dict):
                    tip = _infer_tip_from_tx(tx, base_fee)
                    if tip is not None and tip > 0:
                        mempool_tips.append(tip)

        if mempool_tips:
            mempool_tips.sort()

            # Compute percentiles with interpolation
            p50 = _compute_interpolated_percentile(mempool_tips, 0.50)
            p75 = _compute_interpolated_percentile(mempool_tips, 0.75)
            p90 = _compute_interpolated_percentile(mempool_tips, 0.90)
            p95 = _compute_interpolated_percentile(mempool_tips, 0.95)

            stats = {
                "count": len(mempool_tips),
                "unique_values": len(set(mempool_tips)),
                "p50": p50,
                "p75": p75,
                "p90": p90,
                "p95": p95,
            }

            logger.info(
                "Mempool tip sampling completed",
                extra={
                    "samples": stats["count"],
                    "unique": stats["unique_values"],
                    "p50_gwei": p50 / 10**12,
                    "p75_gwei": p75 / 10**12,
                    "p90_gwei": p90 / 10**12,
                    "p95_gwei": p95 / 10**12,
                },
            )

            return mempool_tips, stats

        else:
            logger.warning("No mempool tips sampled")
            return [], {}

    except Exception as e:
        logger.warning(
            "Mempool tip sampling failed",
            extra={"error": str(e)},
        )
        return [], {}


def compute_dynamic_fallback_tip(base_fee: int, min_tip_gwei: int = 1000) -> int:
    """Compute dynamic fallback tip when mempool sampling unavailable.

    Uses heuristic: 15% of base fee, with minimum floor.

    Args:
        base_fee: Current base fee in wei.
        min_tip_gwei: Minimum tip floor in Gwei.

    Returns:
        Fallback tip in wei.
    """
    min_tip_wei = min_tip_gwei * 10**12
    fallback_tip = max(int(base_fee * 0.15), min_tip_wei)

    logger.info(
        "Using dynamic fallback tip",
        extra={
            "base_fee_gwei": base_fee / 10**12,
            "fallback_tip_gwei": fallback_tip / 10**12,
            "min_tip_gwei": min_tip_gwei,
        },
    )

    return fallback_tip


async def compute_fees_with_mempool_analysis(
    web3: AsyncWeb3,
    gas_oracle: GasOracle,
    base_fee: int,
) -> tuple[int, int]:
    """Compute gas fees with comprehensive mempool analysis.

    This is the production-ready gas estimation that combines:
    - GasOracle P95 percentile calculation
    - Live mempool sampling
    - Dynamic fallback

    Args:
        web3: AsyncWeb3 instance.
        gas_oracle: GasOracle instance.
        base_fee: Current base fee in wei.

    Returns:
        Tuple of (max_fee_per_gas, max_priority_fee_per_gas) in wei.
    """
    # Start with oracle computation
    max_fee, priority_fee = gas_oracle.compute_initial_fees(base_fee)

    # Sample mempool for live data
    mempool_tips, stats = await sample_mempool_tips(web3, base_fee)

    if stats:
        # Use p95 from mempool if available and higher than oracle
        p95_tip = stats["p95"]
        if p95_tip > priority_fee:
            logger.info(
                "Using mempool p95 tip (higher than oracle)",
                extra={
                    "oracle_tip_gwei": priority_fee / 10**12,
                    "mempool_p95_gwei": p95_tip / 10**12,
                },
            )
            priority_fee = p95_tip
    else:
        # Fallback: dynamic tip based on base fee
        fallback_tip = compute_dynamic_fallback_tip(base_fee)
        if fallback_tip > priority_fee:
            priority_fee = fallback_tip

    # Recompute max_fee with updated priority_fee
    # max_fee = base + tip + headroom
    headroom = int(base_fee * 0.50)  # 50% headroom
    max_fee = base_fee + priority_fee + headroom

    # Apply dynamic cap
    fee_cap = int(base_fee * 3.0)
    max_fee = min(max_fee, fee_cap + priority_fee)

    logger.info(
        "Final gas fees computed",
        extra={
            "base_fee_gwei": base_fee / 10**12,
            "priority_fee_gwei": priority_fee / 10**12,
            "max_fee_gwei": max_fee / 10**12,
            "headroom_gwei": headroom / 10**12,
        },
    )

    return max_fee, priority_fee
