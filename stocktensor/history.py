"""Recent Chainlink price history per feed, cached round by round."""

from __future__ import annotations

from .chainlink import Feed, Round


class PriceHistory:
    """Walks a feed backwards from its latest round and caches every round seen.

    ``recent(since)`` returns ``(updated_at, answer)`` pairs, oldest first, for
    rounds updated at or after ``since`` (plus the round just before it, so the
    series covers the whole window). Only the current phase is walked.
    """

    def __init__(self, feed: Feed, max_rounds: int = 2_000):
        self.feed = feed
        self.max_rounds = max_rounds
        self._rounds: dict[int, Round] = {}

    async def recent(self, since: int, latest: Round | None = None) -> list[tuple[int, int]]:
        latest = latest or await self.feed.latest()
        self._rounds[latest.round_id] = latest
        phase = latest.phase
        agg = latest.agg_round
        walked = 0
        while agg >= 1 and walked < self.max_rounds:
            round_id = (phase << 64) | agg
            data = self._rounds.get(round_id)
            if data is None:
                data = await self.feed.round(round_id)
                if data is None:
                    break
                self._rounds[round_id] = data
            walked += 1
            if data.updated_at < since:
                break
            agg -= 1
        series = sorted(
            (r.updated_at, r.answer) for r in self._rounds.values() if r.phase == phase and r.agg_round >= agg
        )
        return series
