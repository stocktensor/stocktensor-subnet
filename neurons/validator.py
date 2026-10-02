"""Stocktensor validator.

    stx-validator --netuid <n> --wallet.name <coldkey> --wallet.hotkey <hotkey>

Every round it asks all served miners for forecasts on a sample of stock
tokens, resolves due tasks against Chainlink, and every epoch it sets weights
and publishes a signed, chained, on-chain-anchored bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import bittensor as bt

from stocktensor.assets import Asset, load_assets
from stocktensor.bundle import TaskRecord, build_bundle, score_record, verify_forecast
from stocktensor.chain import BittensorChain, Chain, NeuronView
from stocktensor.chainlink import DEFAULT_RPC_URLS, Feed, HttpRpc, Round
from stocktensor.protocol import (
    FORECAST_PATH,
    HORIZONS,
    ForecastRequest,
    ForecastResponse,
    InvalidForecast,
    bundle_hash,
    chainlink_to_str,
    commitment_text,
    task_id,
)
from stocktensor.sessions import session_at
from stocktensor.store import Store

log = logging.getLogger("stocktensor.validator")


@dataclass
class Settings:
    netuid: int
    round_interval: int = 600  # seconds between query rounds
    epoch_interval: int = 3_600  # seconds between bundles / weight updates
    assets_per_round: int = 4
    horizons: tuple[str, ...] = tuple(HORIZONS)
    query_timeout: float = 12.0  # seconds a miner has to answer
    resolve_grace: int = 60  # seconds after the horizon before resolving
    max_reference_age: int = 6 * 3_600  # skip assets whose feed is older than this
    bundle_dir: Path = Path("bundles")
    publish_url: str | None = None
    anchor: bool = True
    set_weights: bool = True
    seed: int | None = None


def round_json(data: Round, decimals: int) -> dict[str, Any]:
    return {
        "round_id": str(data.round_id),
        "answer": chainlink_to_str(data.answer, decimals),
        "updated_at": data.updated_at,
    }


@dataclass
class Validator:
    settings: Settings
    keypair: Any  # hotkey signer
    chain: Chain
    feeds: dict[str, Feed]
    assets: dict[str, Asset]
    store: Store
    session: aiohttp.ClientSession
    clock: Callable[[], float] = time.time
    _rng: random.Random = field(init=False)
    _last_epoch_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.settings.seed)

    @property
    def hotkey(self) -> str:
        return self.keypair.ss58_address

    # --- query --------------------------------------------------------------

    async def query_miner(self, neuron: NeuronView, request: ForecastRequest) -> dict[str, Any]:
        record: dict[str, Any] = {"miner": neuron.hotkey, "forecast": None, "signature": None, "error": None}
        body = json.dumps(request.to_json()).encode()
        headers = bt.http_auth.sign(
            self.keypair, method="POST", path=FORECAST_PATH, body=body, receiver_ss58=neuron.hotkey
        )
        headers["Content-Type"] = "application/json"
        url = f"http://{neuron.axon}{FORECAST_PATH}"
        try:
            async with self.session.post(
                url,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.settings.query_timeout),
            ) as response:
                if response.status != 200:
                    record["error"] = "http"
                    return record
                data = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            record["error"] = "timeout"
            return record
        if self.clock() > request.deadline:
            record["error"] = "late"
            return record
        try:
            answer = ForecastResponse.from_json(data)
        except InvalidForecast:
            record["error"] = "invalid"
            return record
        if answer.task_id != request.task_id or answer.miner != neuron.hotkey:
            record["error"] = "invalid"
            return record
        record["forecast"] = answer.forecast.to_json()
        record["signature"] = answer.signature
        if not verify_forecast(request.task_id, neuron.hotkey, answer.forecast, answer.signature):
            record["error"] = "bad_signature"
            return record
        try:
            answer.forecast.validate(float(request.reference_price))
        except InvalidForecast:
            record["error"] = "invalid"
        return record

    async def run_round(self) -> int:
        """Create and send one round of tasks. Returns the number of tasks created."""
        now = int(self.clock())
        if session_at(now) == "closed":
            log.info("market closed (weekend); no tasks this round")
            return 0
        neurons = await self.chain.neurons(self.settings.netuid)
        miners = [n for n in neurons if n.axon and n.hotkey != self.hotkey]
        if not miners:
            log.info("no served miners")
            return 0
        symbols = self._rng.sample(sorted(self.feeds), min(self.settings.assets_per_round, len(self.feeds)))
        created = 0
        for symbol in symbols:
            try:
                latest = await self.feeds[symbol].latest()
            except Exception:  # noqa: BLE001
                log.exception("feed read failed for %s", symbol)
                continue
            if now - latest.updated_at > self.settings.max_reference_age:
                log.info("skip %s: feed not updated for %ss", symbol, now - latest.updated_at)
                continue
            reference = round_json(latest, self.assets[symbol].decimals)
            for horizon in self.settings.horizons:
                tid = task_id(self.hotkey, symbol, horizon, now)
                request = ForecastRequest(
                    task_id=tid,
                    asset=symbol,
                    horizon=horizon,
                    as_of=now,
                    reference_price=reference["answer"],
                    deadline=now + int(self.settings.query_timeout),
                )
                responses = await asyncio.gather(*(self.query_miner(n, request) for n in miners))
                self.store.add_task(
                    TaskRecord(
                        task_id=tid,
                        asset=symbol,
                        horizon=horizon,
                        as_of=now,
                        session=session_at(now),
                        feed=self.assets[symbol].feed,
                        reference=reference,
                        realised=None,
                        void=None,
                        responses=list(responses),
                    )
                )
                created += 1
        log.info("round: %d tasks for %d miners", created, len(miners))
        return created

    # --- resolve ------------------------------------------------------------

    async def resolve(self) -> int:
        now = int(self.clock())
        resolved = 0
        for record in self.store.due_tasks(now, self.settings.resolve_grace):
            feed = self.feeds.get(record.asset)
            if feed is None:
                self.store.resolve(record.task_id, None, "unknown_asset", {})
                continue
            try:
                end = await feed.price_at(record.as_of + HORIZONS[record.horizon])
            except Exception:  # noqa: BLE001 - retry next pass
                log.exception("price_at failed for %s", record.task_id)
                continue
            if end is None or end.updated_at <= int(record.reference["updated_at"]):
                self.store.resolve(record.task_id, None, "no_update", {})
            else:
                record.realised = round_json(end, self.assets[record.asset].decimals)
                self.store.resolve(record.task_id, record.realised, None, score_record(record))
            resolved += 1
        return resolved

    # --- epoch --------------------------------------------------------------

    async def run_epoch(self) -> dict[str, Any] | None:
        now = int(self.clock())
        tasks = self.store.resolved_tasks()
        if not tasks:
            log.info("epoch: nothing resolved yet")
            return None
        last_epoch, prev = self.store.chain_head()
        epoch = last_epoch + 1
        bundle = build_bundle(
            keypair=self.keypair,
            netuid=self.settings.netuid,
            epoch=epoch,
            created_at=now,
            prev=prev,
            tasks=tasks,
            history=self.store.history(now),
        )
        digest = bundle_hash(bundle)
        self.write_bundle(epoch, bundle)
        self.store.mark_bundled([t.task_id for t in tasks], epoch)
        self.store.set_chain_head(epoch, digest)
        log.info("epoch %d: %d tasks, bundle %s", epoch, len(tasks), digest)

        if self.settings.set_weights and bundle["weights"]:
            await self.push_weights(bundle["weights"])
        if self.settings.publish_url:
            await self.publish(bundle)
        if self.settings.anchor:
            ok = await self.chain.set_commitment(self.settings.netuid, commitment_text(epoch, digest))
            log.info("anchor epoch %d -> %s", epoch, "ok" if ok else "failed")
        return bundle

    def write_bundle(self, epoch: int, bundle: dict[str, Any]) -> None:
        directory = self.settings.bundle_dir
        directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(bundle, indent=1, sort_keys=True) + "\n"
        (directory / f"{epoch}.json").write_text(text)
        (directory / "latest.json").write_text(text)

    async def push_weights(self, weights: dict[str, str]) -> None:
        neurons = await self.chain.neurons(self.settings.netuid)
        uid_of = {n.hotkey: n.uid for n in neurons}
        pairs = sorted((uid_of[h], float(w)) for h, w in weights.items() if h in uid_of)
        if not pairs:
            return
        ok = await self.chain.set_weights(self.settings.netuid, [u for u, _ in pairs], [w for _, w in pairs])
        log.info("set_weights for %d uids -> %s", len(pairs), "ok" if ok else "failed")

    async def publish(self, bundle: dict[str, Any]) -> None:
        assert self.settings.publish_url
        body = json.dumps(bundle, sort_keys=True).encode()
        path = "/" + self.settings.publish_url.split("://", 1)[-1].split("/", 1)[-1]
        headers = bt.http_auth.sign(self.keypair, method="POST", path=path, body=body)
        headers["Content-Type"] = "application/json"
        try:
            async with self.session.post(self.settings.publish_url, data=body, headers=headers) as response:
                log.info("publish -> HTTP %s", response.status)
        except aiohttp.ClientError:
            log.exception("publish failed")

    # --- loop ---------------------------------------------------------------

    async def step(self) -> None:
        await self.resolve()
        await self.run_round()
        if self.clock() - self._last_epoch_at >= self.settings.epoch_interval:
            await self.resolve()
            await self.run_epoch()
            self._last_epoch_at = self.clock()

    async def run_forever(self) -> None:
        self._last_epoch_at = self.clock()
        while True:
            started = self.clock()
            try:
                await self.step()
            except Exception:  # noqa: BLE001 - keep validating
                log.exception("step failed")
            await asyncio.sleep(max(1.0, self.settings.round_interval - (self.clock() - started)))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stocktensor validator")
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--network", default="finney", help="finney | test | local | ws://…")
    parser.add_argument("--wallet.name", dest="wallet_name", default="default")
    parser.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    parser.add_argument("--wallet.path", dest="wallet_path", default="~/.bittensor/wallets")
    parser.add_argument("--rpc", action="append", help="Robinhood Chain RPC url (repeatable)")
    parser.add_argument("--db", default="~/.stocktensor/validator.sqlite")
    parser.add_argument("--bundle-dir", default="~/.stocktensor/bundles")
    parser.add_argument("--publish-url", help="POST each bundle here (btauth/1 signed)")
    parser.add_argument("--round-interval", type=int, default=600)
    parser.add_argument("--epoch-interval", type=int, default=3_600)
    parser.add_argument("--assets-per-round", type=int, default=4)
    parser.add_argument("--assets", help="comma-separated subset of symbols")
    parser.add_argument("--query-timeout", type=float, default=12.0)
    parser.add_argument("--no-anchor", action="store_true", help="do not write the on-chain commitment")
    parser.add_argument("--no-set-weights", action="store_true")
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    wallet = bt.Wallet(
        name=args.wallet_name, hotkey=args.wallet_hotkey, path=os.path.expanduser(args.wallet_path)
    )
    keypair = bt.resolve_signer(wallet, role="hotkey")
    assets = load_assets()
    if args.assets:
        wanted = {s.strip().upper() for s in args.assets.split(",")}
        assets = {s: a for s, a in assets.items() if s in wanted}
    rpc = HttpRpc(args.rpc or DEFAULT_RPC_URLS)
    chain = await BittensorChain.connect(args.network, wallet)
    settings = Settings(
        netuid=args.netuid,
        round_interval=args.round_interval,
        epoch_interval=args.epoch_interval,
        assets_per_round=args.assets_per_round,
        query_timeout=args.query_timeout,
        bundle_dir=Path(os.path.expanduser(args.bundle_dir)),
        publish_url=args.publish_url,
        anchor=not args.no_anchor,
        set_weights=not args.no_set_weights,
    )
    async with aiohttp.ClientSession() as session:
        validator = Validator(
            settings=settings,
            keypair=keypair,
            chain=chain,
            feeds={s: Feed(rpc, a.feed) for s, a in assets.items()},
            assets=assets,
            store=Store(os.path.expanduser(args.db)),
            session=session,
        )
        log.info("validator %s on netuid %d, %d assets", keypair.ss58_address, args.netuid, len(assets))
        try:
            await validator.run_forever()
        finally:
            await rpc.close()
            await chain.close()


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(parse_args(argv)))


if __name__ == "__main__":
    main()
