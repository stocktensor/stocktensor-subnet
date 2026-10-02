"""US equity session tagging.

Robinhood Chain stock token feeds update 24/5 (regular, pre-market,
post-market and overnight sessions) and hold the last price over the weekend.
Tasks are tagged so scores can be broken down by session.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

SESSIONS = ("regular", "pre", "post", "overnight", "closed")


def session_at(unix: int) -> str:
    """Session for a unix timestamp (US/Eastern, holidays not modelled)."""
    local = datetime.fromtimestamp(unix, tz=UTC).astimezone(NEW_YORK)
    weekday = local.weekday()  # Mon=0 .. Sun=6
    clock = local.time()
    # Weekend: Friday 20:00 -> Sunday 20:00 ET.
    if weekday == 5 or (weekday == 4 and clock >= time(20)) or (weekday == 6 and clock < time(20)):
        return "closed"
    if time(9, 30) <= clock < time(16):
        return "regular"
    if time(4) <= clock < time(9, 30):
        return "pre"
    if time(16) <= clock < time(20):
        return "post"
    return "overnight"
