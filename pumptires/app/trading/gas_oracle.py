"""Gas oracle for aggressive P95 EIP-1559 fee computation.

This module implements an aggressive P95 gas strategy optimized for competitive
token launch trading. Uses base fee from latest block plus P95 mempool-derived
tip estimates for maximum speed and reliability.

Features:
- Aggressive P95: Always uses P95 percentile for competitive token launches
- Mempool sampling for real-time competition awareness
- Dynamic safety caps (1000-15,000 Gwei) for extreme conditions
- Initial and escalated fee computation APIs
- O(1) fee computation optimized for high-frequency competitive trading
"""

import math
import statistics
from collections import deque
from typing import Any, Deque

from pumptires.app.config.settings import settings
from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)

# Gwei to Beats conversion: 1 Gwei = 10^9 Beats
GWEI_TO_BEATS = 10**9


class GasOracle:
    """Aggressive P95 gas oracle for competitive token launch trading.

    Computes EIP-1559 fees using base fee from latest block plus P95 tip from
    mempool observations. Optimized for speed and reliability in competitive
    token launch scenarios where timing is critical.

    Attributes:
        window_size: Maximum observations in rolling window.
        target_percentile: Target percentile for tip computation (0.95 = P95).
        tip_min_gwei: Minimum tip in Gwei (competitive floor).
        tip_max_gwei: Maximum tip in Gwei (safety cap).
        fee_cap_multiplier: maxFeePerGas = base_fee * multiplier.
        headroom_initial_factor: Headroom factor for initial submission.
        headroom_escalated_factor: Headroom factor for escalated retries.
        priority_fees: Rolling window of observed tips from mempool.
    """

    def __init__(
        self,
        window_size: int | None = None,
        target_percentile: float | None = None,
        tip_min_gwei: int | None = None,
        tip_max_gwei: int | None = None,
        fee_cap_multiplier: float | None = None,
        headroom_initial_factor: float | None = None,
        headroom_escalated_factor: float | None = None,
    ) -> None:
        """Initialize gas oracle with configured parameters.

        Args:
            window_size: Maximum observations (default from settings).
            target_percentile: Percentile for tip (default 0.95 from settings).
            tip_min_gwei: Minimum tip in Gwei (default from settings).
            tip_max_gwei: Maximum tip in Gwei (default from settings).
            fee_cap_multiplier: maxFee multiplier (default from settings).
            headroom_initial_factor: Initial headroom (default from settings).
            headroom_escalated_factor: Escalated headroom (default from settings).
        """
        self.window_size = window_size or settings.gas_window_size
        self.target_percentile = (
            target_percentile
            if target_percentile is not None
            else settings.gas_target_percentile
        )
        
        # Aggressive fee strategy (in Gwei, converted to beats)
        tip_min_gwei_val = tip_min_gwei if tip_min_gwei is not None else settings.gas_tip_min_gwei
        tip_max_gwei_val = tip_max_gwei if tip_max_gwei is not None else settings.gas_tip_max_gwei
        self.tip_min_beats = tip_min_gwei_val * GWEI_TO_BEATS
        self.tip_max_beats = tip_max_gwei_val * GWEI_TO_BEATS
        
        self.fee_cap_multiplier = (
            fee_cap_multiplier
            if fee_cap_multiplier is not None
            else settings.gas_fee_cap_multiplier
        )
        
        self.headroom_initial_factor = (
            headroom_initial_factor
            if headroom_initial_factor is not None
            else settings.gas_headroom_initial_factor
        )
        self.headroom_escalated_factor = (
            headroom_escalated_factor
            if headroom_escalated_factor is not None
            else settings.gas_headroom_escalated_factor
        )

        # Rolling window for tip observations only
        self.priority_fees: Deque[int] = deque(maxlen=self.window_size)

        logger.info(
            "GasOracle initialized (Aggressive P95 for token launches)",
            extra={
                "window_size": self.window_size,
                "target_percentile": self.target_percentile,
                "tip_min_gwei": tip_min_gwei_val,
                "tip_max_gwei": tip_max_gwei_val,
                "fee_cap_multiplier": self.fee_cap_multiplier,
                "headroom_initial_factor": self.headroom_initial_factor,
                "headroom_escalated_factor": self.headroom_escalated_factor,
            },
        )

    def observe(self, priority_fee: int) -> None:
        """Add tip observation to rolling window.

        Args:
            priority_fee: maxPriorityFeePerGas in Beats.
        """
        self.priority_fees.append(priority_fee)

    def observe_from_tx(self, tx: dict[str, Any]) -> None:
        """Add observation from transaction dict.

        Safely extracts maxPriorityFeePerGas from EIP-1559 tx or falls back
        to gasPrice for legacy transactions.

        Args:
            tx: Transaction dictionary from web3.
        """
        # EIP-1559 transaction
        if "maxPriorityFeePerGas" in tx and tx["maxPriorityFeePerGas"] is not None:
            self.observe(tx["maxPriorityFeePerGas"])
        # Legacy transaction: use gasPrice as tip estimate
        elif "gasPrice" in tx and tx["gasPrice"] is not None:
            self.observe(tx["gasPrice"])

    def _compute_percentile(self, data: list[int], percentile: float) -> int:
        """Compute percentile using interpolated quantiles.

        Uses math.floor/ceil interpolation for stability with small samples.

        Args:
            data: List of integer values (in beats).
            percentile: Target percentile (0.75 = p75, 0.90 = p90, etc.).

        Returns:
            Interpolated percentile value in beats.
        """
        if not data:
            return self.tip_min_beats
        
        if len(data) == 1:
            return data[0]
        
        sorted_data = sorted(data)
        n = len(sorted_data)
        
        # Interpolated index
        idx_float = percentile * (n - 1)
        idx_low = math.floor(idx_float)
        idx_high = math.ceil(idx_float)
        
        if idx_low == idx_high:
            return sorted_data[idx_low]
        
        # Linear interpolation
        weight = idx_float - idx_low
        return int(sorted_data[idx_low] * (1 - weight) + sorted_data[idx_high] * weight)

    def _compute_tip_from_samples(self, target_percentile: float | None = None) -> int:
        """Compute percentile tip from mempool samples.

        Args:
            target_percentile: Percentile to compute (defaults to self.target_percentile).

        Returns:
            Tip in Beats derived from target percentile, or tip_min if no samples.
        """
        if not self.priority_fees:
            return self.tip_min_beats

        percentile = target_percentile if target_percentile is not None else self.target_percentile
        priority_fee_list = list(self.priority_fees)
        tip = self._compute_percentile(priority_fee_list, percentile)

        logger.debug(
            "Computed tip from samples",
            extra={
                "observations": len(self.priority_fees),
                "percentile": percentile,
                "tip_beats": tip,
                "tip_gwei": tip / GWEI_TO_BEATS,
            },
        )
        return tip

    def _clamp_tip(self, tip: int, base_fee: int) -> int:
        """Apply tip safety caps.

        Ensures tip is within reasonable bounds using dynamic min/max caps.

        Args:
            tip: Proposed tip in Beats.
            base_fee: Base fee in Beats (for context/logging).

        Returns:
            Clamped tip in Beats.
        """
        # Apply safety caps
        clamped_tip = max(self.tip_min_beats, min(tip, self.tip_max_beats))
        
        if clamped_tip != tip:
            logger.debug(
                "Tip clamped",
                extra={
                    "original_tip_gwei": tip / GWEI_TO_BEATS,
                    "clamped_tip_gwei": clamped_tip / GWEI_TO_BEATS,
                    "base_fee_gwei": base_fee / GWEI_TO_BEATS,
                    "tip_min_gwei": self.tip_min_beats / GWEI_TO_BEATS,
                    "tip_max_gwei": self.tip_max_beats / GWEI_TO_BEATS,
                },
            )
        
        return clamped_tip

    def compute_initial_fees(self, base_fee: int) -> tuple[int, int]:
        """Compute initial EIP-1559 fees using aggressive P95 strategy.

        Args:
            base_fee: Base fee from latest block in Beats.

        Returns:
            Tuple of (maxFeePerGas, maxPriorityFeePerGas) in Beats.
        """
        # Derive tip from mempool samples using P95
        tip = self._compute_tip_from_samples()
        
        # Apply safety clamp
        tip = self._clamp_tip(tip, base_fee)

        # Compute headroom
        headroom = int(base_fee * self.headroom_initial_factor)

        # Compute maxFee: base + tip + headroom, with dynamic cap
        max_fee = base_fee + tip + headroom
        
        # Apply dynamic fee cap: base_fee * multiplier
        fee_cap = int(base_fee * self.fee_cap_multiplier)
        max_fee = min(max_fee, fee_cap)

        logger.debug(
            "Computed initial fees (P95)",
            extra={
                "base_fee_gwei": base_fee / GWEI_TO_BEATS,
                "tip_gwei": tip / GWEI_TO_BEATS,
                "headroom_gwei": headroom / GWEI_TO_BEATS,
                "max_fee_gwei": max_fee / GWEI_TO_BEATS,
                "fee_cap_gwei": fee_cap / GWEI_TO_BEATS,
                "headroom_factor": self.headroom_initial_factor,
            },
        )

        return (max_fee, tip)

    def compute_escalated_fees(
        self, base_fee: int, prev_tip: int, escalation_multiplier: float | None = None
    ) -> tuple[int, int]:
        """Compute escalated EIP-1559 fees for retry attempts.

        Increases tip by escalation_multiplier, uses escalated headroom factor,
        and enforces safety caps.

        Args:
            base_fee: Base fee from latest block in Beats.
            prev_tip: Previous tip value in Beats.
            escalation_multiplier: Tip multiplier (defaults to settings).

        Returns:
            Tuple of (maxFeePerGas, maxPriorityFeePerGas) in Beats.
        """
        multiplier = (
            escalation_multiplier
            if escalation_multiplier is not None
            else settings.gas_escalation_tip_multiplier
        )

        # Re-sample mempool using P95
        sampled_tip = self._compute_tip_from_samples()
        
        escalated_tip = int(max(prev_tip * multiplier, sampled_tip))

        # Apply safety clamp
        escalated_tip = self._clamp_tip(escalated_tip, base_fee)

        # Compute escalated headroom
        headroom = int(base_fee * self.headroom_escalated_factor)

        # Compute maxFee
        max_fee = base_fee + escalated_tip + headroom

        # Apply dynamic fee cap
        fee_cap = int(base_fee * self.fee_cap_multiplier)
        max_fee = min(max_fee, fee_cap)

        logger.debug(
            "Computed escalated fees (P95)",
            extra={
                "base_fee_gwei": base_fee / GWEI_TO_BEATS,
                "prev_tip_gwei": prev_tip / GWEI_TO_BEATS,
                "escalated_tip_gwei": escalated_tip / GWEI_TO_BEATS,
                "headroom_gwei": headroom / GWEI_TO_BEATS,
                "max_fee_gwei": max_fee / GWEI_TO_BEATS,
                "fee_cap_gwei": fee_cap / GWEI_TO_BEATS,
                "multiplier": multiplier,
                "headroom_factor": self.headroom_escalated_factor,
            },
        )

        return (max_fee, escalated_tip)

    def observation_count(self) -> int:
        """Get current number of observations.

        Returns:
            Number of observations in rolling window.
        """
        return len(self.priority_fees)
