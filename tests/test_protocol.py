from __future__ import annotations

import pytest

from stocktensor.canonical import canonical
from stocktensor.protocol import (
    Forecast,
    ForecastRequest,
    ForecastResponse,
    InvalidForecast,
    format_price,
    parse_commitment,
    task_id,
)


def test_valid_forecast_returns_floats() -> None:
    assert Forecast("99", "100", "101", "0.6").validate(100.0) == (99.0, 100.0, 101.0, 0.6)


@pytest.mark.parametrize(
    "forecast",
    [
        Forecast("101", "100", "102", "0.5"),  # low > point
        Forecast("99", "103", "102", "0.5"),  # point > high
        Forecast("0", "1", "2", "0.5"),  # non-positive
        Forecast("99", "100", "101", "1.2"),  # p_up out of range
        Forecast("99", "100", "101", "-0.1"),
        Forecast("9", "100", "101", "0.5"),  # below reference / 10
        Forecast("99", "100", "1001", "0.5"),  # above reference * 10
        Forecast("abc", "100", "101", "0.5"),
        Forecast("nan", "100", "101", "0.5"),
        Forecast("99", "100", "inf", "0.5"),
    ],
)
def test_invalid_forecasts(forecast: Forecast) -> None:
    with pytest.raises(InvalidForecast):
        forecast.validate(100.0)


def test_request_round_trip() -> None:
    request = ForecastRequest("t", "NVDA", "1d", 1, "236.2", 13)
    assert ForecastRequest.from_json(request.to_json()) == request
    with pytest.raises(ValueError):
        ForecastRequest.from_json({**request.to_json(), "horizon": "2h"})
    with pytest.raises(ValueError):
        ForecastRequest.from_json({**request.to_json(), "version": 2})


def test_response_parsing_errors() -> None:
    with pytest.raises(InvalidForecast):
        ForecastResponse.from_json({"task_id": "t"})
    with pytest.raises(InvalidForecast):
        ForecastResponse.from_json(
            {"task_id": "t", "miner": "m", "forecast": {"low": "1"}, "signature": "0x"}
        )


def test_task_id_is_stable_and_distinct() -> None:
    a = task_id("5V", "NVDA", "1h", 100)
    assert a == task_id("5V", "NVDA", "1h", 100)
    assert len(a) == 32
    assert a != task_id("5V", "NVDA", "1d", 100)


def test_commitment_parsing() -> None:
    digest = "ab" * 32
    assert parse_commitment(f"stx1:7:{digest}") == (7, digest)
    for bad in (None, "", "stx2:1:" + digest, "stx1:x:" + digest, "stx1:1:abc", "stx1:1:" + "zz" * 32):
        assert parse_commitment(bad) is None


def test_format_price() -> None:
    assert format_price(236.5) == "236.5"
    assert format_price(100.0) == "100"
    assert format_price(0.1234567) == "0.123457"


def test_canonical_rejects_floats_and_non_ascii() -> None:
    with pytest.raises(TypeError):
        canonical({"a": 1.5})
    with pytest.raises(ValueError):
        canonical({"a": "é"})
    assert canonical({"b": [True, None], "a": 1}) == b'{"a":1,"b":[true,null]}'
