from __future__ import annotations

import copy

import pytest

from stocktensor.bundle import (
    ZERO_HASH,
    BundleError,
    TaskRecord,
    build_bundle,
    bundle_results,
    score_record,
    sign_forecast,
    verify_bundle,
)
from stocktensor.protocol import Forecast, bundle_hash


def record(task: str, as_of: int, responses: list[dict], realised: str | None = "101") -> TaskRecord:
    rec = TaskRecord(
        task_id=task,
        asset="NVDA",
        horizon="1h",
        as_of=as_of,
        session="regular",
        feed="0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15",
        reference={"round_id": "18446744073709551617", "answer": "100", "updated_at": as_of - 5},
        realised=None
        if realised is None
        else {"round_id": "18446744073709551620", "answer": realised, "updated_at": as_of + 3000},
        void=None if realised is not None else "no_update",
        responses=responses,
    )
    rec.scores = score_record(rec)
    return rec


def answer(keypair, task: str, forecast: Forecast) -> dict:
    return {
        "miner": keypair.ss58_address,
        "forecast": forecast.to_json(),
        "signature": sign_forecast(keypair, task, forecast),
        "error": None,
    }


@pytest.fixture
def tasks(alice, charlie):
    good = Forecast("99", "100.8", "102", "0.7")
    worse = Forecast("90", "95", "99", "0.2")
    return [
        record("t1", 1_000_000, [answer(alice, "t1", good), answer(charlie, "t1", worse)]),
        record(
            "t2",
            1_000_100,
            [
                answer(alice, "t2", worse),
                {"miner": charlie.ss58_address, "forecast": None, "signature": None, "error": "timeout"},
            ],
        ),
        record("t3", 1_000_200, [answer(alice, "t3", good)], realised=None),
    ]


def test_build_and_verify(bob, alice, charlie, tasks) -> None:
    bundle = build_bundle(
        keypair=bob, netuid=7, epoch=1, created_at=1_003_700, prev=ZERO_HASH, tasks=tasks, history=[]
    )
    verified = verify_bundle(bundle, prev=ZERO_HASH)
    assert verified.hash == bundle_hash(bundle)
    assert bundle["tasks"][0]["scores"] == {alice.ss58_address: "1.000000", charlie.ss58_address: "0.000000"}
    assert bundle["tasks"][2]["void"] == "no_update" and bundle["tasks"][2]["scores"] == {}
    assert set(bundle["weights"]) == {alice.ss58_address, charlie.ss58_address}
    assert float(bundle["weights"][alice.ss58_address]) > float(bundle["weights"][charlie.ss58_address])
    assert len(verified.results) == 2


@pytest.mark.parametrize(
    "tamper",
    [
        lambda b: b["tasks"][0]["scores"].update({next(iter(b["tasks"][0]["scores"])): "0.500000"}),
        lambda b: b["tasks"][0]["responses"][0]["forecast"].update({"point": "101"}),
        lambda b: b.update({"created_at": b["created_at"] + 1}),
        lambda b: b["rolling"].update({next(iter(b["rolling"])): "0.999999"}),
        lambda b: b.update({"signature": "0x" + "00" * 64}),
    ],
)
def test_tampering_is_rejected(bob, tasks, tamper) -> None:
    bundle = build_bundle(
        keypair=bob, netuid=7, epoch=1, created_at=1_003_700, prev=ZERO_HASH, tasks=tasks, history=[]
    )
    broken = copy.deepcopy(bundle)
    tamper(broken)
    with pytest.raises(BundleError):
        verify_bundle(broken)


def test_resigned_tamper_still_caught(bob, tasks) -> None:
    """Even a validator re-signing a doctored score cannot pass: scores are recomputed."""
    from stocktensor.bundle import sign_bytes
    from stocktensor.protocol import bundle_message

    bundle = build_bundle(
        keypair=bob, netuid=7, epoch=1, created_at=1_003_700, prev=ZERO_HASH, tasks=tasks, history=[]
    )
    miner = next(iter(bundle["tasks"][0]["scores"]))
    bundle["tasks"][0]["scores"][miner] = "0.100000"
    bundle["signature"] = sign_bytes(bob, bundle_message(bundle_hash(bundle)))
    with pytest.raises(BundleError, match="score"):
        verify_bundle(bundle)


def test_prev_mismatch(bob, tasks) -> None:
    bundle = build_bundle(
        keypair=bob, netuid=7, epoch=1, created_at=1_003_700, prev=ZERO_HASH, tasks=tasks, history=[]
    )
    with pytest.raises(BundleError, match="prev"):
        verify_bundle(bundle, prev="11" * 32)


def test_chain_of_two_bundles(bob, alice, charlie) -> None:
    good = Forecast("99", "100.5", "102", "0.6")
    bad = Forecast("90", "95", "99", "0.2")
    first = build_bundle(
        keypair=bob,
        netuid=7,
        epoch=1,
        created_at=1_010_000,
        prev=ZERO_HASH,
        tasks=[record("a", 1_000_000, [answer(alice, "a", good), answer(charlie, "a", bad)])],
        history=[],
    )
    one = verify_bundle(first, prev=ZERO_HASH)
    second = build_bundle(
        keypair=bob,
        netuid=7,
        epoch=2,
        created_at=1_020_000,
        prev=one.hash,
        tasks=[record("b", 1_015_000, [answer(alice, "b", bad), answer(charlie, "b", good)])],
        history=one.results,
    )
    two = verify_bundle(second, prev=one.hash, history=one.results)
    assert two.hash != one.hash
    # rounded results from a published bundle are close enough to verify too
    verify_bundle(second, prev=one.hash, history=bundle_results(first))
    with pytest.raises(BundleError):
        verify_bundle(second, prev=one.hash, history=[])  # rolling needs the history


def test_bad_miner_signature_scores_zero(bob, alice, charlie) -> None:
    f = Forecast("99", "100.5", "102", "0.6")
    forged = answer(alice, "x", f)
    forged["miner"] = charlie.ss58_address  # alice's signature claimed for charlie
    rec = record("x", 1_000_000, [forged, answer(alice, "x", f)])
    assert rec.scores[charlie.ss58_address] == 0.0
    bundle = build_bundle(
        keypair=bob, netuid=7, epoch=1, created_at=1_003_700, prev=ZERO_HASH, tasks=[rec], history=[]
    )
    with pytest.raises(BundleError, match="signature"):
        verify_bundle(bundle)
