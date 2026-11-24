"""Every websocket trade into `deltabot.ticks`, kept for a day.

The feed numbers each trade; once a second the recorder takes the ones it has
not written yet and inserts them in one batch, and once a minute it deletes
what is older than `KEEP`. Market data is the same in LIVE and TEST, so the
table has no `mode`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("ticks")

KEEP = "1 day"
#: The Live chart's windows, in seconds. The longest is what `KEEP` holds.
WINDOWS = {"5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "6h": 21600, "1d": 86400}


class TickRecorder:
    def __init__(self, feed, db, *, interval: float = 1.0, prune_every: float = 60.0):
        self.feed, self.db = feed, db
        self.interval, self.prune_every = interval, prune_every
        self._cursor = 0                 # the last trade number written
        self._pruned_at = 0.0
        self._stopping = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ticks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.flush()

    def _loop(self) -> None:
        while not self._stopping.wait(self.interval):
            try:
                self.flush()
                if time.monotonic() - self._pruned_at >= self.prune_every:
                    self.prune()
            except Exception as exc:  # noqa: BLE001 - a DB hiccup must not stop the recorder
                log.warning("tick recorder failed: %s", exc)

    # ponytail: reads the feed's 2000-trade buffer once a second; a burst of more
    # than 2000 trades in a second loses the oldest. Shorten `interval` if that happens.
    def flush(self) -> int:
        cursor, trades = self.feed.trades_since(self._cursor)
        self.db.execute_many(
            "INSERT INTO deltabot.ticks (symbol, price, traded_at) VALUES %s",
            [(symbol, price, datetime.fromtimestamp(at / 1000, timezone.utc))
             for symbol, price, at in trades],
        )
        self._cursor = cursor            # only after the insert: a failed batch is retried
        return len(trades)

    def prune(self) -> None:
        self.db.execute(f"DELETE FROM deltabot.ticks WHERE traded_at < now() - interval '{KEEP}'")
        self._pruned_at = time.monotonic()

    # ponytail: the last trade of each bucket, so a spike shorter than a bucket
    # (~43 s at 1d) can vanish. Return min/max per bucket if that ever matters.
    def recent(self, symbol: str, seconds: int, points: int = 2000) -> list:
        """`symbol`'s trades over the last `seconds` as [price, epoch ms], oldest
        first: at most `points` of them, the last trade of each equal slice of time."""
        rows = self.db.execute(
            """SELECT DISTINCT ON (bucket) price, at FROM (
                   SELECT price, (extract(epoch FROM traded_at) * 1000)::bigint AS at,
                          floor(extract(epoch FROM traded_at) * 1000 / %(bucket)s) AS bucket
                   FROM deltabot.ticks
                   WHERE symbol = %(symbol)s AND traded_at > now() - make_interval(secs => %(seconds)s)
               ) t ORDER BY bucket, at DESC""",
            {"symbol": symbol, "seconds": seconds, "bucket": seconds * 1000 / points}, fetch=True) or []
        return [[r["price"], r["at"]] for r in rows]
