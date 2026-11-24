"""Chart candles, straight from the venue's public history endpoint.

The console draws whatever resolution the operator picks, so candles are
fetched on demand rather than kept in memory, and cached briefly so a page
polling every few seconds costs the venue one request per symbol per window.
The bar still forming is included - a manual trader is looking at it.
"""

from __future__ import annotations

import logging
import threading
import time

import ist

log = logging.getLogger("data")

RESOLUTION_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800,
}
CHART_BARS = 300
#: The most a page may ask for at once (one venue request).
HISTORY_BARS = 2000


def resolution_seconds(resolution: str) -> int:
    try:
        return RESOLUTION_SECONDS[resolution]
    except KeyError:
        raise ValueError(f"unsupported resolution {resolution!r}; use one of "
                         f"{', '.join(RESOLUTION_SECONDS)}") from None


def fetch_candles(client, symbol: str, resolution: str, bars: int = CHART_BARS) -> list[dict]:
    """The last `bars` candles, oldest first, the forming one included."""
    step = resolution_seconds(resolution)
    end = int(time.time())
    rows = client.get(
        "/v2/history/candles",
        {"resolution": resolution, "symbol": symbol,
         "start": end - (bars + 1) * step, "end": end},
        signed=False,
    ) or []
    out: dict[int, dict] = {}
    for row in rows:
        try:
            start = int(row["time"])
            out[start] = {
                "time": ist.from_epoch(start).isoformat(),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return [out[key] for key in sorted(out)][-bars:]


class CandleCache:
    """`fetch_candles`, memoised per (symbol, resolution, bars) for `ttl` seconds."""

    def __init__(self, client, ttl: float = 4.0):
        self.client = client
        self.ttl = ttl
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def candles(self, symbol: str, resolution: str, bars: int = CHART_BARS) -> list[dict]:
        key = (symbol, resolution, bars)
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        try:
            data = fetch_candles(self.client, symbol, resolution, bars)
        except Exception as exc:  # noqa: BLE001 - a chart is never worth an error page
            log.warning("candles for %s %s failed: %s", symbol, resolution, exc)
            return hit[1] if hit else []
        with self._lock:
            self._cache[key] = (time.monotonic(), data)
        return data


if __name__ == "__main__":
    class _Client:
        calls = 0

        def get(self, path, params=None, signed=True):
            assert path == "/v2/history/candles" and signed is False
            type(self).calls += 1
            now = int(time.time()) // 60 * 60
            return [{"time": now - 60 * i, "open": 1, "high": 2, "low": 0.5, "close": 1.5}
                    for i in range(400)]

    rows = fetch_candles(_Client(), "X", "1m")
    assert len(rows) == CHART_BARS and rows[0]["time"] < rows[-1]["time"]
    cache = CandleCache(_Client())
    cache.candles("X", "1m")
    cache.candles("X", "1m")
    assert _Client.calls == 2, "the second call must come from the cache"
    assert len(cache.candles("X", "1m", 50)) == 50 and _Client.calls == 3, "bars is its own cache entry"
    try:
        resolution_seconds("7m")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown resolution must raise")
    print("data_handler self-check ok")
