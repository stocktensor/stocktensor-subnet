"""Stocktensor miner: answers signed forecast requests from validators.

    stx-miner --netuid <n> --wallet.name <coldkey> --wallet.hotkey <hotkey> --port 8091

The model is pluggable (``--model module:callable``, see ``stocktensor.models``).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import bittensor as bt
from aiohttp import web

from stocktensor.assets import Asset, load_assets
from stocktensor.bundle import sign_forecast
from stocktensor.chain import BittensorChain, Chain
from stocktensor.chainlink import DEFAULT_RPC_URLS, Feed, HttpRpc
from stocktensor.history import PriceHistory
from stocktensor.models import volatility_bands
from stocktensor.protocol import (
    FORECAST_PATH,
    Forecast,
    ForecastRequest,
    ForecastResponse,
)

log = logging.getLogger("stocktensor.miner")

History = Sequence[tuple[float, float]]
Model = Callable[[ForecastRequest, History], Forecast | Awaitable[Forecast]]
HistoryProvider = Callable[[str, int], Awaitable[History]]
PermitCheck = Callable[[str], bool]

HISTORY_WINDOW = 7 * 86_400


def load_model(spec: str) -> Model:
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError("--model must look like 'package.module:callable'")
    return getattr(importlib.import_module(module_name), attr)


async def run_model(model: Model, request: ForecastRequest, history: History) -> Forecast:
    result = model(request, history)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Forecast):
        raise TypeError("model must return a stocktensor.protocol.Forecast")
    return result


class ValidatorSet:
    """Hotkeys allowed to query this miner (validator permit on the subnet)."""

    def __init__(self, hotkeys: Sequence[str] = ()):
        self.hotkeys = set(hotkeys)
        self.extra: set[str] = set()

    def __call__(self, hotkey: str) -> bool:
        return hotkey in self.hotkeys or hotkey in self.extra

    async def refresh(self, chain: Chain, netuid: int) -> None:
        neurons = await chain.neurons(netuid)
        self.hotkeys = {n.hotkey for n in neurons if n.validator_permit}


def create_app(
    *,
    keypair: Any,
    model: Model,
    history: HistoryProvider,
    allowed: PermitCheck,
    model_timeout: float = 8.0,
    clock: Callable[[], float] = time.time,
) -> web.Application:
    hotkey = keypair.ss58_address

    async def forecast(request: web.Request) -> web.Response:
        body = await request.read()
        try:
            caller = bt.http_auth.verify(
                request.headers, body, method="POST", path=request.path_qs, self_hotkey_ss58=hotkey
            )
        except bt.http_auth.AuthError as exc:
            return web.json_response({"error": exc.__class__.__name__}, status=401)
        if not allowed(caller.hotkey_ss58):
            return web.json_response({"error": "caller has no validator permit"}, status=403)
        try:
            task = ForecastRequest.from_json(json.loads(body))
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)
        if clock() > task.deadline:
            return web.json_response({"error": "deadline passed"}, status=408)
        try:
            series = await history(task.asset, task.as_of)
            prediction = await asyncio.wait_for(run_model(model, task, series), model_timeout)
        except Exception:  # noqa: BLE001 - never leak model internals to the caller
            log.exception("model failed for %s %s", task.asset, task.horizon)
            return web.json_response({"error": "model failed"}, status=500)
        response = ForecastResponse(
            task_id=task.task_id,
            miner=hotkey,
            forecast=prediction,
            signature=sign_forecast(keypair, task.task_id, prediction),
        )
        return web.json_response(response.to_json())

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "hotkey": hotkey})

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post(FORECAST_PATH, forecast)
    app.router.add_get("/health", health)
    return app


def chainlink_history(rpc_urls: Sequence[str], assets: dict[str, Asset]) -> HistoryProvider:
    rpc = HttpRpc(rpc_urls)
    caches = {symbol: PriceHistory(Feed(rpc, asset.feed)) for symbol, asset in assets.items()}

    async def provider(symbol: str, as_of: int) -> History:
        cache = caches.get(symbol)
        if cache is None:
            return []
        asset = assets[symbol]
        scale = 10**asset.decimals
        series = await cache.recent(as_of - HISTORY_WINDOW)
        return [(float(t), answer / scale) for t, answer in series if t <= as_of]

    return provider


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stocktensor miner")
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--network", default="finney", help="finney | test | local | ws://…")
    parser.add_argument("--wallet.name", dest="wallet_name", default="default")
    parser.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    parser.add_argument("--wallet.path", dest="wallet_path", default="~/.bittensor/wallets")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--external-ip", help="public IP to publish with serve_axon")
    parser.add_argument("--no-serve-axon", action="store_true", help="do not publish ip:port on chain")
    parser.add_argument("--model", default="stocktensor.models:volatility_bands")
    parser.add_argument("--rpc", action="append", help="Robinhood Chain RPC url (repeatable)")
    parser.add_argument("--allow-hotkey", action="append", default=[], help="extra caller hotkey (testing)")
    parser.add_argument("--metagraph-refresh", type=int, default=600, help="seconds")
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    import os

    wallet = bt.Wallet(
        name=args.wallet_name, hotkey=args.wallet_hotkey, path=os.path.expanduser(args.wallet_path)
    )
    keypair = bt.resolve_signer(wallet, role="hotkey")
    chain = await BittensorChain.connect(args.network, wallet)
    validators = ValidatorSet()
    validators.extra.update(args.allow_hotkey)
    await validators.refresh(chain, args.netuid)

    if not args.no_serve_axon:
        if not args.external_ip:
            raise SystemExit("--external-ip is required to serve the axon (or pass --no-serve-axon)")
        ok = await chain.serve_axon(args.netuid, args.external_ip, args.port)
        log.info("serve_axon %s:%s -> %s", args.external_ip, args.port, "ok" if ok else "failed")

    model = (
        load_model(args.model) if args.model != "stocktensor.models:volatility_bands" else volatility_bands
    )
    app = create_app(
        keypair=keypair,
        model=model,
        history=chainlink_history(args.rpc or DEFAULT_RPC_URLS, load_assets()),
        allowed=validators,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    log.info("miner %s listening on %s:%s", keypair.ss58_address, args.host, args.port)
    try:
        while True:
            await asyncio.sleep(args.metagraph_refresh)
            try:
                await validators.refresh(chain, args.netuid)
            except Exception:  # noqa: BLE001
                log.exception("metagraph refresh failed")
    finally:
        await runner.cleanup()
        await chain.close()


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(parse_args(argv)))


if __name__ == "__main__":
    main()
