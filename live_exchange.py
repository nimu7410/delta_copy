"""The real venue, behind the same methods the paper venue offers.

`TRADE_MODE=LIVE` routes every console action here: orders, cancels, brackets
and leverage go to Delta Exchange India over the signed REST API, with real
money. The venue is the source of truth - balances, positions, liquidation
prices, fees and funding are read back from it rather than modelled - and
every order sent is also written to this system's `orders` table, accepted
or refused, so there is a local record of what the console did.

Reads are cached for a couple of seconds: the console polls every 1.5s and
the venue rate-limits per key.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from typing import Optional

import ist
from delta_login import DeltaError
from paper_engine import (BRACKET_TYPES, LONG, SHORT, OrderRejected, _check_bracket,
                          _check_order_type, _jsonable, _price, stop_limit_price)

log = logging.getLogger("live")

LIVE = "LIVE"


def _f(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stamp(value) -> Optional[str]:
    """Delta stamps some rows in microseconds and some in ISO; render both in IST."""
    if value in (None, ""):
        return None
    try:
        return ist.from_epoch(int(value) / 1_000_000).isoformat()
    except (TypeError, ValueError):
        return str(value)


class LiveExchange:
    mode = LIVE

    def __init__(self, client, instruments, feed, db=None, *, ttl: float = 2.0,
                 stop_buffer_bps: float = 50.0):
        self.client = client
        self.stop_buffer_bps = float(stop_buffer_bps)
        self.instruments = instruments
        self.by_product = {i.product_id: i for i in instruments.values()}
        self.feed = feed
        self.db = db
        self.ttl = ttl
        self._cache: dict[str, tuple[float, object]] = {}   # a few seconds of API answers
        #: No database only: the last leverage Delta reported per symbol. With a
        #: database it is the `leverage` table's LIVE row.
        self._leverage: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- lifecycle (the venue runs itself) --------------------------------
    def load(self) -> bool:
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    # -- plumbing ----------------------------------------------------------
    def _cached(self, key: str, fetch):
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        value = fetch()
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def _invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _instrument(self, symbol: str):
        instrument = self.instruments.get(str(symbol).upper())
        if instrument is None:
            raise OrderRejected(f"{symbol} is not offered", "invalid_product")
        return instrument

    def _call(self, verb: str, path: str, payload=None, params=None):
        try:
            if verb == "get":
                return self.client.get(path, params=params)
            return getattr(self.client, verb)(path, payload, params)
        except DeltaError as exc:
            raise OrderRejected(f"Delta refused: {exc}", exc.code or "venue_error") from exc
        finally:
            if verb != "get":
                self._invalidate()

    def _record(self, order: dict, *, state: str, note: Optional[str] = None,
                result: Optional[dict] = None) -> None:
        """Write what was sent, and what the venue said, to the local ledger."""
        if self.db is None:
            return
        result = result or {}
        try:
            self.db.execute(
                """INSERT INTO deltabot.orders
                       (mode, order_id, client_order_id, symbol, side, size, unfilled_size,
                        order_type, limit_price, stop_order_type, stop_price, reduce_only,
                        post_only, bracket_stop_loss, bracket_take_profit, state,
                        avg_fill_price, paid_commission, note, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s)
                   ON CONFLICT (mode, order_id) DO UPDATE SET state = EXCLUDED.state,
                       unfilled_size = EXCLUDED.unfilled_size,
                       avg_fill_price = EXCLUDED.avg_fill_price,
                       paid_commission = EXCLUDED.paid_commission,
                       note = EXCLUDED.note, updated_at = EXCLUDED.updated_at""",
                (LIVE, str(result.get("id") or order["client_order_id"]), order["client_order_id"],
                 order["symbol"], order["side"], order["size"],
                 result.get("unfilled_size", order["size"]), order["order_type"],
                 _f(order.get("limit_price")), order.get("stop_order_type"),
                 _f(order.get("stop_price")), bool(order.get("reduce_only")),
                 bool(order.get("post_only")), _f(order.get("bracket_stop_loss_price")),
                 _f(order.get("bracket_take_profit_price")), state,
                 _f(result.get("average_fill_price")), _f(result.get("paid_commission")),
                 note, ist.now(), ist.now()))
        except Exception as exc:  # noqa: BLE001 - the order is at the venue either way
            log.error("could not record LIVE order %s: %s", order["client_order_id"], exc)

    # -- orders ------------------------------------------------------------
    def place_order(self, symbol: str, side: str, size, order_type: str = "limit_order", *,
                    limit_price=None, reduce_only: bool = False, post_only: bool = False,
                    stop_loss=None, take_profit=None, client_order_id: Optional[str] = None,
                    reason: str = "manual") -> dict:
        instrument = self._instrument(symbol)
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
        limit = _price(limit_price)
        if limit is None:
            raise OrderRejected("a limit order needs a positive limit price", "invalid_limit_price")
        stop_loss, take_profit = _price(stop_loss), _price(take_profit)
        if (stop_loss or take_profit) and reduce_only:
            raise OrderRejected("brackets attach to an order that opens or adds, "
                                "not a reduce-only one", "invalid_bracket")

        payload = {
            "product_id": instrument.product_id, "size": size, "side": side,
            "order_type": "limit_order", "limit_price": instrument.round_price(limit),
            "time_in_force": "gtc",
            "client_order_id": client_order_id or f"manual-{uuid.uuid4().hex[:20]}",
        }
        if post_only:
            payload["post_only"] = True
        if reduce_only:
            payload["reduce_only"] = True
        if stop_loss or take_profit:
            _check_bracket(1 if side == "buy" else -1, float(payload["limit_price"]), stop_loss,
                           take_profit, instrument)
            exit_side = "sell" if side == "buy" else "buy"
            for kind, price, key in (("stop_loss_order", stop_loss, "bracket_stop_loss"),
                                     ("take_profit_order", take_profit, "bracket_take_profit")):
                if price:
                    stop = float(instrument.round_price(price))
                    payload[f"{key}_price"] = instrument.round_price(stop)
                    payload[f"{key}_limit_price"] = instrument.round_price(stop_limit_price(
                        exit_side, kind, stop, instrument, self.stop_buffer_bps))
            payload["bracket_stop_trigger_method"] = "mark_price"

        record = {**payload, "symbol": instrument.symbol}
        try:
            result = self._call("post", "/v2/orders", payload)
        except OrderRejected as exc:
            self._record(record, state="rejected", note=str(exc)[:500])
            raise
        self._record(record, state=str(result.get("state") or "open"), note=reason, result=result)
        log.info("[%s] LIVE %s %s %d -> %s (order %s)", instrument.symbol, order_type, side,
                 size, result.get("state"), result.get("id"))
        return _order_view(result, self.by_product)

    def cancel_order(self, order_id) -> dict:
        order = next((o for o in self.open_orders() if str(o["order_id"]) == str(order_id)), None)
        if order is None:
            raise OrderRejected(f"order {order_id} is not open", "order_not_open")
        result = self._call("delete", "/v2/orders",
                            {"id": int(order_id), "product_id": order["product_id"]})
        return _order_view(result, self.by_product)

    def order_status(self, order_id) -> Optional[dict]:
        """One order as the venue has it now, open or done. Never cached."""
        row = self._call("get", f"/v2/orders/{int(order_id)}")
        return _order_view(row, self.by_product) if row else None

    def _position_size(self, instrument) -> int:
        row = self._call("get", "/v2/positions", params={"product_id": instrument.product_id}) or {}
        return int(_f(row.get("size")) or 0)

    def set_bracket(self, symbol: str, stop_loss=None, take_profit=None) -> dict:
        instrument = self._instrument(symbol)
        stop_loss, take_profit = _price(stop_loss), _price(take_profit)
        size = self._position_size(instrument)
        if not size:
            raise OrderRejected(f"no open {instrument.symbol} position to bracket", "no_position")
        mark = self.feed.price(instrument.symbol)
        if mark is None:
            raise OrderRejected(f"no price for {instrument.symbol}", "no_price")
        _check_bracket(1 if size > 0 else -1, mark, stop_loss, take_profit, instrument)
        for order in self.open_orders():
            if order["symbol"] == instrument.symbol and order.get("stop_order_type") in BRACKET_TYPES:
                self._call("delete", "/v2/orders",
                           {"id": int(order["order_id"]), "product_id": instrument.product_id})
        if not stop_loss and not take_profit:
            return {"symbol": instrument.symbol, "stop_loss": None, "take_profit": None}
        payload = {"product_id": instrument.product_id, "bracket_stop_trigger_method": "mark_price"}
        exit_side = "sell" if size > 0 else "buy"
        for kind, price in (("stop_loss_order", stop_loss), ("take_profit_order", take_profit)):
            if price:
                stop = float(instrument.round_price(price))
                payload[kind] = {
                    "order_type": "limit_order", "stop_price": instrument.round_price(stop),
                    "limit_price": instrument.round_price(stop_limit_price(
                        exit_side, kind, stop, instrument, self.stop_buffer_bps)),
                }
        self._call("post", "/v2/orders/bracket", payload)
        return {"symbol": instrument.symbol, "stop_loss": stop_loss, "take_profit": take_profit}

    # -- leverage ----------------------------------------------------------
    def _remember_leverage(self, symbol: str, value: float) -> None:
        if self.db is None:
            self._leverage[symbol] = value
            return
        try:
            self.db.execute(
                """INSERT INTO deltabot.leverage (mode, symbol, leverage, updated_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (mode, symbol) DO UPDATE
                       SET leverage = EXCLUDED.leverage, updated_at = EXCLUDED.updated_at""",
                (LIVE, symbol, value))
        except Exception as exc:  # noqa: BLE001 - a display value; never stop a trade over it
            log.warning("could not save %s leverage: %s", symbol, exc)

    def _known_leverage(self, symbol: str) -> Optional[float]:
        if self.db is None:
            return self._leverage.get(symbol)
        try:
            rows = self.db.execute("SELECT leverage FROM deltabot.leverage WHERE mode = %s AND symbol = %s",
                                   (LIVE, symbol), fetch=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s leverage: %s", symbol, exc)
            return None
        return float(rows[0]["leverage"]) if rows else None

    def get_leverage(self, symbol: str) -> float:
        instrument = self._instrument(symbol)

        def fetch():
            row = self._call("get", f"/v2/products/{instrument.product_id}/orders/leverage") or {}
            value = _f(row.get("leverage")) or instrument.max_leverage
            self._remember_leverage(instrument.symbol, value)
            return value
        try:
            return self._cached(f"leverage:{instrument.symbol}", fetch)
        except OrderRejected as exc:
            log.warning("leverage read for %s failed: %s", instrument.symbol, exc)
            known = self._known_leverage(instrument.symbol)
            return known if known is not None else instrument.max_leverage

    def set_leverage(self, symbol: str, leverage) -> dict:
        instrument = self._instrument(symbol)
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            raise OrderRejected("leverage must be a number", "invalid_leverage") from None
        if not math.isfinite(leverage) or leverage < 1 or leverage > instrument.max_leverage + 1e-9:
            raise OrderRejected(f"{instrument.symbol} leverage must be between 1x and "
                                f"{instrument.max_leverage:g}x", "invalid_leverage")
        result = self._call("post", f"/v2/products/{instrument.product_id}/orders/leverage",
                            {"leverage": f"{leverage:g}"}) or {}
        value = _f(result.get("leverage")) or leverage
        self._remember_leverage(instrument.symbol, value)
        return {"symbol": instrument.symbol, "leverage": value,
                "order_margin": _f(result.get("order_margin"))}

    def reset(self, equity=None):
        raise OrderRejected("a LIVE account cannot be reset", "live_account")

    # -- reads -------------------------------------------------------------
    def _wallet(self) -> dict:
        def fetch():
            rows = self._call("get", "/v2/wallet/balances") or []
            return next((dict(r) for r in rows
                         if str(r.get("asset_symbol", "")).upper() == "USD"), {})
        return self._cached("wallet", fetch)

    def _positions(self) -> list[dict]:
        return self._cached("positions", lambda: self._call("get", "/v2/positions/margined") or [])

    def open_orders(self) -> list[dict]:
        rows = self._cached("orders", lambda: self._call(
            "get", "/v2/orders", params={"states": "open,pending", "page_size": 100}) or [])
        return [_order_view(r, self.by_product) for r in rows if int(r.get("product_id") or 0)
                in self.by_product]

    def account(self) -> dict:
        wallet = self._wallet()
        unrealised = sum(p["unrealised"] or 0.0 for p in self.positions_view())
        balance = _f(wallet.get("balance")) or 0.0
        return {
            "mode": LIVE,
            "balance": balance,
            "equity": balance,           # operator's order: realised only; an open trade's P&L counts once it closes
            "available": _f(wallet.get("available_balance")) or 0.0,
            "position_margin": _f(wallet.get("position_margin")) or 0.0,
            "order_margin": _f(wallet.get("order_margin")) or 0.0,
            "unrealised": unrealised,
            "start_balance": None,
            "opened_at": None,
            "return_pct": None,
        }

    def positions_view(self) -> list[dict]:
        brackets: dict[str, dict] = {}
        for order in self.open_orders():
            if order.get("stop_order_type") in BRACKET_TYPES:
                brackets.setdefault(order["symbol"], {})[order["stop_order_type"]] = order
        out = []
        for row in self._positions():
            instrument = self.by_product.get(int(row.get("product_id") or 0))
            size = int(_f(row.get("size")) or 0)
            if instrument is None or not size:
                continue
            symbol = instrument.symbol
            cv = float(instrument.contract_value)
            entry = _f(row.get("entry_price")) or 0.0
            mark = self.feed.price(symbol) or _f(row.get("mark_price"))
            margin = _f(row.get("margin")) or 0.0
            upnl = (mark - entry) * size * cv if mark else _f(row.get("unrealized_pnl"))
            quote = self.feed.quote(symbol)
            b = brackets.get(symbol, {})
            out.append({
                "symbol": symbol, "size": size, "direction": LONG if size > 0 else SHORT,
                "contracts": abs(size), "entry_price": entry, "mark": mark,
                "notional": abs(size) * cv * (mark or entry), "margin": margin,
                "leverage": self._known_leverage(symbol),
                "liquidation_price": _f(row.get("liquidation_price")),
                "bankruptcy_price": _f(row.get("bankruptcy_price")),
                "unrealised": upnl,
                "roe_pct": (100.0 * upnl / margin) if upnl is not None and margin > 0 else None,
                "realized_pnl": _f(row.get("realized_pnl")) or 0.0,
                "realized_funding": _f(row.get("realized_funding")) or 0.0,
                "fees_paid": None,
                "funding_rate": quote.funding_rate if quote else None,
                "next_funding": None,
                "stop_loss": (b.get("stop_loss_order") or {}).get("stop_price"),
                "take_profit": (b.get("take_profit_order") or {}).get("stop_price"),
                "opened_at": row.get("created_at"),
            })
        return out

    def history(self, table: str, limit: int = 100) -> list[dict]:
        limit = int(limit)
        if table == "fills":
            rows = self._cached(f"fills:{limit}", lambda: self._call(
                "get", "/v2/fills", params={"page_size": limit}) or [])
            out = []
            for r in rows:
                instrument = self.by_product.get(int(r.get("product_id") or 0))
                price, size = _f(r.get("price")) or 0.0, int(_f(r.get("size")) or 0)
                cv = float(instrument.contract_value) if instrument else 0.0
                out.append({
                    "created_at": _stamp(r.get("created_at")) if str(r.get("created_at", "")).isdigit()
                    else r.get("created_at"),
                    "symbol": r.get("product_symbol") or (instrument.symbol if instrument else None),
                    "side": r.get("side"), "size": size, "price": price,
                    "notional": size * cv * price, "role": r.get("role"),
                    "fee_rate": None, "fee": None, "gst": None,
                    "commission": _f(r.get("commission")), "realized_pnl": None,
                })
            return out
        if table == "ledger":
            rows = self._cached(f"ledger:{limit}", lambda: self._call(
                "get", "/v2/wallet/transactions", params={"page_size": limit}) or [])
            return [{"created_at": r.get("created_at"), "symbol": r.get("product_symbol"),
                     "kind": r.get("transaction_type"), "amount": _f(r.get("amount")),
                     "balance": _f(r.get("balance")), "note": None} for r in rows]
        if self.db is None:
            return []
        if table == "orders":
            rows = self.db.execute(
                "SELECT * FROM deltabot.orders WHERE mode = %s ORDER BY created_at DESC, id DESC "
                "LIMIT %s", (LIVE, limit), fetch=True) or []
            return [_jsonable(r) for r in rows]
        return []                             # trades: the venue keeps them, not this ledger

    def pnl_summary(self) -> dict:
        return {}


def _order_view(row: dict, by_product: dict) -> dict:
    instrument = by_product.get(int(row.get("product_id") or 0))
    return {
        "order_id": str(row.get("id")),
        "client_order_id": row.get("client_order_id"),
        "product_id": row.get("product_id"),
        "symbol": row.get("product_symbol") or (instrument.symbol if instrument else None),
        "side": row.get("side"),
        "size": int(_f(row.get("size")) or 0),
        "unfilled_size": int(_f(row.get("unfilled_size")) or 0),
        "order_type": row.get("order_type"),
        "limit_price": _f(row.get("limit_price")),
        "stop_order_type": row.get("stop_order_type"),
        "stop_price": _f(row.get("stop_price")),
        "reduce_only": bool(row.get("reduce_only")),
        "post_only": bool(row.get("post_only")),
        "state": row.get("state"),
        "avg_fill_price": _f(row.get("average_fill_price")),
        "paid_commission": _f(row.get("paid_commission")),
        "bracket_stop_loss": _f(row.get("bracket_stop_loss_price")),
        "bracket_take_profit": _f(row.get("bracket_take_profit_price")),
        "created_at": _stamp(row.get("created_at")),
        "note": row.get("cancellation_reason"),
    }
