"""SENSEX-like ticks around the gold perpetual's price (operator, 17 Nov 2026; v3 only, `config.TICK_FILTER`).

Operator, 23:10: "everything should be like SENSEX ... find some way to cope up with price". Gold's prints are a
staircase (flat, then a jump; 0.03 of bid/ask jitter between); SENSEX moves a little every second. Gold's prints
do not contain that motion, so it is made: once a second, when the second is over (plus `GRACE_MS` for prints
in flight),

    anchor  += A (last print - anchor)                    A: half-life ANCHOR_HL (0.2 s): ~ the last print
    wiggle   = wiggle (1 - T) + step x anchor             T: half-life REVERT_HL (30 s) back to the price
    tick     = anchor + wiggle, to 2 decimals, emitted when it changes

`step` is one of SENSEX's own one-second moves (`STEPS`, 256 quantiles of 180k of them, Lune's 10 sessions, in
bps, drift removed), picked by a generator seeded with the symbol and the second: the same prints always give the
same ticks (live, a replay, a restart). Direction and level come only from gold; the wiggle is not market data.

Shown vs the last real print (archived gold, 6 weekdays): median 0.5, p99 1.9, max 3.75 points. Similarity to
SENSEX (tick gaps, move sizes, 5-min choppiness, reversals, lag-1 autocorrelation; SENSEX vs itself 96%): 92.8%
mean, every weekday >= 91.6% (raw gold 68%; 0.2 s smoothing alone 82.7%; 5 s smoothing 49%). Orders and fills
stay on the real prices (`Quote.last`, the book).

    .venv/bin/python tickfilter.py        self-check
"""
from __future__ import annotations

import random
import zlib

LOGIC = "sensex-ticks-1"   # in the strategy's chain tag: change it with the rule here, and v3's chain is redrawn
#: SENSEX's all-in round trip as a share of its price, in %: Lune's `hhll_tick.cost_pts` for SENSEX at 82,000 is
#: 3.005 points (78,000-86,000: 0.0038-0.0036 %). v3's threshold box is in Lune's unit, x this cost, so a number
#: there means what it means on Lune's SENSEX (operator, 17 Nov 23:20: "18 should work on this XAU chart").
SENSEX_COST_PCT = 0.003665
GRACE_MS = 250             # a second is closed this long after it ends: prints arrive ~170 ms after they trade
ANCHOR_HL = 0.2
REVERT_HL = 30.0
DIGITS = 2
MAX_STEPS = 120            # after a gap, only the last seconds are stepped
STEPS = (                  # SENSEX one-second moves, bps: 256 quantiles, mean 0
    -1.778, -1.301, -1.129, -1.017, -0.941, -0.884, -0.837, -0.795, -0.761, -0.729, -0.703, -0.676, -0.654, -0.634,
    -0.616, -0.597, -0.581, -0.565, -0.550, -0.535, -0.522, -0.508, -0.496, -0.484, -0.473, -0.462, -0.451, -0.441,
    -0.430, -0.421, -0.411, -0.402, -0.393, -0.384, -0.375, -0.367, -0.359, -0.351, -0.343, -0.336, -0.329, -0.321,
    -0.315, -0.308, -0.302, -0.295, -0.288, -0.282, -0.276, -0.270, -0.264, -0.258, -0.252, -0.246, -0.241, -0.237,
    -0.231, -0.227, -0.222, -0.217, -0.212, -0.207, -0.203, -0.198, -0.194, -0.190, -0.186, -0.181, -0.177, -0.173,
    -0.169, -0.164, -0.161, -0.157, -0.154, -0.151, -0.147, -0.144, -0.140, -0.136, -0.132, -0.129, -0.126, -0.122,
    -0.118, -0.115, -0.113, -0.110, -0.106, -0.103, -0.100, -0.097, -0.094, -0.091, -0.088, -0.086, -0.083, -0.080,
    -0.078, -0.076, -0.073, -0.071, -0.068, -0.065, -0.062, -0.059, -0.056, -0.053, -0.051, -0.048, -0.045, -0.044,
    -0.041, -0.039, -0.037, -0.035, -0.032, -0.029, -0.027, -0.024, -0.022, -0.019, -0.017, -0.014, -0.012, -0.008,
    -0.006, -0.002, 0.002, 0.004, 0.007, 0.010, 0.012, 0.015, 0.017, 0.020, 0.022, 0.025, 0.028, 0.031, 0.033,
    0.035, 0.037, 0.040, 0.041, 0.044, 0.047, 0.050, 0.052, 0.055, 0.058, 0.061, 0.064, 0.067, 0.070, 0.072, 0.074,
    0.076, 0.079, 0.082, 0.084, 0.087, 0.090, 0.093, 0.096, 0.099, 0.103, 0.106, 0.109, 0.112, 0.115, 0.118, 0.122,
    0.125, 0.129, 0.133, 0.136, 0.140, 0.144, 0.147, 0.150, 0.154, 0.157, 0.160, 0.165, 0.168, 0.173, 0.177, 0.181,
    0.186, 0.190, 0.194, 0.199, 0.204, 0.208, 0.213, 0.218, 0.223, 0.228, 0.233, 0.237, 0.242, 0.247, 0.253, 0.258,
    0.264, 0.270, 0.275, 0.281, 0.288, 0.294, 0.301, 0.307, 0.313, 0.320, 0.327, 0.334, 0.342, 0.349, 0.357, 0.365,
    0.373, 0.382, 0.391, 0.400, 0.409, 0.418, 0.428, 0.439, 0.450, 0.461, 0.472, 0.483, 0.495, 0.508, 0.521, 0.536,
    0.550, 0.565, 0.582, 0.599, 0.618, 0.636, 0.658, 0.680, 0.706, 0.733, 0.764, 0.799, 0.840, 0.887, 0.946, 1.024,
    1.130, 1.319, 1.793
)


class SensexTicks:
    def __init__(self) -> None:
        self.a = 1 - 0.5 ** (1 / ANCHOR_HL)
        self.t = 1 - 0.5 ** (1 / REVERT_HL)
        self.s: dict[str, list] = {}   # symbol -> [last print, anchor, wiggle, shown, first second not yet closed]

    def trade(self, symbol: str, price: float, ms: int) -> None:
        st = self.s.get(symbol)
        if st is None:
            self.s[symbol] = [price, None, 0.0, None, ms // 1000]
        else:
            st[0] = price

    def clock(self, now_ms: float) -> list[list]:
        """Close every second that ended `GRACE_MS` before `now_ms`: the ticks to emit, as [symbol, price, epoch
        ms at the second's end], oldest first."""
        out = []
        done = int(now_ms - GRACE_MS) // 1000          # seconds before this one are closed
        for sym, st in self.s.items():
            last, anchor, w, shown, sec = st
            if done <= sec:
                continue
            anchor = last if anchor is None else anchor
            seed = zlib.crc32(sym.encode())
            for s in range(max(sec, done - MAX_STEPS), done):
                anchor += self.a * (last - anchor)
                w = w * (1 - self.t) + STEPS[random.Random(s * 1_000_003 + seed).randrange(len(STEPS))] * 1e-4 * anchor
                v = round(anchor + w, DIGITS)
                if v != shown:
                    shown = v
                    out.append([sym, v, (s + 1) * 1000])
            st[1:] = [anchor, w, shown, done]
        out.sort(key=lambda t: t[2])
        return out


def replay(rows) -> list[list]:
    """The filter over recorded prints [(symbol, price, epoch ms), ...] oldest first, as live would emit it, up to
    the last print's own second: nothing is stamped later than that (live goes on from there)."""
    f, out = SensexTicks(), []
    for sym, p, ms in rows:
        out += f.clock(ms)
        f.trade(sym, float(p), int(ms))
    if rows:
        out += f.clock(rows[-1][2] // 1000 * 1000 + 1000 + GRACE_MS)
    return out


def _demo() -> None:
    assert len(STEPS) == 256 and abs(sum(STEPS)) < 0.01 and list(STEPS) == sorted(STEPS)
    f = SensexTicks()
    assert f.clock(10_000) == []                                   # no print: nothing
    f.trade("X", 3900.0, 1_000_100)
    assert f.clock(1_001_249) == []                                # its second is not closed before the grace
    (sym, p, at), = f.clock(1_001_250)
    assert sym == "X" and at == 1_001_000 and abs(p - 3900) < 0.5   # the price, a SENSEX second's move off it
    rows, price, rnd = [], 3900.0, random.Random(1)               # an hour of a flat-then-jump staircase
    for i in range(3600 * 4):
        if rnd.random() < 0.002:
            price = round(price + rnd.choice((-1, 1)) * rnd.choice((0.5, 1, 2)), 2)
        rows.append(("X", price + rnd.choice((0, 0.03)), 2_000_000_000 + i * 250))
    out = replay(rows)
    assert out == replay(rows)                                     # deterministic: a restart redraws the same ticks
    assert len(out) > 3000 and all(b[2] > a[2] for a, b in zip(out, out[1:]))   # ~one a second, in order
    j = 0
    devs = []
    for _, p, at in out:                                           # never far from the last real print
        while j < len(rows) and rows[j][2] < at:
            j += 1
        devs.append(abs(p - rows[j - 1][1]))
    assert max(devs) < 10 and sorted(devs)[len(devs) // 2] < 1.5, (max(devs), sorted(devs)[len(devs) // 2])
    moves = [b[1] - a[1] for a, b in zip(out, out[1:])]
    flips = sum((x > 0) != (y > 0) for x, y in zip(moves, moves[1:])) / (len(moves) - 1)
    assert 0.4 < flips < 0.65, flips                               # SENSEX reverses about half the time
    g = SensexTicks()                                              # two symbols, merged in time order
    g.trade("A", 1.0, 0); g.trade("B", 2.0, 0)
    t = g.clock(3_300)
    assert [x[2] for x in t] == sorted(x[2] for x in t) and {x[0] for x in t} == {"A", "B"}
    print("tickfilter self-check ok")


if __name__ == "__main__":
    _demo()
