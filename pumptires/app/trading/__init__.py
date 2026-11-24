"""Trading package for transaction management and execution.

This package provides comprehensive trading functionality including wallet
management (private key handling, nonce tracking, transaction signing,
EIP-1559 construction), factory contract interaction, gas price estimation,
and trade execution orchestration.
"""

from pumptires.app.trading.factory_client import FactoryClient
from pumptires.app.trading.gas_oracle import GasOracle
from pumptires.app.trading.runner import TradingRunner
from pumptires.app.trading.wallet import TradingWallet

__all__ = ["TradingWallet", "FactoryClient", "GasOracle", "TradingRunner"]
