"""How likely the price is to reach a level (the liquidation price) soon.

Volatility clusters, so the last minutes' moves say how far the next ones go:

    sigma  per-minute volatility: 0.7 x a 15-minute EWMA variance of 1m log
           returns + 0.3 x a 240-minute one
    touch  P(price reaches a level `d` away within `t` minutes)
           = 2 (1 - Phi(d / (K sigma sqrt t)))                (reflection)

Measured on 120 days of Delta India 1m candles (BTCUSD, ETHUSD), fit on the
first 60%, scored on the last 40%: forecasts of 5% came true 8-11% of the
time, 28% -> 25-27%, 48% -> 41-43%, 67% -> 55-58%; Brier 10-11% better than a
constant. Price-only drift and momentum scores were tested the same way and
had no edge after fees (|IC| < 0.04, signs flipping between halves), so they
are not here.

# ponytail: K and the 0.7 / 0.3 blend from that one fit; it over-reads the
# highest buckets a little. Refit from fresh candles if the market changes.
"""

from __future__ import annotations

import math

K = 0.9
FAST, SLOW, BLEND = 15, 240, 0.7
#: Enough 1m bars for the 240-minute variance to settle.
BARS = 1000


def sigma(closes: list[float]) -> float | None:
    """Per-minute volatility of `closes` (1m, oldest first), as a fraction."""
    if len(closes) < FAST + 1:
        return None
    fast = slow = None
    af, as_ = 2 / (FAST + 1), 2 / (SLOW + 1)
    for a, b in zip(closes, closes[1:]):
        if a <= 0 or b <= 0:
            continue
        x = math.log(b / a) ** 2
        fast = x if fast is None else fast + af * (x - fast)
        slow = x if slow is None else slow + as_ * (x - slow)
    if fast is None:
        return None
    return math.sqrt(BLEND * fast + (1 - BLEND) * slow)


def touch(distance: float, minutes: float, sig: float) -> float:
    """P(a move of `distance` (a fraction, either sign) within `minutes`)."""
    if sig <= 0:
        return 0.0
    z = abs(distance) / (K * sig * math.sqrt(minutes))
    return min(1.0, math.erfc(z / math.sqrt(2)))       # 2 (1 - Phi(z))


def liquidation_risk(candles: list[dict], price: float, liquidation: float | None) -> dict | None:
    """The chance `liquidation` is reached within 15 minutes and an hour of
    entering at `price`, and the typical 15-minute move, from 1m `candles`."""
    if not liquidation or price <= 0:
        return None
    sig = sigma([float(c["close"]) for c in candles])
    if sig is None:
        return None
    d = math.log(liquidation / price)
    return {"distance_bps": round(abs(d) * 1e4, 1),
            "move_15m_bps": round(sig * math.sqrt(15) * 1e4, 1),
            "touch_15m": round(touch(d, 15, sig), 3),
            "touch_1h": round(touch(d, 60, sig), 3)}


if __name__ == "__main__":
    import random

    # A random walk of known volatility: sigma finds it, touch matches simulation.
    rng = random.Random(7)
    s, p, closes = 0.0005, 80_000.0, []
    for _ in range(BARS):
        p *= math.exp(rng.gauss(0, s))
        closes.append(p)
    est = sigma(closes)
    assert 0.65 * s < est < 1.35 * s, est             # the 15-minute half is noisy
    hits = 0
    for _ in range(4000):
        x = 0.0
        for _ in range(15):
            x += rng.gauss(0, s)
            if x <= -0.0025:
                hits += 1
                break
    sim = hits / 4000
    model = touch(-0.0025, 15, s / K)                  # K undone: the pure random walk
    assert abs(model - sim) < 0.06, (model, sim)       # discrete steps touch a little less
    assert touch(0.01, 15, s) < touch(0.0025, 15, s) < touch(0.0025, 60, s)
    assert touch(0.001, 15, 0) == 0.0
    r = liquidation_risk([{"close": c} for c in closes], closes[-1], closes[-1] * 0.9975)
    assert r["distance_bps"] == 25.0 and 0 < r["touch_15m"] < r["touch_1h"] <= 1, r
    assert liquidation_risk([{"close": 1}] * 3, 1, 0.9) is None
    assert liquidation_risk([{"close": c} for c in closes], closes[-1], None) is None
    print("move_risk self-check ok")
