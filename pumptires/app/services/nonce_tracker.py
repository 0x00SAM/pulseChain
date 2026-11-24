"""Nonce tracking service for accurate token address prediction.

Tracks pending transactions from the factory contract to maintain accurate
nonce state and predict deployment addresses correctly even with multiple
pending transactions.
"""

import asyncio
from collections import OrderedDict, defaultdict
from typing import Any, Dict

from web3 import AsyncWeb3

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


class NonceTracker:
    """Tracks factory creation nonces using a reservation lifecycle.

    Implements a reservation system for factory contract creation nonces to enable
    accurate address prediction even with multiple pending createToken transactions.
    
    Lifecycle:
    1. seed() - Initialize with on-chain creation nonce at startup
    2. reserve_next() - Reserve next nonce for a pending transaction
    3. commit() - Finalize reservation when TokenCreated event observed
    4. release() - Free reservation if transaction dropped
    5. handle_reorg_removal() - Restore reservation on chain reorg
    """

    def __init__(self, web3: AsyncWeb3) -> None:
        """Initialize the nonce tracker.

        Args:
            web3: AsyncWeb3 instance for blockchain queries.
        """
        self.web3 = web3
        
        # Factory creation nonce tracking
        # Maps factory address -> confirmed creation nonce
        self._factory_confirmed_nonce: Dict[str, int] = {}
        # Maps factory address -> OrderedDict[tx_hash, reserved_nonce]
        self._factory_reservations: Dict[str, OrderedDict[str, int]] = defaultdict(
            OrderedDict
        )
        
        # Lock for thread-safe updates
        self._lock = asyncio.Lock()

    # Factory creation nonce reservation API
    
    async def seed(self, address: str, confirmed_start: int) -> None:
        """Initialize the confirmed creation nonce for a factory address.
        
        This should be called at startup with the on-chain transaction count
        to establish the baseline for factory creation nonce tracking.
        
        Args:
            address: Factory contract address (checksummed).
            confirmed_start: The on-chain transaction count (creation nonce) for the factory.
        """
        async with self._lock:
            checksum_address = self.web3.to_checksum_address(address)
            old_nonce = self._factory_confirmed_nonce.get(checksum_address)
            self._factory_confirmed_nonce[checksum_address] = confirmed_start
            
            logger.info(
                "factory_nonce_seeded",
                extra={
                    "address": checksum_address,
                    "confirmed_nonce": confirmed_start,
                    "old_nonce": old_nonce,
                },
            )
    
    async def reserve_next(self, address: str, tx_hash: str) -> int:
        """Atomically reserve the next factory creation nonce for a transaction.
        
        This binds the next available creation nonce to the given tx_hash,
        ensuring deterministic address prediction even with multiple pending
        createToken transactions.
        
        Args:
            address: Factory contract address (checksummed).
            tx_hash: Transaction hash to bind the reservation to.
            
        Returns:
            The reserved creation nonce.
            
        Raises:
            ValueError: If the factory has not been seeded yet.
        """
        async with self._lock:
            checksum_address = self.web3.to_checksum_address(address)
            
            # Ensure factory has been seeded
            if checksum_address not in self._factory_confirmed_nonce:
                raise ValueError(
                    f"Factory {checksum_address} has not been seeded. "
                    "Call seed() first to initialize the confirmed nonce."
                )
            
            # Check if this tx_hash already has a reservation
            reservations = self._factory_reservations[checksum_address]
            if tx_hash in reservations:
                existing_nonce = reservations[tx_hash]
                logger.debug(
                    "reservation_already_exists",
                    extra={
                        "address": checksum_address,
                        "tx_hash": tx_hash,
                        "reserved_nonce": existing_nonce,
                    },
                )
                return existing_nonce
            
            # Calculate next available nonce
            confirmed = self._factory_confirmed_nonce[checksum_address]
            
            # Next nonce is confirmed + number of existing reservations
            next_nonce = confirmed + len(reservations)
            
            # Reserve it
            reservations[tx_hash] = next_nonce
            
            logger.info(
                "nonce_reserved",
                extra={
                    "address": checksum_address,
                    "tx_hash": tx_hash,
                    "reserved_nonce": next_nonce,
                    "confirmed_nonce": confirmed,
                    "total_reservations": len(reservations),
                },
            )
            
            return next_nonce
    
    async def commit(self, address: str, tx_hash: str) -> None:
        """Finalize a nonce reservation when TokenCreated event is observed.
        
        This advances the confirmed nonce and removes the reservation for the
        given tx_hash, indicating the token creation has been mined.
        
        Idempotent: Safe to call multiple times for the same tx_hash.
        If called multiple times, only the first call has an effect.
        
        Args:
            address: Factory contract address (checksummed).
            tx_hash: Transaction hash that was mined.
        """
        async with self._lock:
            checksum_address = self.web3.to_checksum_address(address)
            
            reservations = self._factory_reservations[checksum_address]
            
            if tx_hash not in reservations:
                logger.debug(
                    "commit_no_reservation_already_committed",
                    extra={
                        "address": checksum_address,
                        "tx_hash": tx_hash,
                    },
                )
                return
            
            reserved_nonce = reservations.pop(tx_hash)
            
            # Advance confirmed nonce to at least reserved_nonce + 1
            current_confirmed = self._factory_confirmed_nonce.get(checksum_address, 0)
            new_confirmed = max(current_confirmed, reserved_nonce + 1)
            self._factory_confirmed_nonce[checksum_address] = new_confirmed
            
            logger.info(
                "nonce_committed",
                extra={
                    "address": checksum_address,
                    "tx_hash": tx_hash,
                    "reserved_nonce": reserved_nonce,
                    "old_confirmed": current_confirmed,
                    "new_confirmed": new_confirmed,
                    "remaining_reservations": len(reservations),
                },
            )
    
    async def release(self, address: str, tx_hash: str) -> None:
        """Release a nonce reservation if the transaction is dropped or replaced.
        
        This frees the reservation without advancing the confirmed nonce,
        allowing the nonce to be reused by a future transaction.
        
        Idempotent: Safe to call multiple times for the same tx_hash.
        
        Args:
            address: Factory contract address (checksummed).
            tx_hash: Transaction hash that was dropped or replaced.
        """
        async with self._lock:
            checksum_address = self.web3.to_checksum_address(address)
            
            reservations = self._factory_reservations[checksum_address]
            
            if tx_hash not in reservations:
                logger.debug(
                    "release_no_reservation",
                    extra={
                        "address": checksum_address,
                        "tx_hash": tx_hash,
                    },
                )
                return
            
            reserved_nonce = reservations.pop(tx_hash)
            
            logger.info(
                "nonce_released",
                extra={
                    "address": checksum_address,
                    "tx_hash": tx_hash,
                    "released_nonce": reserved_nonce,
                    "remaining_reservations": len(reservations),
                },
            )

    async def handle_reorg_removal(self, address: str, tx_hash: str, reserved_nonce: int) -> None:
        """Handle a TokenCreated log removal due to a chain reorg.
        
        This method is called when a previously committed TokenCreated event
        is removed due to a reorg. It rolls back the confirmed nonce and
        restores the reservation so that if/when the transaction is re-mined,
        it can be committed again cleanly.
        
        Idempotent: Safe to call multiple times for the same tx_hash.
        
        Args:
            address: Factory contract address (checksummed).
            tx_hash: Transaction hash that was reorg'd out.
            reserved_nonce: The nonce that was originally reserved for this tx.
        """
        async with self._lock:
            checksum_address = self.web3.to_checksum_address(address)
            
            reservations = self._factory_reservations[checksum_address]
            
            # If reservation already exists, nothing to do (idempotent)
            if tx_hash in reservations:
                logger.debug(
                    "reorg_removal_reservation_already_exists",
                    extra={
                        "address": checksum_address,
                        "tx_hash": tx_hash,
                        "existing_nonce": reservations[tx_hash],
                    },
                )
                return
            
            # Restore the reservation
            reservations[tx_hash] = reserved_nonce
            
            # Roll back confirmed nonce if needed
            # We can't know the exact correct confirmed nonce after a reorg
            # without re-querying the chain, but we can at least ensure
            # it's not greater than the reservation we're restoring
            current_confirmed = self._factory_confirmed_nonce.get(checksum_address, 0)
            if current_confirmed > reserved_nonce:
                # Only roll back to the reserved nonce, don't go below it
                self._factory_confirmed_nonce[checksum_address] = reserved_nonce
                logger.warning(
                    "reorg_removal_confirmed_nonce_rolled_back",
                    extra={
                        "address": checksum_address,
                        "tx_hash": tx_hash,
                        "reserved_nonce": reserved_nonce,
                        "old_confirmed": current_confirmed,
                        "new_confirmed": reserved_nonce,
                    },
                )
            
            logger.info(
                "reorg_removal_handled",
                extra={
                    "address": checksum_address,
                    "tx_hash": tx_hash,
                    "restored_nonce": reserved_nonce,
                    "confirmed_nonce": self._factory_confirmed_nonce[checksum_address],
                    "total_reservations": len(reservations),
                },
            )

    async def get_state(self, address: str) -> Dict[str, Any]:
        """Get the current tracking state for a factory address.

        Args:
            address: Factory contract address (checksummed).

        Returns:
            Dictionary with factory creation nonce tracking state.
        """
        async with self._lock:
            checksum_address = self.web3.to_checksum_address(address)
            
            state: Dict[str, Any] = {
                "address": checksum_address,
            }
            
            # Factory creation nonce tracking
            if checksum_address in self._factory_confirmed_nonce:
                reservations = self._factory_reservations[checksum_address]
                state["factory_creation_nonce"] = {
                    "confirmed": self._factory_confirmed_nonce[checksum_address],
                    "reservations": list(reservations.items()),
                    "reservation_count": len(reservations),
                }
            
            return state

