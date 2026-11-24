"""A paper venue that charges what Delta Exchange India charges.

`PaperExchange` keeps a wallet, isolated-margin positions, a book of resting
orders and brackets, and a full ledger, and prices all of it off the live
Delta India feed. What it models, because leaving any of it out flatters the
account:

* **Limit orders only.** There is no market order. A limit that crosses the
  book fills as taker against the live book, level by level for its size - a
  buy from the best ask up, a sell from the best bid down (plus `slippage_bps`,
  default 0) - never through its limit and never at a price the market has
  since traded past; what the book within its limit cannot fill rests at the
  limit. A resting limit fills at its own price as maker once the opposite
  side of the book reaches it, or a trade prints through it (a print at it is
  not enough: the queue ahead of us is unknown). A post-only limit that would
  cross is refused. An order is matched `order_latency` after it is sent (the
  round trip a real one takes), against the book of that moment.
* **Fees per product** from the catalogue - taker or maker rate on notional -
  plus **18% GST** on the fee. Charged on every fill, entry and exit.
* **Leverage per symbol**, capped by the product's maximum and by Delta's
  margin scaling above `max_leverage_notional`. Isolated margin: position
  margin = entry notional / leverage, locked out of the available balance.
* **Order margin**: an order must be covered by order margin + estimated
  entry and exit fees (Delta's definition). A resting limit reserves it.
* **Funding** at every funding boundary of the product (1h/4h/8h, epoch
  aligned), at the live rate on the mark notional, debited from or credited to
  the position's margin.
* **Liquidation** when the mark crosses the liquidation price: the position is
  closed at the bankruptcy price and its margin is forfeited - Delta uses what
  is left between the two to pay the liquidation charge.
* **Brackets**: a stop-loss and/or take-profit per position, triggered on the
  mark price, closing the whole position with a stop-limit: a take-profit's
  limit is its trigger, a stop-loss's sits `stop_buffer_bps` past it. On firing
  it fills at the touch if that is inside its limit, and otherwise rests there.

The database is the account: every call re-reads the wallet, positions,
open orders and leverage from `paper_*`, `orders` and `leverage` before it
acts, and writes every change (and `fills`, `ledger`, `trades`) back before it
returns. The objects in memory are only that call's working copy. If a write
fails the copy is kept and retried on the next poll rather than lost. With no
database (tests, tools) the memory copy is all there is.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional

import charges
import ist

log = logging.getLogger("paper")

TEST = "TEST"
BOOK_CATCH_UP = 0.1                 # seconds an arriving order waits for the book to show a trade just printed
LONG, SHORT = "LONG", "SHORT"
BRACKET_TYPES = ("stop_loss_order", "take_profit_order")
#: Ledger kinds that are trading results, as opposed to money moved in or out.
RESULT_KINDS = ("fee", "fee_voucher", "realized_pnl", "funding", "liquidation")


class OrderRejected(Exception):
    """An order or account action the venue refused. The message is for the operator."""

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.code = code


@dataclass
class PaperPosition:
    symbol: str
    size: int = 0                    # signed contracts: + long, - short
    entry_price: float = 0.0
    margin: float = 0.0              # isolated margin currently locked
    leverage: float = 1.0
    realized_pnl: float = 0.0        # gross, over this position's life
    realized_funding: float = 0.0
    fees_paid: float = 0.0           # incl. GST
    open_fees: float = 0.0           # opening fees (incl. GST) of the contracts still held
    closed_contracts: int = 0
    closed_value: float = 0.0        # sum of exit price x contracts
    funding_at: Optional[int] = None  # last funding boundary accounted, epoch s
    opened_at: Optional[datetime] = None

    @property
    def direction(self) -> int:
        return 1 if self.size > 0 else -1


def _round(price: float, tick: Decimal, rounding) -> float:
    value = (Decimal(str(price)) / tick).to_integral_value(rounding=rounding) * tick
    return float(value)


class PaperExchange:
    """The simulated venue. Every public method is thread-safe."""

    mode = TEST

    def __init__(self, instruments, feed, settings, db=None, *, stop_buffer_bps: float = 50.0):
        self.instruments = instruments
        self.feed = feed
        self.settings = settings
        self.db = db
        self.stop_buffer_bps = float(stop_buffer_bps)
        self.balance = float(settings.start_equity)
        self.start_balance = self.balance
        self.opened_at = ist.now()
        self.positions: dict[str, PaperPosition] = {}
        #: Positions on symbols this run does not offer. Kept, never traded,
        #: still locking their margin - they are the account's, not ours to drop.
        self.foreign: dict[str, PaperPosition] = {}
        self.orders: dict[int, dict] = {}       # open and pending only
        #: No database only: orders that left the book, newest last, so
        #: `order_status` can say how one ended. With a database it asks `orders`.
        self._recent: dict[int, dict] = {}
        self.leverage: dict[str, float] = {}
        self._next_id = 1
        self._rested: dict[int, float] = {}     # order id -> when it came to rest (monotonic)
        self._swept: set[int] = set()           # part-filled takers still facing the levels they took
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Rows waiting to be written. Kept until a write succeeds.
        self._dirty_orders: dict[int, dict] = {}
        self._fills: list[tuple] = []
        self._ledger: list[tuple] = []
        self._trades: list[tuple] = []
        self._dirty_leverage: set[str] = set()
        self._dirty = False
        #: No database only: scalper-offer vouchers credited per IST month, for the
        #: cap. With a database the month's `fee_voucher` ledger rows are summed.
        self._vouchers: dict[str, float] = {}

    # ================================================================== #
    #  Persistence
    # ================================================================== #
    def load(self) -> bool:
        """Resume the account from the database. False if a new one was opened."""
        if self.db is None:
            return False
        with self._lock:
            rows = self.db.execute("SELECT * FROM deltabot.paper_account WHERE id = 1", fetch=True)
            if not rows:
                start, self.balance = self.balance, 0.0
                log.info("no paper account yet; opening one at $%.2f", start)
                self._book(None, "deposit", start, f"paper account opened at ${start:,.2f}")
                self._dirty = True
                self._flush()
                return False
            for order in self._read_state(rows[0]):
                self._cancel(order, "symbol no longer offered")
            for order in self.orders.values():
                if order.get("stop_order_type") in BRACKET_TYPES and order.get("limit_price") is None:
                    # A bracket from before every order was a limit: give it its stop-limit.
                    order["order_type"] = "limit_order"
                    order["limit_price"] = stop_limit_price(
                        order["side"], order["stop_order_type"], float(order["stop_price"]),
                        self.instruments[order["symbol"]], self.stop_buffer_bps)
                    self._touched(order)
            if self.foreign:
                log.critical("paper account holds %s, which this run does not offer. Their "
                             "margin stays locked; add the symbols back to trade them.",
                             ", ".join(f"{s} {p.size:+d}" for s, p in self.foreign.items()))
            log.info("paper account resumed: balance $%.2f, %d position(s), %d open order(s), "
                     "opened %s", self.balance, len(self.positions), len(self.orders),
                     self.opened_at.isoformat())
            self._flush()
            return True

    def _read_state(self, account: dict, cur=None) -> list[dict]:
        """Replace the working copy with the database's: wallet, positions, open
        orders, leverage. Returns the open orders of symbols this run does not offer."""
        def query(statement, params=None):
            if cur is None:
                return self.db.execute(statement, params, fetch=True) or []
            cur.execute(statement, params)
            return cur.fetchall()

        self.balance = float(account["balance"])
        self.start_balance = float(account["start_balance"])
        self.opened_at = ist.to_ist(account["opened_at"])
        self._next_id = int(account["next_order_id"])
        positions, foreign, orders, stray = {}, {}, {}, []
        for row in query("SELECT * FROM deltabot.paper_positions"):
            position = PaperPosition(
                symbol=row["symbol"], size=int(row["size"]),
                entry_price=float(row["entry_price"]), margin=float(row["margin"]),
                leverage=float(row["leverage"]), realized_pnl=float(row["realized_pnl"]),
                realized_funding=float(row["realized_funding"]),
                fees_paid=float(row["fees_paid"]),
                open_fees=float(row.get("open_fees") or 0.0),
                closed_contracts=int(row["closed_contracts"]),
                closed_value=float(row["closed_value"]),
                funding_at=int(row["funding_at"]) if row["funding_at"] is not None else None,
                opened_at=ist.to_ist(row["opened_at"]),
            )
            if position.size:
                (positions if position.symbol in self.instruments else foreign)[position.symbol] = position
        for row in query("SELECT * FROM deltabot.orders WHERE mode = %s AND state IN ('open', 'pending')",
                         (TEST,)):
            order = _order_from_row(row)
            if order["symbol"] in self.instruments:
                orders[order["id"]] = order
                self._next_id = max(self._next_id, order["id"] + 1)
            else:
                stray.append(order)
        self.positions, self.foreign, self.orders = positions, foreign, orders
        self.leverage = {row["symbol"]: float(row["leverage"]) for row in query(
            "SELECT symbol, leverage FROM deltabot.leverage WHERE mode = %s", (TEST,))}
        return stray

    @contextmanager
    def _state(self):
        """Hold the lock with the working copy freshly read from the database.
        Not while a write is pending (`_dirty`): then memory is newer, until it lands."""
        with self._lock:
            if self.db is not None and not self._dirty:
                with self.db.transaction() as cur:
                    cur.execute("SELECT * FROM deltabot.paper_account WHERE id = 1")
                    account = cur.fetchone()
                    if account is not None:
                        self._read_state(account, cur)
            yield

    def _flush(self) -> None:
        """Write everything pending in one transaction. Caller holds the lock."""
        if not self._dirty:
            return
        if self.db is None:
            self._clear_pending()
            return
        now = ist.now()
        try:
            with self.db.transaction() as cur:
                cur.execute(
                    """INSERT INTO deltabot.paper_account
                           (id, balance, start_balance, opened_at, next_order_id, updated_at)
                       VALUES (1, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET balance = EXCLUDED.balance,
                           start_balance = EXCLUDED.start_balance,
                           opened_at = EXCLUDED.opened_at,
                           next_order_id = EXCLUDED.next_order_id,
                           updated_at = EXCLUDED.updated_at""",
                    (self.balance, self.start_balance, self.opened_at, self._next_id, now))
                cur.execute("DELETE FROM deltabot.paper_positions")
                for p in [*self.positions.values(), *self.foreign.values()]:
                    if not p.size:
                        continue
                    cur.execute(
                        """INSERT INTO deltabot.paper_positions
                               (symbol, size, entry_price, margin, leverage, realized_pnl,
                                realized_funding, fees_paid, open_fees, closed_contracts,
                                closed_value, funding_at, opened_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (p.symbol, p.size, p.entry_price, p.margin, p.leverage, p.realized_pnl,
                         p.realized_funding, p.fees_paid, p.open_fees, p.closed_contracts,
                         p.closed_value,
                         p.funding_at, p.opened_at, now))
                for o in self._dirty_orders.values():
                    cur.execute(
                        """INSERT INTO deltabot.orders
                               (mode, order_id, client_order_id, symbol, side, size, unfilled_size,
                                order_type, limit_price, stop_order_type, stop_price, reduce_only,
                                post_only, leverage, reserved_margin, bracket_stop_loss,
                                bracket_take_profit, state, avg_fill_price, paid_commission,
                                note, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (mode, order_id) DO UPDATE SET
                               size = EXCLUDED.size, unfilled_size = EXCLUDED.unfilled_size,
                               side = EXCLUDED.side, order_type = EXCLUDED.order_type,
                               limit_price = EXCLUDED.limit_price,
                               stop_price = EXCLUDED.stop_price,
                               reserved_margin = EXCLUDED.reserved_margin,
                               state = EXCLUDED.state, avg_fill_price = EXCLUDED.avg_fill_price,
                               paid_commission = EXCLUDED.paid_commission,
                               note = EXCLUDED.note, updated_at = EXCLUDED.updated_at""",
                        (TEST, str(o["id"]), o.get("client_order_id"), o["symbol"], o["side"],
                         o["size"], o["unfilled_size"], o["order_type"], o.get("limit_price"),
                         o.get("stop_order_type"), o.get("stop_price"), bool(o.get("reduce_only")),
                         bool(o.get("post_only")), o.get("leverage"), o.get("reserved_margin"),
                         o.get("bracket_stop_loss"), o.get("bracket_take_profit"), o["state"],
                         o.get("avg_fill_price"), o.get("paid_commission"), o.get("note"),
                         o["created_at"], now))
                for row in self._fills:
                    cur.execute(
                        """INSERT INTO deltabot.fills
                               (mode, order_id, symbol, side, size, price, notional, role,
                                fee_rate, fee, gst, commission, realized_pnl, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (TEST, *row))
                for row in self._ledger:
                    cur.execute(
                        """INSERT INTO deltabot.ledger
                               (mode, symbol, kind, amount, balance, note, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""", (TEST, *row))
                for row in self._trades:
                    cur.execute(
                        """INSERT INTO deltabot.trades
                               (mode, symbol, direction, contracts, entry_price, exit_price,
                                leverage, gross_pnl, fees, funding, net_pnl, reason,
                                opened_at, closed_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (TEST, *row))
                for symbol in self._dirty_leverage:
                    cur.execute(
                        """INSERT INTO deltabot.leverage (mode, symbol, leverage, updated_at)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (mode, symbol) DO UPDATE
                               SET leverage = EXCLUDED.leverage, updated_at = EXCLUDED.updated_at""",
                        (TEST, symbol, self.leverage[symbol], now))
        except Exception as exc:  # noqa: BLE001 - retried on the next poll
            log.error("paper account write failed (will retry): %s", exc)
            return
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._dirty_orders.clear()
        self._fills.clear()
        self._ledger.clear()
        self._trades.clear()
        self._dirty_leverage.clear()
        self._dirty = False

    # ================================================================== #
    #  Lifecycle
    # ================================================================== #
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="paper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            self._flush()

    def _loop(self) -> None:
        while not self._stopping.wait(self.settings.poll_interval):
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 - the venue does not crash
                log.exception("paper venue poll failed: %s", exc)

    def poll(self, now: Optional[float] = None) -> None:
        """One pass: liquidations, funding, brackets, resting limits."""
        now = time.time() if now is None else now
        with self._state():
            try:
                for symbol in list(self.positions):
                    self._check_liquidation(symbol)
                for symbol in list(self.positions):
                    self._settle_funding(symbol, now)
                for order in sorted(self.orders.values(), key=lambda o: o["id"]):
                    if order["id"] not in self.orders:
                        continue                # cancelled by an earlier fill this pass
                    if order["state"] == "pending":
                        self._check_trigger(order)
                    elif order["state"] == "open":
                        self._check_limit(order)
            finally:
                self._flush()

    # ================================================================== #
    #  Small helpers (caller holds the lock)
    # ================================================================== #
    def _instrument(self, symbol: str):
        instrument = self.instruments.get(str(symbol).upper())
        if instrument is None:
            raise OrderRejected(f"{symbol} is not offered", "invalid_product")
        return instrument

    def _quote(self, symbol: str):
        quote = self.feed.quote(symbol)
        if quote is None:
            raise OrderRejected(f"no live quote for {symbol}; the feed is stale", "no_price")
        return quote

    def _crossing(self, instrument, side: str, limit: float, size: int) -> Optional[tuple[int, float]]:
        """What a limit for `size` takes on arrival, as taker: (contracts, their average price), level by level
        from the touch and never through its limit. None: it does not cross, and rests. A touch the market has
        since traded past is not there to take (`Quote.sweep`): a limit priced off it rests, as on the venue.
        With no book, the mark decides."""
        qty, value = self._quote(instrument.symbol).sweep(side, int(size), limit)
        if not qty:
            return None
        price = round(value / qty, 10)
        slip = self.settings.slippage_bps / 10_000.0
        if slip and side == "buy":
            price = min(_round(price * (1 + slip), instrument.tick_size, ROUND_CEILING), limit)
        elif slip:
            price = max(_round(price * (1 - slip), instrument.tick_size, ROUND_FLOOR), limit)
        return qty, price

    def _get_leverage(self, symbol: str) -> float:
        position = self.positions.get(symbol)
        if position is not None and position.size:
            return position.leverage            # an open position keeps its own
        instrument = self.instruments[symbol]
        chosen = self.leverage.get(symbol, self.settings.default_leverage)
        return max(1.0, min(chosen, instrument.max_leverage))

    def _used_margin(self) -> float:
        return (sum(p.margin for p in self.positions.values() if p.size)
                + sum(max(0.0, p.margin) for p in self.foreign.values() if p.size)
                + sum(o.get("reserved_margin") or 0.0 for o in self.orders.values()))

    def _available(self) -> float:
        return self.balance - self._used_margin()

    def _book(self, symbol: Optional[str], kind: str, amount: float, note: str = "") -> None:
        """Move the balance and write the movement down. The only place that does."""
        self.balance += amount
        self._ledger.append((symbol, kind, amount, self.balance, note or None, ist.now()))
        self._dirty = True

    def _touched(self, order: dict) -> None:
        order["updated_at"] = ist.now()
        self._dirty_orders[order["id"]] = order
        if self.db is None:
            self._recent.pop(order["id"], None)
            self._recent[order["id"]] = order
            while len(self._recent) > 1000:
                self._recent.pop(next(iter(self._recent)))
        self._dirty = True

    def _new_order(self, symbol, side, size, order_type, **fields) -> dict:
        order = {
            "id": self._next_id, "client_order_id": None, "symbol": symbol, "side": side,
            "size": int(size), "unfilled_size": int(size), "order_type": order_type,
            "limit_price": None, "stop_order_type": None, "stop_price": None,
            "reduce_only": False, "post_only": False, "leverage": None,
            "reserved_margin": 0.0, "bracket_stop_loss": None, "bracket_take_profit": None,
            "state": "open", "avg_fill_price": None, "paid_commission": None,
            "note": None, "created_at": ist.now(),
        }
        order.update(fields)
        self._next_id += 1
        self._dirty = True
        return order

    def _cancel(self, order: dict, note: Optional[str] = None) -> None:
        self._rested.pop(order["id"], None)
        self._swept.discard(order["id"])
        order["state"] = "cancelled"
        order["reserved_margin"] = 0.0
        if note:
            order["note"] = note
        self.orders.pop(order["id"], None)
        self._touched(order)

    def _position(self, symbol: str) -> PaperPosition:
        return self.positions.get(symbol) or PaperPosition(symbol)

    # ================================================================== #
    #  The fill - the one place a position changes
    # ================================================================== #
    def _fill(self, order: dict, price: float, role: str, reason: str, part: Optional[int] = None) -> None:
        """Fill `order` at `price`: all that is left of it, or only `part` contracts (the book had no more
        inside its limit) with the rest left resting. Every check runs before anything moves."""
        symbol = order["symbol"]
        instrument = self.instruments[symbol]
        position = self._position(symbol)
        current = position.size
        want = int(order["unfilled_size"])
        done = int(order["size"]) - want            # filled before this
        side = order["side"]

        if order.get("reduce_only"):
            # The venue trims a reduce-only order to what is there, and
            # cancels it if that is nothing.
            opposing = -current if side == "buy" else current
            want = min(want, max(0, opposing))
            if want <= 0:
                self._cancel(order, "reduce-only: no position left to reduce")
                return
        qty = want if part is None else min(want, int(part))
        signed = qty if side == "buy" else -qty
        closing = min(qty, abs(current)) if current and (current > 0) != (signed > 0) else 0
        opening = qty - closing

        cv = float(instrument.contract_value)
        rate = instrument.maker_fee if role == "maker" else instrument.taker_fee
        now = ist.now()
        scalp = bool(closing) and self.settings.scalper_offer and charges.scalper_free(
            symbol, position.opened_at, now)
        billed = opening if scalp else qty
        charge = charges.fee(billed * cv * price, rate, self.settings.gst_rate)
        commission = charge["total"]

        pnl = 0.0
        released = 0.0
        if closing:
            pnl = (price - position.entry_price) * closing * cv * position.direction
            released = position.margin * closing / abs(current)

        leverage = self._get_leverage(symbol)
        margin_add = 0.0
        if opening:
            open_notional = opening * cv * price
            same_way = closing == 0 and current != 0
            held_notional = abs(current) * cv * position.entry_price if same_way else 0.0
            after_notional = held_notional + open_notional
            after_size = abs(current) + opening if same_way else opening
            if 1.0 / leverage < instrument.im_fraction(after_notional) - 1e-12:
                raise OrderRejected(
                    f"{leverage:g}x is too high for a ${after_notional:,.2f} {symbol} position: "
                    f"Delta allows at most {instrument.leverage_cap(after_notional):.2f}x at that "
                    f"size. Lower the leverage or the size.", "leverage_too_high")
            if instrument.position_size_limit and after_size > instrument.position_size_limit:
                raise OrderRejected(
                    f"{after_size} contracts exceeds {symbol}'s position limit of "
                    f"{instrument.position_size_limit}", "position_size_limit")
            if (instrument.position_notional_limit
                    and after_notional > instrument.position_notional_limit):
                raise OrderRejected(
                    f"${after_notional:,.2f} exceeds {symbol}'s notional limit of "
                    f"${instrument.position_notional_limit:,.0f}", "position_notional_limit")
            margin_add = charges.position_margin(open_notional, leverage)
            exit_estimate = charges.fee(open_notional, instrument.taker_fee,
                                        self.settings.gst_rate)["total"]
            need = margin_add + commission + exit_estimate
            free = self._available() + (order.get("reserved_margin") or 0.0) + released + pnl
            if need > free + 1e-9:
                raise OrderRejected(
                    f"insufficient margin: need ${need:,.4f} (margin ${margin_add:,.4f} + fees "
                    f"${commission + exit_estimate:,.4f} incl. GST and estimated exit), "
                    f"available ${max(0.0, free):,.4f}", "insufficient_margin")

        # ---- nothing above this line has moved any money ----
        order["reserved_margin"] = 0.0
        open_share = commission if scalp else commission * opening / qty
        self._book(symbol, "fee", -commission,
                   f"{role} {rate * 100:.4f}% on ${billed * cv * price:,.4f} = ${charge['fee']:.6f}"
                   f" + GST ${charge['gst']:.6f}"
                   + (f"; closing {closing} free (scalper offer)" if scalp else ""))
        if closing:
            was = position.direction
            opening_fees = position.open_fees * closing / abs(current)
            position.open_fees -= opening_fees
            vouchers_on = getattr(self.settings, "scalper_vouchers", True)
            if scalp and vouchers_on and symbol in charges.SCALPER_VOUCHER_SYMBOLS:
                self._voucher(position, closing, opening_fees, now)
            position.margin -= released
            position.realized_pnl += pnl
            position.fees_paid += commission - open_share
            position.closed_contracts += closing
            position.closed_value += price * closing
            position.size -= position.direction * closing
            if pnl:
                self._book(symbol, "realized_pnl", pnl,
                           f"{closing} closed @ {price:.10g} vs entry {position.entry_price:.10g}")
            if position.size == 0:
                self._finish(position, reason, was)
                position = PaperPosition(symbol)
        if opening:
            direction = 1 if signed > 0 else -1
            if position.size == 0:
                position = PaperPosition(
                    symbol=symbol, size=direction * opening, entry_price=price,
                    margin=margin_add, leverage=leverage, fees_paid=open_share,
                    open_fees=open_share,
                    funding_at=self._boundary(instrument), opened_at=ist.now(),
                )
                if self.leverage.get(symbol) != leverage:
                    self.leverage[symbol] = leverage
                    self._dirty_leverage.add(symbol)
            else:
                total = abs(position.size) + opening
                position.entry_price = (position.entry_price * abs(position.size)
                                        + price * opening) / total
                position.size += direction * opening
                position.margin += margin_add
                position.fees_paid += open_share
                position.open_fees += open_share
            self.positions[symbol] = position
        elif position.size == 0:
            self.positions.pop(symbol, None)

        self._fills.append((str(order["id"]), symbol, side, qty, price, qty * cv * price, role,
                            rate, charge["fee"], charge["gst"], commission, pnl, ist.now()))
        if order.get("stop_order_type"):
            order["size"] = done + want         # a bracket closes whatever is held
        elif done + want < int(order["size"]):
            order["note"] = f"reduce-only trimmed to {done + want}"
        order["unfilled_size"] = want - qty
        order["avg_fill_price"] = price if not done else \
            ((order.get("avg_fill_price") or price) * done + price * qty) / (done + qty)
        order["paid_commission"] = (order.get("paid_commission") or 0.0) + commission if done else commission
        if want > qty:                          # the rest of it rests at its limit
            try:
                order["reserved_margin"] = self._reserve(order)
                self.orders[order["id"]] = order
                self._rested[order["id"]] = time.monotonic()
                if role == "taker":
                    self._swept.add(order["id"])   # it took all the book shows inside its limit
            except OrderRejected as exc:
                order["state"] = "cancelled"
                order["note"] = f"rest cancelled: {exc}"[:500]
                self.orders.pop(order["id"], None)
        else:
            order["state"] = "closed"
            self.orders.pop(order["id"], None)
            self._rested.pop(order["id"], None)
            self._swept.discard(order["id"])
        self._touched(order)
        log.info("[%s] paper %s fill %s %d @ %.10g (fee $%.6f incl. GST) -> %+d, balance $%.4f%s",
                 symbol, role, side, qty, price, commission,
                 self._position(symbol).size, self.balance,
                 f"; {want - qty} left {'resting' if order['state'] == 'open' else 'unfilled'} at "
                 f"{order['limit_price']:.10g}" if want > qty else "")

        if opening and (order.get("bracket_stop_loss") or order.get("bracket_take_profit")):
            try:
                self._set_bracket(symbol, order.get("bracket_stop_loss"),
                                  order.get("bracket_take_profit"))
            except OrderRejected as exc:
                log.warning("[%s] bracket from order %s not attached: %s", symbol, order["id"], exc)
                order["note"] = f"bracket not attached: {exc}"
                self._touched(order)

    def _vouchers_used(self, now) -> float:
        """Scalper-offer vouchers credited so far in `now`'s IST month: the ledger's
        `fee_voucher` rows, written and still waiting to be written."""
        if self.db is None:
            return self._vouchers.get(now.strftime("%Y-%m"), 0.0)
        month = ist.to_ist(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = self.db.execute(
            """SELECT COALESCE(SUM(amount), 0) AS amount FROM deltabot.ledger
               WHERE mode = %s AND kind = 'fee_voucher' AND created_at >= %s""",
            (TEST, month), fetch=True) or []
        pending = sum(r[2] for r in self._ledger if r[1] == "fee_voucher" and r[5] >= month)
        return (float(rows[0]["amount"]) if rows else 0.0) + pending

    def _voucher(self, position: PaperPosition, closing: int, opening_fees: float,
                 now) -> None:
        """Scalper offer: the opening fee of what was just closed comes back.

        Delta's examples quote it without GST, so that is all that comes back.
        """
        # ponytail: a voucher only pays future fees and lapses after 90 days;
        # credited as cash here, which is right for anyone who keeps trading.
        month = now.strftime("%Y-%m")
        used = self._vouchers_used(now)
        amount = min(opening_fees / (1 + self.settings.gst_rate), max(0.0, charges.SCALPER_VOUCHER_CAP - used))
        if amount <= 0:
            return
        if self.db is None:
            self._vouchers[month] = used + amount
        position.fees_paid -= amount
        self._book(position.symbol, "fee_voucher", amount,
                   f"scalper offer: opening fee of {closing} back as voucher"
                   f" (${used + amount:,.2f} of ${charges.SCALPER_VOUCHER_CAP:,.0f} this month)")

    def _finish(self, position: PaperPosition, reason: str, direction: int) -> None:
        """The position went flat: write its whole life down and drop its brackets.

        `direction` is passed in because by now `size` is zero and says nothing.
        """
        symbol = position.symbol
        exit_price = (position.closed_value / position.closed_contracts
                      if position.closed_contracts else None)
        net = position.realized_pnl - position.fees_paid + position.realized_funding
        self._trades.append((
            symbol, LONG if direction > 0 else SHORT, position.closed_contracts,
            position.entry_price, exit_price, position.leverage, position.realized_pnl,
            position.fees_paid, position.realized_funding, net, reason,
            position.opened_at or ist.now(), ist.now(),
        ))
        position.margin = 0.0
        for order in list(self.orders.values()):
            if order["symbol"] == symbol and order.get("stop_order_type") in BRACKET_TYPES:
                self._cancel(order, "position closed")
        log.info("[%s] position closed (%s): gross $%+.4f, fees $%.4f, funding $%+.4f, net $%+.4f",
                 symbol, reason, position.realized_pnl, position.fees_paid,
                 position.realized_funding, net)

    # ================================================================== #
    #  Orders
    # ================================================================== #
    def place_order(self, symbol: str, side: str, size, order_type: str = "limit_order", *,
                    limit_price=None, reduce_only: bool = False, post_only: bool = False,
                    stop_loss=None, take_profit=None, client_order_id: Optional[str] = None,
                    reason: str = "manual") -> dict:
        self._travel(str(symbol).upper(), str(side).lower())
        with self._state():
            try:
                return self._place(symbol, side, size, order_type, limit_price, reduce_only,
                                   post_only, stop_loss, take_profit, client_order_id, reason)
            finally:
                self._flush()

    def _travel(self, symbol: str, side: str) -> None:
        """An order's way to the venue: `order_latency`, while the book moves on. If a trade has just printed
        past the touch and the book has yet to show it, that book is not the one the order meets: wait for it
        (a moment; what is still unknown after that is settled against us, in `Quote.sweep`)."""
        if self.settings.order_latency <= 0:
            return
        time.sleep(self.settings.order_latency)
        deadline = time.monotonic() + BOOK_CATCH_UP
        while True:
            seq = self.feed.seq
            quote = self.feed.quote(symbol)
            left = deadline - time.monotonic()
            if quote is None or left <= 0 or not quote.behind(side):
                return
            self.feed.wait_for_tick(seq, left)

    def _place(self, symbol, side, size, order_type, limit_price, reduce_only, post_only,
               stop_loss, take_profit, client_order_id, reason) -> dict:
        instrument = self._instrument(symbol)
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
        _check_order_type(order_type)
        limit_price = _price(limit_price)
        if limit_price is None:
            raise OrderRejected("a limit order needs a positive limit price", "invalid_limit_price")
        limit_price = float(instrument.round_price(limit_price))
        stop_loss, take_profit = _price(stop_loss), _price(take_profit)

        quote = self._quote(symbol)
        current = self._position(symbol).size
        signed = size if side == "buy" else -size
        if reduce_only:
            opposing = -current if side == "buy" else current
            if opposing <= 0:
                raise OrderRejected(f"reduce-only {side} has no {symbol} position to reduce",
                                    "reduce_only_no_position")
            if stop_loss or take_profit:
                raise OrderRejected("brackets attach to an order that opens or adds, "
                                    "not a reduce-only one", "invalid_bracket")
        if stop_loss or take_profit:
            if current and (current > 0) != (signed > 0) and size <= abs(current):
                raise OrderRejected("this order only reduces the position; set the bracket "
                                    "on the position instead", "invalid_bracket")
            reference = limit_price
            _check_bracket(1 if signed > 0 else -1, reference, stop_loss, take_profit,
                           instrument)
            stop_loss = float(instrument.round_price(stop_loss)) if stop_loss else None
            take_profit = float(instrument.round_price(take_profit)) if take_profit else None

        order = self._new_order(
            symbol, side, size, order_type, limit_price=limit_price,
            reduce_only=bool(reduce_only), post_only=bool(post_only),
            leverage=self._get_leverage(symbol), client_order_id=client_order_id,
            bracket_stop_loss=stop_loss, bracket_take_profit=take_profit, note=reason,
        )

        try:
            hit = self._crossing(instrument, side, limit_price, size)
            if hit is not None:
                if post_only:
                    raise OrderRejected(
                        f"post-only {side} at {limit_price:.10g} would cross the book "
                        f"(best {'ask' if side == 'buy' else 'bid'} "
                        f"{(quote.ask if side == 'buy' else quote.bid) or quote.mark:.10g})",
                        "post_only_would_cross")
                self._fill(order, hit[1], "taker", reason, hit[0])
            else:
                order["reserved_margin"] = self._reserve(order)
                self.orders[order["id"]] = order
                self._rested[order["id"]] = time.monotonic()
                self._touched(order)
                log.info("[%s] paper limit %s %d @ %.10g resting (reserved $%.4f)",
                         symbol, side, size, limit_price, order["reserved_margin"])
        except OrderRejected as exc:
            order["state"] = "rejected"
            order["reserved_margin"] = 0.0
            order["note"] = str(exc)[:500]
            self.orders.pop(order["id"], None)
            self._touched(order)
            raise
        return _public(order)

    def _reserve(self, order: dict) -> float:
        """Margin a resting limit locks: order margin + estimated entry and exit fees."""
        symbol = order["symbol"]
        instrument = self.instruments[symbol]
        current = self._position(symbol).size
        if order.get("reduce_only"):
            return 0.0
        left = int(order["unfilled_size"])          # all of it, or what a part fill on arrival left
        quote = charges.order_quote(
            instrument, order["side"], left, order["limit_price"],
            self._get_leverage(symbol), position_size=current, gst_rate=self.settings.gst_rate)
        if not quote["opening_contracts"]:
            return 0.0
        cv = float(instrument.contract_value)
        signed = left if order["side"] == "buy" else -left
        same_way = current != 0 and (current > 0) == (signed > 0)
        after_notional = (abs(current) * cv * self._position(symbol).entry_price if same_way else 0.0) \
            + quote["opening_contracts"] * cv * order["limit_price"]
        leverage = self._get_leverage(symbol)
        if 1.0 / leverage < instrument.im_fraction(after_notional) - 1e-12:
            raise OrderRejected(
                f"{leverage:g}x is too high for a ${after_notional:,.2f} {symbol} position: Delta "
                f"allows at most {instrument.leverage_cap(after_notional):.2f}x at that size.",
                "leverage_too_high")
        reserve = quote["required"]
        if reserve > self._available() + 1e-9:
            raise OrderRejected(
                f"insufficient margin: this order reserves ${reserve:,.4f} (margin + estimated "
                f"fees incl. GST), available ${max(0.0, self._available()):,.4f}",
                "insufficient_margin")
        return reserve

    def cancel_order(self, order_id) -> dict:
        with self._state():
            try:
                order = self.orders.get(int(order_id))
            except (TypeError, ValueError):
                order = None
            if order is None:
                raise OrderRejected(f"order {order_id} is not open", "order_not_open")
            if order["state"] == "open":
                self._check_limit(order)        # a cancel does not beat a fill the book has already made
            if order["id"] in self.orders:
                self._cancel(order, "cancelled by operator")
            self._flush()
            return _public(order)

    def order_status(self, order_id) -> Optional[dict]:
        """One order as it stands now, on the book or not. None if unknown."""
        with self._state():
            try:
                key = int(order_id)
            except (TypeError, ValueError):
                return None
            order = self.orders.get(key) or self._dirty_orders.get(key) or self._recent.get(key)
            if order is None and self.db is not None:
                rows = self.db.execute("SELECT * FROM deltabot.orders WHERE mode = %s AND order_id = %s",
                                       (TEST, str(key)), fetch=True)
                order = _order_from_row(rows[0]) if rows else None
            return _public(order) if order else None

    # -- resting limits ---------------------------------------------------
    def _check_limit(self, order: dict) -> None:
        quote = self.feed.quote(order["symbol"])
        if quote is None:
            return
        limit, buy = order["limit_price"], order["side"] == "buy"
        # Only what happens after it came to rest can fill it (a restart forgets when: from now, then).
        since = self._rested.setdefault(order["id"], time.monotonic())
        current = quote.book_stamp >= quote.last_stamp     # a touch older than the last print proves nothing
        far = (quote.ask if buy else quote.bid)
        far = quote.mark if far is None else far
        reached = current and (far <= limit if buy else far >= limit)
        if order["id"] in self._swept:
            # The rest of an order that took every level inside its limit on arrival. This book is the venue's,
            # without our order: the levels it still shows there are the ones already taken. Not until the
            # book has stood clear of the limit once is its coming back new liquidity.
            if current and not reached:
                self._swept.discard(order["id"])
            reached = False
        if not reached and quote.last is not None and quote.last_stamp > since:
            reached = quote.last < limit if buy else quote.last > limit    # traded through: our queue place is unknown
        if not reached:
            return
        try:
            self._fill(order, limit, "maker", order.get("note") or "limit")
        except OrderRejected as exc:
            log.warning("[%s] resting limit %s cancelled at fill: %s", order["symbol"], order["id"], exc)
            self._cancel(order, f"cancelled at fill: {exc}")

    # ================================================================== #
    #  Brackets
    # ================================================================== #
    def set_bracket(self, symbol: str, stop_loss=None, take_profit=None) -> dict:
        with self._state():
            try:
                return self._set_bracket(self._instrument(symbol).symbol,
                                         _price(stop_loss), _price(take_profit))
            finally:
                self._flush()

    def _set_bracket(self, symbol, stop_loss, take_profit) -> dict:
        instrument = self.instruments[symbol]
        position = self.positions.get(symbol)
        if position is None or not position.size:
            raise OrderRejected(f"no open {symbol} position to bracket", "no_position")
        mark = self._quote(symbol).mark
        _check_bracket(position.direction, mark, stop_loss, take_profit, instrument)
        for order in list(self.orders.values()):
            if order["symbol"] == symbol and order.get("stop_order_type") in BRACKET_TYPES:
                self._cancel(order, "replaced by a new bracket")
        exit_side = "sell" if position.size > 0 else "buy"
        placed = {}
        for kind, price in (("stop_loss_order", stop_loss), ("take_profit_order", take_profit)):
            if not price:
                continue
            stop = float(instrument.round_price(price))
            order = self._new_order(
                symbol, exit_side, abs(position.size), "limit_order", state="pending",
                stop_order_type=kind, stop_price=stop,
                limit_price=stop_limit_price(exit_side, kind, stop, instrument, self.stop_buffer_bps),
                reduce_only=True, note="bracket")
            self.orders[order["id"]] = order
            self._touched(order)
            placed[kind] = _public(order)
        return {"symbol": symbol, "stop_loss": placed.get("stop_loss_order"),
                "take_profit": placed.get("take_profit_order")}

    def _check_trigger(self, order: dict) -> None:
        symbol = order["symbol"]
        position = self.positions.get(symbol)
        if position is None or not position.size:
            self._cancel(order, "no position")
            return
        quote = self.feed.quote(symbol)
        if quote is None:
            return
        mark, stop = quote.mark, order["stop_price"]
        stop_loss = order["stop_order_type"] == "stop_loss_order"
        if position.size > 0:
            fired = mark <= stop if stop_loss else mark >= stop
        else:
            fired = mark >= stop if stop_loss else mark <= stop
        if not fired:
            return
        log.info("[%s] %s triggered: mark %.10g vs %.10g", symbol, order["stop_order_type"], mark, stop)
        side = "sell" if position.size > 0 else "buy"
        order["side"] = side
        order["size"] = order["unfilled_size"] = abs(position.size)
        order["state"] = "open"
        order["note"] = "stop_loss" if stop_loss else "take_profit"
        self._touched(order)
        hit = self._crossing(self.instruments[symbol], side, order["limit_price"], order["size"])
        if hit is not None:
            self._fill(order, hit[1], "taker", order["note"], hit[0])
        # Otherwise (or for what the book inside its limit could not fill) the market is already past its
        # limit: it rests there, and fills as maker once the book comes back to it.

    # ================================================================== #
    #  Leverage
    # ================================================================== #
    def get_leverage(self, symbol: str) -> float:
        with self._state():
            return self._get_leverage(self._instrument(symbol).symbol)

    def set_leverage(self, symbol: str, leverage) -> dict:
        with self._state():
            try:
                return self._set_leverage(symbol, leverage)
            finally:
                self._flush()

    def _set_leverage(self, symbol, leverage) -> dict:
        instrument = self._instrument(symbol)
        symbol = instrument.symbol
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            raise OrderRejected("leverage must be a number", "invalid_leverage") from None
        if not math.isfinite(leverage) or leverage < 1 or leverage > instrument.max_leverage + 1e-9:
            raise OrderRejected(f"{symbol} leverage must be between 1x and "
                                f"{instrument.max_leverage:g}x", "invalid_leverage")
        if any(o["symbol"] == symbol and o["state"] == "open" and not o.get("reduce_only")
               for o in self.orders.values()):
            raise OrderRejected(f"cancel the open {symbol} orders before changing leverage",
                                "open_orders")
        position = self.positions.get(symbol)
        if position is not None and position.size:
            cv = float(instrument.contract_value)
            entry_notional = abs(position.size) * cv * position.entry_price
            if 1.0 / leverage < instrument.im_fraction(entry_notional) - 1e-12:
                raise OrderRejected(
                    f"{leverage:g}x is too high for this ${entry_notional:,.2f} position; at most "
                    f"{instrument.leverage_cap(entry_notional):.2f}x", "leverage_too_high")
            new_margin = entry_notional / leverage
            extra = new_margin - position.margin
            if extra > self._available() + 1e-9:
                raise OrderRejected(
                    f"insufficient margin: {leverage:g}x needs ${extra:,.4f} more margin, "
                    f"available ${max(0.0, self._available()):,.4f}", "insufficient_margin")
            mark = self._quote(symbol).mark
            liq = charges.liquidation_price(
                position.direction, position.entry_price, abs(position.size) * cv, new_margin,
                instrument.mm_fraction(entry_notional))
            if (position.direction > 0 and mark <= liq) or (position.direction < 0 and mark >= liq):
                raise OrderRejected(
                    f"{leverage:g}x would put the liquidation price at {liq:.10g}, past the mark "
                    f"{mark:.10g}", "would_liquidate")
            position.margin = new_margin
            position.leverage = leverage
        self.leverage[symbol] = leverage
        self._dirty_leverage.add(symbol)
        self._dirty = True
        log.info("[%s] paper leverage set to %gx", symbol, leverage)
        return {"symbol": symbol, "leverage": leverage}

    # ================================================================== #
    #  Funding and liquidation
    # ================================================================== #
    @staticmethod
    def _boundary(instrument, now: Optional[float] = None) -> int:
        interval = int(instrument.funding_interval)
        return int((time.time() if now is None else now) // interval) * interval

    def _settle_funding(self, symbol: str, now: float) -> None:
        position = self.positions.get(symbol)
        if position is None or not position.size:
            return
        instrument = self.instruments[symbol]
        boundary = self._boundary(instrument, now)
        if position.funding_at is None:
            position.funding_at = boundary
            self._dirty = True
            return
        if boundary <= position.funding_at:
            return
        quote = self.feed.quote(symbol)
        if quote is None or quote.funding_rate is None:
            return                          # settle once the rate is known again
        missed = (boundary - position.funding_at) // int(instrument.funding_interval)
        payment = missed * charges.funding_payment(
            position.size, float(instrument.contract_value), quote.mark, quote.funding_rate)
        position.funding_at = boundary
        position.margin += payment
        position.realized_funding += payment
        # ponytail: a boundary missed while the process was down is settled at
        # the rate now, not the historical one. Pull FUNDING:<symbol> candles if
        # downtime across funding ever matters.
        self._book(symbol, "funding", payment,
                   f"{quote.funding_rate:.6f}% on {position.size:+d} @ mark {quote.mark:.10g}"
                   + (f" x{missed} intervals (settled late)" if missed > 1 else ""))

    def liquidation_price(self, position: PaperPosition) -> float:
        instrument = self.instruments[position.symbol]
        cv = float(instrument.contract_value)
        entry_notional = abs(position.size) * cv * position.entry_price
        return charges.liquidation_price(
            position.direction, position.entry_price, abs(position.size) * cv, position.margin,
            instrument.mm_fraction(entry_notional))

    def _check_liquidation(self, symbol: str) -> None:
        position = self.positions.get(symbol)
        if position is None or not position.size:
            return
        quote = self.feed.quote(symbol)
        if quote is None:
            return
        liq = self.liquidation_price(position)
        mark = quote.mark
        if (position.direction > 0 and mark > liq) or (position.direction < 0 and mark < liq):
            return
        instrument = self.instruments[symbol]
        cv = float(instrument.contract_value)
        contracts = abs(position.size)
        forfeited = max(0.0, position.margin)
        bankrupt = charges.bankruptcy_price(position.direction, position.entry_price,
                                            contracts * cv, forfeited)
        # ponytail: single-shot liquidation. Delta liquidates positions above the
        # margin-scaling threshold incrementally; this closes them all at once.
        log.critical("[%s] PAPER LIQUIDATION: mark %.10g crossed liquidation %.10g; closed %d at "
                     "bankruptcy %.10g, margin $%.4f forfeited", symbol, mark, liq, contracts,
                     bankrupt, forfeited)
        position.realized_pnl -= forfeited
        position.closed_contracts += contracts
        position.closed_value += bankrupt * contracts
        was = position.direction
        position.margin = 0.0
        position.size = 0
        self._book(symbol, "liquidation", -forfeited,
                   f"mark {mark:.10g} crossed liquidation {liq:.10g}; {contracts} closed at "
                   f"bankruptcy {bankrupt:.10g}")
        self._fills.append(("liquidation", symbol, "sell" if position.direction > 0 else "buy",
                            contracts, bankrupt, contracts * cv * bankrupt, "liquidation",
                            0.0, 0.0, 0.0, 0.0, -forfeited, ist.now()))
        self._finish(position, "liquidated", was)
        self.positions.pop(symbol, None)

    # ================================================================== #
    #  Account
    # ================================================================== #
    def reset(self, equity=None, clear_history: bool = False) -> dict:
        """Throw the paper account away and open a new one. Operator only."""
        with self._state():
            amount = self.settings.start_equity if equity in (None, "") else _price(equity)
            if amount is None:
                raise OrderRejected("starting balance must be a positive number", "invalid_amount")
            for order in list(self.orders.values()):
                self._cancel(order, "paper account reset")
            dropped = {s: p.size for s, p in self.positions.items() if p.size}
            for position in list(self.positions.values()):
                if position.size:
                    self._finish(position, "paper_reset", position.direction)
            self.positions.clear()

            if clear_history:
                if self.db:
                    try:
                        with self.db.transaction() as cur:
                            cur.execute("DELETE FROM deltabot.paper_positions")
                            cur.execute("DELETE FROM deltabot.orders WHERE mode = %s", (TEST,))
                            cur.execute("DELETE FROM deltabot.fills WHERE mode = %s", (TEST,))
                            cur.execute("DELETE FROM deltabot.trades WHERE mode = %s", (TEST,))
                            cur.execute("DELETE FROM deltabot.ledger WHERE mode = %s", (TEST,))
                            cur.execute("DELETE FROM deltabot.algo_trades WHERE mode = %s", (TEST,))
                            cur.execute(
                                "UPDATE deltabot.paper_account SET balance = %s, start_balance = %s, opened_at = %s, next_order_id = 1 WHERE id = 1",
                                (amount, amount, ist.now()),
                            )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not clear database history on reset: %s", exc)
                self._recent.clear()
                self._dirty_orders.clear()
                self._ledger.clear()
                self._fills.clear()
                self._trades.clear()
                self._vouchers.clear()
                self._next_id = 1
                self.balance = 0.0
                self._book(None, "deposit", amount, f"paper account opened at ${amount:,.2f}")
            else:
                self._book(None, "reset", amount - self.balance,
                           f"paper account reset to ${amount:,.2f}"
                           + (f"; dropped {dropped}" if dropped else ""))

            self.start_balance = self.balance = float(amount)
            self.opened_at = ist.now()
            self._flush()
            log.warning("PAPER RESET: new account at $%.2f%s%s", amount,
                        f", positions dropped {dropped}" if dropped else "",
                        " (ledger cleared)" if clear_history else "")
            return self.account()

    def unrealised(self, position: PaperPosition) -> Optional[float]:
        mark = self.feed.price(position.symbol)
        if mark is None:
            return None
        cv = float(self.instruments[position.symbol].contract_value)
        return (mark - position.entry_price) * position.size * cv

    def account(self) -> dict:
        with self._state():
            unrealised = sum(self.unrealised(p) or 0.0 for p in self.positions.values())
            position_margin = sum(p.margin for p in self.positions.values()) + \
                sum(max(0.0, p.margin) for p in self.foreign.values())
            order_margin = sum(o.get("reserved_margin") or 0.0 for o in self.orders.values())
            equity = self.balance      # operator's order: realised only; an open trade's P&L counts once it closes
            return {
                "mode": TEST,
                "balance": self.balance,
                "equity": equity,
                "available": max(0.0, self._available()),
                "position_margin": position_margin,
                "order_margin": order_margin,
                "unrealised": unrealised,
                "start_balance": self.start_balance,
                "opened_at": self.opened_at.isoformat(),
                "return_pct": (100.0 * (equity / self.start_balance - 1.0)
                               if self.start_balance else 0.0),
            }

    def positions_view(self) -> list[dict]:
        with self._state():
            brackets: dict[str, dict] = {}
            for order in self.orders.values():
                if order.get("stop_order_type") in BRACKET_TYPES:
                    brackets.setdefault(order["symbol"], {})[order["stop_order_type"]] = order
            out = []
            for symbol, p in sorted(self.positions.items()):
                if not p.size:
                    continue
                instrument = self.instruments[symbol]
                cv = float(instrument.contract_value)
                mark = self.feed.price(symbol)
                quote = self.feed.quote(symbol)
                upnl = self.unrealised(p)
                b = brackets.get(symbol, {})
                out.append({
                    "symbol": symbol, "size": p.size,
                    "direction": LONG if p.size > 0 else SHORT,
                    "contracts": abs(p.size), "entry_price": p.entry_price, "mark": mark,
                    "notional": abs(p.size) * cv * (mark or p.entry_price),
                    "margin": p.margin, "leverage": p.leverage,
                    "liquidation_price": self.liquidation_price(p),
                    "bankruptcy_price": charges.bankruptcy_price(
                        p.direction, p.entry_price, abs(p.size) * cv, max(0.0, p.margin)),
                    "unrealised": upnl,
                    "roe_pct": (100.0 * upnl / p.margin) if upnl is not None and p.margin > 0 else None,
                    "realized_pnl": p.realized_pnl, "realized_funding": p.realized_funding,
                    "fees_paid": p.fees_paid,
                    "funding_rate": quote.funding_rate if quote else None,
                    "next_funding": (p.funding_at or self._boundary(instrument))
                    + int(instrument.funding_interval),
                    "stop_loss": (b.get("stop_loss_order") or {}).get("stop_price"),
                    "take_profit": (b.get("take_profit_order") or {}).get("stop_price"),
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                })
            return out

    def open_orders(self) -> list[dict]:
        with self._state():
            return [_public(o) for o in sorted(self.orders.values(), key=lambda o: -o["id"])]

    # -- history, straight from the database ----------------------------
    def history(self, table: str, limit: int = 100) -> list[dict]:
        if self.db is None:
            return []
        order_by = {"orders": "created_at", "fills": "created_at", "ledger": "created_at",
                    "trades": "closed_at"}[table]
        rows = self.db.execute(
            f"SELECT * FROM deltabot.{table} WHERE mode = %s "
            f"ORDER BY {order_by} DESC, id DESC LIMIT %s", (TEST, int(limit)), fetch=True) or []
        return [_jsonable(row) for row in rows]

    def pnl_summary(self) -> dict:
        """Realised results per window, off the ledger: P&L, fees, funding, liquidations."""
        if self.db is None:
            return {}
        today = ist.now().replace(hour=0, minute=0, second=0, microsecond=0)
        windows = {"1D": today, "7D": today - timedelta(days=6),
                   "30D": today - timedelta(days=29), "ALL": self.opened_at}
        out = {}
        for label, since in windows.items():
            since = max(since, self.opened_at)
            rows = self.db.execute(
                """SELECT kind, COALESCE(SUM(amount), 0) AS amount FROM deltabot.ledger
                   WHERE mode = %s AND created_at >= %s AND kind = ANY(%s) GROUP BY kind""",
                (TEST, since, list(RESULT_KINDS)), fetch=True) or []
            parts = {row["kind"]: float(row["amount"]) for row in rows}
            out[label] = {
                "pnl": parts.get("realized_pnl", 0.0) + parts.get("liquidation", 0.0),
                "fees": -parts.get("fee", 0.0) - parts.get("fee_voucher", 0.0),
                "funding": parts.get("funding", 0.0),
                "net": sum(parts.values()),
            }
        return out


# ---------------------------------------------------------------------- #
def _price(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OrderRejected(f"{value!r} is not a price", "invalid_price") from None
    if not math.isfinite(number) or number <= 0:
        raise OrderRejected(f"{value!r} is not a positive price", "invalid_price")
    return number


def _check_order_type(order_type: str) -> None:
    if order_type == "market_order":
        raise OrderRejected("market orders are disabled: every order is a limit order",
                            "market_disabled")
    if order_type != "limit_order":
        raise OrderRejected("order type must be limit_order", "invalid_order_type")


def stop_limit_price(side: str, kind: str, stop: float, instrument, buffer_bps: float) -> float:
    """The limit a fired bracket sends - never a market order.

    A take-profit's limit is its own trigger. A stop-loss's sits `buffer_bps`
    past the trigger, the way the market is moving, so a fast move still fills
    it; it never fills worse than that.
    """
    tick = instrument.tick_size
    if kind == "take_profit_order" or buffer_bps <= 0:
        return float(instrument.round_price(stop))
    if side == "sell":
        return max(float(tick), _round(stop * (1 - buffer_bps / 10_000.0), tick, ROUND_FLOOR))
    return _round(stop * (1 + buffer_bps / 10_000.0), tick, ROUND_CEILING)


def _check_bracket(direction: int, reference: float, stop_loss, take_profit, instrument) -> None:
    """A long's stop sits below the price and its target above; a short's the reverse."""
    if not stop_loss and not take_profit:
        return
    word = "long" if direction > 0 else "short"
    if stop_loss:
        if (direction > 0 and stop_loss >= reference) or (direction < 0 and stop_loss <= reference):
            raise OrderRejected(f"a {word}'s stop-loss must be {'below' if direction > 0 else 'above'} "
                                f"{reference:.10g}", "invalid_bracket")
    if take_profit:
        if (direction > 0 and take_profit <= reference) or (direction < 0 and take_profit >= reference):
            raise OrderRejected(f"a {word}'s take-profit must be {'above' if direction > 0 else 'below'} "
                                f"{reference:.10g}", "invalid_bracket")


def _jsonable(row) -> dict:
    out = {}
    for key, value in dict(row).items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _order_from_row(row: dict) -> dict:
    """An `orders` row as the venue's order dict: keyed by its venue id."""
    order = dict(row)
    order.pop("id", None)
    order["id"] = int(order["order_id"])
    order["created_at"] = ist.to_ist(order["created_at"])
    return order


def _public(order: dict) -> dict:
    out = _jsonable(order)
    out["order_id"] = str(order["id"])
    return out
