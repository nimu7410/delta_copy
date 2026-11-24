"""The operator console: one HTML page, the snapshot it streams, and the order routes.

Every order is a limit order. One sent without a price is auto-priced by
`pegger` (the best maker price on the book, followed until it fills); exits
are always auto-priced.

Every trading action is a POST/DELETE with a JSON body. That is deliberate:
a browser will not send `application/json` cross-site without a CORS
preflight this server never answers, so another web page open in the same
browser cannot place or close orders through it.
"""

from __future__ import annotations

import contextlib
import gzip
import os
import logging
import statistics
import threading
import time
import zlib
from collections import deque
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import charges
import ist
import move_risk
from config import TICK_FILTER, VERSIONS, base_of, port_of
from tickfilter import SENSEX_COST_PCT
from data_handler import CHART_BARS, HISTORY_BARS, RESOLUTION_SECONDS
from paper_engine import OrderRejected, _price
from book_signal import WINDOWS as BOOK_WINDOWS
from ticks import WINDOWS

log = logging.getLogger("server")

HISTORY_TABLES = ("orders", "fills", "ledger", "trades")


class BrokerLatencyProbe:
    """Round-trip time from this machine to the Delta REST API.

    TTL-gated, and never queued: if a probe is in flight, a second caller
    takes the last sample rather than waiting behind the network.

    Every sample is a row in `deltabot.broker_pings` (kept for a day) and the
    snapshot is read back from there. With no database (tests) the samples
    are kept in a short list instead.
    """

    def __init__(self, probe, db=None, *, ttl_s: float = 5.0, history: int = 32) -> None:
        self._probe = probe
        self._db = db
        self._ttl = ttl_s
        self._history = history
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._samples: deque = deque(maxlen=history)   # (latency ms, error): no database only
        self._probed_at = -ttl_s                       # monotonic: when to probe again

    def _measure(self) -> None:
        started = time.perf_counter()
        latency_ms, error = None, None
        try:
            self._probe()
            latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        except Exception as exc:  # noqa: BLE001 - the probe never raises upward
            error = type(exc).__name__
        with self._lock:
            self._probed_at = time.monotonic()
            if self._db is None:
                self._samples.append((latency_ms, error))
                return
        try:
            self._db.execute(
                "DELETE FROM deltabot.broker_pings WHERE at < now() - interval '1 day'; "
                "INSERT INTO deltabot.broker_pings (latency_ms, error) VALUES (%s, %s)",
                (latency_ms, error))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record broker ping: %s", exc)

    def _recent(self) -> list:
        """The last `history` samples, newest last, as (latency ms, error)."""
        if self._db is None:
            with self._lock:
                return list(self._samples)
        try:
            rows = self._db.execute(
                "SELECT latency_ms, error FROM deltabot.broker_pings ORDER BY at DESC, id DESC LIMIT %s",
                (self._history,), fetch=True) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read broker pings: %s", exc)
            return []
        return [(r["latency_ms"], r["error"]) for r in reversed(rows)]

    def snapshot(self) -> dict:
        with self._lock:
            stale = (time.monotonic() - self._probed_at) >= self._ttl
        if stale and self._probe_lock.acquire(blocking=False):
            try:
                self._measure()
            finally:
                self._probe_lock.release()
        samples = self._recent()
        latency_ms, error = samples[-1] if samples else (None, None)
        good = [v for v, _ in samples if v is not None]
        return {
            "available": True, "ok": latency_ms is not None, "latency_ms": latency_ms,
            "error": error, "median_ms": round(statistics.median(good), 1) if good else None,
            "samples": len(samples), "failures": len(samples) - len(good),
        }


def _body() -> dict:
    if not request.is_json:
        raise OrderRejected("send the request as application/json", "json_required")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise OrderRejected("the request body must be a JSON object", "bad_json")
    return payload


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes", "on")


def _gzip_stream(chunks):
    """`chunks` as one gzip stream, each chunk flushed out whole the moment it is yielded."""
    deflate = zlib.compressobj(6, zlib.DEFLATED, 31)
    try:
        for chunk in chunks:
            if isinstance(chunk, str):
                chunk = chunk.encode()
            yield deflate.compress(chunk) + deflate.flush(zlib.Z_SYNC_FLUSH)
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def create_app(console) -> Flask:
    """`console` is `main.Console`: settings, exchange, feed, candles, instruments."""
    app = Flask(__name__)
    exchange = console.exchange
    pegger = console.pegger
    feed = console.feed
    instruments = console.instruments
    pnl_cache = {"at": 0.0, "value": {}}

    @app.after_request
    def _gzip(response):
        # Read from a phone over Tailscale on mobile data: the stream alone is ~90 KB/s raw (a full
        # snapshot per trade), ~60x less as one deflate stream flushed per event, so nothing is delayed.
        if (response.status_code != 200 or "Content-Encoding" in response.headers
                or "gzip" not in request.headers.get("Accept-Encoding", "")):
            return response
        if response.mimetype == "text/event-stream":
            response.response = _gzip_stream(response.response)
            response.headers.pop("Content-Length", None)
        else:
            response.direct_passthrough = False
            data = response.get_data()
            if len(data) < 1024:
                return response
            response.set_data(gzip.compress(data, 6))
        response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
        return response

    @app.errorhandler(OrderRejected)
    def rejected(exc):
        return jsonify({"error": str(exc), "code": exc.code}), 400

    @app.errorhandler(Exception)
    def failed(exc):
        if isinstance(exc, HTTPException):
            return exc                       # 404s and friends keep their status
        log.exception("console request failed: %s", exc)
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    def _instrument(symbol: str):
        instrument = instruments.get(symbol.upper())
        if instrument is None:
            raise OrderRejected(f"{symbol} is not offered", "invalid_product")
        return instrument

    if console.settings.version == "v1":   # every other version (its own process and database) on this port, at /vN/
        import requests
        from flask import redirect, stream_with_context

        hop = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-encoding",
               "content-length"}

        def proxy(version, sub=""):
            url = f"http://127.0.0.1:{port_of(version)}/{version}/{sub}" + (
                f"?{request.query_string.decode()}" if request.query_string else "")
            stream = sub == "api/stream"
            try:
                r = requests.request(request.method, url, data=request.get_data(), stream=True,
                                     headers={k: v for k, v in request.headers.items()
                                              if k.lower() in ("content-type", "accept", "cache-control", "last-event-id")}
                                     | {"Accept-Encoding": "identity"},   # compressed once, by this app's _gzip
                                     timeout=(3, None if stream else 30), allow_redirects=False)
            except requests.RequestException as exc:
                return jsonify({"error": f"{version.upper()} is not reachable: {exc}", "code": "version_down"}), 502

            def body():
                try:
                    yield from r.iter_content(chunk_size=None)
                finally:
                    r.close()
            return Response(stream_with_context(body()), status=r.status_code,
                            headers=[(k, v) for k, v in r.headers.items() if k.lower() not in hop])

        methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
        for v in VERSIONS:
            if v == "v1":
                continue
            app.add_url_rule(f"/{v}", f"{v}_root", lambda v=v: redirect(f"/{v}/"))
            app.add_url_rule(f"/{v}/", f"{v}_page", lambda v=v: proxy(v), methods=methods)
            app.add_url_rule(f"/{v}/<path:sub>", f"{v}_proxy", lambda sub, v=v: proxy(v, sub), methods=methods)

    @app.route("/")
    def page():
        version = console.settings.version
        side = {k: "long" if v > 0 else "short" for k, v in VERSIONS[version].items()}
        return render_template(
            "index.html", mode=exchange.mode, poll_ms=console.settings.server.poll_ms,
            symbols=sorted(instruments), resolutions=list(RESOLUTION_SECONDS),
            version=version, hh_side=side["HH"], ll_side=side["LL"],
            xcost=version in TICK_FILTER, sensex_cost_pct=SENSEX_COST_PCT,
            versions=[(v, base_of(v) + "/") for v in VERSIONS],
        )

    def _snapshot() -> dict:
        now = time.monotonic()
        if now - pnl_cache["at"] > 5.0:
            try:
                pnl_cache["value"] = exchange.pnl_summary()
            except Exception as exc:  # noqa: BLE001
                log.error("pnl summary failed: %s", exc)
            pnl_cache["at"] = now
        quotes = {}
        for symbol in instruments:
            q = feed.quote(symbol, max_age=float("inf"))
            quotes[symbol] = None if q is None else {
                "mark": q.mark, "bid": q.bid, "ask": q.ask, "last": q.last,
                "funding_rate": q.funding_rate, "stale": feed.quote(symbol) is None,
            }
        return {
            "as_of": ist.isoformat(),
            "uptime_s": round(time.time() - console.started_at, 1),
            "mode": exchange.mode,
            "database": console.db.name if console.db else None,
            "feed": feed.status(),
            "account": exchange.account(),
            "positions": exchange.positions_view(),
            "orders": exchange.open_orders(),
            "pegs": pegger.views(),
            "quotes": quotes,
            "pnl": pnl_cache["value"],
            "algo": _algo_status(),
        }

    algo_cache = {"at": 0.0, "value": None}

    def _algo_status():
        """The strategy's status, live on every call; its database part is re-read at most once a second."""
        algo = getattr(console, "algo", None)
        if algo is None:
            return None
        now = time.monotonic()
        fresh = now - algo_cache["at"] > 1.0
        if fresh:
            algo_cache["at"] = now
        try:
            algo_cache["value"] = algo.status(cached_db=not fresh)
        except Exception as exc:  # noqa: BLE001
            log.error("algo status failed: %s", exc)
        return algo_cache["value"]

    @app.route("/api/snapshot")
    def snapshot():
        return jsonify(_snapshot())

    @app.route("/api/stream")
    def stream():
        """Server-sent events, on every feed event: each trade goes out as a
        `trades` event the moment it lands, then the snapshot (board, account,
        positions, strategy). A quiet feed still sends one every few seconds so
        the page sees it go stale."""
        def events():
            seq, trade_no = -1, feed.trade_no
            while True:
                seq = feed.wait_for_tick(seq, timeout=5.0)
                trade_no, trades = feed.trades_since(trade_no)
                if trades:
                    yield f"event: trades\ndata: {app.json.dumps(trades)}\n\n"
                try:
                    data = app.json.dumps(_snapshot())
                except Exception as exc:  # noqa: BLE001 - end it; the browser reconnects
                    log.exception("snapshot stream failed: %s", exc)
                    return
                yield f"data: {data}\n\n"

        return Response(events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.route("/api/instruments")
    def instrument_specs():
        out = {}
        for symbol, instrument in sorted(instruments.items()):
            spec = instrument.spec()
            spec["gst_rate"] = console.settings.paper.gst_rate
            try:
                spec["leverage"] = exchange.get_leverage(symbol)
            except OrderRejected as exc:
                spec["leverage"], spec["leverage_error"] = None, str(exc)
            out[symbol] = spec
        return jsonify(out)

    @app.route("/api/candles/<symbol>")
    def candles(symbol: str):
        instrument = _instrument(symbol)
        resolution = request.args.get("resolution", "5m")
        if resolution not in RESOLUTION_SECONDS:
            raise OrderRejected(f"unsupported resolution {resolution}", "invalid_resolution")
        bars = min(max(request.args.get("bars", CHART_BARS, type=int), 1), HISTORY_BARS)
        return jsonify({"symbol": instrument.symbol, "resolution": resolution,
                        "candles": console.candles.candles(instrument.symbol, resolution, bars)})

    @app.route("/api/ticks/<symbol>")
    def ticks(symbol: str):
        """The Live chart's history over `window` (15m … 1d), as [price, epoch ms]: the raw trades."""
        instrument = _instrument(symbol)
        window = request.args.get("window", "15m")
        if window not in WINDOWS:
            raise OrderRejected(f"unsupported window {window}", "invalid_window")
        recorder = getattr(console, "ticks", None)
        return jsonify({"symbol": instrument.symbol, "window": window,
                        "ticks": recorder.recent(instrument.symbol, WINDOWS[window]) if recorder else []})

    @app.route("/api/signal/<symbol>")
    def book_signal(symbol: str):
        """LONG / SHORT / WAIT for `symbol`, from its order book and trade flow."""
        instrument = _instrument(symbol)
        source = getattr(console, "signal", None)
        if source is None:
            return jsonify({"symbol": instrument.symbol, "ready": False, "connected": False})
        return jsonify(source.read(instrument.symbol))

    @app.route("/api/momentum/<symbol>")
    def momentum(symbol: str):
        """LONG / SHORT / WAIT for `symbol` from its last `minutes` (1 ... 60) of trades."""
        instrument = _instrument(symbol)
        try:
            minutes = int(request.args.get("minutes", "1"))
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= 60:
            raise OrderRejected("minutes must be 1 to 60", "invalid_window")
        source = getattr(console, "momentum", None)
        if source is None:
            return jsonify({"symbol": instrument.symbol, "window_s": minutes * 60, "trades": 0, "ready": False})
        return jsonify(source.read(instrument.symbol, minutes * 60))

    @app.route("/api/algo")
    def algo_status():
        """The HH/LL strategy: per-symbol threshold, last pivot, phase, halt reason, and its trades."""
        algo = getattr(console, "algo", None)
        return jsonify(algo.status() if algo else {"strategy": None, "status": "not_running"})

    def _algo_setting(setter: str, key: str):
        algo = getattr(console, "algo", None)
        if algo is None:
            raise OrderRejected("the strategy is not running", "not_running")
        body = _body()
        try:
            value = getattr(algo, setter)(body.get("symbol", ""), body.get(key))
        except ValueError as exc:
            raise OrderRejected(str(exc), f"invalid_{key}") from None
        algo_cache["at"] = 0.0
        return jsonify({"ok": True, "symbol": str(body.get("symbol", "")).upper(), key: value})

    @app.route("/api/algo/threshold", methods=["POST"])
    def algo_threshold():
        """The HH/LL threshold for a symbol, in % of the extreme (v3: x SENSEX's cost): {"symbol": "XAUTUSD",
        "threshold": 0.05}."""
        return _algo_setting("set_threshold", "threshold")

    @app.route("/api/algo/pivots/<symbol>")
    def algo_pivots(symbol: str):
        """The HH/LL pivots behind the chart's markers: [{type, price, at (epoch ms of the extreme)}], oldest first."""
        algo = getattr(console, "algo", None)
        return jsonify({"symbol": symbol.upper(), "pivots": algo.pivot_list(symbol) if algo else [],
                        "smooth_pivots": algo.smooth_pivot_list(symbol) if algo else []})

    @app.route("/api/algo/smooth", methods=["POST"])
    def algo_smooth():
        """The smoothed line's threshold for a symbol, % of price: {"symbol": "XAUTUSD", "smooth": 0.02}. Its line and
        pivots are redrawn with it; nothing is traded on them."""
        return _algo_setting("set_smooth", "smooth")

    @app.route("/api/smooth/<symbol>")
    def smooth_line(symbol: str):
        """The Live chart's smoothed line over `window`: {pct, points: [[epoch ms, value]], state, fed}."""
        instrument = _instrument(symbol)
        window = request.args.get("window", "15m")
        if window not in WINDOWS:
            raise OrderRejected(f"unsupported window {window}", "invalid_window")
        algo = getattr(console, "algo", None)
        if algo is None:
            return jsonify({"symbol": instrument.symbol, "pct": None, "points": [], "state": None, "fed": None})
        return jsonify(algo.smooth_series(instrument.symbol, WINDOWS[window]))

    @app.route("/api/algo/trades")
    def algo_trades():
        algo = getattr(console, "algo", None)
        if algo is None:
            return jsonify({"trades": []})
        return jsonify({"trades": algo.trades(symbol=request.args.get("symbol"),
                                              limit=int(request.args.get("limit", 50)))})

    @app.route("/api/book/<symbol>")
    def book_history(symbol: str):
        """`symbol`'s recorded order book and flow over `window` (5m … 1h)."""
        instrument = _instrument(symbol)
        window = request.args.get("window", "15m")
        if window not in BOOK_WINDOWS:
            raise OrderRejected(f"unsupported window {window}", "invalid_window")
        recorder = getattr(console, "book", None)
        source = getattr(console, "signal", None)
        if source is not None:
            source.read(instrument.symbol)  # watched, so its book keeps being recorded
        if recorder is None:
            return jsonify({"symbol": instrument.symbol, "seconds": BOOK_WINDOWS[window], "recorded_s": 0})
        return jsonify(recorder.summary(instrument.symbol, BOOK_WINDOWS[window]))

    @app.route("/api/quote", methods=["POST"])
    def quote():
        """What an order would cost - the ticket's live preview. Nothing is sent."""
        body = _body()
        instrument = _instrument(str(body.get("symbol", "")))
        side = str(body.get("side", "buy")).lower()
        if side not in ("buy", "sell"):
            raise OrderRejected("side must be buy or sell", "invalid_side")
        try:
            size = int(body.get("size") or 0)
        except (TypeError, ValueError):
            raise OrderRejected("size must be a whole number of contracts", "invalid_size") from None
        if size < 1:
            raise OrderRejected("size must be at least 1 contract", "invalid_size")
        q = feed.quote(instrument.symbol)
        if q is None:
            raise OrderRejected(f"no live quote for {instrument.symbol}", "no_price")
        limit = _price(body.get("limit_price"))
        auto = limit is None
        if auto:
            price = pegger.target(instrument.symbol, side)
            if price is None:
                raise OrderRejected(f"no live bid/ask for {instrument.symbol}", "no_price")
            maker = True
        else:
            price = float(instrument.round_price(limit))
            touch = q.ask if side == "buy" else q.bid
            crosses = touch is not None and (touch <= price if side == "buy" else touch >= price)
            maker = not crosses
            if crosses:
                price = min(price, touch) if side == "buy" else max(price, touch)
        position = next((p for p in exchange.positions_view()
                         if p["symbol"] == instrument.symbol), {})
        held = position.get("size") or 0
        try:
            opened = ist.to_ist(datetime.fromisoformat(str(position["opened_at"])))
        except (KeyError, TypeError, ValueError):
            opened = None                   # a live position whose open time Delta did not give
        scalp = console.settings.paper.scalper_offer and charges.scalper_free(
            instrument.symbol, opened, ist.now())
        leverage = exchange.get_leverage(instrument.symbol)
        result = charges.order_quote(
            instrument, side, size, price, leverage, position_size=held, maker=maker,
            gst_rate=console.settings.paper.gst_rate, closing_free=scalp)
        candles = console.candles.candles(instrument.symbol, "1m", move_risk.BARS) if console.candles else []
        result["liquidation_risk"] = move_risk.liquidation_risk(candles, price, result["liquidation_price"])
        result.update({"price": price, "role": "maker" if maker else "taker", "auto": auto,
                       "leverage": float(leverage),
                       "available": exchange.account().get("available")})
        return jsonify(result)

    @app.route("/api/orders", methods=["POST"])
    def place():
        """No price: auto-priced. A price: a plain limit at it."""
        body = _body()
        order_type = str(body.get("order_type") or "limit_order")
        if order_type == "market_order":
            raise OrderRejected("market orders are disabled: every order is a limit order",
                                "market_disabled")
        if order_type != "limit_order":
            raise OrderRejected("order type must be limit_order", "invalid_order_type")
        args = (str(body.get("symbol", "")), str(body.get("side", "")), body.get("size"))
        reduce_only = _truthy(body.get("reduce_only"))
        if _price(body.get("limit_price")) is None:
            if _truthy(body.get("fast")) or _truthy(body.get("instant")):
                fast_eng = getattr(console, "fast_limit", None)
                if fast_eng:
                    order = fast_eng.place_fast_limit(
                        args[0], args[1], args[2],
                        reduce_only=reduce_only,
                        reason="manual_fast_limit",
                    )
                    return jsonify({"ok": True, "order": order})
            peg = pegger.submit(*args, reduce_only=reduce_only, stop_loss=body.get("stop_loss"),
                                take_profit=body.get("take_profit"), rr=body.get("rr"))
            return jsonify({"ok": True, "peg": peg})
        if body.get("rr"):
            raise OrderRejected("a target entry is auto-priced: leave the limit blank", "invalid_rr")
        order = exchange.place_order(
            *args, "limit_order", limit_price=body.get("limit_price"), reduce_only=reduce_only,
            post_only=_truthy(body.get("post_only")),
            stop_loss=body.get("stop_loss"), take_profit=body.get("take_profit"),
        )
        return jsonify({"ok": True, "order": order})

    @app.route("/api/orders/<order_id>", methods=["DELETE"])
    def cancel(order_id: str):
        return jsonify({"ok": True, "order": exchange.cancel_order(order_id)})

    @app.route("/api/pegs/<peg_id>", methods=["DELETE"])
    def cancel_peg(peg_id: str):
        return jsonify({"ok": True, "peg": pegger.cancel(peg_id)})

    @app.route("/api/positions/<symbol>/close", methods=["POST"])
    def close(symbol: str):
        """Exit with an auto-priced reduce-only limit, followed until flat."""
        _body()
        if symbol.upper() == "ALL":
            return jsonify({"ok": True, "results": pegger.exit_all()})
        return jsonify({"ok": True, "peg": pegger.exit(_instrument(symbol).symbol)})

    @app.route("/api/positions/<symbol>/target", methods=["POST"])
    def target(symbol: str):
        """Rest a reduce-only post-only limit at the price that nets `net` USD
        (default $1) after the entry fees paid and a maker exit fee, incl. GST."""
        body = _body()
        instrument = _instrument(symbol)
        try:
            net = float(body.get("net", 1.0))
        except (TypeError, ValueError):
            raise OrderRejected("net must be a number of USD", "invalid_net") from None
        if not net > 0:
            raise OrderRejected("net must be above $0", "invalid_net")
        position = next((p for p in exchange.positions_view()
                         if p["symbol"] == instrument.symbol), None)
        if position is None:
            raise OrderRejected(f"no open {instrument.symbol} position", "no_position")
        side = 1 if position["size"] > 0 else -1
        size = abs(position["size"])
        gst = console.settings.paper.gst_rate
        fees = position.get("fees_paid")
        if fees is None:                    # LIVE: Delta does not say; assume a maker entry
            fees = charges.fee(size * float(instrument.contract_value) * position["entry_price"],
                               instrument.maker_fee, gst)["total"]
        try:
            price = charges.target_price(side, size, position["entry_price"], float(fees),
                                         instrument, net, gst)
        except ValueError as exc:
            raise OrderRejected(str(exc), "invalid_net") from None
        order = exchange.place_order(
            instrument.symbol, "sell" if side > 0 else "buy", size, "limit_order",
            limit_price=price, reduce_only=True, post_only=True, reason=f"target ${net:g}")
        return jsonify({"ok": True, "order": order, "price": price, "net": net})

    @app.route("/api/positions/<symbol>/bracket", methods=["POST"])
    def bracket(symbol: str):
        body = _body()
        result = exchange.set_bracket(_instrument(symbol).symbol,
                                      body.get("stop_loss"), body.get("take_profit"))
        return jsonify({"ok": True, **result})

    @app.route("/api/leverage/<symbol>", methods=["GET", "POST"])
    def leverage(symbol: str):
        instrument = _instrument(symbol)
        if request.method == "GET":
            return jsonify({"symbol": instrument.symbol,
                            "leverage": exchange.get_leverage(instrument.symbol),
                            "max_leverage": instrument.max_leverage})
        body = _body()
        return jsonify({"ok": True, **exchange.set_leverage(instrument.symbol, body.get("leverage"))})

    @app.route("/api/history/<table>")
    def history(table: str):
        if table not in HISTORY_TABLES:
            return jsonify({"error": "unknown table"}), 404
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 500))
        except ValueError:
            limit = 100
        return jsonify({"table": table, "rows": exchange.history(table, limit)})

    @app.route("/api/paper/reset", methods=["POST"])
    def reset():
        body = _body()
        if body.get("confirm") != "RESET":
            raise OrderRejected('confirm the reset with {"confirm": "RESET"}', "confirm_required")
        clear_history = bool(body.get("clear_history", True))   # a reset starts a fresh ledger unless told to keep it
        algo = getattr(console, "algo", None)
        # The strategy is held still while the venue resets under it, so it never mistakes the dropped position
        # for a fill; then it starts over with the account (its trades and stats cleared with the ledger).
        with (algo.lock if algo else contextlib.nullcontext()):
            acc = exchange.reset(body.get("equity"), clear_history=clear_history)
            if algo:
                algo.reset(clear_history=clear_history)
        pnl_cache["at"] = 0.0                                    # the next snapshot reads the new ledger
        algo_cache["at"] = 0.0
        return jsonify({"ok": True, "account": acc})

    probe = BrokerLatencyProbe(console.probe, getattr(console, "db", None))

    @app.route("/api/broker_ping")
    def broker_ping():
        return jsonify(probe.snapshot())

    return app
