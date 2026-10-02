# Scoring (version 1)

Reference implementation: `stocktensor/scoring.py`. Golden vectors:
`tests/golden/scoring.json`. Any change bumps `SCORING_VERSION` and regenerates
the vectors (`uv run python scripts/gen_golden.py`).

## Per task

For a resolved task with reference price `r` and realised price `y`, each
valid forecast gets three losses (lower is better):

| component | loss | weight |
|---|---|---:|
| interval | `((high − low) + (2/α)(low − y)[y < low] + (2/α)(y − high)[y > high]) / r`, α = 0.2 | 0.5 |
| direction | Brier: `(p_up − o)²`, o = 1 if y > r, 0 if y < r, 0.5 if equal | 0.3 |
| point | `|point − y| / r` | 0.2 |

The interval score rewards narrow intervals that still contain the price and
punishes misses hard (×10 the distance), so "always very wide" and "always very
narrow" both lose.

Each loss is turned into a score by **rank among the valid forecasts for that
task**: best = 1, worst = 0, ties share the average rank, a single valid
forecast scores 1. The task score is the weighted sum. Missing, late,
badly-signed or invalid forecasts score 0. Ranking makes scores comparable
across assets and horizons without any volatility model.

## Rolling score

Over the last 14 days, per miner:

```
rolling = Σ decay·task_score / Σ decay,   decay = 0.5 ^ (age / 3 days)
```

Every task a miner was queried for counts, so not answering pulls the average
down. Void tasks are skipped.

## Weights

`weight = rolling² / Σ rolling²`. Squaring sharpens the curve so the most
accurate models earn clearly more than the median, without making it
winner-takes-all.

## Consensus (product layer)

For a task, take the top 10 models by rolling score. Consensus `p_up` = median
of their `p_up`; direction = bullish if ≥ 0.55, bearish if ≤ 0.45, else
neutral; agreement = share of those models whose own direction matches.

## Known limits

- A miner could submit a previously published consensus. That forecast is
  already stale (it was made for an earlier `as_of`), and ranking against live
  models makes it a below-average strategy, but it is not impossible.
- US market holidays are not modelled; the feed holds its price, so 1h tasks
  over a holiday void themselves through the `no_update` rule.
