# PulseChain Knowledge Base

**Reviewed:** 2026-11-24

**Scope:** PulseChain mainnet facts and the integration implemented in
`pumptires/`. Repository statements describe the code and configuration as
checked in; they do not by themselves prove a live endpoint or contract works.

## Network identity

| Item | Current understanding | Evidence |
|---|---|---|
| Network | PulseChain mainnet, treated by this project as an EVM JSON-RPC chain | PumpTires uses Web3.py `eth_*` APIs and EVM transaction fields. |
| Chain ID | `369` (`0x171`) | `pumptires/.env.example`, `pumptires/app/config/settings.py`, and `pumptires/app/trading/wallet.py`; also listed by the [PulseChain mainnet guide](https://gitlab.com/pulsechaincom/pulsechain-mainnet/-/blob/master/README.md). |
| Native currency | PLS | Project README and the PulseChain mainnet guide. PLS pays transaction fees; token values and gas fees are separate quantities. |
| Officially documented HTTP RPC | `https://rpc.pulsechain.com` | Listed in the mainnet guide. Availability, rate limits, archive depth, and pending-transaction support are provider properties and were not probed for this document. |
| Project RPC configuration | Separate HTTP and WSS URLs, plus a second HTTP URL for broadcasts | `.env.example` uses placeholders. `settings.py` has historical PublicNode and G4MM4 defaults; these should not be mistaken for configured or verified production endpoints. |

The project expects WebSocket subscriptions for pending transactions and
factory logs, while HTTP is used for transaction reads, nonce reads, and
broadcasting. A provider can serve ordinary JSON-RPC while omitting or
restricting `newPendingTransactions` subscriptions or the `txpool_content`
extension. The code's subscription design does not establish that every public
RPC exposes those feeds.

## Fees and denominations

PulseChain transactions in the project use the EIP-1559 fields
`baseFeePerGas`, `maxFeePerGas`, and `maxPriorityFeePerGas`; the wallet builds
type-2 transactions. Under [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559),
the base fee changes with block gas demand, the priority fee incentivizes
inclusion, and `maxFeePerGas` bounds the sender's per-gas offer. A configured
maximum is not the same as the fee ultimately paid.

The PumpTires README states `1 PLS = 1,000,000,000 Beats`. A PulseChain
[explorer transaction example](https://otter.pulsechain.com/tx/0xf87488ff9df7a42c291169502f639d0b8660174b0133001ce77871e248fc6172)
displays fees in Beats and impulses, with values consistent with 1 Beat being
1,000,000,000 impulses. That conversion is inferred from the explorer display;
the checked-in project does not provide a canonical denomination specification.

**Do not use the checked-in gas numbers as live recommendations.** The code
passes raw Web3 fee integers between components, but labels them inconsistently
as wei, Beats, or Gwei:

- `app/trading/gas_oracle.py` multiplies its Gwei floor by `10^9` to get the raw
  fee integer, while naming the result `*_beats`.
- `app/utils/gas_sampling.py` documents raw values as wei but uses `10^12` for
  Gwei conversion and display. `app/utils/nonce_mgmt.py` uses the same factor.
- `app/contracts/factory.py::format_gas_price` treats a raw transaction fee as
  Beats/Wei, then divides by `10^9` for both the PLS and Gwei outputs.
- `app/trading/runner.py` converts the block base fee by `10^9` and labels that
  result Beats.

These paths do not agree on units. Resolve the unit contract and its conversions
before relying on fee sampling, fee display, retries, or automated trading.

## PumpTires integration flow

The project is a PumpTires factory monitor with a trading layer. Its configured
factory address is
`0x6538A83a81d855B965983161AF6a83e616D16fD5`; its configured
`createToken(string,string)` selector is `0x2f2f2d56`. Treat those as project
configuration, not as an independently verified on-chain deployment.

1. `app/main.py` starts the listener and handles shutdown signals.
2. `app/listeners/mempool/connection.py` opens WSS and HTTP clients, checks the
   WSS chain ID against `EXPECTED_CHAIN_ID` (default `369`), prepares the
   factory contract, seeds a factory nonce, and initializes event and dedup
   persistence.
3. `app/listeners/mempool/subscriptions.py` backfills the last 100 blocks of
   `TokenCreated` logs, then subscribes to pending transactions and factory
   logs. The pending subscription requests full transaction objects.
4. `app/listeners/mempool/pending_processor.py` filters for transactions sent
   to the configured factory and matching the selector, decodes name and
   symbol, deduplicates by transaction hash, reserves a factory nonce, and
   predicts a CREATE address.
5. `app/services/address_predictor.py` applies the standard CREATE address
   formula: the last 20 bytes of `keccak(rlp([creator_address, nonce]))`.
   The prediction is provisional until the factory's `TokenCreated` log is
   observed.
6. `app/listeners/mempool/reconciliation.py` correlates logs with predictions,
   commits nonce reservations, records events, and handles removed logs during
   reorgs. Events are written to JSONL; deduplication and scheduled sells use
   local JSON persistence under `data/`.
7. If enabled, `app/trading/runner.py` consumes detected-token events, signs
   type-2 transactions for chain ID 369, and broadcasts through configured HTTP
   endpoints. Automatic selling has a separate setting.

## Configuration and safety

- Settings load `.env` / environment variables and `config.yaml`; environment
  settings take priority over YAML. Paths such as `config.yaml` and `data/` are
  relative to the process working directory. Start the app from `pumptires/` if
  using those paths as written.
- The repository includes `.env.example` and `config.example.yaml`, not actual
  `.env` or `config.yaml` files. RPC URLs and the private key in `.env.example`
  are placeholders.
- Trading defaults to disabled. `TRADING_PRIVATE_KEY` is supplied through the
  environment; automatic selling also defaults to disabled. Keep both facts
  distinct from the monitor's read-only path.
- Example operational settings include 50 concurrent transaction fetches,
  20-second WebSocket pings, a 300-second subscription timeout, a 10,000-entry
  dedup bound, and an event queue of 1,000. These are project defaults, not
  PulseChain protocol limits.

## Configuration and documentation discrepancies

The following are repository issues to resolve, not settled chain facts:

| Area | Evidence mismatch |
|---|---|
| RPC defaults | `settings.py` defaults to PublicNode WSS/HTTP and G4MM4 secondary HTTP; the official mainnet guide lists `https://rpc.pulsechain.com`; `.env.example` leaves provider URLs as placeholders. |
| Gas keys | `config.example.yaml` supplies `tip_min_beats`, `fee_cap_beats`, and `tip_max_factor`; `settings.py` reads `tip_min_gwei`, `tip_max_gwei`, and `fee_cap_multiplier`. Those example keys therefore do not configure the corresponding settings. |
| Gas defaults | The example YAML specifies P90 and 25% / 37.5% headroom; settings fallbacks specify P95 and 50% / 75% headroom. Which values apply depends on whether a local `config.yaml` exists and which keys it contains. |
| Missing cited source | `config.example.yaml` cites `Resources/PulseChain_Gas_Swap_Tuning.md`; that file is absent from this checkout and the local PumpTires repository history searched for this review. |
| Live capability | No endpoint, pending-transaction feed, `txpool_content` method, factory bytecode, or live gas behavior was verified for this document. |

The values in `config.example.yaml` are historical tuning examples. In
particular, gas limits, percentiles, retry counts, and fee caps are not current
network facts.

## Source map

- Network/configuration: `pumptires/.env.example`,
  `pumptires/config.example.yaml`, `pumptires/app/config/settings.py`.
- Listener and recovery: `pumptires/app/listeners/mempool/`.
- Factory ABI and decoding: `pumptires/app/contracts/`.
- Gas and transactions: `pumptires/app/trading/gas_oracle.py`,
  `pumptires/app/utils/gas_sampling.py`,
  `pumptires/app/utils/nonce_mgmt.py`, `pumptires/app/trading/wallet.py`.
- Official and standards references: [PulseChain mainnet guide](https://gitlab.com/pulsechaincom/pulsechain-mainnet/-/blob/master/README.md),
  [PulseChain explorer transaction example](https://otter.pulsechain.com/tx/0xf87488ff9df7a42c291169502f639d0b8660174b0133001ce77871e248fc6172),
  and [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559).
