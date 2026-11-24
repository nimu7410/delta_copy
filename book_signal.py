"""LONG / SHORT / WAIT for one symbol, from its order book and who is hitting it.

Two things, each -1 (sellers) ... +1 (buyers):

    book   resting bid vs ask size within `NEAR` of the mid      (B - A) / (B + A)
           (or `NEAR_TICKS` ticks, whichever is wider)
    flow   market buys vs market sells over the last `FLOW_S`    (buy - sell) / (buy + sell)

Their average, smoothed over a few seconds, is the score. It says LONG once
the score reaches `ENTER` and keeps saying it until the score falls back under
`EXIT` (SHORT is the mirror), so the call does not flicker on every snapshot.

Its own websocket, apart from `feed`: the full book is ~230 KB a second for
BTCUSD alone, so it is subscribed only for the symbols the console is looking
at (the last `keep` asked for), and dropped once nobody has asked for an hour.

`BookRecorder` keeps that hour in `deltabot.book_seconds`, compressed to one
row per symbol per second - the near-price bid / ask USD and the market buy /
sell USD of that second, ~40 bytes in place of the ~230 KB book - and deletes
rows older than `KEEP_S`. `summary()` reads a window of it back (5m … 1h).

No trade history is held in memory: the flow's last minute is those rows,
summed by the database once a second, plus the trades of the current second
not yet written.

# ponytail: ENTER / EXIT / NEAR / FLOW_S are starting guesses, not yet measured
# against live outcomes; calibrate them from recorded data before real money.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import websocket

from feed import _USER_AGENT, _number, _positive
from ticks import TickRecorder

log = logging.getLogger("signal")

# The book counted: within 0.05% of the mid. 0.25% took in resting walls that
# never move (BTCUSD: one 620k-contract bid, 38% of the band), which pinned the
# book term near +0.25 for an hour (sd 0.006 over a minute; 0.06 at 0.05%).
NEAR = 0.0005
NEAR_TICKS = 5          # ... but never narrower than this many ticks (coarse-tick alts)
FLOW_S = 60             # the trades counted: the last minute
ENTER = 0.30            # the score that starts a LONG / SHORT
EXIT = 0.15             # the score a call holds until
SMOOTH = 0.3            # weight of each new snapshot (~1 s apart): ~3 s of memory
IDLE_S = 3600.0         # unsubscribed when nobody has asked for this long: an hour, so the
                        # recorded hour stays whole while the console looks at another symbol
STALE_S = 5.0
KEEP_S = 3600           # `book_seconds` keeps the last hour
#: The signal's history windows, in seconds. The longest is what `KEEP_S` holds.
#: The chart's resolutions up to an hour; 4h and 1d read the whole hour.
WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


@dataclass
class _State:
    contract_value: float
    tick: float
    bid_usd: float = 0.0
    ask_usd: float = 0.0
    book_at: float = 0.0                # monotonic
    flow_buy: float = 0.0               # market buys / sells (USD) of the last minute's written
    flow_sell: float = 0.0              # seconds, as `BookRecorder` last read them back
    seen: set = field(default_factory=set)   # trades counted since the last sample: snapshot dedup
    score: Optional[float] = None
    call: str = "WAIT"
    since: float = field(default_factory=time.time)
    asked_at: float = field(default_factory=time.monotonic)
    buy_acc: float = 0.0                # market buys / sells (USD) since the last sample
    sell_acc: float = 0.0
    sampled_ms: float = field(default_factory=lambda: time.time() * 1000)


def _ratio(a: float, b: float) -> float:
    return (a - b) / (a + b) if a + b > 0 else 0.0


def _money(usd: float) -> str:
    for unit, scale in (("M", 1e6), ("k", 1e3)):
        if usd >= scale:
            return f"${usd / scale:.1f}{unit}"
    return f"${usd:.0f}"


class BookSignal:
    def __init__(self, url: str, instruments: dict, *, keep: int = 3):
        self.url = url
        self.instruments = instruments
        self.keep = keep
        self.connected = False
        self._watched: OrderedDict = OrderedDict()      # symbol -> _State, oldest first
        self._lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stopping = threading.Event()

    # -- what the console asks -------------------------------------------
    def read(self, symbol: str, now: Optional[float] = None) -> dict:
        """The call for `symbol`, subscribing to it first if it is new."""
        now = time.monotonic() if now is None else now
        evicted, new = [], False
        with self._lock:
            st = self._watched.get(symbol)
            if st is None:
                new = True
                spec = self.instruments[symbol]
                st = self._watched[symbol] = _State(float(spec.contract_value), float(spec.tick_size))
                while len(self._watched) > self.keep:
                    evicted.append(self._watched.popitem(last=False)[0])
            self._watched.move_to_end(symbol)
            st.asked_at = now
            ready = st.score is not None
            out = {"symbol": symbol, "ready": ready, "connected": self.connected}
            if ready:
                buy, sell = self._flow(st)
                out.update({
                    "call": st.call, "since": st.since, "score": round(st.score, 3),
                    "book": round(_ratio(st.bid_usd, st.ask_usd), 3), "flow": round(_ratio(buy, sell), 3),
                    "stale": now - st.book_at > STALE_S,
                    "why": self._why(st.bid_usd, st.ask_usd, buy, sell, "last minute"),
                })
        if new:
            self._send("subscribe", [symbol])
        if evicted:
            self._send("unsubscribe", evicted)
        return out

    @staticmethod
    def _flow(st: _State) -> tuple[float, float]:
        """Market buys / sells over the last minute: written seconds plus this one."""
        return st.flow_buy + st.buy_acc, st.flow_sell + st.sell_acc

    def set_flow(self, sums: dict) -> None:
        """`symbol -> (buy USD, sell USD)` of the last `FLOW_S` written seconds."""
        with self._lock:
            for symbol, st in self._watched.items():
                st.flow_buy, st.flow_sell = sums.get(symbol, (0.0, 0.0))

    @staticmethod
    def _why(b: float, a: float, buy: float, sell: float, span: str) -> str:
        if not b or not a:
            book = "Book is one-sided near the price"
        elif max(b, a) / min(b, a) < 1.2:
            book = f"Bids and asks even near the price ({_money(b)} / {_money(a)})"
        else:
            book = (f"Bids {b / a:.1f}x asks near the price" if b > a
                    else f"Asks {a / b:.1f}x bids near the price")
        if buy + sell <= 0:
            return f"{book} · no trades in the {span}"
        return f"{book} · {span} {buy / (buy + sell) * 100:.0f}% of market orders were buys"

    def sample(self, wall: Optional[float] = None, now: Optional[float] = None) -> list:
        """One `book_seconds` row per watched symbol with a fresh book, and the
        trade accumulators start over. A stale book gives no row: a gap, not a zero."""
        wall = time.time() if wall is None else wall
        now = time.monotonic() if now is None else now
        at = datetime.fromtimestamp(int(wall), timezone.utc)
        rows = []
        with self._lock:
            for symbol, st in self._watched.items():
                if now - st.book_at > STALE_S:
                    continue
                rows.append((symbol, at, st.bid_usd, st.ask_usd, st.buy_acc, st.sell_acc))
                st.buy_acc = st.sell_acc = 0.0
                st.seen.clear()
                st.sampled_ms = wall * 1000
        return rows

    # -- subscriptions -----------------------------------------------------
    def _send(self, kind: str, symbols) -> None:
        ws = self._ws
        if not (ws and self.connected and symbols):
            return                      # on_open subscribes whatever is watched by then
        try:
            ws.send(json.dumps({"type": kind, "payload": {"channels": [
                {"name": "l2_orderbook", "symbols": list(symbols)},
                {"name": "all_trades", "symbols": list(symbols)},
            ]}}, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001 - the socket reconnects and resubscribes
            log.warning("signal %s failed: %s", kind, exc)

    # -- socket ------------------------------------------------------------
    def start(self) -> None:
        self._stopping.clear()
        threading.Thread(target=self._run_forever, name="signal-ws", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            self._ws = websocket.WebSocketApp(
                self.url, header={"User-Agent": _USER_AGENT},
                on_open=self._on_open, on_message=self._on_message,
                on_error=lambda ws, e: log.warning("signal socket error: %s", str(e)[:200]),
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001
                log.error("signal socket crashed: %s", exc)
            self.connected = False
            if self._stopping.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def _on_open(self, ws) -> None:
        self.connected = True
        with self._lock:
            symbols = list(self._watched)
        self._send("subscribe", symbols)

    def _on_close(self, ws, status, message) -> None:
        self.connected = False

    def _on_message(self, ws, raw) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = message.get("type")
        if kind == "l2_orderbook":
            self._on_book(message)
        elif kind == "all_trades":
            self._on_trades(message.get("symbol"), [message])
        elif kind == "all_trades_snapshot":
            self._on_trades(message.get("symbol"), message.get("trades") or [])

    def _on_book(self, message: dict, now: Optional[float] = None) -> None:
        symbol = message.get("symbol")
        now = time.monotonic() if now is None else now
        st = self._watched.get(symbol)
        if st is None:
            return
        if now - st.asked_at > IDLE_S:
            with self._lock:
                self._watched.pop(symbol, None)
            self._send("unsubscribe", [symbol])
            return

        def levels(rows):
            return [(p, q) for p, q in ((_positive(r.get("limit_price")), _number(r.get("size")))
                                        for r in rows or ()) if p and q and q > 0]

        bids, asks = levels(message.get("buy")), levels(message.get("sell"))
        if not bids or not asks:
            return
        best_bid, best_ask = max(p for p, _ in bids), min(p for p, _ in asks)
        if best_bid >= best_ask:
            return                      # crossed: not a book to read
        mid = (best_bid + best_ask) / 2
        band = max(mid * NEAR, NEAR_TICKS * st.tick)
        cv = st.contract_value
        bid_usd = sum(q * cv * p for p, q in bids if p >= mid - band)
        ask_usd = sum(q * cv * p for p, q in asks if p <= mid + band)
        with self._lock:
            st.bid_usd, st.ask_usd, st.book_at = bid_usd, ask_usd, now
            buy, sell = self._flow(st)
            raw = 0.5 * _ratio(bid_usd, ask_usd) + 0.5 * _ratio(buy, sell)
            st.score = raw if st.score is None else (1 - SMOOTH) * st.score + SMOOTH * raw
            call = st.call
            if st.score >= ENTER:
                call = "LONG"
            elif st.score <= -ENTER:
                call = "SHORT"
            elif (call == "LONG" and st.score < EXIT) or (call == "SHORT" and st.score > -EXIT):
                call = "WAIT"
            if call != st.call:
                st.call, st.since = call, time.time()

    def _on_trades(self, symbol, rows) -> None:
        st = self._watched.get(symbol)
        if st is None:
            return
        fresh = []
        for t in rows:
            price, size, stamp = _positive(t.get("price")), _number(t.get("size")), _number(t.get("timestamp"))
            side = ("buy" if t.get("buyer_role") == "taker" else
                    "sell" if t.get("seller_role") == "taker" else None)
            if price and size and stamp and side:
                fresh.append((int(stamp // 1000), size * st.contract_value * price, side))
        cutoff = time.time() * 1000 - FLOW_S * 1000
        with self._lock:
            if len(rows) > 1:           # a snapshot: history that may overlap what was counted
                # Only what is new and since the last sample counts toward the next one.
                new = [t for t in set(fresh) - st.seen if t[0] >= st.sampled_ms]
            else:
                new = [t for t in fresh if t[0] >= cutoff]   # older than the flow window: not counted
            st.seen.update(new)
            for _, usd, side in new:
                if side == "buy":
                    st.buy_acc += usd
                else:
                    st.sell_acc += usd


class BookRecorder(TickRecorder):
    """`BookSignal.sample()` into `deltabot.book_seconds` once a second; rows
    older than `KEEP_S` deleted once a minute."""

    def __init__(self, signal: BookSignal, db, *, interval: float = 1.0, prune_every: float = 60.0):
        super().__init__(signal, db, interval=interval, prune_every=prune_every)
        self.signal = signal

    # ponytail: a failed insert loses that second's rows (the accumulators were
    # already reset); a gap in an hour of seconds, not worth a retry queue.
    def flush(self) -> int:
        rows = self.signal.sample()
        # Two samples inside one wall second fold together rather than collide.
        self.db.execute_many(
            """INSERT INTO deltabot.book_seconds (symbol, at, bid_usd, ask_usd, buy_usd, sell_usd)
               VALUES %s ON CONFLICT (symbol, at) DO UPDATE SET
                   bid_usd = EXCLUDED.bid_usd, ask_usd = EXCLUDED.ask_usd,
                   buy_usd = book_seconds.buy_usd + EXCLUDED.buy_usd,
                   sell_usd = book_seconds.sell_usd + EXCLUDED.sell_usd""", rows)
        # The signal's last minute of flow, read back from what is written.
        sums = self.db.execute(
            """SELECT symbol, coalesce(sum(buy_usd), 0) AS buy, coalesce(sum(sell_usd), 0) AS sell
               FROM deltabot.book_seconds WHERE at > now() - make_interval(secs => %s)
               GROUP BY symbol""", (FLOW_S,), fetch=True) or []
        self.signal.set_flow({r["symbol"]: (float(r["buy"]), float(r["sell"])) for r in sums})
        return len(rows)

    def prune(self) -> None:
        self.db.execute("DELETE FROM deltabot.book_seconds WHERE at < now() - make_interval(secs => %s)",
                        (KEEP_S,))
        self._pruned_at = time.monotonic()

    def summary(self, symbol: str, seconds: int) -> dict:
        """`symbol`'s book and flow over the last `seconds`: average near-price
        bids / asks, total market buys / sells, and the lean they add up to."""
        r = self.db.execute(
            """SELECT count(*) AS n, extract(epoch FROM now() - min(at)) AS covered,
                      avg(bid_usd) AS bid, avg(ask_usd) AS ask, sum(buy_usd) AS buy, sum(sell_usd) AS sell
               FROM deltabot.book_seconds
               WHERE symbol = %(symbol)s AND at > now() - make_interval(secs => %(seconds)s)""",
            {"symbol": symbol, "seconds": seconds}, fetch=True)[0]
        out = {"symbol": symbol, "seconds": seconds, "recorded_s": r["n"]}
        if not r["n"]:
            return out
        bid, ask, buy, sell = (float(r[k] or 0) for k in ("bid", "ask", "buy", "sell"))
        book, flow = _ratio(bid, ask), _ratio(buy, sell)
        score = 0.5 * book + 0.5 * flow
        span = next((k for k, v in WINDOWS.items() if v == seconds), f"{seconds}s")
        out.update({
            "covered_s": min(seconds, int(float(r["covered"]) + 1)),
            "bid_usd": bid, "ask_usd": ask, "buy_usd": buy, "sell_usd": sell,
            "book": round(book, 3), "flow": round(flow, 3), "score": round(score, 3),
            "call": "LONG" if score >= ENTER else "SHORT" if score <= -ENTER else "WAIT",
            "why": f"{self.signal._why(bid, ask, buy, sell, 'last ' + span)} · "
                   f"{_money(buy + sell)} traded",
        })
        return out


if __name__ == "__main__":
    from instruments import from_row

    btc = from_row({"symbol": "BTCUSD", "id": 27, "tick_size": "0.5", "contract_value": "0.001"})
    s = BookSignal("wss://unused", {"BTCUSD": btc})
    assert s.read("BTCUSD") == {"symbol": "BTCUSD", "ready": False, "connected": False}

    def book(bid_size, ask_size):
        s._on_message(None, json.dumps({
            "type": "l2_orderbook", "symbol": "BTCUSD",
            "buy": [{"limit_price": str(80_000 - 10 * i), "size": bid_size} for i in range(30)]
                   + [{"limit_price": "79900", "size": 10 ** 6}],        # a wall 0.12% away: not counted
            "sell": [{"limit_price": str(80_000.5 + 10 * i), "size": ask_size} for i in range(30)]}))

    def trades(side, size, ago_s=1.0):
        now_us = int((time.time() - ago_s) * 1e6)
        s._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD", "price": "80000",
                                        "size": size, "timestamp": now_us,
                                        "buyer_role": "taker" if side == "buy" else "maker",
                                        "seller_role": "maker" if side == "buy" else "taker"}))

    book(1000, 1000)
    r = s.read("BTCUSD")
    assert r["ready"] and r["call"] == "WAIT" and abs(r["book"]) < 0.01 and "even" in r["why"], r
    # Bids 3x asks and buyers hitting: LONG, once the smoothed score gets there.
    trades("buy", 3000)
    trades("sell", 1000)
    for _ in range(10):
        book(3000, 1000)
    r = s.read("BTCUSD")
    assert r["call"] == "LONG" and abs(r["book"] - 0.5) < 0.01 and r["flow"] == 0.5, r
    assert "Bids 3.0x asks" in r["why"] and "75% of market orders were buys" in r["why"], r["why"]
    # A dip into the dead band holds the call; only under EXIT does it let go.
    st = s._watched["BTCUSD"]
    st.score = EXIT + 0.01
    book(1000, 1000)                    # raw 0.25: smoothed stays above EXIT
    assert s.read("BTCUSD")["call"] == "LONG"
    st.buy_acc = st.sell_acc = 0.0      # buyers stop: raw 0, the call lets go
    for _ in range(10):
        book(1000, 1000)
    assert s.read("BTCUSD")["call"] == "WAIT"
    # Asks heavy and sellers hitting: SHORT. A trade older than a minute does not count.
    st.buy_acc = st.sell_acc = 0.0
    trades("buy", 50_000, ago_s=120)
    trades("sell", 5000)
    for _ in range(10):
        book(1000, 4000)
    r = s.read("BTCUSD")
    assert r["call"] == "SHORT" and r["flow"] == -1.0 and "Asks 4.0x bids" in r["why"], r
    # Sampling: one row per fresh book, carrying the trades since the last sample.
    st = s._watched["BTCUSD"]
    rows = s.sample()
    assert len(rows) == 1 and rows[0][0] == "BTCUSD" and rows[0][5] > 0, rows   # the sells above
    assert s.sample()[0][4:] == (0.0, 0.0), "accumulators start over"
    trades("buy", 1000, ago_s=0)
    assert s.sample()[0][4] == 1000 * 0.001 * 80_000
    # A snapshot's old or already-held trades are not counted again.
    s._on_message(None, json.dumps({"type": "all_trades_snapshot", "symbol": "BTCUSD", "trades": [
        {"price": "80000", "size": 7, "timestamp": int((time.time() - 300) * 1e6),
         "buyer_role": "taker", "seller_role": "maker"},
        {"price": "80000", "size": 2, "timestamp": int(time.time() * 1e6) + 5000,
         "buyer_role": "maker", "seller_role": "taker"}]}))
    assert s.sample()[0][4:] == (0.0, 2 * 0.001 * 80_000)
    assert s.sample(now=time.monotonic() + STALE_S + 1) == [], "a stale book is a gap"
    # The flow's minute is the written seconds the recorder reads back, plus this second.
    s.set_flow({"BTCUSD": (900.0, 100.0)})
    st.buy_acc, st.sell_acc = 0.0, 0.0
    assert s._flow(st) == (900.0, 100.0)
    trades("sell", 10, ago_s=0)
    assert s._flow(st) == (900.0, 100.0 + 10 * 0.001 * 80_000)
    assert not hasattr(st, "trades"), "no trade history in memory"
    # Crossed books are ignored; a symbol nobody asks for is dropped.
    stamp = st.book_at
    s._on_book({"symbol": "BTCUSD", "buy": [{"limit_price": "2", "size": 1}],
                "sell": [{"limit_price": "1", "size": 1}]})
    assert st.book_at == stamp
    st.asked_at = time.monotonic() - IDLE_S - 1
    s._on_book({"symbol": "BTCUSD", "buy": [], "sell": []})
    assert "BTCUSD" not in s._watched
    print("book_signal self-check ok")
