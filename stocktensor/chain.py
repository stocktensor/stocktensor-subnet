"""The few Bittensor chain operations the neurons need, behind a small interface.

:class:`BittensorChain` talks to subtensor through the ``bittensor`` v11 SDK;
tests swap in a fake with the same methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import bittensor as bt


@dataclass(frozen=True)
class NeuronView:
    uid: int
    hotkey: str
    axon: str | None  # "ip:port" or None
    validator_permit: bool
    commitment: str | None = None


class Chain(Protocol):
    async def neurons(self, netuid: int) -> list[NeuronView]: ...

    async def set_weights(self, netuid: int, uids: list[int], weights: list[float]) -> bool: ...

    async def serve_axon(self, netuid: int, ip: str, port: int) -> bool: ...

    async def set_commitment(self, netuid: int, text: str) -> bool: ...


def commitment_info(text: str) -> dict[str, Any]:
    """``CommitmentInfo`` for a short plaintext commitment: one ``Raw<n>`` field.

    The Commitments pallet stores ``fields: BoundedVec<Data>`` where ``Data`` has
    ``Raw0`` … ``Raw128`` variants (the SDK's own decoder reads them back as
    ``{"Raw<n>": "0x…"}``).
    """
    data = text.encode()
    if len(data) > 128:
        raise ValueError("commitment text longer than 128 bytes")
    return {"fields": [{f"Raw{len(data)}": data}]}


class BittensorChain:
    """Chain adapter over ``bittensor.Client`` (async)."""

    def __init__(self, client: bt.Client, wallet: Any):
        self.client = client
        self.wallet = wallet

    @classmethod
    async def connect(cls, network: str, wallet: Any) -> BittensorChain:
        client = bt.Client(network)
        await client.connect()
        return cls(client, wallet)

    async def close(self) -> None:
        await self.client.close()

    async def neurons(self, netuid: int) -> list[NeuronView]:
        graph = await self.client.subnets.metagraph(netuid)
        if graph is None:
            raise RuntimeError(f"subnet {netuid} does not exist on this network")
        return [
            NeuronView(
                uid=n.uid,
                hotkey=n.hotkey,
                axon=n.axon,
                validator_permit=bool(n.validator_permit),
                commitment=n.commitment.value if n.commitment is not None else None,
            )
            for n in graph.neurons
        ]

    async def set_weights(self, netuid: int, uids: list[int], weights: list[float]) -> bool:
        result = await self.client.execute(
            bt.SetWeights(netuid=netuid, uids=uids, weights=weights), self.wallet
        )
        return bool(result.success)

    async def serve_axon(self, netuid: int, ip: str, port: int) -> bool:
        result = await self.client.execute(bt.ServeAxon(netuid=netuid, ip=ip, port=port), self.wallet)
        return bool(result.success)

    async def set_commitment(self, netuid: int, text: str) -> bool:
        call = bt.calls.Commitments.set_commitment(netuid=netuid, info=commitment_info(text))
        result = await self.client.submit_call(call, self.wallet, signer="hotkey")
        return bool(result.success)
