"""Auto-priced limit orders: the operator says buy, sell or exit; this picks the price.

Every order it sends is a post-only limit - never a market order, never a
taker fill - at the best price a resting order can have:

* A buy sits at the best bid. When the spread is wider than one tick it steps
  one tick inside, so it is the best bid alone (first in the queue) and still
  below the ask. A sell is the mirror. `min(bid + tick, ask - tick)` is all of
  that, and on a locked book (bid == ask) it backs off a tick rather than cross.
* While it rests it is re-priced only when someone shows a better price on
  its side - the market moved away, or another order jumped ahead. An order at
  or ahead of the touch is left alone: moving it could only cost its place, and
  in LIVE the touch may be the order itself.
* A re-price is a cancel, then a new order for what is still unfilled. What
  filled in between is read off the venue's answer, so nothing is bought twice.
* An entry follows the market at most `max_drift_bps` from its first price,
  then stops: a limit-only console does not chase a runaway market. An exit
  follows until the position is flat.
* A stop-loss / take-profit asked for with an entry is attached to the
  position once the entry has filled. A target entry (`rr`) gets its prices
  worked out then, from the real fill and fees: +$win (and -$loss, if one is
  given) net, every charge included.

Orders it owns carry a `peg-` client order id, so a restart adopts the ones
still resting and keeps working them.

Only working orders are held in memory, and only while their order is live.
A finished one is written to `deltabot.pegs` and dropped; the console's list
of recent ones is read back from there.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional

import charges
import ist
from paper_engine import OrderRejected, _check_bracket, _price, _round

log = logging.getLogger("pegger")

PREFIX = "peg-"
DONE = ("closed", "cancelled", "rejected")
#: Refusals that say "not now", not "never": try again on the next tick.
RETRY = ("post_only", "would_cross", "no_price", "venue_error")


def _retryable(exc: OrderRejected) -> bool:
    return any(word in (exc.code or "") for word in RETRY)


def maker_price(side: str, bid, ask, tick: Decimal) -> Optional[float]:
    """The best price a post-only order can rest at, or None with no book."""
    if not bid or not ask:
        return None
    b, a = Decimal(str(_round(bid, tick, ROUND_FLOOR))), Decimal(str(_round(ask, tick, ROUND_CEILING)))
    price = min(b + tick, a - tick) if side == "buy" else max(a - tick, b + tick)
    return float(price) if price > 0 else None


@dataclass
class Peg:
    id: int
    symbol: str
    side: str
    size: int
    exit: bool
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    rr: Optional[tuple] = None          # (win, loss) USD net, loss 0 = no stop: priced at fill
    anchor: Optional[float] = None      # an entry's first price; drift is measured from it
    filled: int = 0
    filled_value: float = 0.0           # sum of price x contracts filled
    order_id: Optional[str] = None      # the venue order resting for it now
    order_size: int = 0
    price: Optional[float] = None
    placed_at: float = 0.0              # monotonic
    requotes: int = 0
    errors: int = 0
    state: str = "working"              # working | filled | cancelled | failed
    halting: Optional[tuple] = None     # a stop whose cancel has not landed yet: retried
    note: Optional[str] = None
    created_at: datetime = field(default_factory=ist.now)
    updated_at: datetime = field(default_factory=ist.now)

    @property
    def remaining(self) -> int:
        return max(0, self.size - self.filled)


class Pegger:
    """Works every auto-priced order. Every public method is thread-safe."""

    def __init__(self, exchange, feed, instruments, *, interval: float = 0.5,
                 requote_s: float = 1.0, max_drift_bps: float = 30.0, max_errors: int = 10,
                 keep: int = 50, gst_rate: float = charges.GST_RATE, db=None):
        self.exchange = exchange
        self.feed = feed
        self.instruments = instruments
        self.interval = interval
        self.requote_s = requote_s
        self.max_drift = max_drift_bps / 10_000.0
        self.max_errors = max_errors
        self.keep = keep
        self.gst_rate = gst_rate
        self.db = db
        self.mode = str(getattr(exchange, "mode", "TEST"))
        self._pegs: dict[int, Peg] = {}
        self._next = 1
        if db is not None:              # ids carry on from the last run's, so the list never repeats one
            rows = db.execute("SELECT coalesce(max(peg_id), 0) + 1 AS n FROM deltabot.pegs WHERE mode = %s",
                              (self.mode,), fetch=True)
            self._next = int(rows[0]["n"]) if rows else 1
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        try:
            self.adopt()
        except Exception as exc:  # noqa: BLE001 - the console still starts; orders stay resting
            log.error("could not adopt resting auto orders: %s", exc)
        self._thread = threading.Thread(target=self._loop, name="pegger", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop working. Resting orders stay on the book; the next start adopts them."""
        self._stopping.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        while not self._stopping.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the pegger does not die
                log.exception("pegger tick failed: %s", exc)

    def adopt(self) -> int:
        """Take over `peg-` orders a previous run left resting."""
        adopted = 0
        with self._lock:
            owned = {p.order_id for p in self._pegs.values()}
            for order in self.exchange.open_orders():
                if (not str(order.get("client_order_id") or "").startswith(PREFIX)
                        or order.get("stop_order_type") or order["order_id"] in owned
                        or order.get("symbol") not in self.instruments
                        or not order.get("limit_price") or order.get("state") != "open"):
                    continue
                reduce = bool(order.get("reduce_only"))
                limit = float(order["limit_price"])
                peg = self._new(order["symbol"], order["side"], int(order["size"]), reduce,
                                "exit" if reduce else "auto entry")
                peg.anchor = None if reduce else limit
                peg.order_id, peg.order_size = str(order["order_id"]), int(order["size"])
                peg.price, peg.placed_at = limit, time.monotonic()
                peg.note = "resumed after a restart"
                self._absorb(peg, order)
                adopted += 1
                log.warning("[%s] adopted resting auto order %s (%s %s)", peg.symbol,
                            peg.order_id, peg.side, order.get("unfilled_size"))
        return adopted

    # -- pricing ---------------------------------------------------------
    def target(self, symbol: str, side: str) -> Optional[float]:
        quote = self.feed.quote(symbol)
        if quote is None:
            return None
        return maker_price(side, quote.bid, quote.ask, self.instruments[symbol].tick_size)

    def _outbid(self, peg: Peg) -> bool:
        """Someone shows a better price on the order's side: it is no longer first."""
        quote = self.feed.quote(peg.symbol)
        if quote is None or peg.price is None:
            return False
        half = float(self.instruments[peg.symbol].tick_size) / 2
        if peg.side == "buy":
            return quote.bid is not None and quote.bid > peg.price + half
        return quote.ask is not None and quote.ask < peg.price - half

    def _drifted(self, peg: Peg, price: float) -> bool:
        if peg.exit or peg.anchor is None:
            return False
        if peg.side == "buy":
            return price > peg.anchor * (1 + self.max_drift) + 1e-12
        return price < peg.anchor * (1 - self.max_drift) - 1e-12

    # -- the operator's actions -----------------------------------------
    def submit(self, symbol: str, side: str, size, *, reduce_only: bool = False,
               stop_loss=None, take_profit=None, rr=None,
               reason: Optional[str] = None) -> dict:
        """`rr` = [win] or [win, loss] USD: a fresh entry whose take-profit nets +win
        (and stop -loss) after every charge, priced from the fill. Brackets given
        with it are refused."""
        instrument = self.instruments.get(str(symbol).upper())
        if instrument is None:
            raise OrderRejected(f"{symbol} is not offered", "invalid_product")
        symbol = instrument.symbol
        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise OrderRejected("side must be buy or sell", "invalid_side")
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise OrderRejected("size must be a whole number of contracts", "invalid_size") from None
        if size < 1:
            raise OrderRejected("size must be at least 1 contract", "invalid_size")
        stop_loss, take_profit = _price(stop_loss), _price(take_profit)
        with self._lock:
            price = self.target(symbol, side)
            if price is None:
                raise OrderRejected(f"no live bid/ask for {symbol}: nothing to price a limit "
                                    "against", "no_price")
            if rr:
                rr = self._check_rr(instrument, side, size, price, rr, reduce_only,
                                    stop_loss or take_profit)
            if stop_loss or take_profit:
                if reduce_only:
                    raise OrderRejected("brackets attach to an order that opens or adds, "
                                        "not a reduce-only one", "invalid_bracket")
                held = self._held(symbol)
                signed = size if side == "buy" else -size
                if held and (held > 0) != (signed > 0) and size <= abs(held):
                    raise OrderRejected("this order only reduces the position; set the bracket "
                                        "on the position instead", "invalid_bracket")
                _check_bracket(1 if side == "buy" else -1, price, stop_loss, take_profit, instrument)
            peg = self._new(symbol, side, size, bool(reduce_only),
                            reason or ("exit" if reduce_only else "auto entry"))
            peg.stop_loss, peg.take_profit = stop_loss, take_profit
            if rr:
                peg.rr = rr
                peg.reason = reason or f"+${peg.rr[0]:g} target entry"
            peg.anchor = None if reduce_only else price
            try:
                self._place(peg, price)
            except Exception:
                self._pegs.pop(peg.id, None)
                raise
            return self._view(peg)

    def exit(self, symbol: str) -> dict:
        """Flatten one position with an auto-priced reduce-only limit."""
        instrument = self.instruments.get(str(symbol).upper())
        if instrument is None:
            raise OrderRejected(f"{symbol} is not offered", "invalid_product")
        with self._lock:
            for peg in self._working(instrument.symbol):
                self._halt(peg, "replaced by an exit", attach=False)
            held = self._held(instrument.symbol)
            if not held:
                raise OrderRejected(f"no open {instrument.symbol} position", "no_position")
            return self.submit(instrument.symbol, "sell" if held > 0 else "buy", abs(held),
                               reduce_only=True, reason="exit")

    def exit_all(self) -> list[dict]:
        results = []
        for position in self.exchange.positions_view():
            try:
                results.append(self.exit(position["symbol"]))
            except OrderRejected as exc:
                results.append({"symbol": position["symbol"], "error": str(exc)})
        return results

    def cancel(self, peg_id) -> dict:
        with self._lock:
            try:
                peg = self._pegs.get(int(peg_id))
            except (TypeError, ValueError):
                peg = None
            if peg is None or peg.state != "working":
                raise OrderRejected(f"auto order {peg_id} is not working", "order_not_open")
            self._halt(peg, "cancelled by operator")
            return self._view(peg)

    def views(self) -> list[dict]:
        """Working orders, then the last `keep` finished ones (from `deltabot.pegs`), newest first."""
        with self._lock:
            out = [self._view(p) for p in self._pegs.values()]
        if self.db is not None and self.keep:
            try:
                rows = self.db.execute(
                    "SELECT view FROM deltabot.pegs WHERE mode = %s ORDER BY peg_id DESC LIMIT %s",
                    (self.mode, self.keep), fetch=True) or []
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read finished auto orders: %s", exc)
                rows = []
            shown = {v["peg_id"] for v in out}
            out += [r["view"] for r in rows if r["view"]["peg_id"] not in shown]
        return sorted(out, key=lambda v: -v["peg_id"])

    # -- the work --------------------------------------------------------
    def tick(self) -> None:
        with self._lock:
            for peg in [p for p in self._pegs.values() if p.state == "working"]:
                try:
                    self._step(peg)
                except Exception as exc:  # noqa: BLE001 - one bad order must not stall the rest
                    peg.errors += 1
                    peg.note = f"{type(exc).__name__}: {exc}"[:300]
                    log.exception("[%s] auto order %d step failed: %s", peg.symbol, peg.id, exc)
                    if peg.errors >= self.max_errors:
                        try:
                            self._halt(peg, f"stopped after {peg.errors} errors: {peg.note}",
                                       "failed")
                        except Exception as again:  # noqa: BLE001 - retried next tick
                            log.error("[%s] auto order %d could not be stopped: %s",
                                      peg.symbol, peg.id, again)
            self._prune()

    def _step(self, peg: Peg) -> None:
        if peg.halting:
            note, state, attach = peg.halting
            self._halt(peg, note, state, attach=attach)
            return
        if peg.order_id:
            order = self.exchange.order_status(peg.order_id)
            if order is None:
                # Never guess: re-sending could buy twice.
                self._finish(peg, "failed", f"lost track of order {peg.order_id}; check the venue")
                return
            if not self._absorb(peg, order):
                if not self._outbid(peg) or time.monotonic() - peg.placed_at < self.requote_s:
                    return
                price = self.target(peg.symbol, peg.side)
                if price is None:
                    return
                if self._drifted(peg, price):
                    self._halt(peg, self._drift_note(peg, price))
                    return
                if not self._pull(peg):
                    return
                peg.requotes += 1
        if self._complete(peg):
            return
        price = self.target(peg.symbol, peg.side)
        if price is None:
            return
        if self._drifted(peg, price):
            self._halt(peg, self._drift_note(peg, price))
            return
        try:
            self._place(peg, price)
        except OrderRejected as exc:
            self._halt(peg, f"refused: {exc}", "failed")

    def _place(self, peg: Peg, price: float) -> None:
        """Send one post-only limit for what is unfilled. A "not now" refusal
        leaves the order working with nothing resting; anything else raises."""
        try:
            order = self.exchange.place_order(
                peg.symbol, peg.side, peg.remaining, "limit_order", limit_price=price,
                reduce_only=peg.exit, post_only=True,
                client_order_id=f"{PREFIX}{peg.id}-{uuid.uuid4().hex[:12]}", reason=peg.reason)
        except OrderRejected as exc:
            if not _retryable(exc):
                raise
            if "post_only" not in (exc.code or "") and "cross" not in (exc.code or ""):
                peg.errors += 1
                if peg.errors >= self.max_errors:
                    raise
            peg.note = f"retrying: {exc}"[:300]
            peg.updated_at = ist.now()
            return
        peg.order_id, peg.order_size = str(order["order_id"]), int(order["size"])
        peg.price = float(order.get("limit_price") or price)
        peg.placed_at = time.monotonic()
        peg.errors = 0
        peg.note = None
        peg.updated_at = ist.now()
        log.info("[%s] auto %s %s %d @ %.10g (post-only, order %s)", peg.symbol,
                 "exit" if peg.exit else "entry", peg.side, peg.remaining, peg.price, peg.order_id)
        self._absorb(peg, order)                # a venue may answer with it already done
        self._complete(peg)

    def _absorb(self, peg: Peg, order: dict) -> bool:
        """Count what a finished venue order filled. True if it has finished."""
        if order.get("state") not in DONE:
            return False
        done = max(0, peg.order_size - int(order.get("unfilled_size") or 0))
        done = min(done, peg.remaining)
        if done:
            peg.filled += done
            peg.filled_value += done * float(order.get("avg_fill_price") or peg.price or 0.0)
            log.info("[%s] auto order %d filled %d @ %s (%d/%d)", peg.symbol, peg.id, done,
                     order.get("avg_fill_price"), peg.filled, peg.size)
        peg.order_id, peg.order_size = None, 0
        peg.updated_at = ist.now()
        return True

    def _pull(self, peg: Peg) -> bool:
        """Cancel the resting order and count its fills. False if it is still on the book."""
        try:
            order = self.exchange.cancel_order(peg.order_id)
        except OrderRejected:
            order = None                        # left the book on its own; read how
        if order is None or order.get("state") not in DONE:
            order = self.exchange.order_status(peg.order_id)
        if order is None:
            self._finish(peg, "failed", f"lost track of order {peg.order_id}; check the venue")
            return False
        return self._absorb(peg, order)

    def _complete(self, peg: Peg) -> bool:
        if peg.state != "working":
            return True
        if peg.remaining <= 0:
            self._finish(peg, "filled")
            return True
        if peg.exit and peg.order_id is None:
            held = self._held(peg.symbol)
            if (held <= 0) if peg.side == "sell" else (held >= 0):
                self._finish(peg, "filled", None if peg.filled else "position already flat")
                return True
        return False

    def _halt(self, peg: Peg, note: str, state: str = "cancelled", *, attach: bool = True) -> None:
        if peg.order_id and not self._pull(peg):
            if peg.state == "working":
                peg.halting = (note, state, attach)
                peg.note = f"stopping: order {peg.order_id} not cancelled yet; retrying"
            return
        peg.halting = None
        if peg.state != "working":
            return
        if peg.remaining <= 0:
            self._finish(peg, "filled", None, attach=attach)
        else:
            self._finish(peg, state, note, attach=attach)

    def _finish(self, peg: Peg, state: str, note: Optional[str] = None, *,
                attach: bool = True) -> None:
        peg.state = state
        peg.note = note
        peg.updated_at = ist.now()
        log.info("[%s] auto order %d %s: %d/%d filled%s", peg.symbol, peg.id, state, peg.filled,
                 peg.size, f" ({note})" if note else "")
        if attach and peg.filled and not peg.exit and (peg.stop_loss or peg.take_profit or peg.rr):
            try:
                if peg.rr:
                    peg.stop_loss, peg.take_profit, adjusted = self._rr_prices(peg)
                    if adjusted:
                        note = peg.note = ((note + "; ") if note else "") + adjusted
                        log.warning("[%s] 1:2 bracket for auto order %d adjusted: %s",
                                    peg.symbol, peg.id, adjusted)
                self.exchange.set_bracket(peg.symbol, peg.stop_loss, peg.take_profit)
            except OrderRejected as exc:
                peg.note = ((note + "; ") if note else "") + f"bracket not attached: {exc}"
                log.warning("[%s] bracket for auto order %d not attached: %s",
                            peg.symbol, peg.id, exc)

    # -- target brackets -------------------------------------------------
    def _check_rr(self, instrument, side, size, price, rr, reduce_only, bracket) -> tuple:
        """(win, loss) of a target entry, or refused if malformed; its prices are
        fitted at fill (`_rr_prices`)."""
        try:
            win = float(rr[0])
            loss = float(rr[1]) if len(rr) > 1 and rr[1] is not None else 0.0
        except (TypeError, ValueError, IndexError, KeyError):
            raise OrderRejected("rr must be [win] or [win, loss] in USD", "invalid_rr") from None
        if not (win > 0 and loss >= 0):
            raise OrderRejected("rr win must be above $0, loss $0 or more", "invalid_rr")
        if reduce_only or bracket:
            raise OrderRejected("a target entry sets its own bracket; not reduce-only, no SL / TP",
                                "invalid_rr")
        if self._held(instrument.symbol):
            # Its bracket would cover the whole position, so it would not net what it says.
            raise OrderRejected(f"a target entry opens a fresh position; {instrument.symbol} "
                                "already has one", "invalid_rr")
        return win, loss

    def _rr_prices(self, peg: Peg) -> tuple:
        """(stop_loss, take_profit, note) for the filled position, never refused:
        +win / -loss net where the venue allows it, otherwise moved to the nearest
        price it accepts - a stop a tick inside liquidation and on the right side
        of the mark, a target a tick past the mark - and the note says so."""
        position = next((p for p in self.exchange.positions_view()
                         if p["symbol"] == peg.symbol), None)
        if position is None:
            raise OrderRejected(f"no open {peg.symbol} position", "no_position")
        instrument = self.instruments[peg.symbol]
        tick = instrument.tick_size
        t = float(tick)
        direction = 1 if position["size"] > 0 else -1
        size = abs(int(position["size"]))
        entry = float(position["entry_price"])
        mark = float(position.get("mark") or entry)
        fees = position.get("fees_paid")
        if fees is None:                        # LIVE: Delta does not say; the entry was a maker fill
            fees = charges.fee(size * float(instrument.contract_value) * entry,
                               instrument.maker_fee, self.gst_rate)["total"]
        fees = float(fees)
        win, loss = peg.rr
        notes = []

        def target(net):
            try:
                return charges.target_price(direction, size, entry, fees, instrument, net,
                                            self.gst_rate, instrument.taker_fee)
            except ValueError:                  # no positive price nets that much
                return None

        sl, tp = (target(-loss) if loss else None), target(win)
        liq = position.get("liquidation_price") if loss else None
        if direction > 0:
            limit = mark - t                    # a long's stop sits below the mark ...
            if liq:
                floor = _round(float(liq) + t, tick, ROUND_CEILING)   # ... and above liquidation
                if sl is None or sl < floor:
                    sl = floor
                    notes.append(f"SL moved inside liquidation {float(liq):.10g}")
            if sl is None and loss:
                sl = t
            if sl is not None and sl > limit:
                sl = _round(limit, tick, ROUND_FLOOR)
                notes.append("SL moved below the mark")
            if sl is not None and (sl < t or (liq and sl <= float(liq))):
                sl = None
                notes.append("no room for a SL between liquidation and the mark")
            if tp is None or tp < mark + t:
                tp = _round(mark + t, tick, ROUND_CEILING)
                notes.append("TP moved above the mark")
        else:
            limit = mark + t
            if liq:
                ceiling = _round(float(liq) - t, tick, ROUND_FLOOR)
                if sl is None or sl > ceiling:
                    sl = ceiling
                    notes.append(f"SL moved inside liquidation {float(liq):.10g}")
            if sl is not None and sl < limit:
                sl = _round(limit, tick, ROUND_CEILING)
                notes.append("SL moved above the mark")
            if sl is not None and liq and sl >= float(liq):
                sl = None
                notes.append("no room for a SL between liquidation and the mark")
            if tp is None or tp > mark - t:
                tp = _round(mark - t, tick, ROUND_FLOOR) if mark - t >= t else None
                notes.append("TP moved below the mark")
            if tp is not None and tp < t:
                tp = None

        q = size * float(instrument.contract_value)
        exit_fee = instrument.taker_fee * (1 + self.gst_rate)

        def net(x):
            return direction * q * (x - entry) - fees - q * x * exit_fee

        if notes:
            notes.append(f"nets SL {'$%+.2f' % net(sl) if sl else 'none'} / "
                         f"TP {'$%+.2f' % net(tp) if tp else 'none'}")
        return sl, tp, "; ".join(notes) or None

    # -- small helpers ---------------------------------------------------
    def _new(self, symbol, side, size, exit_, reason) -> Peg:
        peg = Peg(self._next, symbol, side, size, exit_, reason)
        self._next += 1
        self._pegs[peg.id] = peg
        return peg

    def _working(self, symbol: str) -> list[Peg]:
        return [p for p in self._pegs.values() if p.symbol == symbol and p.state == "working"]

    def _held(self, symbol: str) -> int:
        return next((int(p["size"]) for p in self.exchange.positions_view()
                     if p["symbol"] == symbol), 0)

    def _drift_note(self, peg: Peg, price: float) -> str:
        return (f"market moved {abs(price / peg.anchor - 1) * 100:.2f}% from {peg.anchor:.10g}, "
                f"past the {self.max_drift * 100:.2f}% an entry follows; not chasing it")

    def _prune(self) -> None:
        done = sorted((p for p in self._pegs.values() if p.state != "working"), key=lambda p: p.id)
        if self.db is not None:
            for peg in done:            # written, then dropped; kept for a retry if the write fails
                try:
                    self.db.execute(
                        """INSERT INTO deltabot.pegs (mode, peg_id, symbol, state, view)
                           VALUES (%s, %s, %s, %s, %s::jsonb)
                           ON CONFLICT (mode, peg_id) DO UPDATE
                               SET state = EXCLUDED.state, view = EXCLUDED.view, finished_at = now()""",
                        (self.mode, peg.id, peg.symbol, peg.state, json.dumps(self._view(peg))))
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not save auto order %d: %s", peg.id, exc)
                    continue
                self._pegs.pop(peg.id, None)
            return
        for peg in done[:-self.keep] if self.keep else done:
            self._pegs.pop(peg.id, None)

    def _view(self, peg: Peg) -> dict:
        return {
            "peg_id": peg.id, "symbol": peg.symbol, "side": peg.side, "size": peg.size,
            "filled": peg.filled, "remaining": peg.remaining,
            "kind": "exit" if peg.exit else "entry", "price": peg.price,
            "order_id": peg.order_id, "state": peg.state, "note": peg.note,
            "requotes": peg.requotes,
            "avg_fill_price": peg.filled_value / peg.filled if peg.filled else None,
            "stop_loss": peg.stop_loss, "take_profit": peg.take_profit, "anchor": peg.anchor,
            "rr": list(peg.rr) if peg.rr else None,
            "created_at": peg.created_at.isoformat(), "updated_at": peg.updated_at.isoformat(),
        }
