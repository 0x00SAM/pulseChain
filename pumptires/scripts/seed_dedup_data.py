#!/usr/bin/env python3
"""Seed deduplication data to prevent buying old tokens.

This script populates the buy deduplication store with transaction hashes
from all historical createToken transactions to the factory. Run this before 
starting the trading system to avoid accidentally buying old tokens.

Usage:
    poetry run python scripts/seed_dedup_data.py [limit]

The script will:
1. Connect to PulseChain RPC
2. Fetch all logs from factory address (all blocks)
3. Extract unique transaction hashes
4. Populate dedup store
"""

import asyncio
import sys
from pathlib import Path
import argparse

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from web3 import AsyncWeb3, AsyncHTTPProvider
from pumptires.app.config.settings import settings
from pumptires.app.io.persistent_dedup import PersistentDedup
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


async def seed_dedup_from_factory_logs(
    limit: int | None = None,
    dedup_file: str = "data/seen_pending_tx.json",
    from_block: int | None = None,
    to_block: int | None = None,
) -> None:
    """Seed deduplication store by fetching all factory transaction logs.

    Args:
        limit: Optional limit on number of tx hashes to collect (most recent).
        dedup_file: Path to dedup data file.
    """
    logger.info(
        "Starting dedup seeding from factory logs",
        extra={
            "factory_address": settings.factory_address,
            "limit": limit,
            "dedup_file": dedup_file,
        },
    )

    # Initialize Web3 connection
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.pulsechain_http_url))

    try:
        # Verify connection
        is_connected = await w3.is_connected()
        if not is_connected:
            logger.error("Failed to connect to PulseChain RPC")
            return

        chain_id = await w3.eth.chain_id
        logger.info(
            "Connected to PulseChain",
            extra={"chain_id": chain_id, "rpc": settings.pulsechain_http_url},
        )

        # Initialize dedup store
        dedup = PersistentDedup(filepath=dedup_file)
        await dedup.initialize()

        # Get target block range
        factory_address = AsyncWeb3.to_checksum_address(settings.factory_address)

        # Determine scanning range: either provided from/to or full history
        if from_block is None or to_block is None:
            start_block = 0
            end_block = await w3.eth.block_number
        else:
            start_block = int(from_block)
            end_block = int(to_block)

        logger.info(
            "Fetching logs from factory address in chunks",
            extra={"factory_address": factory_address, "start_block": start_block, "end_block": end_block},
        )

        # Fetch logs in chunks (RPC limit: 50,000 blocks per request)
        chunk_size = 50000
        tx_hashes = set()
        total_logs = 0

        current_from = start_block
        while current_from <= end_block:
            current_to = min(current_from + chunk_size - 1, end_block)

            logger.info(
                "Fetching chunk",
                extra={
                    "from_block": current_from,
                    "to_block": current_to,
                },
            )

            chunk_logs = await w3.eth.get_logs(
                {
                    "fromBlock": current_from,
                    "toBlock": current_to,
                    "address": factory_address,
                }
            )

            # Extract transaction hashes from this chunk
            for log in chunk_logs:
                # Ensure tx hash strings include 0x prefix
                tx_hash_hex = "0x" + log["transactionHash"].hex()
                tx_hashes.add(tx_hash_hex)

            total_logs += len(chunk_logs)

            logger.info(
                "Chunk processed",
                extra={
                    "chunk_logs": len(chunk_logs),
                    "unique_tx_hashes": len(tx_hashes),
                },
            )

            # Move to next chunk
            current_from = current_to + 1

        logger.info(
            "Retrieved logs from factory",
            extra={"total_logs": total_logs, "unique_tx_hashes": len(tx_hashes)},
        )

        logger.info(
            "Retrieved all logs from factory",
            extra={"total_logs": total_logs, "unique_tx_hashes": len(tx_hashes)},
        )

        # Apply limit if specified (take most recent)
        if limit and len(tx_hashes) > limit:
            tx_hashes_list = sorted(list(tx_hashes))[-limit:]  # Most recent
            logger.info(
                "Applying limit to most recent transactions",
                extra={"limit": limit, "selected": len(tx_hashes_list)},
            )
        else:
            tx_hashes_list = list(tx_hashes)

        # Add all to dedup store
        added_count = 0
        for tx_hash in tx_hashes_list:
            if not await dedup.contains(tx_hash):
                await dedup.add(tx_hash)
                added_count += 1
                
                # Log progress every 10 hashes
                if added_count % 10 == 0:
                    logger.info(
                        "Seeding progress",
                        extra={"added": added_count, "total": len(tx_hashes_list)},
                    )

        logger.info(
            "Dedup seeding completed",
            extra={
                "total_tx_hashes": len(tx_hashes_list),
                "newly_added": added_count,
                "already_present": len(tx_hashes_list) - added_count,
                "dedup_file": dedup_file,
            },
        )

    except Exception as e:
        logger.error("Dedup seeding failed", extra={"error": str(e)})
        raise

    finally:
        await w3.provider.disconnect()


async def main():
    """Main entry point."""
    print("=" * 70)
    print("PumpTires Deduplication Data Seeding Script")
    print("=" * 70)
    print()

    parser = argparse.ArgumentParser(description="Seed dedup store from factory logs")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit on number of tx hashes to collect (most recent)")
    parser.add_argument("--from-block", type=int, default=None, help="Start block (inclusive) to scan")
    parser.add_argument("--to-block", type=int, default=None, help="End block (inclusive) to scan")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    print(f"Factory address: {settings.factory_address}")
    print(f"RPC URL: {settings.pulsechain_http_url}")
    print(f"Limit: {args.limit if args.limit else 'None (fetch all)'}")
    print(f"Block range: {args.from_block if args.from_block is not None else 'genesis'}..{args.to_block if args.to_block is not None else 'latest'}")
    print()

    if not args.yes:
        confirm = input("Proceed with seeding? (y/N): ")
        if confirm.lower() != "y":
            print("Cancelled.")
            sys.exit(0)

    print()
    print("Fetching transaction hashes from factory logs...")
    print()

    await seed_dedup_from_factory_logs(limit=args.limit, from_block=args.from_block, to_block=args.to_block)

    print()
    print("=" * 70)
    print("Seeding complete!")
    print("=" * 70)
    print()
    print("You can now start the trading system without risk of buying old tokens.")


if __name__ == "__main__":
    asyncio.run(main())
