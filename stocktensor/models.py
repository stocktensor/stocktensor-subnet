"""Forecast models for the miner.

A model is any callable ``(request, history) -> Forecast`` (sync or async),
where ``history`` is the recent Chainlink series for the asset as
``(updated_at, price)`` floats, oldest first. Point the miner at your own with
``--model your_module:predict``.

The reference model below is deliberately simple: it is the bar to beat, not
a trading signal.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .protocol import HORIZONS, Forecast, ForecastRequest, format_price

Z80 = 1.2815515655446004  # standard normal quantile for a central 80% interval
MIN_SIGMA = 0.001  # never claim less than 0.1% uncertainty
FALLBACK_DAILY_SIGMA = 0.02  # used when history is too short


def realised_sigma_per_second(history: Sequence[tuple[float, float]]) -> float | None:
    """Variance-per-second estimate from log returns between consecutive rounds."""
    if len(history) < 3:
        return None
    total = 0.0
    for (_, previous), (_, current) in zip(history, history[1:], strict=False):
        if previous > 0 and current > 0:
            total += math.log(current / previous) ** 2
    span = history[-1][0] - history[0][0]
    if span <= 0 or total <= 0:
        return None
    return math.sqrt(total / span)


def volatility_bands(request: ForecastRequest, history: Sequence[tuple[float, float]]) -> Forecast:
    """Random-walk baseline: point = reference, p_up = 0.5, 80% band from realised volatility."""
    reference = float(request.reference_price)
    horizon = HORIZONS[request.horizon]
    per_second = realised_sigma_per_second(history)
    if per_second is None:
        sigma = FALLBACK_DAILY_SIGMA * math.sqrt(horizon / 86_400)
    else:
        sigma = per_second * math.sqrt(horizon)
    sigma = max(sigma, MIN_SIGMA)
    low = reference * math.exp(-Z80 * sigma)
    high = reference * math.exp(Z80 * sigma)
    return Forecast(low=format_price(low), point=format_price(reference), high=format_price(high), p_up="0.5")
