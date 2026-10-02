"""The golden vectors are the contract with the miner kit and the TypeScript verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stocktensor.bundle import verify_bundle, verify_forecast
from stocktensor.canonical import canonical, digest
from stocktensor.protocol import (
    Forecast,
    bundle_hash,
    bundle_message,
    chainlink_to_str,
    commitment_text,
    forecast_message,
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

GOLDEN = Path(__file__).parent / "golden"
SCORING = json.loads((GOLDEN / "scoring.json").read_text())
CANONICAL = json.loads((GOLDEN / "canonical.json").read_text())
SIGNING = json.loads((GOLDEN / "signing.json").read_text())


def test_versions_match() -> None:
    assert SCORING["scoring_version"] == SCORING_VERSION


@pytest.mark.parametrize("case", SCORING["losses"])
def test_losses(case: dict) -> None:
    r, y = case["reference"], case["realised"]
    assert interval_loss(case["low"], case["high"], y, r) == pytest.approx(case["interval"], abs=1e-15)
    assert direction_loss(case["p_up"], y, r) == pytest.approx(case["direction"], abs=1e-15)
    assert point_loss(case["point"], y, r) == pytest.approx(case["point_loss"], abs=1e-15)


@pytest.mark.parametrize("case", SCORING["ranks"])
def test_ranks(case: dict) -> None:
    assert rank_scores(case["losses"]) == case["expected"]


@pytest.mark.parametrize("task", SCORING["tasks"], ids=lambda t: t["task_id"])
def test_task_scores(task: dict) -> None:
    responses = {m: (Forecast.from_json(f) if f else None) for m, f in task["responses"].items()}
    scores = score_task(task["reference"], task["realised"], responses)
    assert scores.keys() == task["expected"].keys()
    for miner, value in task["expected"].items():
        assert scores[miner] == pytest.approx(value, abs=1e-12)


def test_rolling_weights_consensus() -> None:
    results = [TaskResult(t["task_id"], t["as_of"], t["expected"]) for t in SCORING["tasks"]]
    rolling = rolling_scores(results, SCORING["rolling"]["now"])
    assert rolling == pytest.approx(SCORING["rolling"]["expected"], abs=1e-12)
    assert weights(rolling) == pytest.approx(SCORING["weights"], abs=1e-12)
    assert sum(weights(rolling).values()) == pytest.approx(1.0)
    last = next(t for t in SCORING["tasks"] if t["task_id"] == SCORING["consensus"]["task_id"])
    result = consensus({m: Forecast.from_json(f) for m, f in last["responses"].items() if f}, rolling)
    expected = SCORING["consensus"]["expected"]
    assert result is not None and expected is not None
    assert (result.direction, result.models) == (expected["direction"], expected["models"])
    assert result.p_up == pytest.approx(expected["p_up"])
    assert result.agreement == pytest.approx(expected["agreement"])


@pytest.mark.parametrize("case", CANONICAL)
def test_canonical(case: dict) -> None:
    assert canonical(case["value"]).decode() == case["canonical"]
    assert digest(case["value"]) == case["sha256"]


def test_forecast_signature_fixture() -> None:
    forecast = Forecast.from_json(SIGNING["forecast"])
    message = forecast_message(SIGNING["task_id"], SIGNING["miner"], forecast)
    assert message.hex() == SIGNING["forecast_message_hex"]
    assert verify_forecast(SIGNING["task_id"], SIGNING["miner"], forecast, SIGNING["forecast_signature"])
    assert not verify_forecast(
        SIGNING["task_id"], SIGNING["validator"], forecast, SIGNING["forecast_signature"]
    )


def test_bundle_fixture() -> None:
    bundle = SIGNING["bundle"]
    assert bundle_hash(bundle) == SIGNING["bundle_hash"]
    assert bundle_message(SIGNING["bundle_hash"]).hex() == SIGNING["bundle_message_hex"]
    assert commitment_text(bundle["epoch"], SIGNING["bundle_hash"]) == SIGNING["commitment"]
    verified = verify_bundle(bundle, prev="0" * 64)
    assert verified.hash == SIGNING["bundle_hash"]


@pytest.mark.parametrize("case", SIGNING["chainlink_to_str"])
def test_chainlink_to_str(case: dict) -> None:
    assert chainlink_to_str(case["answer"], case["decimals"]) == case["expected"]
