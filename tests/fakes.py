"""Offline stand-ins for the RPC and the chain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from stocktensor.chain import NeuronView
from stocktensor.chainlink import (
    SEL_DECIMALS,
    SEL_GET_ROUND_DATA,
    SEL_LATEST_ROUND,
    SEL_LATEST_ROUND_DATA,
    SEL_PHASE_AGGREGATORS,
    SEL_PHASE_ID,
    ChainlinkError,
)


def _w(value: int) -> str:
    return f"{value % (1 << 256):064x}"


@dataclass
class FakeFeed:
    """A proxy with one or more phases; each phase is a list of (updated_at, answer)."""

    phases: dict[int, list[tuple[int, int]]]
    decimals: int = 8

    def aggregator(self, phase: int) -> str:
        return "0x" + f"{0xA000 + phase:040x}"


@dataclass
class FakeRpc:
    feeds: dict[str, FakeFeed] = field(default_factory=dict)
    calls: int = 0
    clock: Callable[[], float] | None = None  # rounds after "now" are not visible yet

    def add(self, address: str, feed: FakeFeed) -> None:
        self.feeds[address.lower()] = feed

    def _visible(self, feed: FakeFeed, phase: int) -> list[tuple[int, int]]:
        rounds = feed.phases.get(phase) or []
        if self.clock is None:
            return rounds
        now = self.clock()
        return [r for r in rounds if r[0] <= now]

    def _round(self, feed: FakeFeed, phase: int, agg: int) -> str | None:
        rounds = self._visible(feed, phase)
        if not rounds or not 1 <= agg <= len(rounds):
            return None
        updated, answer = rounds[agg - 1]
        rid = (phase << 64) | agg
        return "0x" + _w(rid) + _w(answer) + _w(updated) + _w(updated) + _w(rid)

    async def eth_call(self, to: str, data: str) -> str:
        self.calls += 1
        to = to.lower()
        selector, arg = data[:10], data[10:]
        for address, feed in self.feeds.items():
            for phase in feed.phases:
                if to == feed.aggregator(phase).lower() and selector == SEL_LATEST_ROUND:
                    return "0x" + _w(len(self._visible(feed, phase)))
            if to != address:
                continue
            last_phase = max(feed.phases)
            if selector == SEL_DECIMALS:
                return "0x" + _w(feed.decimals)
            if selector == SEL_PHASE_ID:
                return "0x" + _w(last_phase)
            if selector == SEL_LATEST_ROUND_DATA:
                result = self._round(feed, last_phase, len(self._visible(feed, last_phase)))
                assert result
                return result
            if selector == SEL_GET_ROUND_DATA:
                rid = int(arg, 16)
                result = self._round(feed, rid >> 64, rid & ((1 << 64) - 1))
                if result is None:
                    raise ChainlinkError("execution reverted: No data present")
                return result
            if selector == SEL_PHASE_AGGREGATORS:
                phase = int(arg, 16)
                if phase in feed.phases:
                    return "0x" + _w(int(feed.aggregator(phase), 16))
                return "0x" + _w(0)
        raise ChainlinkError(f"unknown call {to} {selector}")


@dataclass
class FakeChain:
    neuron_list: list[NeuronView] = field(default_factory=list)
    weights: list[tuple[list[int], list[float]]] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    served: list[tuple[str, int]] = field(default_factory=list)

    async def neurons(self, netuid: int) -> list[NeuronView]:
        return list(self.neuron_list)

    async def set_weights(self, netuid: int, uids: list[int], weights: list[float]) -> bool:
        self.weights.append((uids, weights))
        return True

    async def serve_axon(self, netuid: int, ip: str, port: int) -> bool:
        self.served.append((ip, port))
        return True

    async def set_commitment(self, netuid: int, text: str) -> bool:
        self.commitments.append(text)
        return True
