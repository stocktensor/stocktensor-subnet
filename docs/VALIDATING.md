# Validating

The validator asks every served miner for forecasts, resolves them against
Chainlink, sets weights, and publishes one signed bundle per epoch.

## Run

```bash
uv run stx-validator --netuid <NETUID> --network test \
  --wallet.name validator --wallet.hotkey default
```

| flag | default | meaning |
|---|---|---|
| `--round-interval` | `600` | seconds between query rounds |
| `--epoch-interval` | `3600` | seconds between bundles + weight updates |
| `--assets-per-round` | `4` | stock tokens sampled per round (× 3 horizons) |
| `--assets` | all | comma-separated subset, e.g. `NVDA,TSLA,SPY` |
| `--query-timeout` | `12` | seconds a miner has to answer |
| `--rpc` | publicnode | Robinhood Chain RPC, repeatable |
| `--db` | `~/.stocktensor/validator.sqlite` | task store and bundle chain head |
| `--bundle-dir` | `~/.stocktensor/bundles` | `<epoch>.json` + `latest.json` |
| `--publish-url` | none | POST each bundle there, signed with `btauth/1` |
| `--no-anchor` | off | skip the on-chain commitment |
| `--no-set-weights` | off | dry run: score and publish, never set weights |

## One round

1. Skip if the session is `closed` (weekend).
2. Sample assets; skip any whose feed has not updated for 6 hours.
3. For each asset × horizon: build a task, POST it to every miner at once,
   record each answer (or `timeout` / `http` / `late` / `invalid` /
   `bad_signature`).

## Resolving

Once `as_of + horizon + 60 s` has passed, the realised price is the last
Chainlink round with `updatedAt <= as_of + horizon`, found by binary search
over round ids. If that is not newer than the reference round, the task is
void (`no_update`).

## Each epoch

1. Rolling scores over 14 days, weights = `rolling² / Σ rolling²`.
2. `set_weights` (the SDK picks plain or commit-reveal from the subnet's
   hyperparameters).
3. Build the bundle, chained to the previous one with `prev`, signed by the
   hotkey; write it to `--bundle-dir`; POST it to `--publish-url` if set.
4. Anchor: set the hotkey's commitment on the subnet to
   `stx1:<epoch>:<bundle_hash>`.

Anyone can check the result with
[stocktensor-verify](https://github.com/stocktensor/stocktensor-verify):
signatures, the hash chain against the on-chain commitment, every score, and
every Chainlink price.

## Untested on-chain

- **Commitment write.** The call is `Commitments.set_commitment(netuid, info)`
  with `info = {"fields": [{"Raw<n>": <bytes>}]}`, signed by the hotkey. The
  shape follows the pallet's `CommitmentInfo { fields: BoundedVec<Data> }` and
  the SDK's own decoder (`bittensor.metagraph._decode_fields`), but it has not
  been submitted to a live chain from this code yet. Try it on a local
  subtensor or testnet first; if it fails, run with `--no-anchor` and open an
  issue.
- **Weights and serving** use the SDK's `SetWeights` and `ServeAxon` intents
  as documented, but likewise have only been exercised against a fake chain in
  the test suite.

## Keys

The validator signs requests, bundles and the commitment with the **hotkey**
only. The coldkey is never loaded.
