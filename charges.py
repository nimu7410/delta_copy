"""Delta Exchange India's charges and isolated-margin arithmetic, in one place.

Both the paper venue and the order ticket's preview use these, so the number
the console quotes before an order is the number the account is charged.

Linear (USD-margined, "vanilla") perpetuals, isolated margin:

* **Trading fee** = notional x fee rate, where notional = contracts x contract
  value x fill price. Maker or taker rate per product, from the catalogue.
  **GST** of 18% is charged on the fee, so the account pays fee x 1.18.
* **Position margin** = entry notional / leverage.
* **Order margin reserved** = order margin + estimated trading fees (entry and
  exit, both with GST) - Delta's own definition.
* **Bankruptcy price** is where the unrealised loss equals the position margin.
* **Liquidation price** is where position margin + unrealised P&L equals the
  maintenance margin.
* **Funding** is exchanged every funding interval on the position's notional
  at the mark: positive rate, longs pay shorts. It is not a fee - no GST.
* **Scalper Offer** (Delta India, opt-in): a futures position opened and
  closed within the scalping window - 30 minutes for BTCUSD and ETHUSD, 15 for
  every other future - pays no closing fee, partial closes included. On BTCUSD
  the opening fee also comes back as a fee voucher, at most $100 a calendar
  month (IST). https://www.delta.exchange/support/solutions/articles/80001172745
"""

from __future__ import annotations

import math
from decimal import Decimal

GST_RATE = 0.18

SCALPER_WINDOW_S = 15 * 60
SCALPER_LONG_WINDOW_S = {"BTCUSD": 30 * 60, "ETHUSD": 30 * 60}
SCALPER_VOUCHER_SYMBOLS = ("BTCUSD",)
SCALPER_VOUCHER_CAP = 100.0           # USD per calendar month (IST)
SCALPER_EXCLUDED_SYMBOLS = ("PAXGUSD", "SLVONUSD", "XAUTUSD")


def fee(notional: float, rate: float, gst_rate: float = GST_RATE) -> dict:
    """The fee on one fill: venue fee, GST on it, and the total charged."""
    base = abs(notional) * rate
    gst = base * gst_rate
    return {"fee": base, "gst": gst, "total": base + gst}


def scalper_free(symbol: str, opened_at, now) -> bool:
    """Whether a close now, of a position opened at `opened_at`, pays no fee.

    Terms of Delta Scalper Offer:
    - BTCUSD & ETHUSD futures: 30 minutes from opening.
    - Other futures contracts: 15 minutes from opening.
    - Options, PAXGUSD, SLVONUSD, and XAUTUSD contracts are excluded.
    - 0% closing fee on eligible contracts within the window.
    """
    if opened_at is None or now is None:
        return False
    sym = symbol.upper()
    if sym in SCALPER_EXCLUDED_SYMBOLS or "-" in sym:
        return False
    window = SCALPER_LONG_WINDOW_S.get(sym, SCALPER_WINDOW_S)
    if hasattr(opened_at, "tzinfo") and hasattr(now, "tzinfo"):
        if (opened_at.tzinfo is None) != (now.tzinfo is None):
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=now.tzinfo)
            else:
                now = now.replace(tzinfo=opened_at.tzinfo)
    delta_s = (now - opened_at).total_seconds()
    return 0 <= delta_s <= window


def position_margin(notional: float, leverage: float) -> float:
    return abs(notional) / leverage


def bankruptcy_price(direction: int, entry: float, quantity: float, margin: float) -> float:
    """`direction` +1 long / -1 short, `quantity` in underlying units."""
    if quantity <= 0:
        return 0.0
    return max(0.0, entry - direction * margin / quantity)


def liquidation_price(direction: int, entry: float, quantity: float, margin: float,
                      mm_fraction: float) -> float:
    """Price at which margin + unrealised P&L falls to maintenance margin.

    Maintenance is measured on the entry notional, which is how Delta's own
    worked example resolves (entry 10000, IM 1%, MM 0.5% -> 9950).
    """
    if quantity <= 0:
        return 0.0
    cushion = margin - mm_fraction * entry * quantity
    return max(0.0, entry - direction * cushion / quantity)


def funding_payment(size: int, contract_value: float, mark: float, rate_pct: float) -> float:
    """Cash this position receives (+) or pays (-) at one funding exchange.

    `size` is signed contracts, `rate_pct` the venue's funding rate in percent
    per interval, as its ticker publishes it.
    """
    return -size * contract_value * mark * rate_pct / 100.0


def target_price(side: int, size: int, entry: float, entry_fees: float, spec,
                 net: float = 1.0, gst_rate: float = GST_RATE, exit_rate: float = None) -> float:
    """Exit price at which closing `size` contracts nets `net` after the entry
    fees already paid and an exit fee at `exit_rate` (default maker, incl. GST),
    rounded to the tick on the side that nets at least `net`. A negative `net`
    is a loss: the price loses at most that much.

    long:  q(X - E) - Fe - qXf = net  ->  X = (net + Fe + qE) / (q(1 - f))
    short: q(E - X) - Fe - qXf = net  ->  X = (qE - Fe - net) / (q(1 + f))
    """
    q = size * float(spec.contract_value)
    f = (spec.maker_fee if exit_rate is None else exit_rate) * (1 + gst_rate)
    if side > 0:
        raw = (net + entry_fees + q * entry) / (q * (1 - f))
    else:
        raw = (q * entry - entry_fees - net) / (q * (1 + f))
    if raw <= 0:
        raise ValueError("no positive price nets that much")
    tick = float(spec.tick_size)
    ticks = math.ceil(raw / tick - 1e-9) if side > 0 else math.floor(raw / tick + 1e-9)
    return float(Decimal(ticks) * spec.tick_size)


def rr_bracket(side: int, size: int, entry: float, entry_fees: float, spec,
               win: float = 1.0, loss: float = 2.0, gst_rate: float = GST_RATE) -> tuple:
    """(stop_loss, take_profit) that net -`loss` / +`win` USD after the entry
    fees paid and a taker exit fee, incl. GST - a fired bracket crosses the book.

    The stop is a stop-limit on the mark: a fill past its trigger loses more.
    """
    taker = spec.taker_fee
    return (target_price(side, size, entry, entry_fees, spec, -loss, gst_rate, taker),
            target_price(side, size, entry, entry_fees, spec, win, gst_rate, taker))


def order_quote(instrument, side: str, size: int, price: float, leverage: float, *,
                position_size: int = 0, maker: bool = False,
                gst_rate: float = GST_RATE, closing_free: bool = False) -> dict:
    """What an order would cost, before it is sent. Pure; nothing is reserved.

    `position_size` is the signed position already held: the part of an order
    that offsets it needs no margin, only the part that opens or adds does.
    `closing_free`: that part pays no fee either (the Scalper Offer).
    """
    cv = float(instrument.contract_value)
    signed = size if side == "buy" else -size
    if position_size and (position_size > 0) != (signed > 0):
        closing = min(abs(signed), abs(position_size))
    else:
        closing = 0
    opening = abs(signed) - closing
    notional = cv * size * price
    open_notional = cv * opening * price
    rate = instrument.maker_fee if maker else instrument.taker_fee
    entry = fee(cv * (opening if closing_free else size) * price, rate, gst_rate)
    exit_estimate = fee(open_notional, instrument.taker_fee, gst_rate)
    margin = position_margin(open_notional, leverage) if opening else 0.0
    after = abs(position_size + signed)
    after_notional = cv * after * price
    direction = 1 if (position_size + signed) > 0 else -1
    quote = {
        "notional": notional,
        "opening_contracts": opening,
        "closing_contracts": closing,
        "closing_free": bool(closing and closing_free),
        "margin": margin,
        "fee": entry["fee"],
        "gst": entry["gst"],
        "fee_total": entry["total"],
        "exit_fee_estimate": exit_estimate["total"],
        "required": margin + entry["total"] + (exit_estimate["total"] if opening else 0.0),
        "fee_rate": rate,
        "leverage_cap": instrument.leverage_cap(after_notional),
        "liquidation_price": None,
        "bankruptcy_price": None,
    }
    if opening and after:
        # A fresh, flipped or added-to position: liquidation is measured on
        # what it will hold. (Adding re-averages the entry; this previews it
        # as if all of it were entered at `price`.)
        qty = cv * after
        held_margin = position_margin(after_notional, leverage)
        quote["liquidation_price"] = liquidation_price(
            direction, price, qty, held_margin, instrument.mm_fraction(after_notional))
        quote["bankruptcy_price"] = bankruptcy_price(direction, price, qty, held_margin)
    return quote


if __name__ == "__main__":
    # Delta's own fee example: 1000 BTCUSD contracts at $100,000 as maker is
    # $100,000 notional, 0.02% = $20 fee, $23.60 with GST.
    f = fee(1000 * 0.001 * 100_000, 0.0002)
    assert abs(f["fee"] - 20.0) < 1e-9 and abs(f["total"] - 23.6) < 1e-9, f

    # Delta's liquidation example: entry 10000 long, IM 1%, MM 0.5%.
    q = 2.0
    m = 10_000 * q * 0.01
    assert abs(liquidation_price(1, 10_000, q, m, 0.005) - 9950) < 1e-9
    assert abs(bankruptcy_price(1, 10_000, q, m) - 9900) < 1e-9
    assert abs(liquidation_price(-1, 10_000, q, m, 0.005) - 10050) < 1e-9
    assert abs(bankruptcy_price(-1, 10_000, q, m) - 10100) < 1e-9
    # At liquidation, margin + unrealised P&L == maintenance margin exactly.
    liq = liquidation_price(1, 10_000, q, m, 0.005)
    assert abs((m + (liq - 10_000) * q) - 0.005 * 10_000 * q) < 1e-9

    # Funding: positive rate, long pays; short receives the same.
    assert abs(funding_payment(10, 0.001, 80_000, 0.01) - (-0.08)) < 1e-12
    assert abs(funding_payment(-10, 0.001, 80_000, 0.01) - 0.08) < 1e-12
    # Scalper Offer: 30 minutes on BTCUSD, 15 on the rest, boundary included.
    from datetime import datetime, timedelta
    t0 = datetime(2026, 10, 25, 23, 0)
    assert scalper_free("BTCUSD", t0, t0 + timedelta(minutes=30))
    assert not scalper_free("BTCUSD", t0, t0 + timedelta(minutes=30, seconds=1))
    assert scalper_free("ETHUSD", t0, t0 + timedelta(minutes=30))
    assert not scalper_free("ETHUSD", t0, t0 + timedelta(minutes=30, seconds=1))
    assert scalper_free("SOLUSD", t0, t0 + timedelta(minutes=15))
    assert not scalper_free("SOLUSD", t0, t0 + timedelta(minutes=16))
    assert not scalper_free("BTCUSD", None, t0)
    assert not scalper_free("PAXGUSD", t0, t0 + timedelta(minutes=5))
    assert not scalper_free("XAUTUSD", t0, t0 + timedelta(minutes=5))
    assert not scalper_free("BTC-100000-C", t0, t0 + timedelta(minutes=5))
    # 1:2 bracket: the exit at each price nets +$1 / -$2 or a hair better.
    from instruments import from_row
    btc = from_row({"symbol": "BTCUSD", "id": 27, "tick_size": "0.5", "contract_value": "0.001",
                    "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002",
                    "initial_margin": "0.5", "maintenance_margin": "0.25",
                    "product_specs": {"rate_exchange_interval": 28800}})
    for side in (1, -1):
        entry, size = 80_000.0, 10
        fe = fee(size * 0.001 * entry, 0.0002)["total"]
        sl, tp = rr_bracket(side, size, entry, fe, btc)
        def net(x):
            return side * size * 0.001 * (x - entry) - fe - fee(size * 0.001 * x, 0.0005)["total"]
        assert 1.0 <= net(tp) < 1.0 + 0.01, (side, tp, net(tp))
        assert -2.0 <= net(sl) < -2.0 + 0.01, (side, sl, net(sl))
        assert (sl < entry < tp) if side > 0 else (tp < entry < sl)
    print("charges self-check ok")
