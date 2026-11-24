# PulseChain PumpTires Pending Monitor

Real-time monitoring system for detecting pending token creation transactions on the PulseChain network, specifically targeting the PumpTires factory contract.

## Overview

This system monitors the PulseChain mempool for pending transactions that call the `createToken(string name, string symbol)` function on the PumpTires factory contract (`0x6538A83a81d855B965983161AF6a83e616D16fD5`). When detected, it:

- Decodes transaction calldata to extract token name and symbol
- Calculates gas prices in PLS and Beats
- Predicts the deployed token address using CREATE opcode deterministic address calculation
- Logs all relevant information in structured JSON format

## Key Features

- **Real-time Mempool Monitoring**: WebSocket subscription to pending transactions
- **Async/Await Architecture**: High-performance async I/O using Web3.py v7+ and uvloop
- **Predictive Address Calculation**: Determines token address before mining using RLP encoding
- **Robust Error Handling**: Automatic reconnection with exponential backoff
- **Structured Logging**: JSON-formatted logs with full transaction context
- **Type-Safe Configuration**: Pydantic-based settings management

## Technical Stack

- **Python**: 3.11+
- **Web3.py**: 7.14.0+ (AsyncWeb3 with WebSocket support)
- **PulseChain**: EVM-compatible chain with WSS endpoint
- **Key Libraries**: aiohttp, pydantic, orjson, tenacity, uvloop, rlp

## Installation

### Prerequisites

- Python 3.11 or higher
- Poetry package manager

### Setup

1. **Clone the repository**:

```bash
git clone <repository-url>
cd Pump-Tires-Monitor
```

2. **Install dependencies using Poetry**:

```bash
poetry install
```

3. **Configure environment variables**:

```bash
cp .env.example .env
# Edit .env with your preferred settings
```

4. **Verify installation**:

```bash
poetry run python -m app.main --help
```

## Configuration

Configuration is managed through environment variables. Copy `.env.example` to `.env` and customize as needed.

### Key Configuration Options

| Variable                    | Default                                      | Description                                  |
| --------------------------- | -------------------------------------------- | -------------------------------------------- |
| `PULSECHAIN_WSS_URL`        | `wss://pulsechain-rpc.publicnode.com`        | WebSocket RPC endpoint                       |
| `FACTORY_ADDRESS`           | `0x6538A83a81d855B965983161AF6a83e616D16fD5` | PumpTires factory contract                   |
| `CREATE_TOKEN_SELECTOR`     | `0x2f2f2d56`                                 | Function selector for createToken            |
| `LOG_LEVEL`                 | `INFO`                                       | Logging level (DEBUG/INFO/WARNING/ERROR)     |
| `MAX_CONCURRENT_TX_FETCHES` | `50`                                         | Concurrent transaction fetch limit           |
| `WEBSOCKET_PING_INTERVAL`   | `20`                                         | WebSocket keepalive ping interval (seconds)  |
| `MAX_RETRY_ATTEMPTS`        | `5`                                          | Maximum retry attempts for failed operations |

## Usage

- Always check and update `data/seen_pending_tx.json` to ensure it includes the last seen transaction hash of the latest token deployment event on PumpTires to prevent duplicate detection. You can run locally to populate the file before production deployment.

### Persistent Data Files

The system maintains several persistent data files in the `data/` directory:

- **`data/seen_pending_tx.json`**: Tracks processed pending transactions to prevent duplicates across restarts
- **`data/buy_dedup.json`**: Tracks which transaction hashes have triggered buy operations to enforce idempotency
- **`data/scheduled_sells.json`**: Persists scheduled sell tasks with execution timestamps. If the process restarts, scheduled sells are automatically rehydrated and execute at the originally scheduled time. This file can be manually inspected or cleared if needed.

To clear scheduled sells manually:

```bash
# View current scheduled sells
cat data/scheduled_sells.json

# Clear all scheduled sells (will not execute on next restart)
echo '{"scheduled_sells": []}' > data/scheduled_sells.json

# Or simply delete the file
rm data/scheduled_sells.json
```

### Running the Monitor

**Using Poetry**:

```bash
poetry run python -m app.main
```

**Direct Python** (if in activated virtual environment):

```bash
python -m app.main
```

### Core Components

#### 1. Mempool Listener (`app/listeners/mempool/`)

- Establishes WebSocket connection to PulseChain and subscribes to `newPendingTransactions` and `logs` (for `TokenCreated` events).
- Filters transactions by factory address and `createToken` selector.
- Decodes calldata, predicts token addresses, and emits `create_token_detected` events to an internal `EventBus`.
- Reconciles predictions with `TokenCreated` events, commits nonces, and records metrics.
- See "Modular Architecture" section for a detailed breakdown.

#### 2. Trading Layer (`app/trading/`)

- **`TradingRunner`**: Consumes events from the `EventBus` and executes trades.
  - **Buy Logic**: On `create_token_detected`, it triggers a buy using a comprehensive gas strategy (multi-block sampling, mempool analysis). It uses a persistent deduplication store (`data/buy_dedup.json`) to prevent duplicate buys for the same transaction hash.
  - **Sell Logic**: If `trading_auto_sell_enabled` is true, it schedules a sell transaction to execute after a configured delay.
  - **Persistence**: Scheduled sells are persisted in `data/scheduled_sells.json`. If the process restarts, it rehydrates the tasks and executes them at the correct time.
- **`TradingWallet`**: Manages the trader's private key, nonce, and transaction signing.
- **`GasOracle`**: Computes competitive gas fees (EIP-1559) using a rolling window of recent transaction tips and mempool analysis.
- **`FactoryClient`**: Encodes calldata for buy, sell, and approve operations on the PumpTires factory.

#### 3. IO and Services

- **`app/io/`**: Handles persistent storage for deduplication (`persistent_dedup.py`) and scheduled sells (`scheduled_sells.py`) using atomic JSON writes.
- **`app/services/`**: Includes address prediction, metrics tracking, and nonce management.
- **`app/utils/`**: Provides shared utilities for logging, multi-RPC broadcasting, and gas sampling.

## Predictive Address Calculation

### How It Works

When a contract uses the `CREATE` opcode (not `CREATE2`), the deployed address is deterministic:

```python
deployed_address = keccak256(rlp.encode([sender_address, nonce]))[-20:]
```

Where:

- `sender_address`: The factory contract address
- `nonce`: The transaction count of the factory

### Limitations

1. **Race Conditions**: Multiple pending transactions can lead to nonce uncertainty
2. **Mempool Ordering**: Transaction order is not guaranteed until mining
3. **Pending State**: The 'pending' block parameter may not reflect exact mining state
4. **Transaction Replacement**: Transactions can be replaced with higher gas prices

### Future Enhancements

For production use, consider implementing:

- Pending transaction tracking and nonce monitoring
- Transaction pool state caching
- Post-mining validation of predictions
- Transaction replacement detection

## Performance Considerations

### Optimizations

- **uvloop**: Replaces asyncio event loop for ~2-4x performance improvement
- **Async I/O**: Non-blocking WebSocket and RPC operations
- **Concurrency Control**: Semaphore-based rate limiting for RPC calls
- **Connection Keepalive**: WebSocket ping/pong for stable connections
- **Structured Logging**: orjson for fast JSON serialization

### Resource Usage

- **Memory**: ~50-100 MB baseline + transaction buffers
- **Network**: Continuous WebSocket connection + periodic RPC calls
- **CPU**: Low (<5%) except during high mempool activity

## Development

### Code Style

The project follows strict Python code style guidelines defined in `python-style.mdc`:

- **Formatter**: Black (line length 88)
- **Import Sorting**: isort (profile "black")
- **Type Checking**: mypy with configured strictness
- **Docstrings**: Google-style docstrings for all public functions

### Linting

Run type checking and linters:

```bash
# Type checking (mypy)
poetry run mypy app

# Code formatting (black)
poetry run black app

# Import sorting (isort)
poetry run isort app

# Run all checks
poetry run black app && poetry run isort app && poetry run mypy app
```

## Modular Architecture

The mempool listener was refactored into focused modules under `app/listeners/mempool/` while preserving behavior, logs, and entry points.

- **orchestrator.py (MempoolListener)**: Thin coordinator that wires all components, maintains public API, and manages reconnect/backoff.
- **connection.py (ConnectionManager)**: Initializes WebSocket/HTTP clients, verifies chain ID, seeds `NonceTracker`, and prepares IO (`JSONLWriter`, `PersistentDedup`).
- **subscriptions.py (SubscriptionRunner)**: Creates pending-tx and TokenCreated logs subscriptions, performs a small backfill, and runs the subscription loop with retry/timeout.
- **pending_processor.py (PendingTransactionProcessor)**: Filters transactions (factory address + selector), decodes calldata, deduplicates, reserves nonce, predicts address, emits `create_token_detected` events, and updates metrics.
- **reconciliation.py (TokenCreatedReconciler)**: Decodes TokenCreated logs, commits nonce reservations, reconciles predictions, records metrics, deduplicates writes, and handles reorg removals.
- **prediction_store.py (PredictionStore)**: In-memory state for dedup, correlations, committed nonces, and counters with periodic logging.
- **cleanup.py (CleanupManager)**: Periodic task that removes stale correlations and releases nonce reservations.

Import compatibility is maintained via `from app.listeners.mempool import MempoolListener`. Data formats, metrics names, and logs remain unchanged.

### Testing

```bash
# Run tests with pytest
poetry run pytest

# Run with coverage
poetry run pytest --cov=app --cov-report=html
```

## PulseChain Network Details

- **Network**: PulseChain Mainnet
- **Native Currency**: PLS (Pulse)
- **Gas Units**: 1 PLS = 1,000,000,000 Beats
- **Gas Model**: EIP-1559 (Base Fee + Priority Fee)
- **EVM Compatibility**: Ethereum fork with lower fees
