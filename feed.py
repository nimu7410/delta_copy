"""One websocket, every instrument: live mark, best bid/ask and funding rate.

Delta's `v2/ticker` carries everything the console and the paper venue price
off, but only once every ~5 s per symbol. The faster channels refresh their
part of the quote in between:

    all_trades     `last`, on every trade
    l2_updates     the order book, level by level, as it changes: best bid / ask and the depth behind them
                   (what a crossing order fills at, for its size)
    l1_orderbook   best bid / ask once a second: used only while a symbol's book is not in step
    mark_price     mark (what stops, brackets and liquidation trigger on), ~2 s
    v2/ticker      all of it, plus `funding_rate` (percent per funding interval)

Everything on the socket thread is a dict write and a notify. No I/O, no
order ever runs there: a slow consumer must not be able to stall the feed for
the others. Consumers that want every tick block in `wait_for_tick`.
"""

from __future__ import annotations

import heapq
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Optional

import websocket

from tickfilter import SensexTicks

log = logging.getLogger("feed")

# A browser-shaped UA: the CDN in front of the socket refuses the library
# default from some networks.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Quote:
    mark: float
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    funding_rate: Optional[float]   # percent per funding interval
    stamp: float                    # monotonic receipt time
    book_stamp: float = 0.0         # ... of the bid / ask
    last_stamp: float = 0.0         # ... of the last trade
    bids: tuple = ()                # the book's best levels, best first: ((price, contracts), ...)
    asks: tuple = ()                # ... empty while only the top of the book is known

    def take(self, side: str) -> float:
        """Where a crossing order fills: the far touch, or the last print when that is newer than the book and
        already beyond it (the book channel lags the trades by up to a second; a touch the market has traded
        through is not there to be taken)."""
        fresh = self.last is not None and self.last_stamp > self.book_stamp
        if side == "buy":
            far = self.ask or self.mark
            return max(far, self.last) if fresh else far
        far = self.bid or self.mark
        return min(far, self.last) if fresh else far

    def behind(self, side: str) -> bool:
        """True while a print newer than the book has traded past the touch a `side` order would take: the book
        has yet to show that trade (it does within about 40 ms, 80 at most, of the print)."""
        if self.last is None or self.last_stamp <= self.book_stamp:
            return False
        far = (self.ask if side == "buy" else self.bid) or self.mark
        return self.last > far if side == "buy" else self.last < far

    def sweep(self, side: str, size: int, limit: float) -> tuple[int, float]:
        """What a limit for `size` contracts takes on arrival: (contracts, their value), level by level from the
        touch and never through `limit`. (0, 0.0): it does not cross. Levels a newer print has traded past are
        gone. With no depth known, the touch (`take`) is taken to hold the whole size."""
        buy = side == "buy"
        levels = self.asks if buy else self.bids
        if not levels:
            far = self.take(side)
            return (size, size * far) if (far <= limit if buy else far >= limit) else (0, 0.0)
        past = self.last if self.last is not None and self.last_stamp > self.book_stamp else None
        qty, value = 0, 0.0
        for price, there in levels:
            if past is not None and (price < past if buy else price > past):
                continue
            if qty >= size or (price > limit if buy else price < limit):
                break
            n = min(size - qty, there)
            qty, value = qty + n, value + n * price
        return qty, value


DEPTH = 50                          # levels a side of the quote carries: far more than an order here sweeps


def _positive(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _number(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketFeed:
    """Live quotes for every configured instrument."""

    def __init__(self, symbols, *, settings):
        self.symbols = list(symbols)
        self.settings = settings
        self.url = settings.ws_url
        # symbol -> Quote. A plain dict assignment is atomic under the GIL, so
        # readers need no lock.
        self._quotes: dict[str, Quote] = {}
        self.connected = False
        self.connected_since: Optional[float] = None
        self.messages = 0
        self.seq = 0                    # bumped on every quote and connection change
        self.trade_no = 0               # numbers every trade the socket reports
        self._trades: deque = deque(maxlen=2000)   # (trade_no, symbol, price, epoch ms)
        # v3 (config.TICK_FILTER): what `_trades` hands on is SENSEX-like ticks around the prints, not the prints;
        # `last` (what orders and fills price off) stays the raw print either way.
        self._smooth: Optional[SensexTicks] = SensexTicks() if getattr(settings, "tick_filter", False) else None
        self._books: dict[str, dict] = {}          # symbol -> {"seq", "bids": {price: contracts}, "asks": {...}}
        self._tick = threading.Condition()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    # -- public ----------------------------------------------------------
    def quote(self, symbol: str, max_age: Optional[float] = None) -> Optional[Quote]:
        """The last quote, but only while it is still current."""
        entry = self._quotes.get(symbol)
        if entry is None:
            return None
        limit = self.settings.max_price_age if max_age is None else max_age
        return entry if (time.monotonic() - entry.stamp) <= limit else None

    def price(self, symbol: str) -> Optional[float]:
        """Last mark price, however old. For display and for marking positions."""
        entry = self._quotes.get(symbol)
        return entry.mark if entry else None

    def fresh_price(self, symbol: str, max_age: Optional[float] = None) -> Optional[float]:
        entry = self.quote(symbol, max_age)
        return entry.mark if entry else None

    def wait_for_tick(self, seq: int, timeout: float) -> int:
        """Block until something newer than `seq` happens, or `timeout`. The latest seq."""
        with self._tick:
            self._tick.wait_for(lambda: self.seq != seq, timeout)
            return self.seq

    def trades_since(self, trade_no: int) -> tuple[int, list]:
        """Every trade after `trade_no` as [symbol, price, epoch ms], oldest
        first, and the number to ask from next time. With the tick filter on
        (v3), the filtered ticks instead: at most one a second per symbol."""
        with self._tick:
            return self.trade_no, [list(t[1:]) for t in self._trades if t[0] > trade_no]

    def _notify(self) -> None:
        with self._tick:
            self.seq += 1
            self._tick.notify_all()

    def status(self) -> dict:
        stale = [s for s in self.symbols if self.quote(s) is None]
        return {
            "connected": self.connected,
            "url": self.url,
            "messages": self.messages,
            "uptime_s": None if not self.connected_since else round(time.monotonic() - self.connected_since, 1),
            "symbols": len(self.symbols),
            "stale_symbols": stale,
        }

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run_forever, name="feed-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- socket ----------------------------------------------------------
    def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            self._ws = websocket.WebSocketApp(
                self.url,
                header={"User-Agent": _USER_AGENT},
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001
                log.error("websocket crashed: %s", exc)
            if self._stopping.is_set():
                break
            log.info("reconnecting to %s in %.0fs", self.url, backoff)
            self._stopping.wait(backoff)
            backoff = min(backoff * 2, 30.0)
            if self.connected:
                backoff = 1.0

    def _on_open(self, ws) -> None:
        self.connected = True
        self.connected_since = time.monotonic()
        for symbol in list(self._books):
            self._drop_book(symbol)             # a new connection starts every book from its snapshot
        ws.send(json.dumps({
            "type": "subscribe",
            "payload": {"channels": [
                {"name": "v2/ticker", "symbols": self.symbols},
                {"name": "l1_orderbook", "symbols": self.symbols},
                # ponytail: full depth for every symbol; subscribe only the traded ones if the list grows long
                {"name": "l2_updates", "symbols": self.symbols},
                {"name": "all_trades", "symbols": self.symbols},
                {"name": "mark_price", "symbols": [f"MARK:{s}" for s in self.symbols]},
            ]},
        }, separators=(",", ":")))
        log.info("feed connected to %s (%d symbols)", self.url, len(self.symbols))
        self._notify()

    def _on_error(self, ws, error) -> None:
        text = str(error)
        if "Handshake status" in text:
            log.error("feed handshake refused by %s: %s", self.url, text.split("-+-+-")[0].strip())
        else:
            log.error("feed error: %s", text[:200])
        self.connected = False

    def _on_close(self, ws, status, message) -> None:
        if self.connected:
            log.warning("feed socket closed (%s)", status)
        self.connected = False
        self.connected_since = None
        self._notify()

    def _on_message(self, ws, raw) -> None:
        self.messages += 1
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        handler = self._handlers.get(message.get("type"))
        if handler:
            handler(self, message)
        if self._smooth is not None:
            # ponytail: seconds are closed on the next socket message (the book updates several times a second);
            # a timer thread if a quiet socket ever leaves a closed second waiting.
            ticks = self._smooth.clock(time.time() * 1000)
            if ticks:
                with self._tick:
                    for symbol, price, at in ticks:
                        self.trade_no += 1
                        self._trades.append((self.trade_no, symbol, price, at))
                self._notify()

    def _update(self, symbol, *, fresh: bool = True, **fields) -> None:
        """Refresh part of a quote that a ticker has already started."""
        previous = self._quotes.get(symbol)
        if previous is None:
            return
        now = time.monotonic()
        stamp = now if fresh else previous.stamp
        if "bid" in fields:
            fields["book_stamp"] = now
        if "last" in fields:
            fields["last_stamp"] = now
        self._quotes[symbol] = replace(previous, stamp=stamp, **fields)
        self._notify()

    def _handle_book(self, message: dict) -> None:
        symbol = message.get("symbol")
        book = self._books.get(symbol)
        if book is not None:
            # The level-by-level book is ahead of this once-a-second top. If this one is ahead, updates were lost.
            if (_number(message.get("last_sequence_no")) or 0) <= book["seq"]:
                return
            self._resync(symbol, "the top-of-book channel is ahead of it")
        bid = _positive(message.get("best_bid"))
        ask = _positive(message.get("best_ask"))
        if bid and ask and bid > ask:
            bid = ask = None                # crossed snapshot; do not fill off it
        self._update(symbol, bid=bid, ask=ask)

    def _handle_depth(self, message: dict) -> None:
        """`l2_updates`: a snapshot of the whole book, then every change to it in sequence (a level's new size;
        0 removes it). The quote gets the touch and the levels behind it."""
        symbol = message.get("symbol")
        seq = _number(message.get("sequence_no"))
        if symbol not in self.symbols or seq is None:
            return
        if message.get("action") == "snapshot":
            book = self._books[symbol] = {"seq": seq, "bids": {}, "asks": {}}
        else:
            book = self._books.get(symbol)
            if book is None:
                return                          # out of step: waiting for the snapshot asked for
            if seq != book["seq"] + 1:
                return self._resync(symbol, f"update {seq:.0f} after {book['seq']:.0f}")
            book["seq"] = seq
        try:
            for key in ("bids", "asks"):
                levels = book[key]
                for price, size in message.get(key) or ():
                    price, size = float(price), int(float(size))
                    if size > 0:
                        levels[price] = size
                    else:
                        levels.pop(price, None)
        except (TypeError, ValueError):
            return self._resync(symbol, "an unreadable level")
        bids = tuple(heapq.nlargest(DEPTH, book["bids"].items()))
        asks = tuple(heapq.nsmallest(DEPTH, book["asks"].items()))
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return self._resync(symbol, "it is empty or crossed")
        self._update(symbol, bid=bids[0][0], ask=asks[0][0], bids=bids, asks=asks)

    def _drop_book(self, symbol: str) -> None:
        if self._books.pop(symbol, None) is not None and self._quotes.get(symbol) is not None:
            self._quotes[symbol] = replace(self._quotes[symbol], bids=(), asks=())

    def _resync(self, symbol: str, why: str) -> None:
        """The book of `symbol` cannot be trusted: forget its depth (the once-a-second top takes over) and ask
        the venue for a fresh snapshot."""
        log.warning("[%s] order book out of step (%s): asking for a new snapshot", symbol, why)
        self._drop_book(symbol)
        channel = {"payload": {"channels": [{"name": "l2_updates", "symbols": [symbol]}]}}
        try:
            for kind in ("unsubscribe", "subscribe"):
                self._ws.send(json.dumps({"type": kind, **channel}, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001 - the socket is going down: the reconnect subscribes afresh
            log.warning("[%s] order book not asked for again: %s", symbol, exc)

    def _handle_mark(self, message: dict) -> None:
        mark = _positive(message.get("price"))
        if mark is not None:
            self._update(str(message.get("symbol", "")).removeprefix("MARK:"), mark=mark)

    def _handle_trade(self, message: dict, record: bool = True) -> None:
        last = _positive(message.get("price"))
        symbol = message.get("symbol")
        if last is None or symbol not in self._quotes:
            return
        if record:
            at = int(_number(message.get("timestamp")) or time.time() * 1e6) // 1000
            if self._smooth is not None:
                self._smooth.trade(symbol, last, at)      # handed on once its second is closed (`_on_message`)
            else:
                with self._tick:
                    self.trade_no += 1
                    self._trades.append((self.trade_no, symbol, last, at))
        # A trade says nothing about the book: it does not make a quote fresh.
        self._update(symbol, fresh=False, last=last)

    def _handle_trade_snapshot(self, message: dict) -> None:
        trades = message.get("trades") or []
        if trades:
            newest = max(trades, key=lambda t: t.get("timestamp") or 0)
            # History the chart already has: it sets `last`, it is not a new trade.
            self._handle_trade({"symbol": message.get("symbol"), "price": newest.get("price")},
                               record=False)

    def _handle_ticker(self, message: dict) -> None:
        symbol = message.get("symbol")
        if not symbol:
            return
        last = _positive(message.get("close"))
        mark = _positive(message.get("mark_price")) or last
        if mark is None:
            return
        quotes = message.get("quotes") or {}
        bid = _positive(quotes.get("best_bid"))
        ask = _positive(quotes.get("best_ask"))
        if bid and ask and bid > ask:
            bid = ask = None                # crossed snapshot; do not fill off it
        previous = self._quotes.get(symbol)
        if previous is not None and symbol in self._books:
            # The ticker's quotes are seconds old; the level-by-level book is live. Only the rest is taken.
            self._quotes[symbol] = replace(
                previous, mark=mark, stamp=time.monotonic(),
                funding_rate=previous.funding_rate if _number(message.get("funding_rate")) is None
                else _number(message.get("funding_rate")))
            return self._notify()
        if previous is not None and previous.last is not None:
            last = previous.last            # the ticker's `close` lags the trades channel: it must not step `last` back
        funding = _number(message.get("funding_rate"))
        if funding is None and previous is not None:
            funding = previous.funding_rate
        now = time.monotonic()
        self._quotes[symbol] = Quote(mark, bid, ask, last, funding, now, now if bid and ask else
                                     (previous.book_stamp if previous else 0.0),
                                     previous.last_stamp if previous else 0.0)
        self._notify()

    _handlers = {
        "v2/ticker": _handle_ticker, "l1_orderbook": _handle_book, "l2_updates": _handle_depth,
        "mark_price": _handle_mark,
        "all_trades": _handle_trade, "all_trades_snapshot": _handle_trade_snapshot,
    }

    def publish(self, symbol: str, mark: float, bid=None, ask=None, funding_rate=None) -> None:
        """Set a quote directly. Tests and tools only."""
        self._quotes[symbol] = Quote(mark, bid, ask, mark, funding_rate, time.monotonic())
        self._notify()


if __name__ == "__main__":
    from config import FeedSettings

    feed = MarketFeed(["BTCUSD"], settings=FeedSettings())
    feed._handle_ticker({
        "type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "80241.2", "close": 80242.0,
        "funding_rate": "0.0068", "quotes": {"best_bid": "80241.5", "best_ask": "80242"},
    })
    q = feed.quote("BTCUSD")
    assert q and q.mark == 80241.2 and q.bid == 80241.5 and q.ask == 80242.0, q
    assert abs(q.funding_rate - 0.0068) < 1e-12
    # A ticker without a funding field keeps the last known rate.
    feed._handle_ticker({"type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "80250"})
    assert feed.quote("BTCUSD").funding_rate == q.funding_rate
    assert feed.quote("BTCUSD").bid is None
    # The fast channels refresh their part of the quote between tickers.
    feed._handle_ticker({"type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "80250",
                         "close": 80250, "funding_rate": "0.0068"})
    feed._on_message(None, json.dumps({"type": "l1_orderbook", "symbol": "BTCUSD",
                                       "best_bid": "80260", "best_ask": "80260.5"}))
    feed._on_message(None, json.dumps({"type": "mark_price", "symbol": "MARK:BTCUSD", "price": "80259.1"}))
    feed._on_message(None, json.dumps({"type": "all_trades_snapshot", "symbol": "BTCUSD", "trades": [
        {"price": "80261", "timestamp": 2}, {"price": "80100", "timestamp": 1}]}))
    q = feed.quote("BTCUSD")
    assert (q.mark, q.bid, q.ask, q.last, q.funding_rate) == (80259.1, 80260.0, 80260.5, 80261.0, 0.0068), q
    stamp = q.stamp
    feed._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD", "price": "80262"}))
    assert feed.quote("BTCUSD").last == 80262.0 and feed.quote("BTCUSD").stamp == stamp
    # A print newer than the book and beyond the touch is where a crossing order fills; inside it, the touch is.
    q = feed.quote("BTCUSD")
    assert (q.bid, q.ask) == (80260.0, 80260.5) and q.take("buy") == 80262.0 and q.take("sell") == 80260.0
    # A late ticker does not step `last` back to an older print.
    feed._handle_ticker({"type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "80259.1", "close": 80250,
                         "quotes": {"best_bid": "80260", "best_ask": "80260.5"}})
    assert feed.quote("BTCUSD").last == 80262.0
    # Every live trade is kept in order for the chart; the snapshot's history is not.
    feed._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD", "price": "80263",
                                       "timestamp": 1792947782701968}))
    no, trades = feed.trades_since(0)
    assert trades == [["BTCUSD", 80262.0, trades[0][2]], ["BTCUSD", 80263.0, 1792947782701]], trades
    assert feed.trades_since(no) == (no, [])
    feed._on_message(None, json.dumps({"type": "l1_orderbook", "symbol": "BTCUSD",
                                       "best_bid": "80270", "best_ask": "80269"}))
    assert feed.quote("BTCUSD").bid is None, "a crossed book is not a book"
    feed._on_message(None, json.dumps({"type": "l1_orderbook", "symbol": "SOLUSD", "best_bid": "1"}))
    assert feed.quote("SOLUSD") is None, "no quote starts without a ticker or mark"
    # The level-by-level book: a snapshot, then changes in sequence, give the touch and the depth behind it.
    depth = lambda **m: feed._on_message(None, json.dumps({"type": "l2_updates", "symbol": "BTCUSD", **m}))  # noqa: E731
    depth(action="snapshot", sequence_no=10, bids=[["80270", "5"], ["80269.5", "7"]],
          asks=[["80271", "4"], ["80271.5", "6"], ["80273", "9"]])
    depth(action="update", sequence_no=11, bids=[["80270", "0"]], asks=[["80271", "3"]])
    q = feed.quote("BTCUSD")
    assert (q.bid, q.ask, q.bids, q.asks) == (80269.5, 80271.0, ((80269.5, 7),),
                                              ((80271.0, 3), (80271.5, 6), (80273.0, 9))), q
    # A crossing order takes level after level up to its limit; what the limit leaves is not taken.
    assert q.sweep("buy", 2, 80271.5) == (2, 2 * 80271.0)
    assert q.sweep("buy", 20, 80271.5) == (9, 3 * 80271.0 + 6 * 80271.5)
    assert q.sweep("buy", 5, 80270.5) == (0, 0.0) and q.sweep("sell", 9, 80000.0) == (7, 7 * 80269.5)
    # The slow channels do not step the live book back: an older top of book and a ticker's quotes are ignored.
    feed._on_message(None, json.dumps({"type": "l1_orderbook", "symbol": "BTCUSD", "last_sequence_no": 10,
                                       "best_bid": "80200", "best_ask": "80201"}))
    feed._handle_ticker({"type": "v2/ticker", "symbol": "BTCUSD", "mark_price": "80270.2", "close": 1,
                         "quotes": {"best_bid": "80200", "best_ask": "80201"}})
    q = feed.quote("BTCUSD")
    assert (q.bid, q.ask, q.mark, q.last, q.funding_rate) == (80269.5, 80271.0, 80270.2, 80263.0, 0.0068), q
    # A print newer than the book and past its touch: the levels it traded through are not there to take.
    feed._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD", "price": "80271.5"}))
    assert feed.quote("BTCUSD").sweep("buy", 20, 80273.0) == (15, 6 * 80271.5 + 9 * 80273.0)
    feed._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD", "price": "80272"}))
    assert feed.quote("BTCUSD").sweep("buy", 20, 80272.5) == (0, 0.0)
    # A lost update: the depth is dropped, a snapshot is asked for, and the once-a-second top stands in.
    class Socket:
        sent = []
        def send(self, text):
            self.sent.append(json.loads(text))
    feed._ws = Socket()
    depth(action="update", sequence_no=13, bids=[], asks=[["80271", "1"]])
    q = feed.quote("BTCUSD")
    assert q.asks == () and "BTCUSD" not in feed._books and [m["type"] for m in Socket.sent] == ["unsubscribe", "subscribe"]
    depth(action="update", sequence_no=14, bids=[], asks=[["80271", "1"]])           # ignored until the snapshot
    assert feed.quote("BTCUSD").asks == ()
    feed._on_message(None, json.dumps({"type": "l1_orderbook", "symbol": "BTCUSD", "last_sequence_no": 14,
                                       "best_bid": "80280", "best_ask": "80281"}))
    q = feed.quote("BTCUSD")
    assert (q.bid, q.ask) == (80280.0, 80281.0) and q.sweep("buy", 20, 80281.0) == (20, 20 * 80281.0)
    depth(action="snapshot", sequence_no=20, bids=[["80280", "5"]], asks=[["80281", "4"]])
    assert feed.quote("BTCUSD").asks == ((80281.0, 4),)
    # A top of book ahead of the live book means updates were lost without a gap showing: the same remedy.
    feed._on_message(None, json.dumps({"type": "l1_orderbook", "symbol": "BTCUSD", "last_sequence_no": 25,
                                       "best_bid": "80290", "best_ask": "80291"}))
    q = feed.quote("BTCUSD")
    assert (q.bid, q.ask, q.asks) == (80290.0, 80291.0, ()) and len(Socket.sent) == 4
    # Every tick wakes a waiter; a quiet feed times out with the same seq.
    seq = feed.seq
    assert feed.wait_for_tick(seq, 0.01) == seq
    threading.Timer(0.05, feed.publish, ("BTCUSD", 80_300.0)).start()
    started = time.monotonic()
    assert feed.wait_for_tick(seq, 5.0) > seq and time.monotonic() - started < 1.0
    # Stale quotes are not quotes.
    feed._quotes["BTCUSD"] = feed._quotes["BTCUSD"].__class__(1, 1, 1, 1, 0, time.monotonic() - 999)
    assert feed.quote("BTCUSD") is None and feed.price("BTCUSD") == 1
    # v3's tick filter: prints set `last` at once, but what is handed on is one SENSEX-like tick per closed second.
    sf = MarketFeed(["XAUTUSD"], settings=replace(FeedSettings(), tick_filter=True))
    assert MarketFeed(["XAUTUSD"], settings=FeedSettings())._smooth is None
    sf._handle_ticker({"type": "v2/ticker", "symbol": "XAUTUSD", "mark_price": "3900", "close": 3900})
    start_us = int(time.time() // 1 * 1e6) - 10_000_000         # ten seconds ago, at a second's start
    sf._on_message(None, json.dumps({"type": "all_trades", "symbol": "XAUTUSD", "price": "3900",
                                     "timestamp": start_us}))
    no, ticks = sf.trades_since(0)
    # The print came in after its second (and the next ones) had closed: every closed second since shows a tick,
    # one a second, a SENSEX second's wiggle around it, each stamped at its second's end.
    assert len(ticks) >= 8 and all(t[0] == "XAUTUSD" and abs(t[1] - 3900) < 1 for t in ticks), ticks
    stamps = [t[2] for t in ticks]                                  # a second whose 2-decimal tick repeats is skipped
    assert stamps[0] == start_us // 1000 + 1000 and all(b > a and b % 1000 == 0 for a, b in zip(stamps, stamps[1:]))
    for p in ("3900.03", "3901"):
        sf._on_message(None, json.dumps({"type": "all_trades", "symbol": "XAUTUSD", "price": p,
                                         "timestamp": int(time.time() * 1e6)}))
    assert sf.quote("XAUTUSD", max_age=1e9).last == 3901.0          # `last` is the raw print, at once
    _, more = sf.trades_since(no)                                   # nothing stamped before its second is over
    assert all(ticks[-1][2] < t[2] <= time.time() * 1000 for t in more), more
    print("feed self-check ok")
