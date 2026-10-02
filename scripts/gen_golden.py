"""Regenerate tests/golden/*.json from the reference implementation.

Run after any change to scoring or the protocol, then bump SCORING_VERSION:

    uv run python scripts/gen_golden.py

The vectors are consumed by this repo's tests, stocktensor-miner-kit and the
TypeScript stocktensor-verify, so the three can never disagree silently.

sr25519 signatures are randomised, so ``signing.json`` changes on every run.
It is only rewritten with ``--signing`` (or when missing); copy it to the
other repos whenever you do.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from bittensor.sp_core import Keypair

from stocktensor.canonical import canonical, digest
from stocktensor.protocol import (
    Forecast,
    bundle_hash,
    bundle_message,
    chainlink_to_str,
    commitment_text,
    forecast_message,
    task_id,
)
from stocktensor.scoring import (
    SCORING_VERSION,
    TaskResult,
    consensus,
    direction_loss,
    interval_loss,
    point_loss,
    rank_scores,
    rolling_scores,
    score_task,
    weights,
)

OUT = Path(__file__).resolve().parent.parent / "tests" / "golden"
MINERS = [f"miner{i:02d}" for i in range(8)]


def forecast_or_none(rng: random.Random, reference: float) -> Forecast | None:
    roll = rng.random()
    if roll < 0.1:
        return None
    if roll < 0.15:  # invalid: low above point
        return Forecast(
            low=f"{reference * 1.02:.4f}",
            point=f"{reference:.4f}",
            high=f"{reference * 1.05:.4f}",
            p_up="0.5",
        )
    width = reference * rng.uniform(0.002, 0.06)
    center = reference * (1 + rng.uniform(-0.03, 0.03))
    p_up = rng.choice(["0.5", "0.62", "0.31", f"{rng.random():.3f}"])
    return Forecast(
        low=f"{center - width / 2:.4f}",
        point=f"{center:.4f}",
        high=f"{center + width / 2:.4f}",
        p_up=p_up,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(4663)

    losses = []
    for reference, realised, low, point, high, p_up in [
        (100.0, 101.0, 99.0, 100.5, 102.0, 0.7),  # inside
        (100.0, 95.0, 99.0, 100.0, 101.0, 0.5),  # below
        (100.0, 110.0, 99.0, 100.0, 101.0, 0.9),  # above
        (100.0, 100.0, 100.0, 100.0, 100.0, 0.5),  # flat, zero width
        (236.22, 236.21, 235.0, 236.5, 238.0, 0.55),
    ]:
        losses.append(
            {
                "reference": reference,
                "realised": realised,
                "low": low,
                "point": point,
                "high": high,
                "p_up": p_up,
                "interval": interval_loss(low, high, realised, reference),
                "direction": direction_loss(p_up, realised, reference),
                "point_loss": point_loss(point, realised, reference),
            }
        )

    ranks = [
        {"losses": {"a": 0.1, "b": 0.2, "c": 0.3}, "expected": rank_scores({"a": 0.1, "b": 0.2, "c": 0.3})},
        {
            "losses": {"a": 0.1, "b": 0.1, "c": 0.3, "d": 0.4},
            "expected": rank_scores({"a": 0.1, "b": 0.1, "c": 0.3, "d": 0.4}),
        },
        {"losses": {"solo": 5.0}, "expected": rank_scores({"solo": 5.0})},
    ]

    tasks = []
    results: list[TaskResult] = []
    now = 1_790_000_000
    for index in range(40):
        reference = round(rng.uniform(5, 900), 2)
        realised = round(reference * (1 + rng.gauss(0, 0.015)), 2)
        as_of = now - rng.randint(0, 16 * 86_400)
        responses = {m: forecast_or_none(rng, reference) for m in rng.sample(MINERS, rng.randint(1, 8))}
        scores = score_task(reference, realised, responses)
        tid = f"task{index:03d}"
        tasks.append(
            {
                "task_id": tid,
                "as_of": as_of,
                "reference": reference,
                "realised": realised,
                "responses": {m: (f.to_json() if f else None) for m, f in responses.items()},
                "expected": scores,
            }
        )
        results.append(TaskResult(tid, as_of, scores))

    rolling = rolling_scores(results, now)
    final_weights = weights(rolling)
    last = tasks[-1]
    cons = consensus({m: Forecast.from_json(f) for m, f in last["responses"].items() if f}, rolling)

    scoring = {
        "scoring_version": SCORING_VERSION,
        "losses": losses,
        "ranks": ranks,
        "tasks": tasks,
        "rolling": {"now": now, "expected": rolling},
        "weights": final_weights,
        "consensus": {
            "task_id": last["task_id"],
            "expected": None
            if cons is None
            else {
                "direction": cons.direction,
                "p_up": cons.p_up,
                "agreement": cons.agreement,
                "models": cons.models,
            },
        },
    }
    (OUT / "scoring.json").write_text(json.dumps(scoring, indent=1) + "\n")

    canon_cases = []
    for value in [
        {"b": 1, "a": "x", "c": [3, 2, {"z": None, "y": True}]},
        {
            "task_id": "abc",
            "miner": "5Grw",
            "forecast": {"low": "1.5", "point": "2", "high": "2.5", "p_up": "0.6"},
        },
        [],
        {"nested": {"k": "v", "a": [1, "2", False]}},
    ]:
        canon_cases.append({"value": value, "canonical": canonical(value).decode(), "sha256": digest(value)})

    alice = Keypair.create_from_uri("//Alice")
    bob = Keypair.create_from_uri("//Bob")
    forecast = Forecast(low="230.1", point="236.5", high="241.75", p_up="0.61")
    tid = task_id(bob.ss58_address, "NVDA", "1h", 1_790_000_000)
    fmsg = forecast_message(tid, alice.ss58_address, forecast)
    fsig = "0x" + bytes(alice.sign(fmsg)).hex()

    bundle = {
        "version": 1,
        "scoring_version": SCORING_VERSION,
        "netuid": 0,
        "validator": bob.ss58_address,
        "epoch": 1,
        "created_at": 1_790_003_700,
        "prev": "0" * 64,
        "tasks": [
            {
                "task_id": tid,
                "asset": "NVDA",
                "horizon": "1h",
                "as_of": 1_790_000_000,
                "session": "overnight",
                "feed": "0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15",
                "reference": {
                    "round_id": "18446744073709552000",
                    "answer": chainlink_to_str(23622797244, 8),
                    "updated_at": 1_789_999_990,
                },
                "realised": {
                    "round_id": "18446744073709552007",
                    "answer": "237.1",
                    "updated_at": 1_790_003_500,
                },
                "void": None,
                "responses": [
                    {
                        "miner": alice.ss58_address,
                        "forecast": forecast.to_json(),
                        "signature": fsig,
                        "error": None,
                    }
                ],
                "scores": {alice.ss58_address: "1.000000"},
            }
        ],
        "rolling": {alice.ss58_address: "1.000000"},
        "weights": {alice.ss58_address: "1.000000"},
    }
    bhash = bundle_hash(bundle)
    bundle["signature"] = "0x" + bytes(bob.sign(bundle_message(bhash))).hex()

    signing = {
        "miner": alice.ss58_address,
        "validator": bob.ss58_address,
        "task_id": tid,
        "forecast": forecast.to_json(),
        "forecast_message_hex": fmsg.hex(),
        "forecast_signature": fsig,
        "bundle": bundle,
        "bundle_hash": bhash,
        "bundle_message_hex": bundle_message(bhash).hex(),
        "commitment": commitment_text(1, bhash),
        "chainlink_to_str": [
            {"answer": a, "decimals": d, "expected": chainlink_to_str(a, d)}
            for a, d in [(23622797244, 8), (100000000, 8), (5, 8), (-123450000, 8), (7, 0)]
        ],
    }
    (OUT / "canonical.json").write_text(json.dumps(canon_cases, indent=1) + "\n")
    if "--signing" in sys.argv or not (OUT / "signing.json").exists():
        (OUT / "signing.json").write_text(json.dumps(signing, indent=1) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
