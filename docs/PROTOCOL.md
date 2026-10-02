# Protocol (version 1)

How validators, miners and outside verifiers talk to each other. The reference
implementation is `stocktensor/protocol.py`; `tests/golden/` holds byte-exact
fixtures for other languages.

## Assets and prices

- Assets are the Robinhood Chain stock tokens that have a Chainlink feed with
  `marketHours = us_equities_24/5` (`stocktensor/assets.json`, 35 today).
- The **only** price source for scoring is the Chainlink proxy on Robinhood
  Chain (chain id 4663). The feed price is `underlying price × token multiplier`,
  so it is the price of the token, not the share.
- Feeds update 24/5 and hold the last price over the weekend. Validators do not
  create tasks while the session is `closed` (Friday 20:00 → Sunday 20:00 ET).
- `reference` = the latest round at `as_of`. `realised` = the last round with
  `updatedAt <= as_of + horizon`. If that round is not newer than the reference
  round, the task is **void** (`"no_update"`) and is not scored.
- Round ids are uint80, so they travel as decimal strings.

## Horizons

| id | seconds |
|----|--------:|
| `1h` | 3 600 |
| `1d` | 86 400 |
| `1w` | 604 800 |

## Canonical JSON

Every hashed or signed object is canonical JSON: keys sorted, no whitespace,
ASCII only, **no floats** (decimals are strings). `sha256` is lowercase hex.

## Transport

HTTP, authenticated with Bittensor's `btauth/1` signed requests
(`bittensor.http_auth`). Miners publish `ip:port` with the `serve_axon` intent.

`POST /forecast`, body (canonical JSON not required on the wire):

```json
{"version": 1, "task_id": "…", "asset": "NVDA", "horizon": "1h",
 "as_of": 1790000000, "reference_price": "236.22797244", "deadline": 1790000012}
```

Miners must verify the request is signed by a hotkey with a validator permit
and addressed to them (receiver = own hotkey). Response `200`:

```json
{"task_id": "…", "miner": "5F…",
 "forecast": {"low": "230.1", "point": "236.5", "high": "241.75", "p_up": "0.61"},
 "signature": "0x…"}
```

- `low`/`high`: central **80%** prediction interval of the price at `as_of + horizon`.
- `point`: best single estimate. `p_up`: probability the price ends above `reference_price`.
- Valid only if `0 < low ≤ point ≤ high`, `0 ≤ p_up ≤ 1`, and the interval is inside
  `[reference / 10, reference × 10]`.
- `signature` = sr25519 signature by the miner hotkey over
  `"stx-forecast/1\n" + sha256(canonical({"task_id", "miner", "forecast"}))`
  (the hash is the 64-char hex string, as ASCII bytes).
- Answers after `deadline`, with a bad signature, or invalid score 0.

`task_id` = first 32 hex chars of `sha256(canonical({"validator", "asset", "horizon", "as_of"}))`.

## Epoch bundles

After each epoch the validator publishes one JSON bundle with every task it
resolved since the previous bundle:

```json
{
  "version": 1, "scoring_version": 1, "netuid": 0,
  "validator": "5F…", "epoch": 12, "created_at": 1790003700,
  "prev": "<bundle hash of epoch 11, or 64 zeros>",
  "tasks": [{
    "task_id": "…", "asset": "NVDA", "horizon": "1h", "as_of": 1790000000,
    "session": "overnight", "feed": "0x379E…",
    "reference": {"round_id": "…", "answer": "236.22797244", "updated_at": 1789999990},
    "realised":  {"round_id": "…", "answer": "237.1", "updated_at": 1790003500},
    "void": null,
    "responses": [{"miner": "5F…", "forecast": {…} | null, "signature": "0x…" | null,
                   "error": null | "timeout" | "http" | "bad_signature" | "invalid"}],
    "scores": {"5F…": "0.812500"}
  }],
  "rolling": {"5F…": "0.712345"},
  "weights": {"5F…": "0.204000"},
  "signature": "0x…"
}
```

- Scores, rolling scores and weights are strings with 6 decimals. Verifiers
  recompute them and compare with a tolerance of `1e-6`.
- `rolling` is computed at `created_at` over all of this validator's tasks in the
  last 14 days, which is why bundles chain through `prev`.
- **Bundle hash** = `sha256(canonical(bundle without "signature"))`.
- **Signature** = validator hotkey (sr25519) over `"stx-bundle/1\n" + bundle_hash`.
- **Anchor**: the validator sets its on-chain commitment (Commitments pallet,
  Raw field) on the subnet to `stx1:<epoch>:<bundle_hash>`. Because each bundle
  names the previous hash, anchoring the newest bundle anchors the whole chain.
- Bundles are written to `<dir>/<epoch>.json` and `<dir>/latest.json`, and can be
  POSTed (signed with `btauth/1`) to a publish URL.
