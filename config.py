"""Every tunable the manual Delta console has, in one place.

Nothing here is read at import time by anything that trades; `main.py` passes
`SETTINGS` down, so a test can construct its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _symbols() -> tuple[str, ...]:
    raw = os.environ.get("DELTA_SYMBOLS", "")
    if raw.strip():
        return tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    return ("BTCUSD",)


#: The perpetuals the console offers. Startup resolves each one against the
#: venue's own catalogue and drops (and names) anything not tradable there.
SYMBOLS: tuple[str, ...] = _symbols()

#: Strategy versions, by `DELTA_VERSION` (default v1). Each runs as its own process from this directory, v1 as
#: delta.service, the others as delta@<version>.service, with its own database (v1 `Delta_Manual`, vN
#: `Delta_Manual_vN`) and port (5000 + N, local only). Every version is served on v1's port 5001: v1 at /, vN at
#: /vN/ (v1 passes it through; the console's version box switches). The value is the side each pivot trades:
#: +1 long, -1 short. Add a version: one entry here, `CREATE DATABASE "Delta_Manual_vN"`,
#: `sudo systemctl enable --now delta@vN`, then restart delta (v1) so it passes /vN/ through.
#: 18 Nov 2026: v2 and v3 removed (operator); the system stays for the next one. E.g. a v1 with the sides reversed:
#: "v2": {"HH": 1, "LL": -1}.
VERSIONS: dict[str, dict[str, int]] = {
    "v1": {"HH": -1, "LL": 1},       # HH short, LL long
}
#: Versions whose incoming ticks are SENSEX-like ones (`tickfilter.SensexTicks`): what every tick consumer sees
#: (chart, `ticks` table, momentum, the strategy); prices for orders and fills stay the raw prints. None now (v3
#: had them); name a version here to give it them.
TICK_FILTER: frozenset = frozenset()
VERSION: str = os.environ.get("DELTA_VERSION", "v1").strip().lower()
if VERSION not in VERSIONS:
    raise SystemExit(f"DELTA_VERSION must be one of {', '.join(VERSIONS)}, got {VERSION!r}")


def port_of(version: str) -> int:
    return 5000 + int(version[1:])


def base_of(version: str) -> str:
    """The path a version is served under on v1's port: "" for v1, "/vN" for the others."""
    return "" if version == "v1" else f"/{version}"


_N = int(VERSION[1:])


@dataclass(frozen=True)
class DatabaseSettings:
    """The manual system's own database.

    Not `Delta_v1`: that one holds the retired automated strategy's ledger, and
    a reset or a migration here must never be able to touch it.
    """

    name: str = os.environ.get("DELTA_DB_NAME", "Delta_Manual" if _N == 1 else f"Delta_Manual_{VERSION}")


@dataclass(frozen=True)
class FeedSettings:
    # Public market-data socket. Empty means "whatever delta_login resolved".
    ws_url: str = ""
    # A quote older than this is not a quote any more. The socket publishes a
    # ticker per instrument roughly every second.
    max_price_age: float = 20.0
    # True: the trades the feed hands on are `tickfilter.SensexTicks` (config.TICK_FILTER).
    tick_filter: bool = VERSION in TICK_FILTER


@dataclass(frozen=True)
class PaperSettings:
    """The paper venue's model of Delta Exchange India.

    Fee *rates*, margin percentages, leverage caps and funding intervals are
    not here: they are read per product from the venue's own catalogue, so
    they are always the ones Delta is charging today.
    """

    # Balance of a new paper account (first run, or a reset with no amount).
    start_equity: float = float(os.environ.get("PAPER_EQUITY", 1000.0))
    # India levies 18% GST on the trading fee itself.
    gst_rate: float = 0.18
    # A limit that crosses the book fills at the live best bid/ask. This is
    # extra adverse slippage on top of the touch (never past the limit), for
    # size the top of book would not absorb.
    slippage_bps: float = float(os.environ.get("PAPER_SLIPPAGE_BPS", 0.0))
    # Seconds before an order is matched: a real one takes the round trip to Delta (about 170 ms from this
    # host, the console's latency pill) to be matched and answered, and the feed shows the book it met about as
    # late. Matching at once, against the book that prompted the order, fills at prices that are gone by then.
    order_latency: float = float(os.environ.get("PAPER_ORDER_LATENCY_MS", 170.0)) / 1000.0
    # Leverage a symbol starts at until the operator picks one.
    default_leverage: float = float(os.environ.get("PAPER_DEFAULT_LEVERAGE", 10.0))
    # How often resting orders, brackets, funding and liquidation are checked.
    poll_interval: float = 0.5
    # Delta's Scalper Offer (`charges`): no closing fee inside the scalping
    # window, BTCUSD opening fee back as a voucher. Opt-in on Delta; set
    # PAPER_SCALPER_OFFER=0 if the account has not joined it.
    scalper_offer: bool = os.environ.get("SCALPER_OFFER", os.environ.get("PAPER_SCALPER_OFFER", "1")).strip().lower() not in ("0", "false", "no", "off")
    # Voucher rebate feature: can be disabled via SCALPER_VOUCHERS=0
    scalper_vouchers: bool = os.environ.get("SCALPER_VOUCHERS", os.environ.get("PAPER_SCALPER_VOUCHERS", "1")).strip().lower() not in ("0", "false", "no", "off")


def _mode() -> str:
    mode = os.environ.get("TRADE_MODE", "TEST").strip().upper()
    return "TEST" if mode in ("DEMO", "PAPER") else mode


@dataclass(frozen=True)
class ExecutionSettings:
    # "LIVE" sends orders to Delta Exchange India with real money. "TEST"
    # sends them to `paper_engine`, a simulator of that venue priced off the
    # live order book.
    mode: str = field(default_factory=_mode)
    # There are no market orders. A stop-loss that fires sends a limit this far
    # past its trigger, so it fills through a fast move without being a market
    # order; a take-profit's limit is its own trigger price.
    stop_limit_buffer_bps: float = float(os.environ.get("STOP_LIMIT_BUFFER_BPS", 50.0))
    # Auto-priced orders (`pegger`): how often each one is checked against the
    # book, and the least time between two re-prices of the same order.
    peg_interval: float = float(os.environ.get("PEG_INTERVAL", 0.5))
    peg_requote_s: float = float(os.environ.get("PEG_REQUOTE_S", 1.0))
    # An auto entry follows the market at most this far from where it started,
    # then stops (exits always follow until they fill).
    peg_max_drift_bps: float = float(os.environ.get("PEG_MAX_DRIFT_BPS", 30.0))


@dataclass(frozen=True)
class ServerSettings:
    host: str = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port: int = int(os.environ.get("DASHBOARD_PORT", port_of(VERSION)))
    poll_ms: int = 1500


@dataclass(frozen=True)
class Settings:
    version: str = VERSION
    symbols: tuple[str, ...] = SYMBOLS
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    feed: FeedSettings = field(default_factory=FeedSettings)
    paper: PaperSettings = field(default_factory=PaperSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    server: ServerSettings = field(default_factory=ServerSettings)


SETTINGS = Settings()
