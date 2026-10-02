# Mining

A miner is an HTTP server that answers signed forecast requests from
validators. You earn emissions by being more accurate than the other miners
on the same tasks (see [SCORING.md](SCORING.md)).

## 1. Wallet and registration

```bash
uv run btcli wallet create -w miner
uv run btcli tx burned-register --netuid <NETUID> -w miner -H default --network test
```

## 2. Run

```bash
uv run stx-miner --netuid <NETUID> --network test \
  --wallet.name miner --wallet.hotkey default \
  --port 8091 --external-ip <YOUR_PUBLIC_IP>
```

On start the miner publishes `ip:port` on chain (`serve_axon`) so validators
can find it. The port must be reachable from the internet. Use
`--no-serve-axon` if you publish the endpoint some other way.

Flags worth knowing:

| flag | default | meaning |
|---|---|---|
| `--model` | `stocktensor.models:volatility_bands` | your forecasting function |
| `--rpc` | `https://robinhood-rpc.publicnode.com` | Robinhood Chain RPC, repeat for fallbacks |
| `--metagraph-refresh` | `600` | seconds between validator-permit refreshes |
| `--allow-hotkey` | none | extra hotkey allowed to query (local testing) |

## 3. Your own model

```python
# my_model.py
from stocktensor.protocol import Forecast, ForecastRequest, format_price

def predict(request: ForecastRequest, history: list[tuple[float, float]]) -> Forecast:
    reference = float(request.reference_price)
    # history = recent Chainlink rounds for request.asset as (unix_time, price)
    ...
    return Forecast(low=format_price(lo), point=format_price(mid),
                    high=format_price(hi), p_up="0.57")
```

```bash
uv run stx-miner ... --model my_model:predict
```

The function can be `async`. It has 8 seconds; the validator's whole
request window is 12 seconds by default.

What a valid answer looks like:

- `low ≤ point ≤ high`, all positive, inside `[reference/10, reference×10]`
- `low`/`high` = your **80%** interval for the price at `as_of + horizon`
- `p_up` = your probability that the price ends **above** the reference
- prices as decimal strings (`format_price` does this)

## What the scoring rewards

- **Calibrated intervals.** About 8 in 10 realised prices should land inside
  your interval. Too wide costs width, too narrow costs ×10 the miss.
- **Honest probabilities.** `p_up` is scored with a Brier score. 0.5 is a safe
  default; move away from it only with evidence.
- **Showing up.** Every task you are queried for counts. A timeout scores 0.

The reference model (random walk + realised volatility, `p_up = 0.5`) is the
bar. Beating it on direction, on interval sharpness during calm sessions, or
around earnings is where the edge is.

## Things to know about the feeds

- Prices are Chainlink's Robinhood Chain feeds: underlying price × the token's
  multiplier (dividends, corporate actions).
- Feeds update 24/5 and hold over the weekend. Validators send no tasks between
  Friday 20:00 and Sunday 20:00 New York time.
- A task with no feed update during its horizon is void and does not count.
