"""Wire protocol between validators and miners (version 1).

Transport is plain HTTP authenticated with Bittensor's ``btauth/1`` signed
requests. The validator POSTs a :class:`ForecastRequest` to ``/forecast`` on
the miner's served axon; the miner answers with a :class:`ForecastResponse`
whose forecast is signed by the miner hotkey, so the validator cannot alter
it and anyone can check it later in the epoch bundle.

All prices are USD decimal strings. See ``docs/PROTOCOL.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .canonical import canonical, digest, sha256_hex

PROTOCOL_VERSION = 1
FORECAST_PATH = "/forecast"

HORIZONS: dict[str, int] = {"1h": 3_600, "1d": 86_400, "1w": 604_800}

# The forecast interval is the central 80% prediction interval.
INTERVAL_COVERAGE = "0.8"

# Sanity bounds: anything outside [reference / 10, reference * 10] is invalid.
MAX_PRICE_RATIO = 10.0

FORECAST_DOMAIN = b"stx-forecast/1\n"
BUNDLE_DOMAIN = b"stx-bundle/1\n"


class InvalidForecast(ValueError):
    """A forecast that fails validation. It scores zero for its task."""


def task_id(validator_hotkey: str, asset: str, horizon: str, as_of: int) -> str:
    """Deterministic id for one forecast task."""
    return digest({"validator": validator_hotkey, "asset": asset, "horizon": horizon, "as_of": as_of})[:32]


@dataclass(frozen=True)
class ForecastRequest:
    task_id: str
    asset: str
    horizon: str
    as_of: int  # unix seconds
    reference_price: str  # Chainlink answer at as_of, USD decimal string
    deadline: int  # unix seconds; answers after this are discarded

    def to_json(self) -> dict[str, Any]:
        return {
            "version": PROTOCOL_VERSION,
            "task_id": self.task_id,
            "asset": self.asset,
            "horizon": self.horizon,
            "as_of": self.as_of,
            "reference_price": self.reference_price,
            "deadline": self.deadline,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ForecastRequest:
        if data.get("version") != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version {data.get('version')!r}")
        if data.get("horizon") not in HORIZONS:
            raise ValueError(f"unknown horizon {data.get('horizon')!r}")
        return cls(
            task_id=str(data["task_id"]),
            asset=str(data["asset"]),
            horizon=str(data["horizon"]),
            as_of=int(data["as_of"]),
            reference_price=str(data["reference_price"]),
            deadline=int(data["deadline"]),
        )


@dataclass(frozen=True)
class Forecast:
    """A miner's answer for one task.

    ``low``/``high`` bound the central 80% prediction interval of the price at
    ``as_of + horizon``; ``point`` is the best single guess; ``p_up`` is the
    probability that the price ends above the reference price.
    """

    low: str
    point: str
    high: str
    p_up: str

    def to_json(self) -> dict[str, str]:
        return {"low": self.low, "point": self.point, "high": self.high, "p_up": self.p_up}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Forecast:
        try:
            return cls(
                low=str(data["low"]),
                point=str(data["point"]),
                high=str(data["high"]),
                p_up=str(data["p_up"]),
            )
        except (KeyError, TypeError) as exc:
            raise InvalidForecast(f"malformed forecast: {exc}") from exc

    def numbers(self) -> tuple[float, float, float, float]:
        try:
            values = tuple(float(v) for v in (self.low, self.point, self.high, self.p_up))
        except ValueError as exc:
            raise InvalidForecast("non-numeric field") from exc
        if not all(math.isfinite(v) for v in values):
            raise InvalidForecast("non-finite field")
        return values  # type: ignore[return-value]

    def validate(self, reference_price: float) -> tuple[float, float, float, float]:
        """Return (low, point, high, p_up) as floats or raise InvalidForecast."""
        low, point, high, p_up = self.numbers()
        if not (0 < low <= point <= high):
            raise InvalidForecast("need 0 < low <= point <= high")
        if not (0.0 <= p_up <= 1.0):
            raise InvalidForecast("p_up must be in [0, 1]")
        if low < reference_price / MAX_PRICE_RATIO or high > reference_price * MAX_PRICE_RATIO:
            raise InvalidForecast("interval outside sanity bounds")
        return low, point, high, p_up


def forecast_message(task: str, miner_hotkey: str, forecast: Forecast) -> bytes:
    """The exact bytes a miner signs for a forecast."""
    body = {"task_id": task, "miner": miner_hotkey, "forecast": forecast.to_json()}
    return FORECAST_DOMAIN + digest(body).encode()


def bundle_message(bundle_hash: str) -> bytes:
    """The exact bytes a validator signs for an epoch bundle."""
    return BUNDLE_DOMAIN + bundle_hash.encode()


def bundle_hash(bundle: dict[str, Any]) -> str:
    """Hash of a bundle with its ``signature`` field removed."""
    unsigned = {k: v for k, v in bundle.items() if k != "signature"}
    return sha256_hex(canonical(unsigned))


def commitment_text(epoch: int, bundle_hash_hex: str) -> str:
    """What the validator writes to its on-chain commitment after each epoch."""
    return f"stx1:{epoch}:{bundle_hash_hex}"


def parse_commitment(text: str | None) -> tuple[int, str] | None:
    if not text or not text.startswith("stx1:"):
        return None
    try:
        _, epoch, value = text.split(":")
        if len(value) != 64:
            return None
        int(value, 16)
        return int(epoch), value
    except ValueError:
        return None


@dataclass(frozen=True)
class ForecastResponse:
    task_id: str
    miner: str  # hotkey ss58
    forecast: Forecast
    signature: str  # 0x-hex signature over forecast_message()

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "miner": self.miner,
            "forecast": self.forecast.to_json(),
            "signature": self.signature,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ForecastResponse:
        try:
            return cls(
                task_id=str(data["task_id"]),
                miner=str(data["miner"]),
                forecast=Forecast.from_json(data["forecast"]),
                signature=str(data["signature"]),
            )
        except (KeyError, TypeError) as exc:
            raise InvalidForecast(f"malformed response: {exc}") from exc


def format_price(value: float, places: int = 6) -> str:
    """Decimal string for a price, trimmed of trailing zeros."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def chainlink_to_str(answer: int, decimals: int) -> str:
    """Exact decimal string for a Chainlink integer answer."""
    sign = "-" if answer < 0 else ""
    digits = str(abs(answer)).rjust(decimals + 1, "0")
    whole, frac = digits[:-decimals] if decimals else digits, digits[-decimals:] if decimals else ""
    frac = frac.rstrip("0")
    return f"{sign}{whole}.{frac}" if frac else f"{sign}{whole}"
