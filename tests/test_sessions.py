from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stocktensor.sessions import session_at


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # Winter (EST, UTC-5)
        ("2026-01-12T14:30:00", "regular"),  # Mon 09:30 ET
        ("2026-01-12T14:29:59", "pre"),
        ("2026-01-12T21:00:00", "post"),  # 16:00 ET
        ("2026-01-13T01:00:00", "overnight"),  # Mon 20:00 ET
        ("2026-01-13T08:59:59", "overnight"),  # 03:59 ET
        ("2026-01-13T09:00:00", "pre"),  # 04:00 ET
        # Summer (EDT, UTC-4) right after the 2026-03-08 switch
        ("2026-03-09T13:30:00", "regular"),
        ("2026-03-09T13:29:59", "pre"),
        ("2026-03-09T20:00:00", "post"),
        # Weekend boundaries
        ("2026-01-17T00:59:59", "post"),  # Fri 19:59 ET
        ("2026-01-17T01:00:00", "closed"),  # Fri 20:00 ET
        ("2026-01-18T12:00:00", "closed"),  # Sunday
        ("2026-01-19T00:59:59", "closed"),  # Sun 19:59 ET
        ("2026-01-19T01:00:00", "overnight"),  # Sun 20:00 ET
    ],
)
def test_sessions(utc: str, expected: str) -> None:
    assert session_at(ts(utc)) == expected
