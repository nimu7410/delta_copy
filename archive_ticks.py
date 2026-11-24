"""Copy every finished hour of `deltabot.ticks` to data/ticks/YYYYMMDD_HH.xz (UTC hours).

Compressed: lzma over lines "symbol,price,ms since previous tick" (first line: absolute ms).

The table keeps a day; these files keep everything, so `hhll.py replay` and `hhll.py tune` can test on many days.
Run hourly (delta-archive.timer). Hours already archived are skipped, so re-runs are safe.
"""
from __future__ import annotations

import lzma
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import Database

DIR = Path(__file__).resolve().parent / "data" / "ticks"


def main() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    db = Database()
    db.connect()
    first = db.execute("SELECT min(traded_at) AS m FROM deltabot.ticks", fetch=True)[0]["m"]
    if first is None:
        return
    # the oldest hour may be partly pruned already: start at the next full hour
    h = first.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    while h < end:
        path = DIR / f"{h:%Y%m%d_%H}.xz"
        if not path.exists():
            rows = db.execute("SELECT symbol, price, (extract(epoch from traded_at)*1000)::bigint AS t "
                              "FROM deltabot.ticks WHERE traded_at >= %s AND traded_at < %s ORDER BY traded_at, id",
                              (h, h + timedelta(hours=1)), fetch=True) or []
            prev, out = 0, []
            for r in rows:
                out.append(f"{r['symbol']},{float(r['price'])!r},{r['t'] - prev}")
                prev = r["t"]
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(lzma.compress("\n".join(out).encode(), preset=9 | lzma.PRESET_EXTREME))
            os.replace(tmp, path)
        h += timedelta(hours=1)


def read(symbol: str, since_ms: int = 0) -> tuple[list[tuple[float, int]], int]:
    """Archived ticks of `symbol` as [(price, ms)] from the hour of `since_ms`, and the ms where the archive ends."""
    out, end = [], 0
    for path in sorted(DIR.glob("*.xz")):
        hour_end = int((datetime.strptime(path.stem, "%Y%m%d_%H").replace(tzinfo=timezone.utc)
                        + timedelta(hours=1)).timestamp() * 1000)
        if hour_end <= since_ms:
            continue
        t = 0
        for line in lzma.decompress(path.read_bytes()).decode().splitlines():
            sym, p, dt = line.split(",")
            t += int(dt)
            if sym == symbol:
                out.append((float(p), t))
        end = hour_end
    return out, end


if __name__ == "__main__":
    main()
