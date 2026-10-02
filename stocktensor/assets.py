"""Stock tokens scored by the subnet and their Chainlink feeds on Robinhood Chain.

``assets.json`` is generated from Chainlink's public feed registry (equity
feeds with ``marketHours = us_equities_24/5``) and every address was checked
on-chain (``description()``, ``decimals()``, a recent ``latestRoundData()``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources

CHAIN_ID = 4663


@dataclass(frozen=True)
class Asset:
    symbol: str
    feed: str  # Chainlink proxy address
    decimals: int
    name: str


def load_assets() -> dict[str, Asset]:
    data = json.loads(resources.files(__package__).joinpath("assets.json").read_text())
    if data.get("chain_id") != CHAIN_ID:
        raise ValueError("assets.json is for another chain")
    return {
        item["symbol"]: Asset(item["symbol"], item["feed"], int(item["decimals"]), item["name"])
        for item in data["assets"]
    }
