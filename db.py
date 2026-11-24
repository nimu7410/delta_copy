"""PostgreSQL pool and additive schema provisioning for the manual Delta console.

Its own database, `Delta_Manual`: not the Nifty bot's (`Lune`) and not the
retired automated strategy's (`Delta_v1`), so a reset, a restore or a bad
migration here can never touch either one's rows. The name comes from
`Settings.database.name`; `DB_NAME` is only the fallback for a caller that
constructs a `Database()` with no settings.

Provisioning is additive and idempotent - it creates what is missing and never
alters or drops what exists - so starting the bot is always safe.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

from psycopg2 import OperationalError, connect as pg_connect, pool, sql
from psycopg2.extras import RealDictCursor, execute_values

log = logging.getLogger("db")

DB_TIMEZONE = "Asia/Kolkata"
SCHEMA = "deltabot"
DB_NAME = os.environ.get("DELTA_DB_NAME", "Delta_Manual")
DB_PASSWORD = os.environ.get("DELTA_DB_PASSWORD") or os.environ.get("TSDB_PASSWORD") or "Login7410*"


class DatabaseNotFoundError(RuntimeError):
    """The database has not been created. Provisioning tables is additive; the
    database itself is not created automatically."""


# name -> {column: type}. Columns are added if missing, never altered.
#
# `mode` is on every row: LIVE and TEST share the tables, and a ledger query
# that forgot it would answer a LIVE console with the paper account's money.
TABLES: dict[str, dict[str, str]] = {}
#: The paper venue's wallet. One row per paper account; `id` 1 is the live one.
TABLES['paper_account'] = {'id': 'SMALLINT PRIMARY KEY', 'balance': 'DOUBLE PRECISION NOT NULL', 'start_balance': 'DOUBLE PRECISION NOT NULL', 'opened_at': 'TIMESTAMPTZ NOT NULL', 'next_order_id': 'BIGINT NOT NULL', 'updated_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}
#: The paper venue's open positions, rewritten on every change. Isolated margin.
TABLES['paper_positions'] = {'symbol': 'TEXT PRIMARY KEY', 'size': 'INTEGER NOT NULL', 'entry_price': 'DOUBLE PRECISION NOT NULL', 'margin': 'DOUBLE PRECISION NOT NULL', 'leverage': 'DOUBLE PRECISION NOT NULL', 'realized_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'realized_funding': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'fees_paid': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'open_fees': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'closed_contracts': 'INTEGER NOT NULL DEFAULT 0', 'closed_value': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'funding_at': 'BIGINT', 'opened_at': 'TIMESTAMPTZ NOT NULL', 'updated_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}
#: Leverage per symbol, as the operator last set it.
TABLES['leverage'] = {'mode': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'leverage': 'DOUBLE PRECISION NOT NULL', 'updated_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}
#: Every order: the paper venue's whole book, and every order sent to LIVE.
TABLES['orders'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'order_id': 'TEXT NOT NULL', 'client_order_id': 'TEXT', 'symbol': 'TEXT NOT NULL', 'side': 'TEXT NOT NULL', 'size': 'INTEGER NOT NULL', 'unfilled_size': 'INTEGER', 'order_type': 'TEXT NOT NULL', 'limit_price': 'DOUBLE PRECISION', 'stop_order_type': 'TEXT', 'stop_price': 'DOUBLE PRECISION', 'reduce_only': 'BOOLEAN NOT NULL DEFAULT FALSE', 'post_only': 'BOOLEAN NOT NULL DEFAULT FALSE', 'leverage': 'DOUBLE PRECISION', 'reserved_margin': 'DOUBLE PRECISION', 'bracket_stop_loss': 'DOUBLE PRECISION', 'bracket_take_profit': 'DOUBLE PRECISION', 'state': 'TEXT NOT NULL', 'avg_fill_price': 'DOUBLE PRECISION', 'paid_commission': 'DOUBLE PRECISION', 'note': 'TEXT', 'created_at': 'TIMESTAMPTZ NOT NULL', 'updated_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}
#: One row per fill, with the fee split into the venue's part and GST.
TABLES['fills'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'order_id': 'TEXT', 'symbol': 'TEXT NOT NULL', 'side': 'TEXT NOT NULL', 'size': 'INTEGER NOT NULL', 'price': 'DOUBLE PRECISION NOT NULL', 'notional': 'DOUBLE PRECISION NOT NULL', 'role': 'TEXT NOT NULL', 'fee_rate': 'DOUBLE PRECISION NOT NULL', 'fee': 'DOUBLE PRECISION NOT NULL', 'gst': 'DOUBLE PRECISION NOT NULL', 'commission': 'DOUBLE PRECISION NOT NULL', 'realized_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'created_at': 'TIMESTAMPTZ NOT NULL'}
#: Every movement of the wallet balance: fees, realised P&L, funding,
#: liquidations, resets. Summing `amount` reproduces the balance exactly.
TABLES['ledger'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'symbol': 'TEXT', 'kind': 'TEXT NOT NULL', 'amount': 'DOUBLE PRECISION NOT NULL', 'balance': 'DOUBLE PRECISION NOT NULL', 'note': 'TEXT', 'created_at': 'TIMESTAMPTZ NOT NULL'}
#: A position's whole life, written when it goes flat.
TABLES['trades'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'direction': 'TEXT NOT NULL', 'contracts': 'INTEGER NOT NULL', 'entry_price': 'DOUBLE PRECISION NOT NULL', 'exit_price': 'DOUBLE PRECISION', 'leverage': 'DOUBLE PRECISION', 'gross_pnl': 'DOUBLE PRECISION NOT NULL', 'fees': 'DOUBLE PRECISION NOT NULL', 'funding': 'DOUBLE PRECISION NOT NULL', 'net_pnl': 'DOUBLE PRECISION NOT NULL', 'reason': 'TEXT', 'opened_at': 'TIMESTAMPTZ NOT NULL', 'closed_at': 'TIMESTAMPTZ NOT NULL'}
#: Every websocket trade, for the console's Live chart. `ticks.TickRecorder`
#: deletes rows older than a day. Market data: no `mode`.
TABLES['ticks'] = {'id': 'BIGSERIAL PRIMARY KEY', 'symbol': 'TEXT NOT NULL', 'price': 'DOUBLE PRECISION NOT NULL', 'traded_at': 'TIMESTAMPTZ NOT NULL'}
#: The order book, compressed to one row per symbol per second: near-price bid /
#: ask USD and that second's market buy / sell USD, as 4-byte REALs.
#: `book_signal.BookRecorder` deletes rows older than an hour. Market data: no `mode`.

TABLES['book_seconds'] = {'symbol': 'TEXT NOT NULL', 'at': 'TIMESTAMPTZ NOT NULL', 'bid_usd': 'REAL NOT NULL', 'ask_usd': 'REAL NOT NULL', 'buy_usd': 'REAL NOT NULL', 'sell_usd': 'REAL NOT NULL'}
#: Confirmed HH/LL swing pivots strictly on 5m tick data. Market data: no `mode`.
#: Auto-flushed after a day (pivots older than 1 day are pruned).
TABLES['pivots'] = {'id': 'BIGSERIAL PRIMARY KEY', 'symbol': 'TEXT NOT NULL', 'pivot_type': 'TEXT NOT NULL', 'price': 'DOUBLE PRECISION NOT NULL', 'extreme_at': 'TIMESTAMPTZ NOT NULL', 'confirmed_at': 'TIMESTAMPTZ NOT NULL', 'lag_ticks': 'INTEGER NOT NULL', 'lag_ms': 'BIGINT NOT NULL', 'leg_size': 'DOUBLE PRECISION NOT NULL', 'reversal_size': 'DOUBLE PRECISION NOT NULL', 'anchor_price': 'DOUBLE PRECISION NOT NULL', 'created_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}
#: Automated swing trades taken on confirmed HH (short) and LL (long).
TABLES['algo_trades'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'side': 'TEXT NOT NULL', 'size': 'INTEGER NOT NULL', 'entry_pivot_type': 'TEXT NOT NULL', 'entry_pivot_price': 'DOUBLE PRECISION NOT NULL', 'entry_price': 'DOUBLE PRECISION NOT NULL', 'entry_at': 'TIMESTAMPTZ NOT NULL', 'exit_pivot_type': 'TEXT', 'exit_pivot_price': 'DOUBLE PRECISION', 'exit_price': 'DOUBLE PRECISION', 'exit_at': 'TIMESTAMPTZ', 'leg_size': 'DOUBLE PRECISION NOT NULL', 'entry_latency_ms': 'DOUBLE PRECISION DEFAULT 0', 'exit_latency_ms': 'DOUBLE PRECISION DEFAULT 0', 'gross_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'fees': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'net_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'state': 'TEXT NOT NULL', 'created_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()', 'updated_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}

#: The HH/LL strategy's trades (hhll.py): one row per round trip, OPEN until it closes.
TABLES['hhll_trades'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'side': 'TEXT NOT NULL', 'size': 'INTEGER NOT NULL', 'pivot': 'TEXT NOT NULL', 'leg': 'DOUBLE PRECISION NOT NULL', 'tp_net_usd': 'DOUBLE PRECISION', 'tp_price': 'DOUBLE PRECISION', 'entry_price': 'DOUBLE PRECISION NOT NULL', 'entry_at': 'TIMESTAMPTZ NOT NULL', 'exit_price': 'DOUBLE PRECISION', 'exit_at': 'TIMESTAMPTZ', 'exit_reason': 'TEXT', 'gross_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'fees': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'net_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'state': 'TEXT NOT NULL', 'created_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}

#: The tournament strategy's real trades (strat.py): one row per round trip, with the shadow twin's result.
TABLES['strat_trades'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'side': 'TEXT NOT NULL', 'size': 'INTEGER NOT NULL', 'cand': 'TEXT NOT NULL', 'entry_price': 'DOUBLE PRECISION NOT NULL', 'entry_at': 'TIMESTAMPTZ NOT NULL', 'exit_price': 'DOUBLE PRECISION', 'exit_at': 'TIMESTAMPTZ', 'exit_reason': 'TEXT', 'gross_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'fees': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'net_pnl': 'DOUBLE PRECISION NOT NULL DEFAULT 0', 'net_bps': 'DOUBLE PRECISION', 'shadow_bps': 'DOUBLE PRECISION', 'state': 'TEXT NOT NULL', 'created_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}

#: Every fast-limit fill's latency: the algo's fill-speed stats are read from here.
TABLES['fill_latency'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'side': 'TEXT NOT NULL', 'size': 'INTEGER NOT NULL', 'price': 'DOUBLE PRECISION NOT NULL', 'latency_ms': 'DOUBLE PRECISION NOT NULL', 'execution_type': 'TEXT NOT NULL', 'is_maker': 'BOOLEAN NOT NULL DEFAULT FALSE', 'created_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}
#: Round trips to the Delta REST API. Rows older than a day are deleted. No `mode`.
TABLES['broker_pings'] = {'id': 'BIGSERIAL PRIMARY KEY', 'latency_ms': 'DOUBLE PRECISION', 'error': 'TEXT', 'at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}

#: Finished auto-priced orders (the pegger's), as the console shows them.
TABLES['pegs'] = {'id': 'BIGSERIAL PRIMARY KEY', 'mode': 'TEXT NOT NULL', 'peg_id': 'BIGINT NOT NULL', 'symbol': 'TEXT NOT NULL', 'state': 'TEXT NOT NULL', 'view': 'JSONB NOT NULL', 'finished_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}

#: The HH/LL algo's decision state per symbol (last eligible side / pivot, candidate extremes).
TABLES['algo_state'] = {'mode': 'TEXT NOT NULL', 'name': 'TEXT NOT NULL', 'symbol': 'TEXT NOT NULL', 'value': 'JSONB NOT NULL', 'updated_at': 'TIMESTAMPTZ NOT NULL DEFAULT now()'}

# Retired tables. Provisioning is otherwise additive and never drops anything.
DROPPED_TABLES: tuple[str, ...] = ('auto_trader', 'ui_settings', 'hhll_pivots')

INDEXES: tuple[tuple[str, str, str, bool], ...] = (
    ('leverage_uidx', 'leverage', '(mode, symbol)', True),
    ('orders_mode_order_uidx', 'orders', '(mode, order_id)', True),
    ('orders_mode_time_idx', 'orders', '(mode, created_at DESC, id DESC)', False),
    ('orders_open_idx', 'orders', '(mode, state)', False),
    ('fills_mode_time_idx', 'fills', '(mode, created_at DESC, id DESC)', False),
    ('ledger_mode_time_idx', 'ledger', '(mode, created_at DESC, id DESC)', False),
    ('trades_mode_time_idx', 'trades', '(mode, closed_at DESC, id DESC)', False),
    ('ticks_symbol_time_idx', 'ticks', '(symbol, traded_at DESC)', False),
    ('ticks_time_idx', 'ticks', '(traded_at)', False),
    ('book_seconds_uidx', 'book_seconds', '(symbol, at)', True),
    ('pivots_uidx', 'pivots', '(symbol, extreme_at, pivot_type)', True),
    ('pivots_symbol_confirmed_idx', 'pivots', '(symbol, confirmed_at DESC)', False),
    ('pivots_confirmed_idx', 'pivots', '(confirmed_at)', False),
    ('algo_trades_mode_time_idx', 'algo_trades', '(mode, created_at DESC, id DESC)', False),
    ('algo_trades_symbol_idx', 'algo_trades', '(mode, symbol, state)', False),
    ('hhll_trades_symbol_idx', 'hhll_trades', '(mode, symbol, state, exit_at)', False),
    ('fill_latency_mode_idx', 'fill_latency', '(mode)', False),
    ('broker_pings_at_idx', 'broker_pings', '(at DESC)', False),
    ('pegs_mode_peg_uidx', 'pegs', '(mode, peg_id)', True),
    ('algo_state_uidx', 'algo_state', '(mode, name, symbol)', True),
)


class Database:
    """Thread-safe pool. Every consumer in the bot shares one instance."""

    def __init__(self, name: str = DB_NAME, *, minconn: int = 2, maxconn: int = 12,
                 attempts: int = 5):
        self.name = name
        self.cfg = {
            "host": os.getenv("TSDB_HOST", "127.0.0.1"),
            "port": int(os.getenv("TSDB_PORT", 5432)),
            "dbname": name,
            "user": os.getenv("TSDB_USER", "postgres"),
            "password": DB_PASSWORD,
            "connect_timeout": int(os.getenv("TSDB_CONNECT_TIMEOUT", 5)),
            # Pin the session zone: TIMESTAMPTZ is stored as an instant, and
            # without this every timestamp the bot reads back renders in
            # whatever zone the server defaults to.
            "options": f"-c timezone={DB_TIMEZONE}",
        }
        self.minconn, self.maxconn, self.attempts = minconn, maxconn, attempts
        self._pool: pool.ThreadedConnectionPool | None = None
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        if self._pool is not None:
            self.ensure_schema()
            return
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                self._pool = pool.ThreadedConnectionPool(
                    self.minconn, self.maxconn, **self.cfg,
                    cursor_factory=RealDictCursor,
                )
                break
            except OperationalError as exc:
                last = exc
                if attempt == 1 and not self._database_exists():
                    raise DatabaseNotFoundError(
                        f'Database "{self.name}" does not exist. Create it with: '
                        f'CREATE DATABASE "{self.name}";'
                    ) from exc
                log.warning("DB unreachable (%d/%d): %s", attempt, self.attempts, exc)
                if attempt == self.attempts:
                    raise
                threading.Event().wait(2.0 * attempt)
        else:  # pragma: no cover - loop always breaks or raises
            raise last or OperationalError("could not connect")

        try:
            self._verify_timezone()
            self._ensure_database_default_timezone()
            self.ensure_schema()
        except Exception:
            self.close()
            raise
        log.info("PostgreSQL pool ready for %s@%s/%s",
                 self.cfg["user"], self.cfg["host"], self.name)

    def _database_exists(self) -> bool:
        admin = dict(self.cfg, dbname="postgres")
        try:
            conn = pg_connect(**admin, cursor_factory=RealDictCursor)
        except OperationalError:
            return True  # cannot tell; let the caller's retry report the real error
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok FROM pg_database WHERE datname = %s", (self.name,))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def _verify_timezone(self) -> None:
        rows = self.execute("SHOW TimeZone;", fetch=True) or []
        actual = str(rows[0].get("TimeZone", "")).strip() if rows else ""
        if actual.lower() != DB_TIMEZONE.lower():
            raise RuntimeError(
                f"Session timezone is {actual!r}, expected {DB_TIMEZONE!r}; "
                "every timestamp would be shifted. Refusing to start."
            )

    def _ensure_database_default_timezone(self) -> None:
        """Persist IST as the database's own default, for everything else.

        Pool connections already carry `-c timezone=Asia/Kolkata` and
        `_verify_timezone` has confirmed it took - that is what makes *this
        bot* correct. This is for psql, reporting tools and any future code
        that does not come through this connector: without it they render the
        same rows in whatever zone the server defaults to.

        Best-effort: ALTER DATABASE needs ownership, and a permissions gap must
        not keep a trading bot down when its own connections are already right.
        """
        try:
            rows = self.execute(
                """
                SELECT COALESCE(bool_or(lower(item) = lower(%s)), false) AS configured
                FROM   pg_database AS d
                LEFT JOIN pg_db_role_setting AS s
                       ON s.setdatabase = d.oid AND s.setrole = 0
                LEFT JOIN LATERAL unnest(s.setconfig) AS item ON true
                WHERE  d.datname = current_database()
                """,
                (f"TimeZone={DB_TIMEZONE}",), fetch=True,
            ) or []
            if rows and rows[0].get("configured"):
                return
            conn = self._pool.getconn()
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("ALTER DATABASE {} SET timezone TO {}").format(
                            sql.Identifier(self.name), sql.Literal(DB_TIMEZONE)
                        )
                    )
                log.info("Set database default timezone to %s for %s", DB_TIMEZONE, self.name)
            finally:
                conn.autocommit = False
                self._pool.putconn(conn)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning(
                "Could not set the database default timezone to %s: %s. This "
                "process is unaffected (its pool is pinned to it); psql sessions "
                "may render timestamps in another zone. Fix with: "
                "ALTER DATABASE \"%s\" SET timezone TO '%s';",
                DB_TIMEZONE, exc, self.name, DB_TIMEZONE,
            )

    def close(self) -> None:
        connection_pool, self._pool = self._pool, None
        self._schema_ready = False
        if connection_pool:
            try:
                connection_pool.closeall()
                log.info("DB pool closed")
            except Exception as exc:  # noqa: BLE001
                log.debug("pool close ignored: %s", exc)

    # -- schema ----------------------------------------------------------
    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    # One provisioner at a time, even across processes.
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (SCHEMA,))
                    cur.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA))
                    )
                    for dropped in DROPPED_TABLES:
                        cur.execute(
                            sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                                sql.Identifier(SCHEMA), sql.Identifier(dropped)
                            )
                        )
                    for table, columns in TABLES.items():
                        cur.execute(
                            sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
                                sql.Identifier(SCHEMA), sql.Identifier(table),
                                sql.SQL(", ").join(
                                    sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(spec))
                                    for name, spec in columns.items()
                                ),
                            )
                        )
                        for name, spec in columns.items():
                            # Additive only: an existing column is never altered.
                            add = spec.replace(" PRIMARY KEY", "").replace(" NOT NULL", "")
                            cur.execute(
                                sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} {}").format(
                                    sql.Identifier(SCHEMA), sql.Identifier(table),
                                    sql.Identifier(name), sql.SQL(add),
                                )
                            )
                    for name, table, key, unique in INDEXES:
                        cur.execute(
                            sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {}.{} {}").format(
                                sql.SQL("UNIQUE ") if unique else sql.SQL(""),
                                sql.Identifier(name), sql.Identifier(SCHEMA),
                                sql.Identifier(table), sql.SQL(key),
                            )
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._pool.putconn(conn)
            self._schema_ready = True

    # -- queries ---------------------------------------------------------
    @contextmanager
    def _cursor(self):
        if not self._pool:
            raise RuntimeError("connect() must be called first")
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def execute(self, statement: str, params=None, *, fetch: bool = False):
        with self._cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if fetch else None

    @contextmanager
    def transaction(self):
        """One cursor, one commit: everything inside lands together or not at all."""
        with self._cursor() as cur:
            yield cur

    def execute_many(self, statement: str, rows) -> None:
        """One round-trip for a batch. This is what keeps the writers cheap."""
        rows = list(rows)
        if not rows:
            return
        with self._cursor() as cur:
            execute_values(cur, statement, rows)
