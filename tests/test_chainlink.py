from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fakes import FakeFeed, FakeRpc

from stocktensor.chainlink import ChainlinkError, Feed, HttpRpc, decode_round
from stocktensor.history import PriceHistory

ADDR = "0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15"


def feed_with(rpc: FakeRpc, phases: dict[int, list[tuple[int, int]]]) -> Feed:
    rpc.add(ADDR, FakeFeed(phases))
    return Feed(rpc, ADDR)


async def test_latest_and_decimals(fake_rpc: FakeRpc) -> None:
    feed = feed_with(fake_rpc, {1: [(100, 1_00000000), (200, 2_00000000)]})
    latest = await feed.latest()
    assert (latest.phase, latest.agg_round, latest.answer, latest.updated_at) == (1, 2, 2_00000000, 200)
    assert await feed.decimals() == 8
    assert await feed.phase_id() == 1


@pytest.mark.parametrize(
    ("ts", "expected"),
    [(50, None), (100, 100), (150, 100), (299, 200), (300, 300), (10_000, 900)],
)
async def test_price_at_binary_search(fake_rpc: FakeRpc, ts: int, expected: int | None) -> None:
    feed = feed_with(fake_rpc, {1: [(t, t * 10) for t in range(100, 1000, 100)]})
    found = await feed.price_at(ts)
    assert (found.updated_at if found else None) == expected


async def test_price_at_uses_few_calls(fake_rpc: FakeRpc) -> None:
    feed = feed_with(fake_rpc, {1: [(t, t) for t in range(1, 5001)]})
    found = await feed.price_at(1234)
    assert found is not None and found.updated_at == 1234
    assert fake_rpc.calls < 20


async def test_price_at_falls_back_to_previous_phase(fake_rpc: FakeRpc) -> None:
    feed = feed_with(fake_rpc, {1: [(100, 1), (200, 2), (300, 3)], 2: [(400, 4), (500, 5)]})
    found = await feed.price_at(350)
    assert found is not None
    assert (found.phase, found.agg_round, found.answer) == (1, 3, 3)
    assert await feed.price_at(450) is not None
    assert await feed.price_at(50) is None


async def test_missing_round_returns_none(fake_rpc: FakeRpc) -> None:
    feed = feed_with(fake_rpc, {1: [(100, 1)]})
    assert await feed.round((1 << 64) | 9) is None


def test_decode_negative_answer() -> None:
    words = [1, (1 << 256) - 5, 10, 11, 1]
    data = decode_round("0x" + "".join(f"{w:064x}" for w in words))
    assert data.answer == -5


def test_decode_rejects_short_data() -> None:
    with pytest.raises(ChainlinkError):
        decode_round("0x1234")


async def test_history_walks_back_and_caches(fake_rpc: FakeRpc) -> None:
    feed = feed_with(fake_rpc, {1: [(t, t) for t in range(100, 2100, 100)]})
    history = PriceHistory(feed)
    series = await history.recent(since=1500)
    assert series[0][0] == 1400 and series[-1][0] == 2000
    calls = fake_rpc.calls
    again = await history.recent(since=1500)
    assert again == series
    assert fake_rpc.calls - calls == 1  # only latestRoundData again


async def test_http_rpc_falls_back_between_endpoints() -> None:
    async def broken(_: web.Request) -> web.Response:
        return web.Response(status=500, text="nope")

    async def good(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["method"] == "eth_call"
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": "0x" + "0" * 63 + "8"})

    async def reverted(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "error": {"message": "reverted"}})

    apps = []
    for handler in (broken, good, reverted):
        app = web.Application()
        app.router.add_post("/", handler)
        apps.append(TestServer(app))
    for server in apps:
        await server.start_server()
    try:
        rpc = HttpRpc([str(apps[0].make_url("/")), str(apps[1].make_url("/"))])
        assert await Feed(rpc, ADDR).decimals() == 8
        await rpc.close()
        failing = HttpRpc([str(apps[2].make_url("/"))])
        with pytest.raises(ChainlinkError):
            await Feed(failing, ADDR).decimals()
        await failing.close()
    finally:
        for server in apps:
            await server.close()
