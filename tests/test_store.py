from __future__ import annotations

from stocktensor.bundle import ZERO_HASH, TaskRecord
from stocktensor.store import Store


def rec(task: str, as_of: int, horizon: str = "1h") -> TaskRecord:
    return TaskRecord(
        task,
        "NVDA",
        horizon,
        as_of,
        "regular",
        "0xfeed",
        {"round_id": "1", "answer": "100", "updated_at": as_of},
        None,
        None,
        [],
    )


def test_lifecycle(tmp_path) -> None:
    store = Store(tmp_path / "v.sqlite")
    assert store.chain_head() == (0, ZERO_HASH)
    store.add_task(rec("a", 1_000))
    store.add_task(rec("a", 1_000))  # duplicate ignored
    store.add_task(rec("b", 1_000, "1d"))
    assert [t.task_id for t in store.due_tasks(1_000 + 3_600)] == ["a"]
    assert store.due_tasks(1_000 + 3_600, grace=60) == []
    store.resolve("a", {"round_id": "2", "answer": "101", "updated_at": 4_000}, None, {"m": 1.0})
    assert store.count("pending") == 1 and store.count("resolved") == 1
    resolved = store.resolved_tasks()
    assert resolved[0].scores == {"m": 1.0} and resolved[0].realised["answer"] == "101"
    assert store.history(5_000) == []
    store.mark_bundled(["a"], 1)
    store.set_chain_head(1, "ab" * 32)
    assert [r.task_id for r in store.history(5_000)] == ["a"]
    assert store.history(5_000 + 15 * 86_400) == []
    store.close()
    reopened = Store(tmp_path / "v.sqlite")
    assert reopened.chain_head() == (1, "ab" * 32)
