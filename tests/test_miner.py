from __future__ import annotations

import json

import bittensor as bt
import pytest
from aiohttp.test_utils import TestClient, TestServer

from neurons.miner import ValidatorSet, create_app, load_model
from stocktensor.bundle import verify_forecast
from stocktensor.models import volatility_bands
from stocktensor.protocol import Forecast, ForecastRequest, ForecastResponse

NOW = 1_790_000_000


def request_body(deadline: int = NOW + 12) -> bytes:
    return json.dumps(ForecastRequest("task-1", "NVDA", "1h", NOW, "236.2", deadline).to_json()).encode()


@pytest.fixture
async def client(alice, bob):
    async def history(symbol: str, as_of: int):
        return [(float(NOW - 3600 * i), 236.0 + i * 0.1) for i in range(24, 0, -1)]

    app = create_app(
        keypair=alice,
        model=volatility_bands,
        history=history,
        allowed=ValidatorSet([bob.ss58_address]),
        clock=lambda: NOW,
    )
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def post(client, signer, body: bytes, receiver: str):
    headers = bt.http_auth.sign(signer, method="POST", path="/forecast", body=body, receiver_ss58=receiver)
    return await client.post("/forecast", data=body, headers=headers)


async def test_signed_request_gets_signed_forecast(client, alice, bob) -> None:
    response = await post(client, bob, request_body(), alice.ss58_address)
    assert response.status == 200
    answer = ForecastResponse.from_json(await response.json())
    assert answer.miner == alice.ss58_address and answer.task_id == "task-1"
    assert verify_forecast("task-1", alice.ss58_address, answer.forecast, answer.signature)
    low, point, high, p_up = answer.forecast.validate(236.2)
    assert low < point < high and p_up == 0.5


async def test_caller_without_permit_is_refused(client, alice, charlie) -> None:
    response = await post(client, charlie, request_body(), alice.ss58_address)
    assert response.status == 403


async def test_wrong_receiver_is_refused(client, bob, charlie) -> None:
    response = await post(client, bob, request_body(), charlie.ss58_address)
    assert response.status == 401


async def test_unsigned_and_tampered_requests(client, alice, bob) -> None:
    assert (await client.post("/forecast", data=request_body())).status == 401
    headers = bt.http_auth.sign(
        bob, method="POST", path="/forecast", body=request_body(), receiver_ss58=alice.ss58_address
    )
    tampered = request_body().replace(b"236.2", b"999.9")
    assert (await client.post("/forecast", data=tampered, headers=headers)).status == 401


async def test_deadline_and_bad_body(client, alice, bob) -> None:
    assert (await post(client, bob, request_body(deadline=NOW - 1), alice.ss58_address)).status == 408
    assert (await post(client, bob, b'{"version": 1}', alice.ss58_address)).status == 400


async def test_model_errors_are_hidden(alice, bob) -> None:
    def broken(request, history):
        raise RuntimeError("secret internals")

    async def history(symbol, as_of):
        return []

    app = create_app(
        keypair=alice,
        model=broken,
        history=history,
        allowed=ValidatorSet([bob.ss58_address]),
        clock=lambda: NOW,
    )
    async with TestClient(TestServer(app)) as test_client:
        response = await post(test_client, bob, request_body(), alice.ss58_address)
        assert response.status == 500
        assert "secret" not in await response.text()


def test_load_model() -> None:
    assert load_model("stocktensor.models:volatility_bands") is volatility_bands
    with pytest.raises(ValueError):
        load_model("stocktensor.models")


def test_reference_model_bands_scale_with_horizon() -> None:
    history = [(float(t), 100.0 * (1.01 if t % 2 else 0.99)) for t in range(0, 86_400 * 3, 3_600)]
    widths = {}
    for horizon in ("1h", "1d", "1w"):
        f = volatility_bands(ForecastRequest("t", "X", horizon, 0, "100", 1), history)
        low, point, high, _ = f.validate(100.0)
        widths[horizon] = high - low
    assert widths["1h"] < widths["1d"] < widths["1w"]
    fallback = volatility_bands(ForecastRequest("t", "X", "1d", 0, "100", 1), [])
    assert isinstance(fallback, Forecast)
