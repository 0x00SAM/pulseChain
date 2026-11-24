"""Metrics and observability service for prediction accuracy tracking.

Provides simple, log-based metrics without external dependencies.
Tracks prediction hit rate, active reservations, and nonce state.
"""

import time
from dataclasses import dataclass, field
from typing import Dict

from pumptires.app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PredictionMetrics:
    """Tracks prediction accuracy metrics."""
    
    total_predictions: int = 0
    total_reconciliations: int = 0
    successful_matches: int = 0
    failed_matches: int = 0
    pending_predictions: int = 0
    
    # Timing metrics
    last_prediction_time: float = 0.0
    last_reconciliation_time: float = 0.0
    
    # Nonce tracker state
    active_reservations: int = 0
    confirmed_nonce: int = 0
    
    def get_hit_rate(self) -> float:
        """Calculate prediction hit rate.
        
        Returns:
            Hit rate as percentage (0-100), or 0.0 if no reconciliations yet.
        """
        if self.total_reconciliations == 0:
            return 0.0
        return (self.successful_matches / self.total_reconciliations) * 100.0
    
    def to_dict(self) -> Dict[str, float | int]:
        """Convert metrics to dictionary for logging.
        
        Returns:
            Dictionary of metric name to value.
        """
        return {
            "total_predictions": self.total_predictions,
            "total_reconciliations": self.total_reconciliations,
            "successful_matches": self.successful_matches,
            "failed_matches": self.failed_matches,
            "pending_predictions": self.pending_predictions,
            "hit_rate_percent": round(self.get_hit_rate(), 2),
            "active_reservations": self.active_reservations,
            "confirmed_nonce": self.confirmed_nonce,
        }


class MetricsTracker:
    """Simple metrics tracker using log-based observability."""
    
    def __init__(self, log_interval_seconds: float = 300.0) -> None:
        """Initialize metrics tracker.
        
        Args:
            log_interval_seconds: How often to log aggregate metrics (default: 5 minutes).
        """
        self.metrics = PredictionMetrics()
        self.log_interval_seconds = log_interval_seconds
        self.last_log_time = time.time()
    
    def record_prediction(self, predicted_address: str, factory_nonce: int) -> None:
        """Record a new prediction.
        
        Args:
            predicted_address: The predicted token address.
            factory_nonce: The factory nonce used for prediction.
        """
        self.metrics.total_predictions += 1
        self.metrics.pending_predictions += 1
        self.metrics.last_prediction_time = time.time()
        
        logger.debug(
            "prediction_recorded",
            extra={
                "predicted_address": predicted_address,
                "factory_nonce": factory_nonce,
                "total_predictions": self.metrics.total_predictions,
            },
        )
        
        # Check if we should log aggregated metrics
        self._maybe_log_metrics()
    
    def record_reconciliation(
        self,
        predicted_address: str,
        actual_address: str,
        match: bool,
    ) -> None:
        """Record a prediction reconciliation (when token is actually created).
        
        Args:
            predicted_address: The predicted token address.
            actual_address: The actual token address from blockchain.
            match: Whether prediction matched actual.
        """
        self.metrics.total_reconciliations += 1
        self.metrics.pending_predictions = max(0, self.metrics.pending_predictions - 1)
        
        if match:
            self.metrics.successful_matches += 1
        else:
            self.metrics.failed_matches += 1
        
        self.metrics.last_reconciliation_time = time.time()
        
        logger.info(
            "prediction_reconciled",
            extra={
                "predicted_address": predicted_address,
                "actual_address": actual_address,
                "match": match,
                "hit_rate_percent": round(self.metrics.get_hit_rate(), 2),
                "total_reconciliations": self.metrics.total_reconciliations,
            },
        )
        
        # Check if we should log aggregated metrics
        self._maybe_log_metrics()
    
    def update_tracker_state(
        self,
        active_reservations: int,
        confirmed_nonce: int,
    ) -> None:
        """Update nonce tracker state metrics.
        
        Args:
            active_reservations: Number of active nonce reservations.
            confirmed_nonce: Current confirmed nonce.
        """
        self.metrics.active_reservations = active_reservations
        self.metrics.confirmed_nonce = confirmed_nonce
        
        logger.debug(
            "tracker_state_updated",
            extra={
                "active_reservations": active_reservations,
                "confirmed_nonce": confirmed_nonce,
            },
        )
    
    def _maybe_log_metrics(self) -> None:
        """Log aggregated metrics if interval has elapsed."""
        current_time = time.time()
        elapsed = current_time - self.last_log_time
        
        if elapsed >= self.log_interval_seconds:
            self.log_metrics()
            self.last_log_time = current_time
    
    def log_metrics(self) -> None:
        """Log current aggregated metrics."""
        metrics_dict = self.metrics.to_dict()
        
        logger.info(
            "prediction_metrics_snapshot",
            extra={
                **metrics_dict,
                "log_interval_seconds": self.log_interval_seconds,
            },
        )
    
    def get_metrics(self) -> PredictionMetrics:
        """Get current metrics snapshot.
        
        Returns:
            Current metrics state.
        """
        return self.metrics
    
    def reset_metrics(self) -> None:
        """Reset all metrics to zero.
        
        Useful for testing or after a restart to start fresh.
        """
        self.metrics = PredictionMetrics()
        self.last_log_time = time.time()
        
        logger.info("prediction_metrics_reset")

