"""Configuration settings for the PumpTires monitor.

Loads configuration from config.yaml and environment variables.
Secrets and deployment-specific settings come from .env.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def load_yaml_config(yaml_path: str = "config.yaml") -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        yaml_path: Path to YAML configuration file.

    Returns:
        Configuration dictionary.
    """
    config_file = Path(yaml_path)
    if not config_file.exists():
        return {}

    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# Load YAML config first
_yaml_config = load_yaml_config()


class Settings(BaseSettings):
    """Application settings loaded from config.yaml and environment variables.

    Priority (highest to lowest):
    1. Environment variables (.env file)
    2. YAML config file (config.yaml)
    3. Default values
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # PulseChain connection settings
    pulsechain_wss_url: str = Field(
        default="wss://pulsechain-rpc.publicnode.com",
        description="WebSocket URL for PulseChain RPC connection",
    )

    pulsechain_http_url: str = Field(
        default="https://pulsechain-rpc.publicnode.com",
        description="HTTP URL for PulseChain RPC connection (for transaction fetching)",
    )

    pulsechain_http_url2: str = Field(
        default="https://rpc-pulsechain.g4mm4.io",
        description="Secondary HTTP URL for PulseChain RPC connection (fallback)",
    )

    pulsechain_rpc_urls: list[str] = Field(
        default_factory=list,
        description="List of RPC URLs for multi-RPC transaction broadcasting (auto-populated from HTTP URLs)",
    )

    expected_chain_id: int | None = Field(
        default=369,
        description="Expected PulseChain chain ID for safety checks",
    )

    # Contract settings
    factory_address: str = Field(
        default="0x6538A83a81d855B965983161AF6a83e616D16fD5",
        description="PumpTires factory contract address",
    )

    create_token_selector: str = Field(
        default=_yaml_config.get("contract", {}).get(
            "create_token_selector", "0x2f2f2d56"
        ),
        description="Function selector for createToken(string,string)",
    )

    # Logging settings
    log_level: str = Field(
        default=_yaml_config.get("logging", {}).get("level", "INFO"),
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    event_log_path: str = Field(
        default=_yaml_config.get("logging", {}).get(
            "event_log_path", "data/pending_create_token.jsonl"
        ),
        description="Path to JSONL file for logging detected events",
    )

    # Performance settings
    max_concurrent_tx_fetches: int = Field(
        default=_yaml_config.get("performance", {}).get(
            "max_concurrent_tx_fetches", 50
        ),
        description="Maximum concurrent transaction fetch operations",
    )

    websocket_ping_interval: int = Field(
        default=_yaml_config.get("performance", {}).get("websocket_ping_interval", 20),
        description="WebSocket ping interval in seconds",
    )

    websocket_ping_timeout: int = Field(
        default=_yaml_config.get("performance", {}).get("websocket_ping_timeout", 10),
        description="WebSocket ping timeout in seconds",
    )

    disconnect_timeout: int = Field(
        default=_yaml_config.get("performance", {}).get("disconnect_timeout", 10),
        description="Timeout for graceful provider disconnect in seconds",
    )

    subscription_timeout: int = Field(
        default=_yaml_config.get("performance", {}).get("subscription_timeout", 300),
        description="Timeout for subscription operations in seconds",
    )

    # Retry settings
    max_retry_attempts: int = Field(
        default=_yaml_config.get("retry", {}).get("max_attempts", 5),
        description="Maximum retry attempts for failed operations",
    )

    retry_min_wait: float = Field(
        default=_yaml_config.get("retry", {}).get("min_wait", 1.0),
        description="Minimum wait time between retries in seconds",
    )

    retry_max_wait: float = Field(
        default=_yaml_config.get("retry", {}).get("max_wait", 300.0),
        description="Maximum wait time between retries in seconds",
    )

    # Deduplication settings
    dedup_max_size: int = Field(
        default=_yaml_config.get("dedup", {}).get("max_size", 10000),
        description="Maximum entries in deduplication stores",
    )

    # Metrics settings
    metrics_log_interval: float = Field(
        default=_yaml_config.get("metrics", {}).get("log_interval_seconds", 300.0),
        description="Interval for logging metrics summaries in seconds",
    )

    # Cleanup settings
    cleanup_interval: float = Field(
        default=_yaml_config.get("cleanup", {}).get("interval_seconds", 300.0),
        description="Interval for cleanup operations in seconds",
    )

    cleanup_max_age: float = Field(
        default=_yaml_config.get("cleanup", {}).get("max_age_seconds", 3600.0),
        description="Maximum age for stale entries in seconds",
    )

    # Trading settings
    trading_private_key: str = Field(
        default="",
        validation_alias="TRADING_PRIVATE_KEY",
        description="Private key for trading wallet (0x-prefixed hex string)",
    )

    trading_enabled: bool = Field(
        default=_yaml_config.get("trading", {}).get("enabled", False),
        description="Enable trading functionality (default: False for safety)",
    )

    trading_buy_amount_pls: int = Field(
        default=_yaml_config.get("trading", {}).get("buy_amount_pls", 1),
        description="Amount of PLS to spend per buy transaction",
    )

    trading_slippage_bps: int = Field(
        default=_yaml_config.get("trading", {}).get("slippage_bps", 500),
        description="Slippage tolerance in basis points (500 = 5%)",
    )

    trading_fast_path: bool = Field(
        default=_yaml_config.get("trading", {}).get("fast_path", False),
        description="Skip quote and use min_tokens=0 for minimum latency",
    )

    trading_approve_on_buy: bool = Field(
        default=_yaml_config.get("trading", {}).get("approve_on_buy", True),
        description="Submit optimistic approve with nonce+1 after buy",
    )

    trading_auto_sell_enabled: bool = Field(
        default=_yaml_config.get("trading", {}).get("auto_sell_enabled", False),
        description="Enable automatic sell after specified delay",
    )

    trading_sell_delay_seconds: int = Field(
        default=_yaml_config.get("trading", {}).get("sell_delay_seconds", 180),
        description="Delay in seconds before executing sell after buy (default: 180 = 3 minutes)",
    )

    trading_sell_slippage_bps: int = Field(
        default=_yaml_config.get("trading", {}).get("sell_slippage_bps", 500),
        description="Slippage tolerance for sell operations in basis points (500 = 5%)",
    )

    trading_buy_retry_max_attempts: int = Field(
        default=_yaml_config.get("trading", {}).get("buy_retry_max_attempts", 3),
        description="Maximum retry attempts for failed buy transactions (TokenNotGraduated errors)",
    )

    trading_buy_retry_delay_ms: int = Field(
        default=_yaml_config.get("trading", {}).get("buy_retry_delay_ms", 500),
        description="Delay between buy retry attempts in milliseconds",
    )

    # Gas oracle settings (Aggressive P95 Strategy)
    gas_window_size: int = Field(
        default=_yaml_config.get("gas", {}).get("window_size", 200),
        description="Number of observations in gas oracle rolling window",
    )

    gas_target_percentile: float = Field(
        default=_yaml_config.get("gas", {}).get("target_percentile", 0.95),
        description="Target percentile for aggressive competition (0.95 = P95 for token launches)",
    )

    # Aggressive fee strategy for token launch competition
    gas_tip_min_gwei: int = Field(
        default=_yaml_config.get("gas", {}).get("tip_min_gwei", 1000),
        description="Minimum tip in Gwei (competitive floor for token launches)",
    )

    gas_tip_max_gwei: int = Field(
        default=_yaml_config.get("gas", {}).get("tip_max_gwei", 15_000),
        description="Maximum tip in Gwei (safety cap for extreme congestion)",
    )

    gas_fee_cap_multiplier: float = Field(
        default=_yaml_config.get("gas", {}).get("fee_cap_multiplier", 4.0),
        description="maxFeePerGas = base_fee * multiplier (generous headroom for launch competition)",
    )

    gas_headroom_initial_factor: float = Field(
        default=_yaml_config.get("gas", {}).get("headroom_initial_factor", 0.50),
        description="Headroom factor for initial submission (0.50 = 50% of base fee)",
    )

    gas_headroom_escalated_factor: float = Field(
        default=_yaml_config.get("gas", {}).get("headroom_escalated_factor", 0.75),
        description="Headroom factor for escalated retries (0.75 = 75% of base fee)",
    )

    gas_escalation_max_retries: int = Field(
        default=_yaml_config.get("gas", {}).get("escalation_max_retries", 2),
        description="Maximum number of escalation retries for stuck transactions",
    )

    gas_escalation_tip_multiplier: float = Field(
        default=_yaml_config.get("gas", {}).get("escalation_tip_multiplier", 1.3),
        description="Multiplier for tip escalation (1.3 = 30% increase)",
    )

    gas_limit_baseline: int = Field(
        default=_yaml_config.get("gas", {}).get("limit_baseline", 230_000),
        description="Baseline gas limit for standard swaps",
    )

    gas_limit_conservative: int = Field(
        default=_yaml_config.get("gas", {}).get("limit_conservative", 350_000),
        description="Conservative gas limit for uncertain swaps",
    )

    gas_limit_ceiling: int = Field(
        default=_yaml_config.get("gas", {}).get("limit_ceiling", 550_000),
        description="Maximum gas limit ceiling for complex swaps",
    )

    gas_limit_retry_bump_pct: float = Field(
        default=_yaml_config.get("gas", {}).get("limit_retry_bump_pct", 0.10),
        description="Gas limit increase per retry (0.10 = 10%)",
    )

    gas_limit_start_rung: str = Field(
        default=_yaml_config.get("gas", {}).get("limit_start_rung", "baseline"),
        description="Starting gas limit rung: baseline, conservative, or ceiling",
    )

    # Event bus settings
    event_queue_maxsize: int = Field(
        default=_yaml_config.get("event_bus", {}).get("maxsize", 1000),
        description="Maximum size of EventBus queue for trading triggers",
    )

    @field_validator("pulsechain_rpc_urls", mode="before")
    @classmethod
    def parse_rpc_urls(cls, value: Any) -> list[str]:
        """Parse RPC URLs from comma-separated string or list.

        This validator is deprecated as we now use PULSECHAIN_HTTP_URL and 
        PULSECHAIN_HTTP_URL2 fields directly. Will be populated in model_post_init.

        Args:
            value: Either a comma-separated string or a list of URLs.

        Returns:
            List of RPC URL strings.
        """
        if isinstance(value, str):
            # Split by comma and strip whitespace
            return [url.strip() for url in value.split(",") if url.strip()]
        elif isinstance(value, list):
            return value
        return []

    def model_post_init(self, __context: Any) -> None:
        """Post-initialization hook to build RPC URLs list from individual fields."""
        # Build RPC URLs list from PULSECHAIN_HTTP_URL and PULSECHAIN_HTTP_URL2
        if not self.pulsechain_rpc_urls:
            self.pulsechain_rpc_urls = [
                self.pulsechain_http_url,
                self.pulsechain_http_url2,
            ]

    def get_log_level(self) -> int:
        """Convert string log level to logging constant.

        Returns:
            Logging level constant.
        """
        return getattr(logging, self.log_level.upper(), logging.INFO)

    def get_event_log_path(self) -> Path:
        """Get Path object for event log file.

        Returns:
            Path to event log file.
        """
        return Path(self.event_log_path)


# Global settings instance
settings = Settings()
