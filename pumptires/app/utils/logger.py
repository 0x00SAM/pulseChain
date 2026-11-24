"""Structured logging utilities for the PumpTires monitor.

Provides JSON-based structured logging using Python's standard logging with
orjson for performance.
"""

import logging
import sys
from typing import Any

import orjson


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging output."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON string.

        Args:
            record: Log record to format.

        Returns:
            JSON-formatted log string.
        """
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        # Python's logging adds extra dict keys as direct attributes on the record
        # Exclude standard LogRecord attributes
        standard_attrs = {
            'name', 'msg', 'args', 'created', 'msecs', 'levelname', 'levelno',
            'pathname', 'filename', 'module', 'exc_info', 'exc_text', 'stack_info',
            'lineno', 'funcName', 'process', 'processName', 'thread', 'threadName',
            'getMessage', 'message', 'asctime', 'relativeCreated', 'extra', 'taskName'
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs:
                # Convert large integers to strings to avoid orjson 64-bit overflow
                if isinstance(value, int) and (value > 9007199254740991 or value < -9007199254740991):
                    log_data[key] = str(value)
                else:
                    log_data[key] = value

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return orjson.dumps(log_data).decode("utf-8")


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Create and configure a structured logger.

    Args:
        name: Logger name (typically __name__).
        level: Logging level (default: INFO).

    Returns:
        Configured logger instance with structured JSON formatting.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()

    # Create console handler with structured formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = StructuredFormatter()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger


def log_with_extra(logger: logging.Logger, level: int, message: str, **kwargs: Any) -> None:
    """Log message with extra structured fields.

    Args:
        logger: Logger instance.
        level: Logging level.
        message: Log message.
        **kwargs: Additional fields to include in structured log.
    """
    extra_data = {"extra": kwargs}
    logger.log(level, message, extra=extra_data)

