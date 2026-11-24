"""Delta Exchange India manual trading console. Run this file; nothing else.

    python main.py

One strategy runs here in TEST, `hhll` (the HH/LL tick engine Lune trades, on the gold perpetual); the operator can
still place any order from the console on DASHBOARD_PORT (5001). Every order is a limit order - there are no market
orders - and unless the operator types a price, `pegger` picks it: the best
maker price on the book, followed until it fills. It resolves the instruments the venue will trade,
connects its own database (`Delta_Manual`), opens one market-data socket, and
serves the console. `TRADE_MODE` decides who fills the orders:

    TRADE_MODE=TEST   `paper_engine`, a paper venue priced off the live Delta
                      India order book, charging Delta's fees + 18% GST,
                      funding, isolated margin and liquidation. The default.
    TRADE_MODE=LIVE   Delta Exchange India itself, with real money. Needs API
                      credentials (see delta_login.py).

Environment:
    TRADE_MODE=TEST|LIVE
    DELTA_DB_NAME=Delta_Manual
    DELTA_SYMBOLS=SOLUSD,BTCUSD       perpetuals to offer and trade (default: config.py)
    PAPER_EQUITY=1000                 balance of a new paper account
    PAPER_DEFAULT_LEVERAGE=10         leverage a symbol starts at in TEST
    STOP_LIMIT_BUFFER_BPS=50          how far past its trigger a stop-loss's limit sits
    PEG_MAX_DRIFT_BPS=30              how far an auto entry follows the market
    DASHBOARD_HOST=0.0.0.0  DASHBOARD_PORT=5001
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from werkzeug.serving import make_server

import instruments as instrument_catalogue
import ist
from config import SETTINGS, VERSIONS, base_of
from data_handler import CandleCache
from db import Database, DatabaseNotFoundError
from delta_login import BASE_URL, WS_URL, DeltaClient, DeltaError, login
from book_signal import BookRecorder, BookSignal
from feed import MarketFeed
from momentum import Momentum
from fast_limit import FastLimitEngine
from hhll import HHLL
from live_exchange import LIVE, LiveExchange
from live_server import create_app
from paper_engine import TEST, PaperExchange
from pegger import Pegger
from ticks import TickRecorder

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
log = logging.getLogger("main")


def configure_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    formatter = ist.ISTFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)-10s | %(message)s", ist.LOG_TIME_FORMAT
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    tag = "delta" if SETTINGS.version == "v1" else f"delta-{SETTINGS.version}"
    handler = logging.FileHandler(LOG_DIR / f"{tag}_{ist.date_key()}.log", encoding="utf-8")
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, handler], force=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("websocket").setLevel(logging.CRITICAL)


class Console:
    """Everything the console serves: the venue, the feed, the ledger."""

    def __init__(self, settings=SETTINGS):
        self.settings = settings
        self.mode = settings.execution.mode
        if self.mode not in (LIVE, TEST):
            raise SystemExit(f"TRADE_MODE must be LIVE or TEST, got {self.mode!r}")
        self.started_at = time.time()
        self.client = None
        self.db = Database(settings.database.name)
        self.feed: MarketFeed | None = None
        self.exchange = None
        self.pegger: Pegger | None = None
        self.ticks: TickRecorder | None = None
        self.signal: BookSignal | None = None
        self.book: BookRecorder | None = None
        self.momentum: Momentum | None = None
        self.fast_limit: FastLimitEngine | None = None
        self.algo: HHLL | None = None
        self.instruments: dict = {}
        self.candles: CandleCache | None = None
        self._server = None
        self._quit = threading.Event()     # asked to stop
        self._stopped = False

    def probe(self):
        """One request to the venue, timed by the console's latency pill. In
        LIVE it is the signed call an order depends on; in TEST a public one,
        made unique so Delta's CDN cannot answer it from cache (a cached answer
        reads ~7 ms, not the ~170 ms an order really takes)."""
        if self.mode == LIVE:
            return self.client.get("/v2/wallet/balances")
        return self.client.get("/v2/settings", {"_": time.time_ns()}, signed=False)

    def start(self) -> None:
        log.info("Starting Delta manual console %s | venue=%s mode=%s database=%s",
                 self.settings.version, BASE_URL, self.mode, self.db.name)
        if self.mode == LIVE:
            self.client = login()
            profile = self.client.profile or {}
            log.warning("LIVE: orders go to Delta Exchange India with real money "
                        "(logged in as %s)", profile.get("email"))
        else:
            # Market data is public; a paper account needs no credentials.
            self.client = DeltaClient("", "")

        self.instruments = instrument_catalogue.load(self.client, self.settings.symbols)
        self.db.connect()
        self.candles = CandleCache(self.client)

        feed_settings = self.settings.feed
        if not feed_settings.ws_url:
            feed_settings = replace(feed_settings, ws_url=WS_URL)
        self.feed = MarketFeed(sorted(self.instruments), settings=feed_settings)
        self.feed.start()
        self.ticks = TickRecorder(self.feed, self.db)
        self.ticks.start()
        self.signal = BookSignal(feed_settings.ws_url, self.instruments)
        self.signal.start()
        self.book = BookRecorder(self.signal, self.db)
        self.book.start()
        self.momentum = Momentum(self.db)
        self._await_prices()

        execution = self.settings.execution
        if self.mode == LIVE:
            self.exchange = LiveExchange(self.client, self.instruments, self.feed, self.db,
                                         stop_buffer_bps=execution.stop_limit_buffer_bps)
        else:
            self.exchange = PaperExchange(self.instruments, self.feed, self.settings.paper, self.db,
                                          stop_buffer_bps=execution.stop_limit_buffer_bps)
        self.exchange.load()
        self.exchange.start()
        self.pegger = Pegger(self.exchange, self.feed, self.instruments,
                             interval=execution.peg_interval, requote_s=execution.peg_requote_s,
                             max_drift_bps=execution.peg_max_drift_bps,
                             gst_rate=self.settings.paper.gst_rate, db=self.db)
        self.pegger.start()
        self.fast_limit = FastLimitEngine(self.exchange, self.instruments,
                                          default_slippage_ticks=int(os.environ.get("FAST_LIMIT_SLIPPAGE_TICKS", "1")),
                                          db=self.db, mode=self.mode)
        if self.mode == TEST:              # real money needs the operator's say-so: the strategy is paper only
            self.algo = HHLL(self.feed, self.exchange, self.instruments, self.fast_limit, db=self.db, mode=self.mode,
                             symbols=self.settings.symbols,
                             sides=VERSIONS[self.settings.version],
                             execution_mode=os.environ.get("HHLL_EXECUTION_MODE", "maker"))
            self.algo.start()

        server = self.settings.server
        app = create_app(self)
        base = base_of(self.settings.version)
        if base:                           # served under /vN, so v1's port passes it through unchanged
            from werkzeug.exceptions import NotFound
            from werkzeug.middleware.dispatcher import DispatcherMiddleware
            app = DispatcherMiddleware(NotFound(), {base: app})
        self._server = make_server(server.host, server.port, app, threaded=True)
        threading.Thread(target=self._server.serve_forever, name="console", daemon=True).start()
        log.info("Console live on http://%s:%d (%d instruments, %s, database %s)",
                 server.host, server.port, len(self.instruments), self.mode, self.db.name)

    def _await_prices(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self.feed.quote(s) for s in self.instruments):
                log.info("feed has quotes for all %d instruments", len(self.instruments))
                return
            time.sleep(0.5)
        log.warning("no quote yet for %s after %.0fs; they cannot be traded until one arrives",
                    ", ".join(s for s in self.instruments if not self.feed.quote(s)), timeout)

    def wait(self) -> None:
        while not self._quit.wait(1.0):
            pass

    def request_stop(self) -> None:
        self._quit.set()

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("Shutting down…")
        if self._server:
            self._server.shutdown()
        if self.algo:
            self.algo.stop()
        if self.pegger:
            self.pegger.stop()
        if self.feed:
            self.feed.stop()
        if self.ticks:
            self.ticks.stop()
        if self.book:
            self.book.stop()
        if self.signal:
            self.signal.stop()
        if self.exchange:
            self.exchange.stop()
        self.db.close()
        if self.client:
            self.client.close()
        log.info("Stopped. Open positions and orders stay where they are.")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv:
        print(__doc__)
        print(f"unrecognised argument(s): {' '.join(argv)}")
        return 2
    configure_logging()
    console = Console()

    def handle_signal(signum, _frame):
        log.info("Received signal %s", signum)
        console.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        console.start()
    except DatabaseNotFoundError as exc:
        log.critical("%s", exc)
        console.shutdown()
        return 1
    except DeltaError as exc:
        log.critical("Delta API failure: %s", exc)
        console.shutdown()
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("Startup failed: %s", exc)
        console.shutdown()
        return 1
    console.wait()
    console.shutdown()               # on the main thread, so the last write lands
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
