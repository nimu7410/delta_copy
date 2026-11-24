"""Long points vs Short points for one symbol, from its websocket trades over
the last N minutes (1 ... 60).

Every trade's price is compared with the trade before it:

    price moved up     Long  += the move, in price points (80000 -> 80012: +12)
    price moved down   Short += the move, in price points (80012 -> 80005: +7)
    same price         nothing

The side with more points is the call: LONG, SHORT, or EVEN when they tie.
Long - Short is the window's net move.

Nothing is held in memory: every read is one query over `deltabot.ticks`
(written once a second by `ticks.TickRecorder`), so the newest second of
trades shows on the next read.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("momentum")

KEEP_S = 3600           # the longest window the console can ask for
DEFAULT_S = 60


class Momentum:
    def __init__(self, db=None):
        self.db = db

    def read(self, symbol: str, window_s: int = DEFAULT_S, wall: Optional[float] = None) -> dict:
        """Long / Short points for `symbol` over its last `window_s` seconds (60 ... 3600)."""
        window_s = max(60, min(KEEP_S, int(window_s)))
        out = {"symbol": symbol, "window_s": window_s, "trades": 0, "ready": False}
        if self.db is None:
            return out
        cutoff = (time.time() if wall is None else wall) - window_s
        # Each trade against the one before it, summed by the database.
        r = self.db.execute(
            """SELECT count(*) AS n,
                      coalesce(sum(greatest(d, 0)), 0) AS long, coalesce(sum(greatest(-d, 0)), 0) AS short,
                      count(*) FILTER (WHERE d > 0) AS ups, count(*) FILTER (WHERE d < 0) AS downs
               FROM (SELECT price - lag(price) OVER (ORDER BY id) AS d
                     FROM deltabot.ticks
                     WHERE symbol = %s AND traded_at >= to_timestamp(%s)) t""",
            (symbol, cutoff), fetch=True)[0]
        out["trades"] = int(r["n"])
        out["ready"] = out["trades"] >= 2
        if not out["ready"]:
            return out
        long, short = float(r["long"]), float(r["short"])
        lead = long - short
        out.update({
            "call": "LONG" if lead > 0 else "SHORT" if lead < 0 else "EVEN",
            "long_pts": round(long, 8), "short_pts": round(short, 8),
            "lead_pts": round(lead, 8), "ups": int(r["ups"]), "downs": int(r["downs"]),
        })
        return out


if __name__ == "__main__":
    # No database: nothing to read, never an error.
    assert Momentum().read("BTCUSD", 10 ** 6) == {"symbol": "BTCUSD", "window_s": KEEP_S,
                                                  "trades": 0, "ready": False}
    assert Momentum().read("BTCUSD", 1)["window_s"] == 60
    # The query itself is checked against a scratch database in tests/run_checks.py.
    print("momentum self-check ok")
