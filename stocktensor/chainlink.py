"""Minimal async reader for Chainlink price feed proxies on Robinhood Chain.

Plain JSON-RPC ``eth_call`` with hand-encoded ABI, so the subnet has no web3
dependency. Proxy round ids are ``phase << 64 | aggregator_round``; within a
phase, ``updatedAt`` grows with the round, which lets :meth:`price_at` find
the price at any timestamp with a binary search.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

DEFAULT_RPC_URLS = ("https://robinhood-rpc.publicnode.com",)

SEL_DECIMALS = "0x313ce567"
SEL_LATEST_ROUND_DATA = "0xfeaf968c"
SEL_GET_ROUND_DATA = "0x9a6fc8f5"
SEL_PHASE_ID = "0x58303b10"
SEL_PHASE_AGGREGATORS = "0xc1597304"
SEL_LATEST_ROUND = "0x668a0f02"

PHASE_SHIFT = 64
AGG_MASK = (1 << PHASE_SHIFT) - 1


class ChainlinkError(RuntimeError):
    """An RPC call failed on every endpoint, or a feed returned no data."""


@dataclass(frozen=True)
class Round:
    round_id: int
    answer: int
    started_at: int
    updated_at: int

    @property
    def phase(self) -> int:
        return self.round_id >> PHASE_SHIFT

    @property
    def agg_round(self) -> int:
        return self.round_id & AGG_MASK


class Rpc(Protocol):
    async def eth_call(self, to: str, data: str) -> str: ...


def _word(value: int) -> str:
    return f"{value:064x}"


def _words(result: str) -> list[int]:
    raw = result.removeprefix("0x")
    if not raw or len(raw) % 64:
        raise ChainlinkError(f"unexpected eth_call result length {len(raw)}")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _signed(value: int) -> int:
    return value - (1 << 256) if value >= 1 << 255 else value


def decode_round(result: str) -> Round:
    words = _words(result)
    if len(words) < 5:
        raise ChainlinkError("short round data")
    return Round(words[0], _signed(words[1]), words[2], words[3])


class HttpRpc:
    """JSON-RPC over HTTP with endpoint fallback."""

    def __init__(
        self,
        urls: Sequence[str] = DEFAULT_RPC_URLS,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 10.0,
    ):
        if not urls:
            raise ValueError("at least one RPC url is required")
        self.urls = list(urls)
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._ids = itertools.count(1)

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    async def eth_call(self, to: str, data: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }
        client = await self._client()
        errors: list[str] = []
        for url in self.urls:
            try:
                async with client.post(url, json=payload) as response:
                    body: Any = await response.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                errors.append(f"{url}: {exc.__class__.__name__}")
                continue
            if isinstance(body, dict) and "result" in body:
                return str(body["result"])
            error = body.get("error") if isinstance(body, dict) else body
            errors.append(f"{url}: {error}")
        raise ChainlinkError("eth_call failed: " + "; ".join(errors))


class Feed:
    """One Chainlink proxy."""

    def __init__(self, rpc: Rpc, address: str):
        self.rpc = rpc
        self.address = address

    async def decimals(self) -> int:
        return _words(await self.rpc.eth_call(self.address, SEL_DECIMALS))[0]

    async def latest(self) -> Round:
        return decode_round(await self.rpc.eth_call(self.address, SEL_LATEST_ROUND_DATA))

    async def round(self, round_id: int) -> Round | None:
        """Round data, or None when the round does not exist (call reverted or empty)."""
        try:
            result = await self.rpc.eth_call(self.address, SEL_GET_ROUND_DATA + _word(round_id))
            data = decode_round(result)
        except ChainlinkError:
            return None
        if data.updated_at == 0:
            return None
        return data

    async def phase_id(self) -> int:
        return _words(await self.rpc.eth_call(self.address, SEL_PHASE_ID))[0]

    async def phase_aggregator(self, phase: int) -> str:
        word = _words(await self.rpc.eth_call(self.address, SEL_PHASE_AGGREGATORS + _word(phase)))[0]
        return "0x" + f"{word:040x}"

    async def phase_latest_agg_round(self, phase: int) -> int:
        aggregator = await self.phase_aggregator(phase)
        if int(aggregator, 16) == 0:
            return 0
        return _words(await self.rpc.eth_call(aggregator, SEL_LATEST_ROUND))[0]

    async def _search_phase(self, phase: int, last_agg: int, ts: int) -> Round | None:
        """Last round of ``phase`` (agg rounds 1..last_agg) with updated_at <= ts."""
        lo, hi = 1, last_agg
        found: Round | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            data = await self.round((phase << PHASE_SHIFT) | mid)
            if data is None:
                # Missing rounds are rare (pruned or reverted); treat as too new.
                hi = mid - 1
                continue
            if data.updated_at <= ts:
                found = data
                lo = mid + 1
            else:
                hi = mid - 1
        return found

    async def price_at(self, ts: int, *, latest: Round | None = None) -> Round | None:
        """The last round with ``updated_at <= ts``, searching back through phases."""
        latest = latest or await self.latest()
        if latest.updated_at <= ts:
            return latest
        phase, last_agg = latest.phase, latest.agg_round
        while phase >= 1:
            found = await self._search_phase(phase, last_agg, ts)
            if found is not None:
                return found
            phase -= 1
            if phase < 1:
                break
            last_agg = await self.phase_latest_agg_round(phase)
            if last_agg == 0:
                break
        return None
