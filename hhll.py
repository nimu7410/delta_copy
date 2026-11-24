"""HH/LL on Delta: the plain tick zigzag on the gold perpetual. Paper only.

Operator's order, 17 Nov 2026: one simple logic on tick data and one box. On every print:

    price `threshold` % below the running high  -> HH at that high      -> short
    price `threshold` % above the running low   -> LL at that low       -> long

stop-and-reverse on the print that confirms the pivot. HH and LL strictly alternate; a printed pivot never moves.
18 Nov 2026, operator's order: trades are taken on the smoothed line's pivots instead (the same zigzag on
`Smoother`'s line, confirmed at the end of a second: the white dash), every order, fill and P&L on the real prints;
the prints' own pivots stay on the chart.
The threshold is the console's box, a percentage of the extreme's price (operator, 17 Nov: percent, not points).
Nothing else: no minimum leg, no breaks, no adaptive R, no stops.

Archived gold ticks (2-4, 11-17 Nov, price about 3,900), fills 170 ms late and 0.05 worse, fees, as points (0.01% is
about 0.39): threshold 0.5 / 1 / 2 / 5 / 10 / 15 / 20 / 30 points -> -15698 / -6949 / -2490 / -369 / -241 / -2 / -52
/ +60 points (17220 / 7747 / 2903 / 627 / 196 / 84 / 54 / 22 trades): a small threshold pays the charges on every
trade.

"Market" is Lune's limit 2% through the touch (`fast_limit`; Delta has no market orders); size is what the paper
account affords at 10x. Restarts continue the stored chain (`CHAIN`) while the logic stands (`LOGIC` + the
threshold); a new logic redraws it. Bump `LOGIC` whenever the pivot rule changes. On every pivot the venue's
position is brought to the pivot's side, whoever opened it; nothing is done between pivots. Which side a pivot
trades is the version's (`sides`, config.VERSIONS): v1 HH short / LL long, v2 the reverse. The pivots are the same.

    .venv/bin/python hhll.py              self-check
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import charges
from decimal import Decimal
from pegger import maker_price
import tickfilter

log = logging.getLogger("hhll")

SYMBOLS = ("XAUTUSD",)
LEVERAGE = 10.0
DEFAULT_X = 0.05                # the box: the reversal that prints a pivot, % of the extreme (about 2 points on gold)
X_MIN, X_MAX = 0.001, 50.0
DEFAULT_XCOST = 18.0            # v3 (SENSEX-like ticks): the box is x SENSEX's cost (tickfilter.SENSEX_COST_PCT), as Lune's
XCOST_MIN, XCOST_MAX = 0.5, 100.0   # ... Settings accepts (manual_desk.FIXED_MIN / FIXED_MAX)
DONE = ("closed", "filled")
BUFFER = 0.02                   # Lune's market order: a limit this far through the touch (hhll_live.limit_price)
PIVOTS_KEPT = 3000              # the chart's most recent pivots
STATE = "hhll_cost_x"           # deltabot.algo_state name of the console's box
CHAIN = "hhll_chain"            # deltabot.algo_state name of the printed pivots
LOGIC = "plain-zigzag-pct-1"    # the pivot rule's version: change it with the rule, and the next start redraws
# The smoothed line and its own pivots (operator, 18 Nov): the same zigzag, on the smoothed line instead of the prints.
# The strategy trades these (operator, 18 Nov, later): HH / LL of the smoothed line, on the second that confirms it
# (its white dash); every order, fill and P&L on the real prints. The prints' own pivots are shown, not traded.
DEFAULT_SMOOTH = 0.3            # the smallest move the smoothed line shows, % of price (about 12 points on gold)
SMOOTH_MIN, SMOOTH_MAX = 0.001, 1.0
SMOOTH_STATE = "hhll_smooth"    # deltabot.algo_state name of the smoother's box
SMOOTH_CHAIN = "hhll_smooth_chain"   # ... and of the smoothed line's printed pivots
SMOOTH_LOGIC = "smooth-ratchet-1"   # the smoother's version: change it with `Smoother`, and the next start redraws
SMOOTH_ROUND_S, SMOOTH_GAP_MS = 30.0, 3_600_000
SMOOTH_GRACE_MS = 500           # a second with no print is closed this long after it ends (feed latency ~170 ms)
LINE_KEPT = 90_000              # seconds of smoothed line kept for the chart (a day and a bit)
# Breakeven lock (operator, 24 Nov): once a trade is LOCK_ARM % in its favour (on the real prints), it is closed if
# price comes back to LOCK_AT % beyond its entry fill (the round trip's charges covered), flat to the next pivot;
# a trade that never gets there runs to the opposite pivot as before, so winners still run. LOCK_AT is never under
# the round-trip fee. SOL ticks (data/research/be_lock/README.md): losses and drawdown cut on every dataset, the
# strategy still loses. 0 arm turns it off.
LOCK_ARM = float(os.environ.get("HHLL_LOCK_ARM", "0.15"))
LOCK_AT = float(os.environ.get("HHLL_LOCK_AT", "0.1"))


# ── pivots ───────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Pivot:
    kind: str            # "HH" | "LL"
    price: float         # the extreme
    ms: int
    conf_price: float    # the tick that confirmed it
    conf_ms: int


class ZigZag:
    """The plain one-threshold zigzag on ticks: a pivot prints on the first tick `pct` % back from the running
    extreme, at that extreme. Append-only, strictly alternating."""

    def __init__(self, pct: float) -> None:
        if not pct > 0:
            raise ValueError("the threshold must be positive")
        self.R, self.r = pct, pct / 100
        self.dir = 0            # 0 = no pivot yet, -1 = searching LL (last was HH), +1 = searching HH
        self.hp = self.lp = None
        self.hm = self.lm = None
        self.last: Optional[Pivot] = None

    def update(self, p: float, ms: int) -> Optional[Pivot]:
        if self.hp is None:
            self.hp = self.lp = p
            self.hm = self.lm = ms
            return None
        d, r = self.dir, self.r
        if d >= 0:
            if p > self.hp:
                self.hp, self.hm = p, ms
            elif self.hp - p >= self.hp * r - 1e-9:
                self.last = Pivot("HH", self.hp, self.hm, p, ms)
                self.dir, self.lp, self.lm = -1, p, ms          # p is the lowest tick since the HH
                return self.last
        if d <= 0:
            if p < self.lp:
                self.lp, self.lm = p, ms
            elif p - self.lp >= self.lp * r - 1e-9:
                self.last = Pivot("LL", self.lp, self.lm, p, ms)
                self.dir, self.hp, self.hm = 1, p, ms
                return self.last
        return None

    def trigger(self) -> Optional[float]:
        """Where the next pivot prints if price goes straight there. None before the first pivot."""
        return None if not self.dir else self.hp * (1 - self.r) if self.dir > 0 else self.lp * (1 + self.r)

    def resume(self, kind: str, price: float, ms: int, cp: float, cms: int) -> None:
        """Continue after a stored pivot, as if it had just printed on its confirming tick."""
        self.last = Pivot(kind, price, ms, cp, cms)
        self.dir = -1 if kind == "HH" else 1
        self.hp = self.lp = cp
        self.hm = self.lm = cms


class Smoother:
    """The chart's smoothed line; static/delta.js `Smoother` is the same code (tests/check_smoother.js compares them).

    Operator, 19 Nov: "smoothed yet keeps up with price, but never shows sudden or small movements". Whole seconds,
    each on its time-weighted price (every print held until the next):
      1. the ratchet: rising, the line holds the highest price since it turned up and follows every new high at
         once; it turns down only when price is the threshold (`pct` % of price) below that high, and then holds
         the lowest price, mirror. A move back smaller than the threshold is never shown: the line holds flat;
      2. two 15 s poles round every step and turn (no jumps, no corners); they keep a one-way stretch one-way.
    Archived gold ticks (400 h): the old denoiser turned 140 times a day, 2104 of its swings under 5 points; this at
    0.3 / 0.4 % turns 10 / 6 times a day, 3 swings under 5 points in all (rounding after a jump). The cost: in a
    pullback it sits up to the threshold off price (mean 4.1 / 5.8 points; the old line 1.7). Causal: a closed
    second's value never changes."""

    def __init__(self, pct: float) -> None:
        self.pct = pct
        self.sec: Optional[int] = None      # the last closed second
        self.fed: Optional[int] = None      # the newest print fed, epoch ms

    def _close(self) -> tuple[int, float]:
        end = (self.sec + 2) * 1000
        x = (self.area + self.x * (end - self.mark)) / 1000
        s = self.s
        self.sec, self.mark, self.area = self.sec + 1, end, 0.0
        h = self.pct / 100
        if self.m > 0:
            if x > s[0]:
                s[0] = x
            elif x <= s[0] * (1 - h):
                self.m, s[0] = -1, x
        elif x < s[0]:
            s[0] = x
        elif x >= s[0] * (1 + h):
            self.m, s[0] = 1, x
        a = 1 - math.exp(-2 / SMOOTH_ROUND_S)
        s[1] += a * (s[0] - s[1])
        s[2] += a * (s[1] - s[2])
        return end, s[2]

    def feed(self, price: float, at: int) -> list[tuple[int, float]]:
        """A print -> the seconds it closes, as (their end, epoch ms; the line's value). A print from a second
        already closed changes nothing."""
        sec = at // 1000
        if self.sec is None or sec - self.sec > SMOOTH_GAP_MS // 1000:
            self.s, self.m, self.sec, self.x, self.mark = [price] * 3, 1, sec - 1, price, at
            self.area, self.fed = price * (at - sec * 1000), at
            return []
        if sec <= self.sec:
            return []
        out = []
        while self.sec + 1 < sec:
            out.append(self._close())
        if at > self.mark:
            self.area += self.x * (at - self.mark)
            self.mark = at
        self.x, self.fed = price, max(self.fed, at)
        return out

    def clock(self, now: float, grace: float) -> list[tuple[int, float]]:
        """The seconds that ended `grace` ms or more before `now` (epoch ms) with no print since: closed on the
        price held, as the next print would close them. A print later than that from a closed second changes
        nothing. After a gap (SMOOTH_GAP_MS) the clock waits for a print."""
        out = []
        if self.sec is None or now - self.fed > SMOOTH_GAP_MS:
            return out
        while (self.sec + 2) * 1000 + grace <= now:
            out.append(self._close())
        return out

    def state(self) -> Optional[dict]:
        """Where the line stands, for the chart to go on from with the prints after `fed`."""
        return None if self.sec is None else {"s": list(self.s), "m": self.m, "sec": self.sec, "x": self.x, "mark": self.mark,
                                              "area": self.area}


# ── cost ─────────────────────────────────────────────────────────────────────────
def cost_pts(inst, price: float, spread: float) -> float:
    """All-in cost of one flat round trip in price points: the taker fee with GST on both sides, plus the spread."""
    return price * 2 * inst.taker_fee * (1 + charges.GST_RATE) + spread


# ── the trader ───────────────────────────────────────────────────────────────────
class HHLL:
    def __init__(self, feed, exchange, instruments: dict, fast_limit, *, db=None, mode: str = "TEST",
                 symbols=None, sides=None, trades_on: Optional[str] = None, execution_mode: Optional[str] = None):
        self.feed, self.exchange, self.fast_limit = feed, exchange, fast_limit
        self.instruments, self.db, self.mode = instruments, db, mode.upper()
        syms = symbols if symbols is not None else instruments
        self.symbols = tuple(s for s in syms if s in instruments)
        self.sides = dict(sides or {"HH": -1, "LL": 1})                 # pivot -> +1 long / -1 short
        self.trades_on = str(trades_on or os.environ.get("HHLL_TRADES_ON", "ticks")).lower()
        self.execution_mode = str(execution_mode or os.environ.get("HHLL_EXECUTION_MODE", "maker")).lower()
        # v3's ticks are SENSEX-like (config.TICK_FILTER): its box is in Lune's unit, x SENSEX's cost; others: %.
        self.xcost = bool(getattr(getattr(feed, "settings", None), "tick_filter", False))
        self.key = "x_cost" if self.xcost else "pct"                    # its name in algo_state
        self.x = {s: DEFAULT_XCOST if self.xcost else DEFAULT_X for s in self.symbols}   # the box
        self.cost: dict[str, Optional[float]] = {s: None for s in self.symbols}
        self.zz: dict[str, Optional[ZigZag]] = {s: None for s in self.symbols}
        self.pivots = {s: deque(maxlen=PIVOTS_KEPT) for s in self.symbols}   # for the chart
        self.smooth = {s: DEFAULT_SMOOTH for s in self.symbols}              # the smoother's box
        self.sm: dict[str, Optional[Smoother]] = {s: None for s in self.symbols}
        self.szz: dict[str, Optional[ZigZag]] = {s: None for s in self.symbols}   # the smoothed line's zigzag
        self.spivots = {s: deque(maxlen=PIVOTS_KEPT) for s in self.symbols}
        self.sline = {s: deque(maxlen=LINE_KEPT) for s in self.symbols}    # (epoch ms, value) a closed second
        self.st: dict[str, Optional[dict]] = {s: None for s in self.symbols}   # the open trade, for the books
        self.halt: dict[str, Optional[str]] = {s: None for s in self.symbols}
        self._db_part = None
        self._cur = 0
        self.clock = time.time         # seconds; the smoothed line closes quiet seconds on it
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._cur = self.feed.trades_since(0)[0]
        for sym in self.symbols:
            try:
                if float(self.exchange.get_leverage(sym)) != LEVERAGE:
                    self.exchange.set_leverage(sym, LEVERAGE)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] leverage not set to %gx: %s", sym, LEVERAGE, exc)
            self._load(sym)
            self._build(sym)
        self._thread = threading.Thread(target=self._run, name="hhll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def lock(self) -> threading.RLock:
        """Held while the strategy acts; hold it to change the venue under it (a paper reset)."""
        return self._lock

    def reset(self, clear_history: bool = False) -> None:
        """After a paper reset the venue is flat: forget the open trade; with `clear_history` its trades too.
        The threshold and the pivot chain stay. Call it under `self.lock`, with the venue's reset."""
        with self._lock:
            if self.db is not None:
                try:
                    if clear_history:
                        self.db.execute("DELETE FROM deltabot.hhll_trades WHERE mode = %s AND symbol = ANY(%s)",
                                        (self.mode, list(self.symbols)))
                    else:
                        self.db.execute("UPDATE deltabot.hhll_trades SET state='LOST', exit_reason='paper reset' "
                                        "WHERE mode = %s AND state='OPEN'", (self.mode,))
                except Exception as exc:  # noqa: BLE001
                    log.warning("strategy history not reset: %s", exc)
            for sym in self.symbols:
                self.st[sym] = self.halt[sym] = None
        log.warning("HH/LL strategy reset%s", " (trades and stats cleared)" if clear_history else "")

    def _load(self, sym: str) -> None:
        """The operator's threshold from the console, and this strategy's open trade if the venue still holds it."""
        if self.db is None:
            return
        try:
            rows = self.db.execute("SELECT value FROM deltabot.algo_state WHERE mode=%s AND symbol=%s AND name=%s",
                                   (self.mode, sym, STATE), fetch=True) or []
            v = rows[0]["value"] if rows else {}
            if self.key in v:                     # settings of an older logic (or unit) leave the default
                self.x[sym] = float(v[self.key])
            rows = self.db.execute("SELECT value FROM deltabot.algo_state WHERE mode=%s AND symbol=%s AND name=%s",
                                   (self.mode, sym, SMOOTH_STATE), fetch=True) or []
            if rows and "pct" in rows[0]["value"]:
                self.smooth[sym] = float(rows[0]["value"]["pct"])
            rows = self.db.execute(
                "SELECT id, side, size, entry_price, fees FROM deltabot.hhll_trades "
                "WHERE mode=%s AND symbol=%s AND state='OPEN' ORDER BY id DESC", (self.mode, sym), fetch=True) or []
            pos = self._position(sym)
            for i, r in enumerate(rows):
                d = 1 if r["side"] == "LONG" else -1
                if i == 0 and pos and (pos > 0) == (d > 0):
                    self.st[sym] = {"dir": d, "size": int(r["size"]), "entry_price": float(r["entry_price"]),
                                    "entry_fee": float(r["fees"]), "id": r["id"]}
                else:
                    self._lost(r["id"], "not on the venue at restart")
        except Exception as exc:  # noqa: BLE001 - the default stands
            log.warning("[%s] stored threshold / open trade unreadable: %s", sym, exc)

    def _lost(self, trade_id, why: str) -> None:
        if self.db is not None and trade_id:
            self.db.execute("UPDATE deltabot.hhll_trades SET state='LOST', exit_reason=%s WHERE id=%s", (why, trade_id))

    def _build(self, sym: str) -> bool:
        """The chain. Printed pivots never move while the logic stands; a new logic redraws them (operator, 16 Nov).
        So: the stored chain, continued over the prints recorded since its last pivot, if it was drawn by this
        logic and this threshold; otherwise a fresh one run over the recorded prints. False while the feed has no
        price yet."""
        q = self.feed.quote(sym, max_age=float("inf"))
        price = q and (q.last or q.mark)
        if not price:
            return False
        inst = self.instruments[sym]
        spread = q.ask - q.bid if q.bid and q.ask and q.ask > q.bid else float(inst.tick_size)
        self.cost[sym] = cost_pts(inst, price, spread)
        z = self.zz[sym] = ZigZag(self.pct(sym))
        self.pivots[sym].clear()
        rows, since = [], None
        if self.db is not None:
            try:
                got = self.db.execute("SELECT value FROM deltabot.algo_state WHERE mode=%s AND symbol=%s AND name=%s",
                                      (self.mode, sym, CHAIN), fetch=True) or []
                saved = got and got[0]["value"]
                if saved and saved.get("pivots") and saved.get("logic") == self._logic(sym):
                    self.pivots[sym].extend(saved["pivots"])
                    lp = saved["pivots"][-1]
                    z.resume(lp["type"], lp["price"], lp["at"], lp["confirmed_price"], lp["confirmed_at"])
                    since = lp["confirmed_at"]
            except Exception as exc:  # noqa: BLE001 - unreadable: a fresh chain
                log.warning("[%s] stored pivot chain unreadable: %s", sym, exc)
            rows = self._rows(sym)
        for r in rows:
            ms = int(r["at"])
            if since is not None and ms <= since:   # ponytail: prints in the confirming tick's own ms are skipped
                continue                             # (the table keeps ms); key on tick id if that ever matters
            self._note(sym, z.update(float(r["price"]), ms))
        self._keep(sym)
        log.info("[%s] HH/LL chain (plain zigzag): threshold %g%%; %d pivots from %d recorded prints",
                 sym, z.R, len(self.pivots[sym]), len(rows))
        self._build_smooth(sym, rows)
        return True

    def _rows(self, sym: str) -> list:
        """The recorded prints (a day), oldest first."""
        if self.db is None:
            return []
        try:
            # ponytail: the prints between the last recorded second and the live cursor are skipped; replay the
            # feed's hand-off buffer over them if a pivot a second off at start ever matters.
            return self.db.execute(
                "SELECT price, (extract(epoch FROM traded_at) * 1000)::bigint AS at FROM deltabot.ticks "
                "WHERE symbol=%s ORDER BY traded_at, id", (sym,), fetch=True) or []
        except Exception as exc:  # noqa: BLE001 - no history: the chain starts from live prints
            log.warning("[%s] pivot chain not seeded: %s", sym, exc)
            return []

    def _build_smooth(self, sym: str, rows: list) -> None:
        """The smoothed line over the recorded prints, and its pivot chain: the stored one continued if drawn by
        this logic (smoother + zigzag threshold), else a fresh one. As `_build`: printed pivots never move."""
        self.sm[sym] = Smoother(self.smooth[sym])
        z = self.szz[sym] = ZigZag(self.pct(sym))
        self.spivots[sym].clear()
        self.sline[sym].clear()
        since = None
        if self.db is not None:
            try:
                got = self.db.execute("SELECT value FROM deltabot.algo_state WHERE mode=%s AND symbol=%s AND name=%s",
                                      (self.mode, sym, SMOOTH_CHAIN), fetch=True) or []
                saved = got and got[0]["value"]
                if saved and saved.get("pivots") and saved.get("logic") == self._smooth_logic(sym):
                    self.spivots[sym].extend(saved["pivots"])
                    lp = saved["pivots"][-1]
                    z.resume(lp["type"], lp["price"], lp["at"], lp["confirmed_price"], lp["confirmed_at"])
                    since = lp["confirmed_at"]
            except Exception as exc:  # noqa: BLE001 - unreadable: a fresh chain
                log.warning("[%s] stored smoothed pivot chain unreadable: %s", sym, exc)
        for r in rows:
            self._smooth_feed(sym, float(r["price"]), int(r["at"]), since)
        self._keep_smooth(sym)
        log.info("[%s] smoothed line: threshold %g%%; %d pivots on it (zigzag %g%%)",
                 sym, self.smooth[sym], len(self.spivots[sym]), z.R)

    def _smooth_feed(self, sym: str, p: float, ms: int, since: Optional[int] = None) -> Optional[Pivot]:
        """A print into the smoothed line. -> the pivot it printed, if any."""
        return self._smooth_seconds(sym, self.sm[sym].feed(p, ms), since)

    def _smooth_seconds(self, sym: str, seconds: list, since: Optional[int] = None) -> Optional[Pivot]:
        """Closed seconds of the smoothed line into its zigzag. -> the last pivot printed, if any."""
        printed = None
        for at, v in seconds:
            self.sline[sym].append((at, v))
            if since is not None and at <= since:
                continue
            pv = self.szz[sym].update(v, at)
            if pv is not None:
                self.spivots[sym].append({"type": pv.kind, "price": pv.price, "at": pv.ms,
                                          "confirmed_price": pv.conf_price, "confirmed_at": pv.conf_ms})
                printed = pv
        return printed

    def _smooth_logic(self, sym: str) -> list:
        return [SMOOTH_LOGIC, self.smooth[sym]] + self._logic(sym)

    def _keep_smooth(self, sym: str) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                """INSERT INTO deltabot.algo_state (mode, name, symbol, value, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, now())
                   ON CONFLICT (mode, name, symbol) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (self.mode, SMOOTH_CHAIN, sym,
                 json.dumps({"pivots": list(self.spivots[sym]), "logic": self._smooth_logic(sym)})))
        except Exception as exc:  # noqa: BLE001 - the next pivot stores it again
            log.warning("[%s] smoothed pivot chain not stored: %s", sym, exc)

    def set_smooth(self, sym: str, x) -> float:
        """The smoother's box, % of price. A new logic: the line and its pivots are redrawn with it."""
        sym = str(sym).upper()
        if sym not in self.zz:
            raise ValueError(f"{sym or 'that symbol'} is not traded by the strategy")
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not SMOOTH_MIN <= x <= SMOOTH_MAX:
            raise ValueError(f"the smoothing threshold must be {SMOOTH_MIN:g} to {SMOOTH_MAX:g} %")
        with self._lock:
            self.smooth[sym] = float(x)
            if self.db is not None:
                self.db.execute(
                    """INSERT INTO deltabot.algo_state (mode, name, symbol, value, updated_at)
                       VALUES (%s, %s, %s, %s::jsonb, now())
                       ON CONFLICT (mode, name, symbol) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                    (self.mode, SMOOTH_STATE, sym, json.dumps({"pct": self.smooth[sym]})))
            if self.zz[sym] is not None:
                self._build_smooth(sym, self._rows(sym))
        log.warning("[%s] smoothing threshold set to %g%% by the operator", sym, x)
        return float(x)

    def smooth_series(self, symbol: str, seconds: int, points: int = 3000) -> dict:
        """The smoothed line over the last `seconds` (at most `points` of its seconds, evenly, and always the
        newest), and where it stands now, for the chart to go on from with the prints after `fed`."""
        sym = str(symbol).upper()
        with self._lock:
            sm, line = self.sm.get(sym), self.sline.get(sym, ())
            start = time.time() * 1000 - seconds * 1000
            got = [p for p in line if p[0] >= start] if line and line[0][0] < start else list(line)
            state = sm.state() if sm else None
            fed = sm.fed if sm else None
            pct = self.smooth.get(sym)
        step = max(1, -(-len(got) // points))
        pts = got[::step]
        if got and pts[-1] is not got[-1]:
            pts.append(got[-1])
        return {"symbol": sym, "pct": pct, "points": [list(p) for p in pts], "state": state, "fed": fed}

    def _keep(self, sym: str) -> None:
        """Store the printed pivots: a restart continues this chain, it never recomputes it."""
        if self.db is None:
            return
        try:
            self.db.execute(
                """INSERT INTO deltabot.algo_state (mode, name, symbol, value, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, now())
                   ON CONFLICT (mode, name, symbol) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (self.mode, CHAIN, sym, json.dumps({"pivots": list(self.pivots[sym]), "logic": self._logic(sym)})))
        except Exception as exc:  # noqa: BLE001 - the next pivot stores it again
            log.warning("[%s] pivot chain not stored: %s", sym, exc)

    def pct(self, sym: str) -> float:
        """The reversal that prints a pivot, in % of the extreme: the box itself, or x SENSEX's cost on v3."""
        return self.x[sym] * tickfilter.SENSEX_COST_PCT if self.xcost else self.x[sym]

    def _logic(self, sym: str) -> list:
        # The ticks it runs on are part of the logic: v3's are SENSEX-like (config.TICK_FILTER), so its tag says so
        # and a change of that rule redraws; v1 / v2 keep their tag, so their chains are not redrawn by this.
        return [LOGIC, self.x[sym]] + ([tickfilter.LOGIC, "x-sensex-cost"] if self.xcost else [])

    def _note(self, sym: str, pv: Optional[Pivot]) -> None:
        if pv is not None:
            self.pivots[sym].append({"type": pv.kind, "price": pv.price, "at": pv.ms,
                                     "confirmed_price": pv.conf_price, "confirmed_at": pv.conf_ms})

    def set_threshold(self, sym: str, x) -> float:
        """The console's box: the reversal that prints a pivot, in % of the extreme (v3: x SENSEX's cost). A new
        logic: the chain is redrawn with it."""
        sym = str(sym).upper()
        if sym not in self.zz:
            raise ValueError(f"{sym or 'that symbol'} is not traded by the strategy")
        lo, hi, unit = (XCOST_MIN, XCOST_MAX, "x SENSEX cost") if self.xcost else (X_MIN, X_MAX, "%")
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not lo <= x <= hi:
            raise ValueError(f"the threshold must be {lo:g} to {hi:g} {unit}")
        with self._lock:
            self.x[sym] = float(x)
            if self.db is not None:
                self.db.execute(
                    """INSERT INTO deltabot.algo_state (mode, name, symbol, value, updated_at)
                       VALUES (%s, %s, %s, %s::jsonb, now())
                       ON CONFLICT (mode, name, symbol) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                    (self.mode, STATE, sym, json.dumps({self.key: self.x[sym]})))
            self._build(sym)
        log.warning("[%s] threshold set to %g %s (%g%%) by the operator", sym, x, self.key, self.pct(sym))
        return float(x)

    # ------------------------------------------------------------------ the loop
    def _run(self) -> None:
        # Woken by the feed on every print, not polled: a pivot is confirmed on the print that confirms it.
        seq = -1
        while not self._stop.is_set():
            seq = self.feed.wait_for_tick(seq, 0.25)
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - one bad pass must not stop the strategy
                log.exception("HH/LL step failed: %s", exc)

    def step(self) -> None:
        self._cur, trades = self.feed.trades_since(self._cur)
        with self._lock:
            for sym, p, ms in trades:
                if sym not in self.zz or (self.zz[sym] is None and not self._build(sym)):
                    continue
                spv = self._smooth_feed(sym, float(p), int(ms))
                if spv is not None:
                    self._keep_smooth(sym)
                    if self.trades_on == "smoothed":
                        self._act(sym, spv)
                pv = self.zz[sym].update(float(p), int(ms))      # the prints' own pivot
                if pv is not None:
                    self._note(sym, pv)
                    self._keep(sym)
                    if self.trades_on == "ticks":
                        self._act(sym, pv)
                    else:
                        self.feed._notify()      # the console's stream: push the confirmation now, not on the next print
                if (pv is None or self.trades_on != "ticks") and (spv is None or self.trades_on != "smoothed"):
                    self._guard(sym, float(p))
            # A quiet second still ends: closed on the clock, so a pivot confirmed by it trades without waiting for
            # the next print.
            now = self.clock() * 1000
            for sym in self.symbols:
                if self.sm[sym] is not None:
                    spv = self._smooth_seconds(sym, self.sm[sym].clock(now, SMOOTH_GRACE_MS))
                    if spv is not None:
                        self._keep_smooth(sym)
                        if self.trades_on == "smoothed":
                            self._act(sym, spv)

    def _act(self, sym: str, pv: Optional[Pivot]) -> None:
        """A pivot confirmed: traded."""
        if pv is None:
            return
        self.feed._notify()
        log.info("[%s] pivot %s at %.3f (confirmed at %.3f)", sym, pv.kind, pv.price, pv.conf_price)
        try:
            self._trade(sym, pv)
        except Exception as exc:  # noqa: BLE001 - this pivot is lost; the next one puts the side right
            self.halt[sym] = f"order failed: {exc}"
            log.exception("[%s] %s not traded: %s", sym, pv.kind, exc)

    def _trade(self, sym: str, pv: Pivot) -> None:
        """Bring the venue's position to the pivot's side (`self.sides`; v1 HH short, LL long)."""
        want = self.sides[pv.kind]
        self.halt[sym] = None
        if self.feed.quote(sym) is None:
            self.halt[sym] = "feed stale"
            log.warning("[%s] %s not traded: the feed is stale", sym, pv.kind)
            return
        pos = self._position(sym)
        if not pos and self.st[sym]:                           # closed by hand since: nothing of ours to book
            self._lost(self.st[sym]["id"], "closed outside the strategy")
            self.st[sym] = None
        if pos and (pos > 0) == (want > 0):
            return
        if pos:
            q, v, f = self._cross(sym, "sell" if pos > 0 else "buy", abs(pos), True, f"hhll {pv.kind} exit")
            self._closed(sym, q, v, f, f"{pv.kind} pivot")
            if self._position(sym):
                self.halt[sym] = "exit not filled"
                return
        side = "buy" if want > 0 else "sell"
        q = self.feed.quote(sym)              # sized on the real print, never the (v3: filtered) tick that confirmed
        size = self._size(sym, side, q.last or q.mark)
        if size < 1:
            self.halt[sym] = "no margin for one contract"
            return
        q, v, f = self._cross(sym, side, size, False, f"hhll {pv.kind} entry")
        if not q:
            self.halt[sym] = "entry not filled"
            return
        self._opened(sym, want, pv, q, v, f)

    def _lock_pct(self, sym: str) -> float:
        """The lock's distance from entry, % : LOCK_AT, never under the round trip's fee."""
        inst = self.instruments[sym]
        rate = inst.maker_fee if self.execution_mode in ("maker", "fast_maker") else inst.taker_fee
        return max(LOCK_AT, 2 * rate * (1 + charges.GST_RATE) * 100)

    def _guard(self, sym: str, p: float) -> None:
        """The breakeven lock on the open trade, on every real print."""
        st = self.st[sym]
        if not st or LOCK_ARM <= 0:
            return
        d, e = st["dir"], st["entry_price"]
        if d * (p - st.get("best", e)) > 0:
            st["best"] = p
        if not st.get("armed"):
            if d * (st.get("best", e) - e) < LOCK_ARM / 100 * e:
                return
            st["armed"] = True
            st["lock"] = e * (1 + d * self._lock_pct(sym) / 100)
            log.info("[%s] lock armed: %s exits at %.4f if price comes back", sym, "LONG" if d > 0 else "SHORT",
                     st["lock"])
        if d * (p - st["lock"]) > 0:
            return
        pos = self._position(sym)
        if not pos or (pos > 0) != (d > 0):                 # closed or turned outside the strategy
            self._lost(st["id"], "closed outside the strategy")
            self.st[sym] = None
            return
        q, v, f = self._cross(sym, "sell" if pos > 0 else "buy", abs(pos), True, "hhll lock exit")
        if not q:                                           # tried again on the next print
            self.halt[sym] = "lock exit not filled"
            return
        self.halt[sym] = None
        self._closed(sym, q, v, f, "breakeven lock")
        self.feed._notify()

    # ------------------------------------------------------------------ orders
    def _position(self, sym: str) -> int:
        for p in self.exchange.positions_view():
            if p["symbol"] == sym:
                return int(p["size"])
        return 0

    def _size(self, sym: str, side: str, price: float) -> int:
        inst = self.instruments[sym]
        fixed = os.environ.get("HHLL_CONTRACTS")
        if fixed and fixed.strip():
            try:
                cnt = int(fixed.strip())
                if cnt > 0:
                    limit = getattr(inst, "position_size_limit", None)
                    return min(cnt, int(limit)) if limit else cnt
            except ValueError:
                pass
        usable = float(self.exchange.account().get("available") or 0.0) * 0.98
        is_maker = self.execution_mode in ("maker", "fast_maker")
        per = charges.order_quote(inst, side, 1, price, LEVERAGE, maker=is_maker)["required"]
        size = int(usable // per) if per > 0 else 0
        limit = getattr(inst, "position_size_limit", None)
        return min(size, int(limit)) if limit else size

    def _maker_price(self, sym: str, side: str) -> Optional[float]:
        """Calculates top passive post-only maker price inside touch / spread."""
        quote = self.feed.quote(sym)
        if quote is None:
            return None
        inst = self.instruments.get(sym)
        tick = Decimal(str(getattr(inst, "tick_size", 0.5))) if inst else Decimal("0.5")
        b, a = quote.bid, quote.ask
        if b and a:
            p = maker_price(side, b, a, tick)
        else:
            p = b if side == "buy" else a
        if p is not None and inst and hasattr(inst, "round_price"):
            return float(inst.round_price(p))
        return float(p) if p else None

    def _cross(self, sym: str, side: str, size: int, reduce_only: bool, reason: str) -> tuple[int, float, float]:
        """Order execution for entries and exits.
        In maker mode: executes as strict post-only maker limit with rapid touch-chasing (<100ms target).
        In fast_limit mode: executes as crossing marketable limit.
        -> (filled contracts, their value, fees)."""
        q, v, f = 0, 0.0, 0.0
        quote = self.feed.quote(sym, max_age=float("inf"))
        through = max(1, int(BUFFER * (quote.last or quote.mark) / float(self.instruments[sym].tick_size))) if quote else 1
        is_maker = self.execution_mode in ("maker", "fast_maker") and hasattr(self.fast_limit, "place_fast_maker_limit")
        timeout_s = float(os.environ.get("HHLL_MAKER_TIMEOUT", "2.0")) if os.environ.get("HHLL_MAKER_TIMEOUT") else None
        for _ in range(5):
            if q >= size:
                break
            try:
                if is_maker:
                    o = self.fast_limit.place_fast_maker_limit(
                        sym, side, size - q,
                        maker_price_fn=self._maker_price,
                        timeout_s=timeout_s,
                        reduce_only=reduce_only,
                        reason=reason,
                    )
                else:
                    o = self.fast_limit.place_fast_limit(
                        sym, side, size - q,
                        slippage_ticks=through,
                        reduce_only=reduce_only,
                        reason=reason,
                    )
                if str(o.get("state")) not in DONE:
                    time.sleep(0.2)
                    try:
                        self.exchange.cancel_order(o["order_id"])
                    except Exception:  # noqa: BLE001 - filled meanwhile
                        pass
                    o = self.exchange.order_status(o["order_id"]) or o
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] %s %d not placed: %s", sym, side, size - q, exc)
                time.sleep(0.2)
                continue
            got = int(o.get("size") or 0) - int(o.get("unfilled_size") or 0)
            if got > 0 and o.get("avg_fill_price"):
                q, v, f = q + got, v + got * float(o["avg_fill_price"]), f + float(o.get("paid_commission") or 0.0)
        return q, v, f

    # ------------------------------------------------------------------ the books
    def _opened(self, sym: str, d: int, pv: Pivot, q: int, v: float, fee: float) -> None:
        price, side = v / q, "LONG" if d > 0 else "SHORT"
        self.st[sym] = {"dir": d, "size": q, "entry_price": price, "entry_fee": fee, "id": None}
        if self.db is not None:
            try:
                leg = self.zz[sym].R if self.trades_on == "ticks" else self.szz[sym].R
                rows = self.db.execute(
                    """INSERT INTO deltabot.hhll_trades (mode, symbol, side, size, pivot, leg, entry_price, entry_at,
                       fees, state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN') RETURNING id""",
                    (self.mode, sym, side, q, pv.kind, leg, price, datetime.now(timezone.utc), fee),
                    fetch=True)
                self.st[sym]["id"] = rows[0]["id"] if rows else None
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] trade not saved: %s", sym, exc)
        tag = "MAKER " if self.execution_mode in ("maker", "fast_maker") else ""
        log.info("[%s] %s: OPEN %s%s %d @ %s (fee $%.3f)", sym, pv.kind, tag, side, q, price, fee)

    def _closed(self, sym: str, q: int, v: float, fee: float, reason: str) -> None:
        # ponytail: a part-filled exit books the whole trade at what did fill; split the row if the venue ever
        # leaves part of a crossing exit unfilled.
        st, self.st[sym] = self.st[sym], None
        if not st or not q:
            return
        price = v / q
        gross = st["dir"] * (price - st["entry_price"]) * min(q, st["size"]) * float(self.instruments[sym].contract_value)
        fees = st["entry_fee"] + fee
        if self.db is not None and st["id"]:
            try:
                self.db.execute(
                    """UPDATE deltabot.hhll_trades SET exit_price=%s, exit_at=now(), exit_reason=%s,
                       gross_pnl=%s, fees=%s, net_pnl=%s, state='CLOSED' WHERE id=%s""",
                    (price, reason, gross, fees, gross - fees, st["id"]))
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] closed trade %s not saved: %s", sym, st["id"], exc)
        tag = "MAKER " if self.execution_mode in ("maker", "fast_maker") else ""
        log.info("[%s] CLOSED %s%s %d: %s -> %s (%s) gross $%+.3f fees $%.3f net $%+.3f", sym,
                 tag, "LONG" if st["dir"] > 0 else "SHORT", st["size"], st["entry_price"], price, reason, gross, fees,
                 gross - fees)

    # ------------------------------------------------------------------ console
    def pivot_list(self, symbol: str) -> list[dict]:
        """The chart's pivots for `symbol`, oldest first (HH and LL, alternating)."""
        with self._lock:
            return list(self.pivots.get(str(symbol).upper(), ()))

    def smooth_pivot_list(self, symbol: str) -> list[dict]:
        """The smoothed line's pivots for `symbol`, oldest first (HH and LL, alternating); never traded."""
        with self._lock:
            return list(self.spivots.get(str(symbol).upper(), ()))

    def trades(self, symbol: Optional[str] = None, limit: int = 50) -> list[dict]:
        if self.db is None:
            return []
        syms = [symbol.upper()] if symbol else list(self.symbols)
        rows = self.db.execute("SELECT * FROM deltabot.hhll_trades WHERE mode = %s AND symbol = ANY(%s) "
                               f"ORDER BY id DESC LIMIT {int(limit)}", (self.mode, syms), fetch=True) or []
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]

    def status(self, cached_db: bool = False) -> dict[str, Any]:
        """`cached_db`: the trade metrics and recent trades as last read (the rest is live, every call)."""
        if not (cached_db and self._db_part is not None):
            self._db_part = self._db_status()
        metrics, recent = self._db_part
        with self._lock:
            symbols = {}
            for s in self.symbols:
                st, z, piv, sz, spiv = self.st[s], self.zz[s], self.pivots[s], self.szz[s], self.spivots[s]
                run = None if z is None or not z.dir else (   # the print at the trigger confirms it
                    {"price": z.hp if z.dir > 0 else z.lp, "at": z.hm if z.dir > 0 else z.lm, "trigger": z.trigger()})
                srun = None if sz is None or not sz.dir else (
                    {"price": sz.hp if sz.dir > 0 else sz.lp, "at": sz.hm if sz.dir > 0 else sz.lm,
                     "trigger": sz.trigger()})
                ref = z and (z.hp if z.dir > 0 else z.lp) if z and z.dir else None
                symbols[s] = {"phase": "open" if st else "idle", "threshold": self.x[s],
                              "unit": self.key, "threshold_pct": self.pct(s),
                              "threshold_pts": ref * self.pct(s) / 100 if ref else None, "cost": self.cost[s],
                              "tick": float(self.instruments[s].tick_size), "halt": self.halt[s],
                              "side": {1: "LONG", -1: "SHORT"}.get(st["dir"]) if st else None,
                              "entry_price": st["entry_price"] if st else None, "working": None,
                              "lock": st.get("lock") if st and st.get("armed") else None,
                              "last_pivot": piv[-1] if piv else None, "pivot_count": len(piv), "running": run,
                              "swing": {1: "rising", -1: "falling"}.get(z.dir) if z else None,
                              "smooth": self.smooth[s], "smooth_pivot_count": len(spiv),
                              "smooth_last_pivot": spiv[-1] if spiv else None, "smooth_running": srun,
                              "smooth_swing": {1: "rising", -1: "falling"}.get(sz.dir) if sz else None,
                              "trades_on": self.trades_on,
                              "execution_mode": self.execution_mode}
        return {"strategy": "hhll", "mode": self.mode, "execution_mode": self.execution_mode,
                "symbols": symbols, "metrics": metrics, "recent_trades": recent}

    def _db_status(self) -> tuple[dict, list]:
        metrics = {}
        if self.db is not None:
            try:
                r = self.db.execute(
                    """SELECT count(*) AS n, count(*) FILTER (WHERE net_pnl > 0) AS wins,
                              coalesce(sum(net_pnl), 0) AS net, coalesce(sum(fees), 0) AS fees
                       FROM deltabot.hhll_trades WHERE mode = %s AND state = 'CLOSED' AND symbol = ANY(%s)""",
                    (self.mode, list(self.symbols)), fetch=True)[0]
                metrics = {"trades": int(r["n"]), "wins": int(r["wins"]), "net_usd": round(float(r["net"]), 4),
                           "fees_usd": round(float(r["fees"]), 4)}
            except Exception as exc:  # noqa: BLE001
                metrics = {"error": str(exc)}
        return metrics, self.trades(limit=20)


# ── self-check ───────────────────────────────────────────────────────────────────
def _demo() -> None:
    import random
    from types import SimpleNamespace as NS

    from instruments import from_row

    rnd, z, p, prev, seg = random.Random(7), ZigZag(3.0), 100.0, None, []
    for i in range(300_000):                      # a random walk: alternating, each pivot the extreme since the last
        p = max(10.0, round(p * (1 + rnd.choice((-0.002, 0.002))), 4))
        seg.append(p)
        pv = z.update(p, i)
        if pv:                                    # ... confirmed 3% back from it
            assert pv.conf_ms == i and pv.conf_price == p and (prev is None or pv.kind != prev.kind)
            assert pv.price == (max(seg) if pv.kind == "HH" else min(seg)) and abs(pv.price - p) >= 0.03 * pv.price - 1e-9
            prev, seg = pv, [p]
    assert prev is not None and prev.conf_ms > 290_000

    inst = from_row({"symbol": "XAUTUSD", "id": 1, "tick_size": "0.1", "contract_value": "0.001",
                     "taker_commission_rate": "0.0001", "maker_commission_rate": "0.0001", "initial_margin": "1",
                     "maintenance_margin": "0.5"})
    assert abs(cost_pts(inst, 4000.0, 0.1) - (4000 * 2 * 0.0001 * 1.18 + 0.1)) < 1e-12

    class Feed:
        def __init__(self):
            self.t, self.q = [], NS(last=4000.0, mark=4000.0, bid=3999.95, ask=4000.05)
        def trades_since(self, n):
            return len(self.t), self.t[n:]
        def quote(self, sym, max_age=None):
            return self.q
        def _notify(self):
            self.notified = getattr(self, "notified", 0) + 1

    class Venue:                                  # fills every crossing limit whole, at the last print, fee $1
        def __init__(self, feed):
            self.feed, self.pos, self.sent = feed, 0, []
        def positions_view(self):
            return [{"symbol": "XAUTUSD", "size": self.pos}] if self.pos else []
        def account(self):
            return {"available": 1000.0}
        def place_fast_limit(self, sym, side, size, *, slippage_ticks=None, reduce_only=False, reason=""):
            assert slippage_ticks == int(0.02 * self.feed.q.last / 0.1)          # 2% through the touch, in ticks
            self.sent.append((side, size, reduce_only))
            self.pos += size if side == "buy" else -size
            return {"order_id": "1", "state": "closed", "size": size, "unfilled_size": 0,
                    "avg_fill_price": self.feed.q.last, "paid_commission": 1.0}
        def place_fast_maker_limit(self, sym, side, size, *, maker_price_fn=None, timeout_s=None, reduce_only=False, reason=""):
            self.sent.append((side, size, reduce_only))
            self.pos += size if side == "buy" else -size
            p = maker_price_fn(sym, side) if maker_price_fn else self.feed.q.last
            return {"order_id": "1", "state": "closed", "size": size, "unfilled_size": 0,
                    "avg_fill_price": p or self.feed.q.last, "paid_commission": 0.2, "is_maker": True}

    feed = Feed()
    venue = Venue(feed)
    algo = HHLL(feed, venue, {"XAUTUSD": inst}, venue, trades_on="smoothed", execution_mode="fast_limit")
    algo.x["XAUTUSD"] = 0.05                      # 2 points at 4000
    algo.smooth["XAUTUSD"] = 0.1                  # the line shows moves of 4 points or more
    assert algo._build("XAUTUSD") and algo.zz["XAUTUSD"].R == 0.05

    def run(*prices):
        for p in prices:
            feed.q.last = float(p)
            feed.t.append(["XAUTUSD", float(p), len(feed.t) * 1000])     # a print a second
            algo.step()

    # Trades are taken on the smoothed line's pivots, at the second that confirms them (the white dash); the
    # prints' own pivots only go on the chart. Every fill is at the real print (`feed.q.last`), never the line.
    run(*[4000] * 60)
    run(4005)                                     # 5 up (0.05% = 2): the prints' LL, not traded
    assert [x["type"] for x in algo.pivot_list("XAUTUSD")] == ["LL"] and not venue.sent
    n = 0
    while not algo.smooth_pivot_list("XAUTUSD"):  # the line climbs; its LL confirms 2 above its low
        assert not venue.sent
        run(4005)
        n += 1
    assert 3 <= n <= 40, n
    sp = algo.smooth_pivot_list("XAUTUSD")
    assert sp[0]["type"] == "LL" and sp[0]["confirmed_at"] == (len(feed.t) - 1) * 1000   # this second's end
    assert sp[0]["confirmed_price"] - sp[0]["price"] >= sp[0]["price"] * 0.0005 - 1e-9
    size = venue.sent[0][1]
    assert venue.sent == [("buy", size, False)] and venue.pos == size and feed.notified >= 2
    assert size == int(980 // charges.order_quote(inst, "buy", 1, 4005.0, LEVERAGE)["required"])
    assert algo.st["XAUTUSD"]["entry_price"] == 4005.0 != sp[0]["confirmed_price"]    # the real print, not the line
    s = algo.status()["symbols"]["XAUTUSD"]
    assert s["side"] == "LONG" and s["smooth_last_pivot"] == sp[0] and s["smooth_swing"] == "rising"
    run(*[4005] * 60, 3995)                       # the prints' HH at once; the line's HH a few seconds on
    assert [x["type"] for x in algo.pivot_list("XAUTUSD")] == ["LL", "HH"] and len(venue.sent) == 1
    while len(algo.smooth_pivot_list("XAUTUSD")) < 2:
        run(3995)
    assert algo.smooth_pivot_list("XAUTUSD")[1]["type"] == "HH"
    assert venue.sent[1] == ("sell", size, True) and venue.sent[2][0] == "sell" and venue.pos < 0
    assert algo.st["XAUTUSD"]["entry_price"] == 3995.0
    # a quiet second is closed on the clock, 0.5 s after it ends: a confirmation does not wait for the next print
    algo.clock = lambda: (len(feed.t) - 1) + 1.0 + SMOOTH_GRACE_MS / 1000      # the last print's second, ended + grace
    k = len(algo.sline["XAUTUSD"])
    algo.step()
    assert len(algo.sline["XAUTUSD"]) == k + 1 and algo.sline["XAUTUSD"][-1][0] == len(feed.t) * 1000
    algo.clock = time.time
    venue.pos = 0                                 # the operator closes it by hand: the next pivot opens afresh
    run(*[3995] * 60, 4005)
    while len(algo.smooth_pivot_list("XAUTUSD")) < 3:
        run(4005)
    assert venue.pos > 0 and venue.sent[-1] == ("buy", venue.sent[-1][1], False)
    for bad in (0, 51, True, "3", None):
        try:
            algo.set_threshold("XAUTUSD", bad)
            raise AssertionError(f"threshold {bad!r} was accepted")
        except ValueError:
            pass
    assert algo.set_threshold("xautusd", 0.5) == 0.5 and algo.zz["XAUTUSD"].R == 0.5 and not algo.pivot_list("XAUTUSD")
    assert algo.szz["XAUTUSD"].R == 0.5 and not algo.smooth_pivot_list("XAUTUSD")

    class DB:                                     # algo_state and ticks only
        def __init__(self):
            self.state, self.ticks = {}, []
        def execute(self, q, args=(), fetch=False):
            if "FROM deltabot.ticks" in q:
                return [{"price": p, "at": t} for p, t in self.ticks]
            if "INSERT INTO deltabot.algo_state" in q:
                self.state[args[1]] = json.loads(args[3])
            elif "FROM deltabot.algo_state" in q:
                return [{"value": self.state[args[2]]}] if args[2] in self.state else []
            return []

    db, fa = DB(), Feed()                         # a restart continues the stored chain: nothing printed moves
    va = Venue(fa)
    a = HHLL(fa, va, {"XAUTUSD": inst}, va, db=db)
    assert a._build("XAUTUSD") and CHAIN in db.state and not a.pivot_list("XAUTUSD")
    for i, p in enumerate([4000, 4002, 4005, 4002.9, 4001, 4003.1]):
        fa.q.last = float(p)
        fa.t.append(["XAUTUSD", float(p), i * 1000])
        db.ticks.append((float(p), i * 1000))
    a.step()
    assert [x["type"] for x in a.pivot_list("XAUTUSD")] == ["LL", "HH", "LL"]
    db.ticks = db.ticks[2:] + [(4004.0, 6000)]   # the oldest prints pruned, one more since: a fresh chain would differ
    a.zz["XAUTUSD"].update(4004.0, 6000)
    b = HHLL(Feed(), va, {"XAUTUSD": inst}, va, db=db)
    assert b._build("XAUTUSD") and b.pivot_list("XAUTUSD") == a.pivot_list("XAUTUSD") and b.x["XAUTUSD"] == 0.05
    za, zb = a.zz["XAUTUSD"], b.zz["XAUTUSD"]
    assert (zb.dir, zb.hp, zb.last) == (za.dir, za.hp, za.last)
    for i, p in enumerate([4006, 4003.9, 4001, 4003, 4010, 4007], 7):   # and both go on alike
        assert za.update(p, i * 1000) == zb.update(p, i * 1000)
    b.set_threshold("XAUTUSD", 0.04)              # a new logic redraws the chain over the recorded prints
    assert [(x["type"], x["price"]) for x in b.pivot_list("XAUTUSD")] == [("HH", 4005.0), ("LL", 4001.0)]
    assert db.state[CHAIN]["logic"] == [LOGIC, 0.04] and db.state[STATE] == {"pct": 0.04}
    c = HHLL(Feed(), va, {"XAUTUSD": inst}, va, db=db)
    c._load("XAUTUSD")
    assert c.x["XAUTUSD"] == 0.04 and c._build("XAUTUSD") and c.pivot_list("XAUTUSD") == b.pivot_list("XAUTUSD")

    # The smoothed line's pivots: alternating, each at the line's extreme since the last, its dash on the line at the
    # second that confirmed it; a restart continues them, a new smoothing threshold redraws them.
    db2, f3 = DB(), Feed()
    v3 = Venue(f3)
    m = HHLL(f3, v3, {"XAUTUSD": inst}, v3, db=db2)
    m.x["XAUTUSD"] = 0.01
    assert m._build("XAUTUSD")
    rnd, p = random.Random(3), 4000.0
    for i in range(4 * 3600 * 4):                 # 4 h, a print every 250 ms
        p = round(p + rnd.choice((-0.2, 0.0, 0.2)) + (0.15 if (i // 4000) % 2 else -0.15) * rnd.random(), 2)
        f3.t.append(["XAUTUSD", p, 1_800_000_000_000 + i * 250])
        db2.ticks.append((p, 1_800_000_000_000 + i * 250))
        if i % 500 == 499:
            m.step()
    m.step()
    sp, line = m.smooth_pivot_list("XAUTUSD"), dict(m.sline["XAUTUSD"])
    assert len(sp) >= 6 and all(u["type"] != w["type"] for u, w in zip(sp, sp[1:]))
    for u, w in zip(sp, sp[1:]):
        seg = [v for t, v in line.items() if u["at"] <= t <= w["at"]]
        assert w["price"] == (max(seg) if w["type"] == "HH" else min(seg))
        assert line[w["at"]] == w["price"] and line[w["confirmed_at"]] == w["confirmed_price"]
        assert abs(w["price"] - w["confirmed_price"]) >= w["price"] * 0.0001 - 1e-9
    assert db2.state[SMOOTH_CHAIN]["pivots"] == sp
    ser = m.smooth_series("XAUTUSD", 10**9, points=1000)
    assert len(ser["points"]) <= 1001 and ser["points"][-1] == list(m.sline["XAUTUSD"][-1]) and ser["state"]["sec"]
    db2.ticks = db2.ticks[20000:]                 # pruned: a fresh line would start elsewhere
    n = HHLL(Feed(), v3, {"XAUTUSD": inst}, v3, db=db2)
    n._load("XAUTUSD")
    n.x["XAUTUSD"] = 0.01
    assert n._build("XAUTUSD") and n.smooth_pivot_list("XAUTUSD")[:len(sp)] == sp
    for bad in (0, 1.5, True, "0.02", None):
        try:
            n.set_smooth("XAUTUSD", bad)
            raise AssertionError(f"smoothing {bad!r} was accepted")
        except ValueError:
            pass
    assert n.set_smooth("XAUTUSD", 0.05) == 0.05 and db2.state[SMOOTH_STATE] == {"pct": 0.05}
    assert db2.state[SMOOTH_CHAIN]["logic"][:2] == [SMOOTH_LOGIC, 0.05] and n.smooth_pivot_list("XAUTUSD") != sp
    st = n.status()["symbols"]["XAUTUSD"]
    assert st["smooth"] == 0.05 and st["smooth_pivot_count"] == len(n.smooth_pivot_list("XAUTUSD"))

    # The line follows a rise at once and holds flat through a move back smaller than its threshold (0.3 % = 12)
    sm, line = Smoother(0.3), []
    for i, p in enumerate([4000] * 60 + [4000 + k for k in range(20)] + [4010] * 600 + [3990] * 300):
        line += [v for _, v in sm.feed(p, i * 1000)]
    up = line[:675]
    assert all(b >= a - 1e-9 for a, b in zip(up, up[1:])) and up[-1] > 4018.9, up[-1]   # never shows the 9 back
    assert line[-1] < 3990.1 and all(b <= a + 1e-9 for a, b in zip(line[700:], line[701:]))   # 30 back: shown

    from config import VERSIONS                   # a version with the sides reversed: the same pivots
    f2 = Feed()
    v2 = Venue(f2)
    r = HHLL(f2, v2, {"XAUTUSD": inst}, v2, sides={"HH": 1, "LL": -1}, trades_on="smoothed")
    r.x["XAUTUSD"] = 0.05
    r.smooth["XAUTUSD"] = 0.1
    assert r._build("XAUTUSD")

    def run2(p, n):
        for _ in range(n):
            f2.q.last = float(p)
            f2.t.append(["XAUTUSD", float(p), len(f2.t) * 1000])
            r.step()
    run2(4000, 60)
    run2(4005, 60)                                # the line's LL -> short
    assert v2.sent[0][0] == "sell" and v2.pos < 0 and r.status()["symbols"]["XAUTUSD"]["side"] == "SHORT"
    run2(3995, 60)                                # its HH -> closed, long
    assert v2.sent[1][:3:2] == ("buy", True) and v2.sent[2][0] == "buy" and v2.pos > 0
    assert [x["type"] for x in r.smooth_pivot_list("XAUTUSD")] == ["LL", "HH"]
    assert VERSIONS["v1"] == {"HH": -1, "LL": 1} and all(v["HH"] == -v["LL"] for v in VERSIONS.values())
    from types import SimpleNamespace as _NS                      # filtered ticks (v3) are a logic of their own
    fv = Feed(); fv.settings = _NS(tick_filter=True)
    hv = HHLL(fv, Venue(fv), {"XAUTUSD": inst}, Venue(fv))
    assert hv._logic("XAUTUSD") == [LOGIC, DEFAULT_XCOST, tickfilter.LOGIC, "x-sensex-cost"]
    assert a._logic("XAUTUSD")[2:] == [] and a.key == "pct" and not a.xcost
    assert hv.x["XAUTUSD"] == 18 and abs(hv.pct("XAUTUSD") - 18 * tickfilter.SENSEX_COST_PCT) < 1e-12
    assert hv._build("XAUTUSD") and abs(hv.zz["XAUTUSD"].R - 0.06597) < 1e-9     # 18 x SENSEX cost = 0.066 %
    st = hv.status()["symbols"]["XAUTUSD"]
    assert st["unit"] == "x_cost" and st["threshold"] == 18 and abs(st["threshold_pct"] - 0.06597) < 1e-9
    for bad in (0.4, 101, 0.001):
        try:
            hv.set_threshold("XAUTUSD", bad)
            raise AssertionError(f"x cost {bad!r} was accepted")
        except ValueError:
            pass
    assert hv.set_threshold("XAUTUSD", 22) == 22 and abs(hv.zz["XAUTUSD"].R - 22 * tickfilter.SENSEX_COST_PCT) < 1e-12

    # Tick trading (trades_on="ticks"): trades immediately on prints' own pivots
    ft = Feed()
    vt = Venue(ft)
    at = HHLL(ft, vt, {"XAUTUSD": inst}, vt, trades_on="ticks")
    at.x["XAUTUSD"] = 0.05
    assert at._build("XAUTUSD")
    def runt(*prices):
        for p in prices:
            ft.q.last = float(p)
            ft.t.append(["XAUTUSD", float(p), len(ft.t) * 1000])
            at.step()
    runt(4000, 4005)   # LL confirmed -> BUY
    assert [x["type"] for x in at.pivot_list("XAUTUSD")] == ["LL"]
    assert vt.sent and vt.sent[-1][0] == "buy" and vt.pos > 0
    assert at.status()["symbols"]["XAUTUSD"]["trades_on"] == "ticks"
    runt(3995)         # HH confirmed -> SELL (stop & reverse)
    assert [x["type"] for x in at.pivot_list("XAUTUSD")] == ["LL", "HH"]
    assert vt.pos < 0 and at.status()["symbols"]["XAUTUSD"]["side"] == "SHORT"

    # Breakeven lock: short filled at the maker ask (3995.05); 0.15 % in favour arms it, back to entry - 0.1 % exits
    e = at.st["XAUTUSD"]["entry_price"]
    at.zz["XAUTUSD"].r = 0.01                         # a 1 % threshold here, so only the lock acts
    runt(e * 0.9986)                                  # 0.14 %: not armed
    assert not at.st["XAUTUSD"].get("armed")
    runt(e * 0.9984, e * 0.9988)                      # armed at 0.16 %; back to 0.12 %: holds
    assert at.st["XAUTUSD"]["armed"] and abs(at.status()["symbols"]["XAUTUSD"]["lock"] - e * 0.999) < 1e-9
    n = len(vt.sent)
    runt(e * 0.9990)                                  # back to the lock (0.1 %): bought back, flat
    assert vt.pos == 0 and at.st["XAUTUSD"] is None and vt.sent[n] == ("buy", vt.sent[n - 1][1], True)
    runt(e * 0.9992)                                  # flat stays flat until the next pivot
    assert vt.pos == 0 and len(vt.sent) == n + 1

    print("hhll self-check ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _demo()
