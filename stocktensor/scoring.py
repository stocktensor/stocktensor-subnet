"""Scoring rules, version 1. The normative description is ``docs/SCORING.md``.

The TypeScript verifier (``stocktensor-verify``) and the miner kit run the same
rules against the golden vectors in ``tests/golden``. Change anything here and
those vectors must be regenerated and the version bumped.

Determinism: only basic float arithmetic, a fixed summation order (tasks
sorted by ``(as_of, task_id)``, components in ``COMPONENTS`` order) and ties
resolved by average rank. ``pow`` is the one call that may differ in the last
bit across languages, so cross-language checks compare with a tolerance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .protocol import Forecast, InvalidForecast

SCORING_VERSION = 1

ALPHA = 0.2  # 1 - interval coverage (80% interval)
COMPONENTS: tuple[tuple[str, float], ...] = (
    ("interval", 0.5),
    ("direction", 0.3),
    ("point", 0.2),
)
HALF_LIFE_SECONDS = 3 * 86_400
WINDOW_SECONDS = 14 * 86_400
WEIGHT_POWER = 2
NEUTRAL_BAND = 0.05  # p_up within 0.5 +/- band reads as neutral
CONSENSUS_TOP_K = 10


def interval_loss(low: float, high: float, realised: float, reference: float) -> float:
    """Interval score (Gneiting & Raftery 2007) for the 80% interval, per unit of reference price."""
    loss = high - low
    if realised < low:
        loss += (2.0 / ALPHA) * (low - realised)
    elif realised > high:
        loss += (2.0 / ALPHA) * (realised - high)
    return loss / reference


def direction_loss(p_up: float, realised: float, reference: float) -> float:
    """Brier score of ``p_up`` against the realised direction."""
    if realised > reference:
        outcome = 1.0
    elif realised < reference:
        outcome = 0.0
    else:
        outcome = 0.5
    return (p_up - outcome) ** 2


def point_loss(point: float, realised: float, reference: float) -> float:
    """Absolute error of the point estimate, per unit of reference price."""
    return abs(point - realised) / reference


def rank_scores(losses: Mapping[str, float]) -> dict[str, float]:
    """Map losses to 0..1 scores by rank: best = 1, worst = 0, ties share the average rank."""
    n = len(losses)
    if n == 0:
        return {}
    if n == 1:
        return {key: 1.0 for key in losses}
    ordered = sorted(losses.items(), key=lambda kv: (kv[1], kv[0]))
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = average
        i = j + 1
    return {key: 1.0 - (rank - 1.0) / (n - 1) for key, rank in ranks.items()}


def score_task(
    reference: float,
    realised: float,
    responses: Mapping[str, Forecast | None],
) -> dict[str, float]:
    """Score one resolved task.

    ``responses`` has one entry per queried miner; ``None`` means no answer,
    a late answer, or a bad signature. Invalid or missing forecasts score 0.
    """
    valid: dict[str, tuple[float, float, float, float]] = {}
    for miner, forecast in responses.items():
        if forecast is None:
            continue
        try:
            valid[miner] = forecast.validate(reference)
        except InvalidForecast:
            continue

    component_losses: dict[str, dict[str, float]] = {name: {} for name, _ in COMPONENTS}
    for miner, (low, point, high, p_up) in valid.items():
        component_losses["interval"][miner] = interval_loss(low, high, realised, reference)
        component_losses["direction"][miner] = direction_loss(p_up, realised, reference)
        component_losses["point"][miner] = point_loss(point, realised, reference)

    component_scores = {name: rank_scores(component_losses[name]) for name, _ in COMPONENTS}
    scores: dict[str, float] = {}
    for miner in sorted(responses):
        if miner not in valid:
            scores[miner] = 0.0
            continue
        total = 0.0
        for name, weight in COMPONENTS:
            total += weight * component_scores[name][miner]
        scores[miner] = total
    return scores


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    as_of: int
    scores: Mapping[str, float]  # every queried miner, 0 for missing/invalid


def rolling_scores(results: Sequence[TaskResult], now: int) -> dict[str, float]:
    """Time-decayed average task score per miner over the last ``WINDOW_SECONDS``.

    A task counts for every miner that was queried, so missing answers pull
    the average down. Weight of a task = 0.5 ** (age / HALF_LIFE_SECONDS).
    """
    numerator: dict[str, float] = {}
    denominator: dict[str, float] = {}
    for result in sorted(results, key=lambda r: (r.as_of, r.task_id)):
        age = now - result.as_of
        if age < 0 or age > WINDOW_SECONDS:
            continue
        decay = 0.5 ** (age / HALF_LIFE_SECONDS)
        for miner in sorted(result.scores):
            numerator[miner] = numerator.get(miner, 0.0) + decay * result.scores[miner]
            denominator[miner] = denominator.get(miner, 0.0) + decay
    return {
        miner: (numerator[miner] / denominator[miner] if denominator[miner] > 0 else 0.0)
        for miner in sorted(numerator)
    }


def weights(scores: Mapping[str, float]) -> dict[str, float]:
    """Normalised weights: score ** WEIGHT_POWER, summing to 1. Empty if all scores are 0."""
    powered = {miner: max(score, 0.0) ** WEIGHT_POWER for miner, score in sorted(scores.items())}
    total = 0.0
    for value in powered.values():
        total += value
    if total <= 0:
        return {}
    return {miner: value / total for miner, value in powered.items()}


def direction_of(p_up: float) -> str:
    if p_up >= 0.5 + NEUTRAL_BAND:
        return "bullish"
    if p_up <= 0.5 - NEUTRAL_BAND:
        return "bearish"
    return "neutral"


@dataclass(frozen=True)
class Consensus:
    direction: str
    p_up: float  # median p_up of the top models
    agreement: float  # share of top models whose own direction matches
    models: int


def consensus(
    forecasts: Mapping[str, Forecast], rolling: Mapping[str, float], top_k: int = CONSENSUS_TOP_K
) -> Consensus | None:
    """Consensus of the top-``top_k`` models (by rolling score) for one task."""
    ranked = sorted(
        (m for m in forecasts if rolling.get(m, 0.0) > 0),
        key=lambda m: (-rolling[m], m),
    )[:top_k]
    p_values: list[float] = []
    for miner in ranked:
        try:
            p_values.append(forecasts[miner].numbers()[3])
        except InvalidForecast:
            continue
    if not p_values:
        return None
    ordered = sorted(p_values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    direction = direction_of(median)
    agreeing = sum(1 for p in p_values if direction_of(p) == direction)
    return Consensus(direction, median, agreeing / len(p_values), len(p_values))
