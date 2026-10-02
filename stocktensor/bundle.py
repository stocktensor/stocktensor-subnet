"""Epoch bundles: build, sign and verify (see docs/PROTOCOL.md).

A bundle is the public record of one validator epoch: every resolved task,
every miner answer (with the miner's own signature), the Chainlink rounds used,
and the scores. Bundles chain through ``prev`` and the newest hash is anchored
on-chain, so the history cannot be rewritten without it showing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bittensor.sp_core import verify as sp_verify

from .protocol import (
    PROTOCOL_VERSION,
    Forecast,
    bundle_hash,
    bundle_message,
    forecast_message,
)
from .scoring import SCORING_VERSION, TaskResult, rolling_scores, score_task, weights

ZERO_HASH = "0" * 64
ERRORS = ("timeout", "http", "bad_signature", "invalid", "late")


def fmt_score(value: float) -> str:
    return f"{value:.6f}"


def _signature_bytes(signature: str) -> bytes | None:
    try:
        return bytes.fromhex(signature.removeprefix("0x"))
    except (ValueError, AttributeError):
        return None


def sign_bytes(keypair: Any, message: bytes) -> str:
    """0x-hex signature of ``message`` by a bittensor keypair / signer."""
    return "0x" + bytes(keypair.sign(message)).hex()


def verify_signature(message: bytes, signature: str | None, ss58: str) -> bool:
    if not signature:
        return False
    raw = _signature_bytes(signature)
    if raw is None:
        return False
    try:
        return bool(sp_verify(message, raw, ss58))
    except Exception:  # noqa: BLE001 - malformed keys/signatures are just "invalid"
        return False


def sign_forecast(keypair: Any, task_id: str, forecast: Forecast) -> str:
    return sign_bytes(keypair, forecast_message(task_id, keypair.ss58_address, forecast))


def verify_forecast(task_id: str, miner: str, forecast: Forecast, signature: str | None) -> bool:
    return verify_signature(forecast_message(task_id, miner, forecast), signature, miner)


@dataclass
class TaskRecord:
    """One task as it appears in a bundle."""

    task_id: str
    asset: str
    horizon: str
    as_of: int
    session: str
    feed: str
    reference: dict[str, Any]  # {"round_id": str, "answer": str, "updated_at": int}
    realised: dict[str, Any] | None
    void: str | None
    responses: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "asset": self.asset,
            "horizon": self.horizon,
            "as_of": self.as_of,
            "session": self.session,
            "feed": self.feed,
            "reference": self.reference,
            "realised": self.realised,
            "void": self.void,
            "responses": sorted(self.responses, key=lambda r: r["miner"]),
            "scores": {m: fmt_score(s) for m, s in sorted(self.scores.items())},
        }


def response_forecasts(responses: Sequence[Mapping[str, Any]], task_id: str) -> dict[str, Forecast | None]:
    """Forecasts that count for scoring: present, error-free and correctly signed."""
    out: dict[str, Forecast | None] = {}
    for response in responses:
        miner = response["miner"]
        raw = response.get("forecast")
        if raw is None or response.get("error"):
            out[miner] = None
            continue
        try:
            forecast = Forecast.from_json(raw)
        except ValueError:
            out[miner] = None
            continue
        out[miner] = (
            forecast if verify_forecast(task_id, miner, forecast, response.get("signature")) else None
        )
    return out


def score_record(record: TaskRecord) -> dict[str, float]:
    """Scores for a resolved, non-void task record."""
    if record.void or record.realised is None:
        return {}
    reference = float(record.reference["answer"])
    realised = float(record.realised["answer"])
    return score_task(reference, realised, response_forecasts(record.responses, record.task_id))


def build_bundle(
    *,
    keypair: Any,
    netuid: int,
    epoch: int,
    created_at: int,
    prev: str,
    tasks: Sequence[TaskRecord],
    history: Sequence[TaskResult],
) -> dict[str, Any]:
    """Build and sign a bundle.

    ``history`` holds the scored tasks of earlier bundles (any age; the rolling
    window is applied here); this bundle's own scored tasks are added to it.
    """
    results = list(history)
    for record in tasks:
        if not record.void and record.realised is not None:
            results.append(TaskResult(record.task_id, record.as_of, record.scores))
    rolling = rolling_scores(results, created_at)
    bundle: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "scoring_version": SCORING_VERSION,
        "netuid": netuid,
        "validator": keypair.ss58_address,
        "epoch": epoch,
        "created_at": created_at,
        "prev": prev,
        "tasks": [t.to_json() for t in sorted(tasks, key=lambda t: (t.as_of, t.task_id))],
        "rolling": {m: fmt_score(s) for m, s in rolling.items()},
        "weights": {m: fmt_score(w) for m, w in weights(rolling).items()},
    }
    bundle["signature"] = sign_bytes(keypair, bundle_message(bundle_hash(bundle)))
    return bundle


class BundleError(ValueError):
    pass


def verify_bundle(
    bundle: Mapping[str, Any],
    *,
    prev: str | None = None,
    history: Sequence[TaskResult] = (),
    tolerance: float = 1e-6,
) -> VerifiedBundle:
    """Check a bundle end to end.

    ``history`` = the ``results`` of earlier verified bundles of the same
    validator (needed for the rolling scores). Returns the bundle hash and
    this bundle's recomputed task results.

    Verifies the validator signature, ``prev`` linkage (when given), every
    miner signature, every task score, the rolling scores and weights.
    Raises :class:`BundleError` on the first problem.
    """
    if bundle.get("version") != PROTOCOL_VERSION:
        raise BundleError(f"unsupported bundle version {bundle.get('version')!r}")
    if bundle.get("scoring_version") != SCORING_VERSION:
        raise BundleError(f"unsupported scoring version {bundle.get('scoring_version')!r}")
    digest = bundle_hash(dict(bundle))
    if not verify_signature(bundle_message(digest), bundle.get("signature"), bundle["validator"]):
        raise BundleError("validator signature does not verify")
    if prev is not None and bundle.get("prev") != prev:
        raise BundleError("prev hash does not match the previous bundle")

    def close(a: float, b: str, what: str) -> None:
        if abs(a - float(b)) > tolerance:
            raise BundleError(f"{what}: expected {a:.6f}, bundle says {b}")

    results = list(history)
    own: list[TaskResult] = []
    for task in bundle["tasks"]:
        for response in task["responses"]:
            if response.get("forecast") is not None and not response.get("error"):
                forecast = Forecast.from_json(response["forecast"])
                if not verify_forecast(
                    task["task_id"], response["miner"], forecast, response.get("signature")
                ):
                    raise BundleError(f"bad miner signature in task {task['task_id']}")
        record = TaskRecord(
            task_id=task["task_id"],
            asset=task["asset"],
            horizon=task["horizon"],
            as_of=int(task["as_of"]),
            session=task["session"],
            feed=task["feed"],
            reference=task["reference"],
            realised=task["realised"],
            void=task["void"],
            responses=list(task["responses"]),
        )
        expected = score_record(record)
        if set(expected) != set(task["scores"]):
            raise BundleError(f"scored miners differ in task {task['task_id']}")
        for miner, value in expected.items():
            close(value, task["scores"][miner], f"score {task['task_id']}/{miner}")
        if expected:
            own.append(TaskResult(record.task_id, record.as_of, expected))
    results.extend(own)

    rolling = rolling_scores(results, int(bundle["created_at"]))
    if set(rolling) != set(bundle["rolling"]):
        raise BundleError("rolling score miners differ")
    for miner, value in rolling.items():
        close(value, bundle["rolling"][miner], f"rolling {miner}")
    expected_weights = weights(rolling)
    if set(expected_weights) != set(bundle["weights"]):
        raise BundleError("weight miners differ")
    for miner, value in expected_weights.items():
        close(value, bundle["weights"][miner], f"weight {miner}")
    return VerifiedBundle(digest, own)


@dataclass(frozen=True)
class VerifiedBundle:
    hash: str
    results: list[TaskResult]


def bundle_results(bundle: Mapping[str, Any]) -> list[TaskResult]:
    """Scored tasks of a bundle, as inputs for the next bundle's rolling scores."""
    out = []
    for task in bundle["tasks"]:
        if task["scores"]:
            out.append(
                TaskResult(
                    task["task_id"], int(task["as_of"]), {m: float(s) for m, s in task["scores"].items()}
                )
            )
    return out
