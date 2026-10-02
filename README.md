# stocktensor-subnet

A Bittensor subnet where AI miners forecast **Robinhood Chain stock tokens**
and validators score them against **Chainlink prices** on Robinhood Chain.

Every forecast is signed by the miner that made it, every score can be
recomputed from public data, and every validator epoch is published as a
signed bundle whose hash is anchored on the Bittensor chain.

- Website: https://stocktensor.io
- Docs: https://docs.stocktensor.io
- Protocol: [docs/PROTOCOL.md](docs/PROTOCOL.md) · Scoring: [docs/SCORING.md](docs/SCORING.md)
- Run a miner: [docs/MINING.md](docs/MINING.md) · Run a validator: [docs/VALIDATING.md](docs/VALIDATING.md)

## How it works

```
                 Robinhood Chain (4663)
                 Chainlink stock token feeds (35, 24/5)
                        │ reference price at as_of
                        │ realised price at as_of + horizon
                        ▼
 validator ──btauth/1 signed POST /forecast──▶ miners
     ▲              {asset, horizon, as_of, reference_price}
     │
     └── signed forecast {low, point, high, p_up} ◀── (sr25519, miner hotkey)

 every epoch:
   score tasks ─▶ rolling scores ─▶ set_weights
             └──▶ signed bundle (prev-hash chained) ─▶ files / publish URL
                                                   └─▶ commitment "stx1:<epoch>:<hash>"
```

- **Assets:** the stock tokens with a Chainlink equity feed on Robinhood Chain
  (AAPL, NVDA, TSLA, SPY, QQQ, … — see `stocktensor/assets.json`). Every feed
  address was checked on-chain.
- **Horizons:** 1 hour, 1 day, 1 week.
- **Forecast:** an 80% price interval, a point estimate and the probability the
  price ends higher (`p_up`).
- **Scoring:** interval score + Brier score + point error, ranked among the
  miners answering the same task, then a 3-day half-life average over 14 days.
  `weight = rolling² / Σ rolling²`.
- **Transport:** plain HTTP signed with Bittensor's `btauth/1` (the SDK v11
  replacement for axon/dendrite). No custom networking stack.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/stocktensor/stocktensor-subnet
cd stocktensor-subnet
uv sync

# miner (reference model: volatility bands from recent Chainlink rounds)
uv run stx-miner --netuid <NETUID> --network test \
  --wallet.name miner --wallet.hotkey default \
  --port 8091 --external-ip <YOUR_PUBLIC_IP>

# validator
uv run stx-validator --netuid <NETUID> --network test \
  --wallet.name validator --wallet.hotkey default
```

Bring your own model with `--model your_package.module:predict`. A model is a
function `(request, history) -> Forecast`; see `stocktensor/models.py` and
[docs/MINING.md](docs/MINING.md).

## Status

- Netuid: not registered yet. Run on testnet or a local subtensor for now.
- The on-chain commitment write (`Commitments.set_commitment`) is built from
  the SDK's own type layout but has not been exercised against a live chain
  yet. See [docs/VALIDATING.md](docs/VALIDATING.md).

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Tests are fully offline: fake RPC, fake chain, real signed HTTP between a real
miner app and the validator.

`tests/golden/` holds the scoring, canonical-JSON and signature vectors shared
with [stocktensor-miner-kit](https://github.com/stocktensor/stocktensor-miner-kit)
and [stocktensor-verify](https://github.com/stocktensor/stocktensor-verify).
Change the scoring and those vectors must be regenerated
(`uv run python scripts/gen_golden.py`) and `SCORING_VERSION` bumped.

## License

MIT © 2026 Stocktensor
