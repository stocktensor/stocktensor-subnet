"""End to end: real miner apps over HTTP, fake chain and fake Chainlink."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from fakes import FakeChain, FakeFeed, FakeRpc

from neurons.miner import ValidatorSet, create_app
from neurons.validator import Settings, Validator
from stocktensor.assets import Asset
from stocktensor.bundle import verify_bundle
from stocktensor.chain import NeuronView, commitment_info
from stocktensor.chainlink import Feed
from stocktensor.models import volatility_bands
from stocktensor.protocol import Forecast, parse_commitment
from stocktensor.store import Store

FEED = "0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15"
WEDNESDAY = int(datetime(2026, 1, 14, 15, 0, tzinfo=UTC).timestamp())  # 10:00 ET
SATURDAY = int(datetime(2026, 1, 17, 15, 0, tzinfo=UTC).timestamp())


class Clock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def rounds(start: int, end: int, step: int, price: float, drift: float) -> list[tuple[int, int]]:
    out, t, p = [], start, price
    while t <= end:
        out.append((t, int(p * 1e8)))
        t += step
        p *= 1 + drift
    return out


async def start_miner(keypair, validator_hotkey, model, clock):
    async def history(symbol, as_of):
        return []

    app = create_app(
        keypair=keypair, model=model, history=history, allowed=ValidatorSet([validator_hotkey]), clock=clock
    )
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def setup(tmp_path, alice, bob, charlie):
    clock = Clock(WEDNESDAY)
    rpc = FakeRpc(clock=clock)
    rpc.add(FEED, FakeFeed({1: rounds(WEDNESDAY - 86_400, WEDNESDAY + 8 * 86_400, 600, 236.0, 0.0002)}))

    def bullish(request, history):
        ref = float(request.reference_price)
        return Forecast(f"{ref * 0.99:.4f}", f"{ref * 1.004:.4f}", f"{ref * 1.02:.4f}", "0.8")

    miners = [
        await start_miner(alice, bob.ss58_address, bullish, clock),
        await start_miner(charlie, bob.ss58_address, volatility_bands, clock),
    ]
    chain = FakeChain(
        [
            NeuronView(0, bob.ss58_address, None, True),
            NeuronView(1, alice.ss58_address, f"127.0.0.1:{miners[0].port}", False),
            NeuronView(2, charlie.ss58_address, f"127.0.0.1:{miners[1].port}", False),
            NeuronView(3, "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy", "127.0.0.1:1", False),  # dead
        ]
    )
    session = aiohttp.ClientSession()
    validator = Validator(
        settings=Settings(
            netuid=7, assets_per_round=1, query_timeout=3, bundle_dir=tmp_path / "bundles", seed=1
        ),
        keypair=bob,
        chain=chain,
        feeds={"NVDA": Feed(rpc, FEED)},
        assets={"NVDA": Asset("NVDA", FEED, 8, "Robinhood NVDA / USD")},
        store=Store(tmp_path / "v.sqlite"),
        session=session,
        clock=clock,
    )
    yield validator, chain, clock, tmp_path
    await session.close()
    for miner in miners:
        await miner.close()


async def test_full_epoch(setup, alice, charlie) -> None:
    validator, chain, clock, tmp_path = setup
    assert await validator.run_round() == 3  # 1 asset x 3 horizons
    assert await validator.resolve() == 0  # nothing due yet
    clock.now += 7 * 86_400 + 120
    assert await validator.resolve() == 3
    bundle = await validator.run_epoch()
    assert bundle is not None
    verified = verify_bundle(bundle, prev="0" * 64)
    # files, anchor, weights
    on_disk = json.loads((tmp_path / "bundles" / "1.json").read_text())
    assert on_disk == bundle == json.loads((tmp_path / "bundles" / "latest.json").read_text())
    assert parse_commitment(chain.commitments[-1]) == (1, verified.hash)
    uids, weights = chain.weights[-1]
    assert sorted(uids) == [1, 2, 3]
    assert sum(weights) == pytest.approx(1.0, abs=1e-5)
    errors = {r["miner"]: r["error"] for t in bundle["tasks"] for r in t["responses"]}
    assert errors["5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"] == "timeout"
    rolling = {m: float(v) for m, v in bundle["rolling"].items()}
    assert rolling["5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"] == 0.0
    assert rolling[alice.ss58_address] > 0 and rolling[charlie.ss58_address] > 0
    assert rolling[alice.ss58_address] + rolling[charlie.ss58_address] == pytest.approx(1.0, abs=1e-5)
    # second epoch chains to the first
    clock.now += 600
    await validator.run_round()
    clock.now += 7 * 86_400 + 120
    await validator.resolve()
    second = await validator.run_epoch()
    assert second["prev"] == verified.hash
    verify_bundle(second, prev=verified.hash, history=verified.results)


async def test_no_tasks_when_market_closed(setup) -> None:
    validator, _, clock, _ = setup
    clock.now = SATURDAY
    assert await validator.run_round() == 0


async def test_no_update_voids_task(setup, tmp_path) -> None:
    validator, chain, clock, _ = setup
    rpc = FakeRpc(clock=clock)
    rpc.add(FEED, FakeFeed({1: [(WEDNESDAY - 100, int(236e8))]}))  # feed never updates again
    validator.feeds = {"NVDA": Feed(rpc, FEED)}
    assert await validator.run_round() == 3
    clock.now += 7 * 86_400 + 120
    await validator.resolve()
    bundle = await validator.run_epoch()
    assert {t["void"] for t in bundle["tasks"]} == {"no_update"}
    assert bundle["weights"] == {}
    assert chain.weights == []
    verify_bundle(bundle)


async def test_stale_reference_is_skipped(setup) -> None:
    validator, _, clock, _ = setup
    clock.now += 30 * 86_400  # far past the last fake round
    assert await validator.run_round() == 0


def test_commitment_info_shape() -> None:
    info = commitment_info("stx1:3:" + "ab" * 32)
    (field,) = info["fields"]
    ((variant, value),) = field.items()
    assert variant == f"Raw{len(value)}" and value.startswith(b"stx1:3:")
    with pytest.raises(ValueError):
        commitment_info("x" * 129)
