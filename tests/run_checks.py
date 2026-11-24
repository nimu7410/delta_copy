"""Every runnable check in one command:  .venv/bin/python tests/run_checks.py

Module self-checks first, then the paper venue driven end to end - fees with
GST, maker/taker, leverage and margin scaling, brackets, funding, liquidation,
flips, persistence through a real scratch database - and the console's HTTP
routes. Nothing here touches a real Delta account.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import threading
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import charges  # noqa: E402
from config import FeedSettings, PaperSettings, Settings  # noqa: E402
from feed import MarketFeed  # noqa: E402
from instruments import from_row  # noqa: E402
from paper_engine import OrderRejected, PaperExchange, stop_limit_price  # noqa: E402
from pegger import Pegger, maker_price  # noqa: E402

GST = 1.18
BTC = from_row({
    "symbol": "BTCUSD", "id": 27, "tick_size": "0.5", "contract_value": "0.001",
    "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002",
    "initial_margin": "0.5", "maintenance_margin": "0.25",
    "initial_margin_scaling_factor": "0.0000025",
    "maintenance_margin_scaling_factor": "0.00000125",
    "max_leverage_notional": "100000", "position_size_limit": 125000,
    "position_notional_limit": "5000000", "product_specs": {"rate_exchange_interval": 28800},
})
ADA = from_row({
    "symbol": "ADAUSD", "id": 16614, "tick_size": "0.0001", "contract_value": "1",
    "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002",
    "initial_margin": "1", "maintenance_margin": "0.5",
    "initial_margin_scaling_factor": "0.000004", "maintenance_margin_scaling_factor": "0.000002",
    "max_leverage_notional": "50000", "position_size_limit": 1337000,
    "position_notional_limit": "250000", "product_specs": {"rate_exchange_interval": 14400},
})
INSTRUMENTS = {"BTCUSD": BTC, "ADAUSD": ADA}


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def venue(equity=10_000.0, db=None, **paper):
    feed = MarketFeed(list(INSTRUMENTS), settings=FeedSettings())
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    feed.publish("ADAUSD", 0.2, bid=0.1999, ask=0.2001, funding_rate=-0.02)
    paper.setdefault("scalper_offer", False)           # its own check turns it on
    paper.setdefault("order_latency", 0.0)             # its own check turns it on
    settings = PaperSettings(start_equity=equity, default_leverage=10.0, **paper)
    return PaperExchange(INSTRUMENTS, feed, settings, db), feed


def take(ex, symbol, side, size, **kwargs):
    """A limit at the far touch: it crosses and fills there, as taker. There
    is no market order to do this with any more."""
    q = ex.feed.quote(symbol)
    return ex.place_order(symbol, side, size, "limit_order",
                          limit_price=q.ask if side == "buy" else q.bid, **kwargs)


def flatten(ex, symbol):
    held = ex.positions[symbol].size
    return take(ex, symbol, "sell" if held > 0 else "buy", abs(held), reduce_only=True)


def rejects(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except OrderRejected as exc:
        assert exc.code == code, (exc.code, str(exc))
        return str(exc)
    raise AssertionError(f"expected {code}")


# --------------------------------------------------------------------------- #
def check_fees_and_margin():
    ex, feed = venue()
    take(ex, "BTCUSD", "buy", 100)                             # 0.1 BTC
    p = ex.positions["BTCUSD"]
    notional = 100 * 0.001 * 80_000.5
    fee = notional * 0.0005 * GST
    assert p.size == 100 and p.entry_price == 80_000.5, p
    assert close(ex.balance, 10_000 - fee), ex.balance
    assert close(p.margin, notional / 10), p.margin
    assert close(ex.account()["available"], ex.balance - notional / 10)
    # Selling at the bid: realised = (bid - entry) * qty, minus the exit fee.
    feed.publish("BTCUSD", 81_000.0, bid=81_000.0, ask=81_000.5, funding_rate=0.01)
    acc = ex.account()                                         # equity is realised only: the open gain is not in it
    assert acc["unrealised"] > 0 and close(acc["equity"], ex.balance), acc
    before = ex.balance
    flatten(ex, "BTCUSD")
    exit_notional = 100 * 0.001 * 81_000.0
    pnl = (81_000.0 - 80_000.5) * 0.1
    assert close(ex.balance, before + pnl - exit_notional * 0.0005 * GST), ex.balance
    assert "BTCUSD" not in ex.positions
    trade = ex._trades[-1] if ex._trades else None
    assert trade is None, "with no database the trade is flushed immediately"


def check_scalper_offer():
    from datetime import timedelta
    ex, feed = venue(scalper_offer=True)
    # BTCUSD inside 30 minutes: the close is free, the opening fee (ex GST) comes back.
    take(ex, "BTCUSD", "buy", 100)
    open_fee = 100 * 0.001 * 80_000.5 * 0.0005 * GST
    assert close(ex.balance, 10_000 - open_fee)
    ex.positions["BTCUSD"].opened_at -= timedelta(minutes=29)
    flatten(ex, "BTCUSD")
    pnl = (79_999.5 - 80_000.5) * 0.1
    voucher = open_fee / GST
    assert close(ex.balance, 10_000 - open_fee + pnl + voucher), ex.balance

    # Partial closes inside the window: each gets its share of the opening fee back.
    before = ex.balance
    take(ex, "BTCUSD", "buy", 10)
    fee10 = 10 * 0.001 * 80_000.5 * 0.0005 * GST
    take(ex, "BTCUSD", "sell", 4, reduce_only=True)
    assert close(ex.positions["BTCUSD"].open_fees, fee10 * 0.6)
    flatten(ex, "BTCUSD")
    assert close(ex.balance, before - fee10 + fee10 / GST + (79_999.5 - 80_000.5) * 0.01)

    # Past 30 minutes: the close pays, nothing comes back.
    take(ex, "BTCUSD", "buy", 100)
    ex.positions["BTCUSD"].opened_at -= timedelta(minutes=31)
    before = ex.balance
    flatten(ex, "BTCUSD")
    assert close(ex.balance, before + pnl - 100 * 0.001 * 79_999.5 * 0.0005 * GST)

    # Other futures: 15 minutes, close free, but no voucher.
    take(ex, "ADAUSD", "buy", 1000)
    ex.positions["ADAUSD"].opened_at -= timedelta(minutes=14)
    before = ex.balance
    flatten(ex, "ADAUSD")
    assert close(ex.balance, before + (0.1999 - 0.2001) * 1000), ex.balance
    take(ex, "ADAUSD", "buy", 1000)
    ex.positions["ADAUSD"].opened_at -= timedelta(minutes=16)
    before = ex.balance
    flatten(ex, "ADAUSD")
    assert close(ex.balance, before + (0.1999 - 0.2001) * 1000 - 1000 * 0.1999 * 0.0005 * GST)

    # The voucher stops at $100 a month.
    month = next(iter(ex._vouchers))
    ex._vouchers[month] = 99.99
    take(ex, "BTCUSD", "buy", 100)
    before = ex.balance
    flatten(ex, "BTCUSD")
    assert close(ex.balance, before + pnl + 0.01), ex.balance
    assert close(ex._vouchers[month], 100.0)
    before = ex.balance
    take(ex, "BTCUSD", "buy", 100)
    flatten(ex, "BTCUSD")
    assert close(ex.balance, before - open_fee + pnl), "cap reached: nothing back"


def check_maker_limit_and_post_only():
    ex, feed = venue()
    order = ex.place_order("BTCUSD", "buy", 10, "limit_order", limit_price=79_000)
    assert order["state"] == "open" and order["reserved_margin"] > 0, order
    reserved = order["reserved_margin"]
    notional = 10 * 0.001 * 79_000
    expected = notional / 10 + notional * 0.0005 * GST * 2
    assert close(reserved, expected), (reserved, expected)
    assert close(ex.account()["available"], 10_000 - reserved)
    ex.poll()
    assert ex.orders, "must rest while the ask is above the limit"
    feed.publish("BTCUSD", 79_010.0, bid=78_999.5, ask=79_000.0, funding_rate=0.01)
    ex.poll()
    assert not ex.orders and ex.positions["BTCUSD"].entry_price == 79_000
    assert close(ex.balance, 10_000 - notional * 0.0002 * GST), "resting fill pays the maker rate"
    # Post-only that would cross is refused; a marketable limit fills at the touch.
    rejects("post_only_would_cross", ex.place_order, "BTCUSD", "buy", 1, "limit_order",
            limit_price=80_000, post_only=True)
    filled = ex.place_order("BTCUSD", "buy", 1, "limit_order", limit_price=90_000)
    assert filled["state"] == "closed" and filled["avg_fill_price"] == 79_000.0, filled
    # Cancelling releases the reservation.
    resting = ex.place_order("BTCUSD", "sell", 5, "limit_order", limit_price=95_000,
                             reduce_only=True)
    assert resting["reserved_margin"] == 0.0, "reduce-only reserves nothing"
    ex.cancel_order(resting["order_id"])
    assert not ex.orders
    rejects("order_not_open", ex.cancel_order, resting["order_id"])


def check_leverage():
    ex, feed = venue(equity=1_000_000.0)
    rejects("invalid_leverage", ex.set_leverage, "BTCUSD", 250)
    rejects("invalid_leverage", ex.set_leverage, "BTCUSD", 0.5)
    ex.set_leverage("BTCUSD", 200)
    # $80k at 200x is fine; $240k needs IM 0.5% + 0.0000025*140000 = 0.85% -> 117x max.
    take(ex, "BTCUSD", "buy", 1000)
    message = rejects("leverage_too_high", take, ex, "BTCUSD", "buy", 2000)
    assert "117" in message, message
    assert ex.positions["BTCUSD"].size == 1000, "a refused order changes nothing"
    # Changing leverage with a position re-margins it.
    p = ex.positions["BTCUSD"]
    entry_notional = 1000 * 0.001 * p.entry_price
    ex.set_leverage("BTCUSD", 20)
    assert close(p.margin, entry_notional / 20) and p.leverage == 20
    ex.place_order("BTCUSD", "buy", 1, "limit_order", limit_price=70_000)
    rejects("open_orders", ex.set_leverage, "BTCUSD", 10)


def check_insufficient_margin():
    ex, _ = venue(equity=100.0)
    before = ex.balance
    message = rejects("insufficient_margin", take, ex, "BTCUSD", "buy", 20)
    assert "GST" in message
    assert ex.balance == before and not ex.positions


def check_brackets():
    ex, feed = venue()
    rejects("invalid_bracket", take, ex, "BTCUSD", "buy", 10, stop_loss=81_000)
    take(ex, "BTCUSD", "buy", 10, stop_loss=78_000, take_profit=84_000)
    view = ex.positions_view()[0]
    assert view["stop_loss"] == 78_000 and view["take_profit"] == 84_000, view
    assert len(ex.orders) == 2
    # Every bracket is a stop-limit: the stop-loss's limit sits 0.5% past its trigger.
    sl = next(o for o in ex.orders.values() if o["stop_order_type"] == "stop_loss_order")
    tp = next(o for o in ex.orders.values() if o["stop_order_type"] == "take_profit_order")
    assert sl["order_type"] == tp["order_type"] == "limit_order"
    assert sl["limit_price"] == 77_610.0 and tp["limit_price"] == 84_000.0, (sl, tp)
    feed.publish("BTCUSD", 78_500.0, bid=78_400.0, ask=78_401.0, funding_rate=0.01)
    ex.poll()
    assert "BTCUSD" in ex.positions, "the mark has not reached the stop"
    feed.publish("BTCUSD", 77_999.0, bid=77_990.0, ask=77_991.0, funding_rate=0.01)
    ex.poll()
    assert "BTCUSD" not in ex.positions and not ex.orders, "stop fires; sibling is cancelled"
    assert ex._recent[sl["id"]]["avg_fill_price"] == 77_990.0, "fills at the bid, inside its limit"
    # A stop that fires with the book already past its limit rests there, then fills as maker.
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    take(ex, "BTCUSD", "buy", 10, stop_loss=78_000)
    feed.publish("BTCUSD", 77_000.0, bid=77_000.0, ask=77_000.5, funding_rate=0.01)
    ex.poll()
    assert ex.positions["BTCUSD"].size == 10, "never sold below its 77,610 limit"
    sl = next(iter(ex.orders.values()))
    assert sl["state"] == "open" and sl["limit_price"] == 77_610.0, sl
    feed.publish("BTCUSD", 77_620.0, bid=77_610.0, ask=77_610.5, funding_rate=0.01)
    ex.poll()
    assert "BTCUSD" not in ex.positions and ex._recent[sl["id"]]["avg_fill_price"] == 77_610.0
    # Bracket set on an existing short: its stop-loss buys up to 0.5% above the trigger.
    feed.publish("BTCUSD", 75_000.0, bid=74_999.5, ask=75_000.5, funding_rate=0.01)
    take(ex, "BTCUSD", "sell", 4)
    rejects("invalid_bracket", ex.set_bracket, "BTCUSD", 70_000, None)
    ex.set_bracket("BTCUSD", stop_loss=80_000, take_profit=70_000)
    assert next(o for o in ex.orders.values()
                if o["stop_order_type"] == "stop_loss_order")["limit_price"] == 80_400.0
    feed.publish("BTCUSD", 69_990.0, bid=69_990.0, ask=69_991.0, funding_rate=0.01)
    ex.poll()
    assert "BTCUSD" not in ex.positions


def check_funding():
    ex, feed = venue()
    take(ex, "BTCUSD", "buy", 100)
    p = ex.positions["BTCUSD"]
    start = p.funding_at
    margin, balance = p.margin, ex.balance
    ex.poll(now=start + 100)
    assert ex.balance == balance, "no boundary crossed yet"
    ex.poll(now=start + 28800 + 1)
    paid = charges.funding_payment(100, 0.001, 80_000.0, 0.01)
    assert close(ex.balance, balance + paid) and paid < 0, (ex.balance, paid)
    assert close(p.margin, margin + paid) and close(p.realized_funding, paid)
    ex.poll(now=start + 28800 + 2)
    assert close(ex.balance, balance + paid), "one exchange per boundary"
    # A short on a negative rate pays too (ADA rate is -0.02%).
    take(ex, "ADAUSD", "sell", 1000)
    a = ex.positions["ADAUSD"]
    b = ex.balance
    ex.poll(now=a.funding_at + 14400 + 1)
    assert close(ex.balance - b, charges.funding_payment(-1000, 1.0, 0.2, -0.02)) and ex.balance < b


def check_liquidation():
    ex, feed = venue(equity=1_000.0)
    ex.set_leverage("BTCUSD", 100)
    take(ex, "BTCUSD", "buy", 100)
    p = ex.positions["BTCUSD"]
    margin = p.margin
    liq = ex.liquidation_price(p)
    assert close(liq, p.entry_price * (1 - 0.01 + 0.0025)), liq
    balance = ex.balance
    feed.publish("BTCUSD", liq + 1, bid=liq, ask=liq + 1, funding_rate=0.01)
    ex.poll()
    assert "BTCUSD" in ex.positions
    feed.publish("BTCUSD", liq - 1, bid=liq - 2, ask=liq - 1, funding_rate=0.01)
    ex.poll()
    assert "BTCUSD" not in ex.positions, "crossing liquidation closes the position"
    assert close(ex.balance, balance - margin), "the whole isolated margin is forfeited, no more"


def check_flip_and_reduce_only():
    ex, feed = venue()
    take(ex, "BTCUSD", "buy", 10)
    take(ex, "BTCUSD", "sell", 15)
    p = ex.positions["BTCUSD"]
    assert p.size == -5 and p.entry_price == 79_999.5, p
    assert close(p.margin, 5 * 0.001 * 79_999.5 / 10), "only the new short carries margin"
    rejects("reduce_only_no_position", take, ex, "BTCUSD", "sell", 1, reduce_only=True)
    order = take(ex, "BTCUSD", "buy", 50, reduce_only=True)
    assert "BTCUSD" not in ex.positions and "trimmed to 5" in order["note"], order


def check_rest_of_api():
    ex, _ = venue()
    rejects("invalid_size", ex.place_order, "BTCUSD", "buy", 0, limit_price=1)
    rejects("invalid_size", ex.place_order, "BTCUSD", "buy", "abc", limit_price=1)
    rejects("invalid_side", ex.place_order, "BTCUSD", "hold", 1, limit_price=1)
    rejects("invalid_product", ex.place_order, "NOPEUSD", "buy", 1, limit_price=1)
    rejects("invalid_limit_price", ex.place_order, "BTCUSD", "buy", 1, "limit_order")
    rejects("invalid_limit_price", ex.place_order, "BTCUSD", "buy", 1)
    # There is no market order, by any route.
    rejects("market_disabled", ex.place_order, "BTCUSD", "buy", 1, "market_order")
    rejects("market_disabled", ex.place_order, "BTCUSD", "buy", 1, "market_order", limit_price=1)
    rejects("invalid_order_type", ex.place_order, "BTCUSD", "buy", 1, "stop_market", limit_price=1)
    assert not hasattr(ex, "close_position") and not hasattr(ex, "close_all")
    ex.feed._quotes["BTCUSD"] = replace(ex.feed._quotes["BTCUSD"], stamp=time.monotonic() - 999)
    rejects("no_price", ex.place_order, "BTCUSD", "buy", 1, limit_price=80_000)


def check_auto_pricing():
    """`pegger`: post-only limits at the best maker price, re-priced until filled."""
    tick = Decimal("0.5")
    assert maker_price("buy", 100.0, 100.5, tick) == 100.0, "one-tick spread: join the bid"
    assert maker_price("sell", 100.0, 100.5, tick) == 100.5, "one-tick spread: join the ask"
    assert maker_price("buy", 100.0, 102.0, tick) == 100.5, "wide spread: one tick inside"
    assert maker_price("sell", 100.0, 102.0, tick) == 101.5
    assert maker_price("buy", 100.0, 100.0, tick) == 99.5, "locked book: never cross"
    assert maker_price("sell", 100.0, 100.0, tick) == 100.5
    assert maker_price("buy", None, 100.0, tick) is None

    ex, feed = venue()
    pegger = Pegger(ex, feed, INSTRUMENTS, requote_s=0.0, max_drift_bps=30.0)
    # An entry rests at the bid, post-only, with a stop-loss / take-profit to attach later.
    peg = pegger.submit("BTCUSD", "buy", 10, stop_loss=78_000, take_profit=84_000)
    assert peg["state"] == "working" and peg["price"] == 80_000.0, "two-tick spread: one inside"
    first = ex.orders[int(peg["order_id"])]
    assert first["post_only"] and first["order_type"] == "limit_order" and not ex.positions
    pegger.tick()
    assert pegger.views()[0]["order_id"] == peg["order_id"], "at the touch: left alone"
    # The market lifts: someone bids above it, so it follows, one order at a time.
    feed.publish("BTCUSD", 80_050.0, bid=80_050.0, ask=80_050.5, funding_rate=0.01)
    pegger.tick()
    now = pegger.views()[0]
    assert now["price"] == 80_050.0 and now["requotes"] == 1 and len(ex.orders) == 1, now
    assert ex._recent[first["id"]]["state"] == "cancelled"
    # Sellers come down to it: a maker fill at its own price, then the bracket goes on.
    feed.publish("BTCUSD", 80_050.0, bid=80_049.5, ask=80_050.0, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    done = pegger.views()[0]
    p = ex.positions["BTCUSD"]
    assert done["state"] == "filled" and done["avg_fill_price"] == 80_050.0, done
    assert p.size == 10 and p.entry_price == 80_050.0
    assert close(p.fees_paid, 10 * 0.001 * 80_050.0 * 0.0002 * GST), "maker fee, not taker"
    view = ex.positions_view()[0]
    assert view["stop_loss"] == 78_000 and view["take_profit"] == 84_000, view

    # A runaway market: an entry follows 0.30% at most, then stops.
    feed.publish("ADAUSD", 0.2, bid=0.1999, ask=0.2001, funding_rate=-0.02)
    peg = pegger.submit("ADAUSD", "sell", 100)
    assert peg["price"] == 0.2, "two-tick spread: one tick inside the ask"
    feed.publish("ADAUSD", 0.1996, bid=0.1995, ask=0.1996, funding_rate=-0.02)
    pegger.tick()
    assert pegger.views()[0]["price"] == 0.1996, "followed down 0.2%"
    feed.publish("ADAUSD", 0.199, bid=0.1989, ask=0.199, funding_rate=-0.02)
    pegger.tick()
    halted = next(v for v in pegger.views() if v["symbol"] == "ADAUSD")
    assert halted["state"] == "cancelled" and "not chasing" in halted["note"], halted
    assert not [o for o in ex.orders.values() if o["symbol"] == "ADAUSD"]
    feed.publish("ADAUSD", 0.2, bid=0.1999, ask=0.2001, funding_rate=-0.02)

    # An exit follows however far it must, and ends flat.
    ex.set_bracket("BTCUSD", None, None)
    exit_peg = pegger.exit("BTCUSD")
    assert exit_peg["kind"] == "exit" and exit_peg["price"] == 80_050.0, exit_peg
    rejects("invalid_bracket", pegger.submit, "BTCUSD", "sell", 1, reduce_only=True,
            stop_loss=90_000)
    feed.publish("BTCUSD", 79_000.0, bid=79_000.0, ask=79_000.5, funding_rate=0.01)
    pegger.tick()
    assert pegger.views()[0]["price"] == 79_000.5 and ex.positions["BTCUSD"].size == 10
    feed.publish("BTCUSD", 79_001.0, bid=79_000.5, ask=79_001.0, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    assert not ex.positions and pegger.views()[0]["state"] == "filled"
    rejects("no_position", pegger.exit, "BTCUSD")

    # Exit cancels the symbol's working entries first; a refused entry leaves nothing.
    take(ex, "BTCUSD", "buy", 2)
    entry = pegger.submit("BTCUSD", "buy", 5)
    out = pegger.exit("BTCUSD")
    assert next(v for v in pegger.views() if v["peg_id"] == entry["peg_id"])["state"] == "cancelled"
    assert out["size"] == 2 and len(ex.orders) == 1
    pegger.cancel(out["peg_id"])
    rejects("order_not_open", pegger.cancel, out["peg_id"])
    rejects("insufficient_margin", pegger.submit, "BTCUSD", "buy", 2_000)
    rejects("invalid_bracket", pegger.submit, "BTCUSD", "buy", 1, stop_loss=90_000)
    rejects("invalid_side", pegger.submit, "BTCUSD", "hold", 1)
    rejects("invalid_size", pegger.submit, "BTCUSD", "buy", 0)
    feed._quotes["ADAUSD"] = replace(feed._quotes["ADAUSD"], stamp=time.monotonic() - 999)
    rejects("no_price", pegger.submit, "ADAUSD", "buy", 1)
    assert all(v["state"] != "working" for v in pegger.views())

    # A restart adopts the auto order still resting, and keeps working it.
    resting = pegger.submit("BTCUSD", "buy", 4)
    again = Pegger(ex, feed, INSTRUMENTS, requote_s=0.0)
    assert again.adopt() == 1 and again.adopt() == 0
    adopted = again.views()[0]
    assert adopted["order_id"] == resting["order_id"] and adopted["size"] == 4
    pegger.cancel(resting["peg_id"])
    again.tick()
    assert again.views()[0]["state"] == "working" and again.views()[0]["order_id"] != resting["order_id"]
    again.cancel(again.views()[0]["peg_id"])

    # 1:2 entry: the bracket is priced off the real fill to net +$1 / -$2 after every fee.
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    if ex.positions:
        flatten(ex, "BTCUSD")
    rejects("invalid_rr", pegger.submit, "BTCUSD", "buy", 10, rr=(1, 2), stop_loss=70_000)
    rejects("invalid_rr", pegger.submit, "BTCUSD", "buy", 10, rr=(0, 2))
    # $2 would be past liquidation (1 contract at 100x): the stop moves inside it, no error.
    ex.set_leverage("BTCUSD", 100)
    peg = pegger.submit("BTCUSD", "buy", 1, rr=(1, 2))
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.0, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    done = next(v for v in pegger.views() if v["peg_id"] == peg["peg_id"])
    view = ex.positions_view()[0]
    assert done["state"] == "filled" and "inside liquidation" in done["note"], done
    assert view["liquidation_price"] < view["stop_loss"] < 80_000 < view["take_profit"], view
    flatten(ex, "BTCUSD")
    ex.set_leverage("BTCUSD", 10)
    # The mark already ran past the $2 stop when the entry fills: stop goes under the mark.
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    peg = pegger.submit("BTCUSD", "buy", 10, rr=(1, 2))
    feed.publish("BTCUSD", 79_500.0, bid=79_499.5, ask=80_000.0, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    done = next(v for v in pegger.views() if v["peg_id"] == peg["peg_id"])
    view = ex.positions_view()[0]
    assert done["state"] == "filled" and "below the mark" in done["note"], done
    assert view["stop_loss"] < 79_500 < view["take_profit"], view
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    flatten(ex, "BTCUSD")
    peg = pegger.submit("BTCUSD", "sell", 10, rr=(1, 2))
    assert peg["rr"] == [1.0, 2.0] and peg["stop_loss"] is None and peg["price"] == 80_000.0, peg
    feed.publish("BTCUSD", 80_000.0, bid=80_000.0, ask=80_000.5, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    p = ex.positions["BTCUSD"]
    view = ex.positions_view()[0]
    assert p.size == -10 and view["stop_loss"] > 80_000 > view["take_profit"], view
    q = 10 * 0.001
    def net(x):
        return -q * (x - p.entry_price) - p.fees_paid - q * x * 0.0005 * GST
    assert 1.0 <= net(view["take_profit"]) < 1.01 and -2.0 <= net(view["stop_loss"]) < -1.99, view
    rejects("invalid_rr", pegger.submit, "BTCUSD", "sell", 10, rr=(1, 2)), "only a fresh position"
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    flatten(ex, "BTCUSD")
    # The console's button: [win] alone is a take-profit only, no stop-loss.
    peg = pegger.submit("BTCUSD", "buy", 10, rr=[1])
    assert peg["rr"] == [1.0, 0.0], peg
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.0, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    p = ex.positions["BTCUSD"]
    view = ex.positions_view()[0]
    assert view["stop_loss"] is None and view["take_profit"] > 80_000, view
    net_tp = q * (view["take_profit"] - p.entry_price) - p.fees_paid - q * view["take_profit"] * 0.0005 * GST
    assert 1.0 <= net_tp < 1.01, net_tp
    note = next(v for v in pegger.views() if v["peg_id"] == peg["peg_id"])["note"]
    assert note is None, note
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    flatten(ex, "BTCUSD")

    # Not one order the venue saw was anything but a limit; every auto one was post-only.
    assert all(o["order_type"] == "limit_order" for o in ex._recent.values())
    assert all(o["post_only"] for o in ex._recent.values()
               if str(o.get("client_order_id") or "").startswith("peg-"))


def check_auto_pricing_against_a_venue():
    """What only a real venue does: partial fills, post-only refusals, slow cancels."""
    class Venue:
        def __init__(self):
            self.orders, self.sent, self.refuse, self.next = {}, [], 0, 1
            self.position = 0

        def place_order(self, symbol, side, size, order_type, **kw):
            assert order_type == "limit_order" and kw["post_only"]
            self.sent.append((side, size, kw["limit_price"]))
            if self.refuse:
                self.refuse -= 1
                raise OrderRejected("post-only would cross", "post_only_would_cross")
            o = {"order_id": str(self.next), "size": size, "unfilled_size": size, "state": "open",
                 "limit_price": kw["limit_price"], "avg_fill_price": None, "symbol": symbol,
                 "side": side, "reduce_only": kw["reduce_only"], "stop_order_type": None,
                 "client_order_id": kw["client_order_id"]}
            self.next += 1
            self.orders[o["order_id"]] = o
            return dict(o)

        def fill(self, order_id, qty):
            o = self.orders[order_id]
            o["unfilled_size"] -= qty
            o["avg_fill_price"] = o["limit_price"]
            self.position += qty if o["side"] == "buy" else -qty
            if not o["unfilled_size"]:
                o["state"] = "closed"

        def order_status(self, order_id):
            return dict(self.orders[order_id])

        def cancel_order(self, order_id):
            o = self.orders[order_id]
            if o["state"] != "open":
                raise OrderRejected("not open", "order_not_open")
            o["state"] = "cancelled"
            return dict(o)

        def open_orders(self):
            return [dict(o) for o in self.orders.values() if o["state"] == "open"]

        def positions_view(self):
            return [{"symbol": "BTCUSD", "size": self.position}] if self.position else []

        def set_bracket(self, *a):
            raise AssertionError("no bracket was asked for")

    v = Venue()
    feed = MarketFeed(list(INSTRUMENTS), settings=FeedSettings())
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5)
    pegger = Pegger(v, feed, INSTRUMENTS, requote_s=0.0)
    # Refused as post-only on arrival: still working, sent again next tick.
    v.refuse = 1
    peg = pegger.submit("BTCUSD", "buy", 5)
    assert peg["state"] == "working" and peg["order_id"] is None and "retrying" in peg["note"]
    pegger.tick()
    assert pegger.views()[0]["order_id"] == "1"
    # 2 of 5 fill, then the market lifts: the re-price is for the 3 left, never 5.
    v.fill("1", 2)
    feed.publish("BTCUSD", 80_100.0, bid=80_100.0, ask=80_100.5)
    pegger.tick()
    now = pegger.views()[0]
    assert now["filled"] == 2 and v.sent[-1] == ("buy", 3, 80_100.0), (now, v.sent)
    # It fills in full between the look and the cancel: counted once, nothing re-sent.
    feed.publish("BTCUSD", 80_200.0, bid=80_200.0, ask=80_200.5)
    real_status, stale = v.order_status, [dict(v.orders["2"])]
    v.order_status = lambda oid: stale.pop() if stale else real_status(oid)
    v.fill("2", 3)
    pegger.tick()
    now = pegger.views()[0]
    assert now["state"] == "filled" and now["filled"] == 5 and len(v.sent) == 3, (now, v.sent)
    assert close(now["avg_fill_price"], (2 * 80_000.0 + 3 * 80_100.0) / 5)
    assert v.position == 5
    # An exit of a partially filled sell resumes for exactly what is left.
    out = pegger.exit("BTCUSD")
    assert v.sent[-1] == ("sell", 5, 80_200.5) and out["kind"] == "exit"
    v.fill(out["order_id"], 5)
    pegger.tick()
    assert pegger.views()[0]["state"] == "filled" and v.position == 0


def check_old_brackets_become_stop_limits():
    """A bracket saved before market orders were removed loads as a stop-limit."""
    from contextlib import contextmanager

    class FakeDb:
        def __init__(self):
            self.writes = []

        def execute(self, statement, params=None, fetch=False):
            if "paper_account" in statement:
                return [{"balance": 1000.0, "start_balance": 1000.0, "opened_at": ist_now(),
                         "next_order_id": 10}]
            if "paper_positions" in statement:
                return [{"symbol": "BTCUSD", "size": 5, "entry_price": 80_000.0, "margin": 40.0,
                         "leverage": 10.0, "realized_pnl": 0.0, "realized_funding": 0.0,
                         "fees_paid": 0.0, "closed_contracts": 0, "closed_value": 0.0,
                         "funding_at": None, "opened_at": ist_now()}]
            if "FROM deltabot.orders" in statement:
                base = {"id": 1, "client_order_id": None, "symbol": "BTCUSD", "side": "sell",
                        "size": 5, "unfilled_size": 5, "order_type": "market_order",
                        "limit_price": None, "reduce_only": True, "post_only": False,
                        "leverage": None, "reserved_margin": 0.0, "bracket_stop_loss": None,
                        "bracket_take_profit": None, "state": "pending", "avg_fill_price": None,
                        "paid_commission": None, "note": "bracket", "created_at": ist_now()}
                return [dict(base, order_id="7", stop_order_type="stop_loss_order", stop_price=78_000.0),
                        dict(base, order_id="8", stop_order_type="take_profit_order", stop_price=84_000.0)]
            return []

        @contextmanager
        def transaction(self):
            db = self

            class Cur:
                def execute(self, statement, params=None):
                    db.writes.append((statement, params))
            yield Cur()

    from ist import now as ist_now
    db = FakeDb()
    ex, feed = venue(db=db)
    assert ex.load()
    sl, tp = ex.orders[7], ex.orders[8]
    assert sl["order_type"] == tp["order_type"] == "limit_order"
    assert sl["limit_price"] == 77_610.0 and tp["limit_price"] == 84_000.0, (sl, tp)
    saved = [p for s, p in db.writes if "INSERT INTO deltabot.orders" in s]
    assert {p[1]: (p[7], p[8]) for p in saved} == {"7": ("limit_order", 77_610.0),
                                                   "8": ("limit_order", 84_000.0)}, saved
    feed.publish("BTCUSD", 77_999.0, bid=77_990.0, ask=77_990.5, funding_rate=0.01)
    ex.db = None        # the fake keeps no writes; from here the loaded copy is the account
    ex.poll()
    assert "BTCUSD" not in ex.positions and ex._recent[7]["avg_fill_price"] == 77_990.0
    assert stop_limit_price("buy", "stop_loss_order", 0.2, ADA, 50) == 0.201
    assert stop_limit_price("sell", "stop_loss_order", 0.2, ADA, 50) == 0.199
    assert stop_limit_price("sell", "take_profit_order", 0.21, ADA, 50) == 0.21


# --------------------------------------------------------------------------- #
def check_database_round_trip():
    """The account survives the process, and the ledger reproduces the balance."""
    from psycopg2 import connect, sql

    from db import DB_PASSWORD, Database
    name = f"Delta_Manual_check_{os.getpid()}"
    admin = dict(host=os.getenv("TSDB_HOST", "127.0.0.1"), port=int(os.getenv("TSDB_PORT", 5432)),
                 user=os.getenv("TSDB_USER", "postgres"), password=DB_PASSWORD, dbname="postgres")
    try:
        conn = connect(**admin)
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipped: no PostgreSQL: {exc})")
        return
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    db = Database(name)
    try:
        db.connect()
        ex, feed = venue(equity=5_000.0, db=db)
        assert not ex.load(), "first run opens an account"
        ex.set_leverage("ADAUSD", 25)
        take(ex, "BTCUSD", "buy", 30, stop_loss=70_000)
        ex.place_order("ADAUSD", "buy", 500, "limit_order", limit_price=0.15)
        take(ex, "BTCUSD", "sell", 10)
        ex.poll(now=ex.positions["BTCUSD"].funding_at + 28800 + 1)

        back, _ = venue(equity=1.0, db=db)
        back.feed = feed
        assert back.load(), "second run resumes"
        assert close(back.balance, ex.balance) and back.start_balance == 5_000.0
        assert back.positions["BTCUSD"].size == 20
        for field in ("entry_price", "margin", "realized_pnl", "realized_funding", "fees_paid",
                      "funding_at", "leverage"):
            assert close(getattr(back.positions["BTCUSD"], field) or 0,
                         getattr(ex.positions["BTCUSD"], field) or 0), field
        assert sorted(back.orders) == sorted(ex.orders), (back.orders, ex.orders)
        assert back._next_id == ex._next_id
        # Every movement of the balance is on the ledger.
        total = db.execute("SELECT SUM(amount) AS s FROM deltabot.ledger WHERE mode='TEST'",
                           fetch=True)[0]["s"]
        assert close(total, back.balance), (total, back.balance)
        kinds = {r["kind"] for r in db.execute("SELECT DISTINCT kind FROM deltabot.ledger", fetch=True)}
        assert {"deposit", "fee", "funding"} <= kinds, kinds
        fees = db.execute("SELECT SUM(commission) AS c, SUM(fee) AS f, SUM(gst) AS g "
                          "FROM deltabot.fills", fetch=True)[0]
        assert close(fees["g"], fees["f"] * 0.18) and close(fees["c"], fees["f"] * 1.18)
        # Close through the resumed venue: a trade row with the whole life on it.
        flatten(back, "BTCUSD")
        back._flush()
        trade = db.execute("SELECT * FROM deltabot.trades ORDER BY id DESC LIMIT 1", fetch=True)[0]
        assert trade["contracts"] == 30 and trade["direction"] == "LONG", dict(trade)
        assert close(trade["net_pnl"], trade["gross_pnl"] - trade["fees"] + trade["funding"])
        summary = back.pnl_summary()
        assert close(summary["ALL"]["net"], back.balance - 5_000.0), summary
        # Reset keeps the ledger whole.
        back.reset(2_000)
        total = db.execute("SELECT SUM(amount) AS s FROM deltabot.ledger", fetch=True)[0]["s"]
        assert close(total, 2_000.0) and not back.orders and not back.positions
        again, _ = venue(db=db)
        again.load()
        assert close(again.balance, 2_000.0) and not again.orders and not again.positions
        # Reset with clear_history wipes ledger, fills, orders, trades, leaving only the new deposit
        again.reset(1_000, clear_history=True)
        assert close(again.balance, 1_000.0)
        ledger_rows = db.execute("SELECT * FROM deltabot.ledger", fetch=True)
        assert len(ledger_rows) == 1 and ledger_rows[0]["kind"] == "deposit" and close(ledger_rows[0]["amount"], 1_000.0)
        assert len(db.execute("SELECT * FROM deltabot.trades", fetch=True)) == 0
        # Websocket trades land in `ticks`; a day later they are deleted.
        from ticks import TickRecorder
        recorder = TickRecorder(feed, db)
        now_ms = int(time.time() * 1000)
        for price, at in (("80101", now_ms - 2000), ("80102", now_ms - 1000)):
            feed._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD",
                                               "price": price, "timestamp": at * 1000}))
        assert recorder.flush() == 2 and recorder.flush() == 0
        assert recorder.recent("BTCUSD", 900) == [[80101.0, now_ms - 2000], [80102.0, now_ms - 1000]]
        # Thinned to `points`: the last trade of each slice of the window.
        thinned = recorder.recent("BTCUSD", 900, points=1)
        assert len(thinned) <= 2 and thinned[-1] == [80102.0, now_ms - 1000], thinned
        db.execute("INSERT INTO deltabot.ticks (symbol, price, traded_at) "
                   "VALUES ('BTCUSD', 2, now() - interval '20 minutes')")
        assert [p for p, _ in recorder.recent("BTCUSD", 900)] == [80101.0, 80102.0]
        assert [p for p, _ in recorder.recent("BTCUSD", 1800)] == [2.0, 80101.0, 80102.0]
        db.execute("INSERT INTO deltabot.ticks (symbol, price, traded_at) "
                   "VALUES ('BTCUSD', 1, now() - interval '25 hours')")
        recorder.prune()
        # Half-second slices, so trades a second apart never share one.
        assert [p for p, _ in recorder.recent("BTCUSD", 86400, points=172800)] == [2.0, 80101.0, 80102.0]
        check_momentum_reads_the_database(db)
        check_venue_and_telemetry_read_the_database(db)
        # The order book, a row a second in `book_seconds`; an hour later deleted.
        from book_signal import KEEP_S, BookRecorder, BookSignal
        signal = BookSignal("wss://unused", INSTRUMENTS)
        signal.read("BTCUSD")
        st = signal._watched["BTCUSD"]
        book = BookRecorder(signal, db)
        assert book.flush() == 0, "no fresh book: nothing written"
        assert book.summary("BTCUSD", 300) == {"symbol": "BTCUSD", "seconds": 300, "recorded_s": 0}
        st.bid_usd, st.ask_usd, st.book_at, st.buy_acc = 3000.0, 1000.0, time.monotonic(), 600.0
        assert book.flush() == 1
        st.buy_acc, st.sell_acc = 100.0, 200.0
        assert book.flush() == 1             # same wall second, most likely: folds into the row
        # The live signal's minute of flow is read back from those rows, not held.
        assert close(st.flow_buy, 700.0) and close(st.flow_sell, 200.0), (st.flow_buy, st.flow_sell)
        assert signal._flow(st) == (st.flow_buy, st.flow_sell), "accumulators start over each second"
        db.execute("INSERT INTO deltabot.book_seconds VALUES "
                   "('BTCUSD', now() - interval '10 minutes', 1000, 3000, 0, 5000), "
                   "('BTCUSD', now() - make_interval(secs => %s), 1, 1, 1, 1)", (KEEP_S + 60,))
        five = book.summary("BTCUSD", 300)
        assert five["recorded_s"] in (1, 2) and five["call"] == "LONG", five
        assert close(five["buy_usd"], 700.0) and close(five["sell_usd"], 200.0), five
        fifteen = book.summary("BTCUSD", 900)
        assert fifteen["call"] == "SHORT" and close(fifteen["sell_usd"], 5200.0), fifteen
        assert "last 15m" in fifteen["why"] and fifteen["covered_s"] >= 600, fifteen
        assert book.summary("BTCUSD", 3600 * 2)["recorded_s"] == fifteen["recorded_s"] + 1
        book.prune()
        assert book.summary("BTCUSD", 3600 * 2)["recorded_s"] == fifteen["recorded_s"], "older than an hour: gone"

        # The retired auto trader's and chart tunings' tables are dropped.
        assert not db.execute("""SELECT 1 FROM information_schema.tables WHERE table_schema = 'deltabot'
                                 AND table_name IN ('auto_trader', 'ui_settings')""", fetch=True)
    finally:
        db.close()
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.close()


def check_http_routes():
    from live_server import create_app

    ex, feed = venue()
    pegger = Pegger(ex, feed, INSTRUMENTS, requote_s=0.0)

    class _Candles:
        def candles(self, symbol, resolution, bars=300):
            return [{"time": "t", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}] * min(bars, 3)

    class _Console:
        settings = replace(Settings(), paper=PaperSettings(scalper_offer=True, order_latency=0.0))
        exchange, instruments, candles, db = ex, INSTRUMENTS, _Candles(), None
        pegger = None
        started_at = time.time()

        def probe(self):
            return True

    _Console.feed = feed
    _Console.pegger = pegger
    client = create_app(_Console()).test_client()
    assert client.get("/").status_code == 200
    snap = client.get("/api/snapshot").get_json()
    assert snap["mode"] == "TEST" and snap["account"]["balance"] == 10_000.0
    assert snap["quotes"]["BTCUSD"]["ask"] == 80_000.5
    # The stream sends a snapshot at once, then one per tick - not per timer.
    r = client.get("/api/stream", buffered=False)
    assert r.mimetype == "text/event-stream"
    events = iter(r.response)
    first = json.loads(next(events).decode().removeprefix("data: "))
    assert first["quotes"]["BTCUSD"]["mark"] == 80_000.0, first
    threading.Timer(0.05, feed.publish, ("BTCUSD", 80_100.0),
                    {"bid": 80_099.5, "ask": 80_100.5, "funding_rate": 0.01}).start()
    started = time.monotonic()
    second = json.loads(next(events).decode().removeprefix("data: "))
    assert second["quotes"]["BTCUSD"]["mark"] == 80_100.0 and time.monotonic() - started < 1.0
    # Every trade goes out on its own, not folded into a snapshot.
    for price in ("80101", "80102", "80103"):
        feed._on_message(None, json.dumps({"type": "all_trades", "symbol": "BTCUSD", "price": price}))
    seen = []
    while len(seen) < 3:
        chunk = next(events).decode()
        if chunk.startswith("event: trades"):
            seen += [t[1] for t in json.loads(chunk.split("data: ", 1)[1])]
    assert seen == [80101.0, 80102.0, 80103.0], seen
    r.close()
    # Gzipped for a phone on mobile data, and the stream still arrives whole, one event at a time.
    import gzip, zlib
    r = client.get("/api/stream", headers={"Accept-Encoding": "gzip"}, buffered=False)
    assert r.headers["Content-Encoding"] == "gzip"
    assert zlib.decompressobj(31).decompress(next(iter(r.response))).startswith(b"data: {")
    r.close()
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.headers["Content-Encoding"] == "gzip" and gzip.decompress(r.data) == client.get("/").data
    feed.publish("BTCUSD", 80_000.0, bid=79_999.5, ask=80_000.5, funding_rate=0.01)
    # Trading needs a JSON body - a cross-site form cannot place an order.
    assert client.post("/api/orders", data={"symbol": "BTCUSD"}).status_code == 400
    assert client.post("/api/positions/ALL/close").status_code == 400
    # No price: auto-priced inside the two-tick spread as maker - the preview says so.
    quote = client.post("/api/quote", json={"symbol": "BTCUSD", "side": "buy", "size": 10}).get_json()
    assert quote["auto"] and quote["role"] == "maker" and quote["price"] == 80_000.0, quote
    assert close(quote["fee_total"], 10 * 0.001 * 80_000.0 * 0.0002 * GST), quote
    assert close(quote["margin"], 10 * 0.001 * 80_000.0 / 10)
    assert quote["liquidation_risk"] is None, quote     # flat test candles: no volatility to read
    quote = client.post("/api/quote", json={"symbol": "BTCUSD", "side": "buy", "size": 10,
                                            "limit_price": 90_000}).get_json()
    assert not quote["auto"] and quote["role"] == "taker" and quote["price"] == 80_000.5, quote
    # Market orders are gone from the API.
    r = client.post("/api/orders", json={"symbol": "BTCUSD", "side": "buy", "size": 10,
                                         "order_type": "market_order"})
    assert r.status_code == 400 and r.get_json()["code"] == "market_disabled", r.get_json()
    assert not ex.orders and not ex.positions
    # A manual price is a plain limit; this one crosses and fills at the touch.
    r = client.post("/api/orders", json={"symbol": "BTCUSD", "side": "buy", "size": 10,
                                         "limit_price": 80_000.5})
    assert r.status_code == 200 and r.get_json()["order"]["state"] == "closed", r.get_json()
    quote = client.post("/api/quote", json={"symbol": "BTCUSD", "side": "sell", "size": 10}).get_json()
    assert quote["closing_free"] and quote["fee_total"] == 0, quote
    quote = client.post("/api/quote", json={"symbol": "BTCUSD", "side": "sell", "size": 15}).get_json()
    assert close(quote["fee_total"], 5 * 0.001 * 80_000.0 * 0.0002 * GST), quote
    r = client.post("/api/orders", json={"symbol": "BTCUSD", "side": "buy", "size": 10_000_000})
    assert r.status_code == 400 and r.get_json()["code"] == "leverage_too_high", r.get_json()
    assert not pegger.views(), "a refused auto order leaves nothing behind"
    r = client.post("/api/orders", json={"symbol": "BTCUSD", "side": "buy", "size": 3})
    peg = r.get_json()["peg"]
    assert r.status_code == 200 and peg["state"] == "working" and peg["price"] == 80_000.0, peg
    snap = client.get("/api/snapshot").get_json()
    assert snap["pegs"][0]["peg_id"] == peg["peg_id"] and snap["orders"][0]["post_only"]
    assert client.delete(f"/api/pegs/{peg['peg_id']}").status_code == 200
    assert client.delete(f"/api/pegs/{peg['peg_id']}").status_code == 400
    assert not ex.orders
    r = client.post("/api/orders", json={"symbol": "BTCUSD", "side": "buy", "size": 10,
                                         "limit_price": 79_000, "rr": [1, 2]})
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_rr", r.get_json()
    r = client.post("/api/orders", json={"symbol": "BTCUSD", "side": "buy", "size": 10, "rr": [1, 2]})
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_rr", "a position is held"
    r = client.post("/api/leverage/BTCUSD", json={"leverage": 20})
    assert r.status_code == 200 and ex.positions["BTCUSD"].leverage == 20
    r = client.post("/api/positions/BTCUSD/bracket", json={"stop_loss": 70_000})
    assert r.status_code == 200 and r.get_json()["stop_loss"]["stop_price"] == 70_000
    r = client.post("/api/orders", json={"symbol": "ADAUSD", "side": "buy", "size": 100,
                                         "order_type": "limit_order", "limit_price": 0.1})
    oid = r.get_json()["order"]["order_id"]
    assert client.delete(f"/api/orders/{oid}").status_code == 200
    # $1 net target: a resting reduce-only post-only sell above entry.
    r = client.post("/api/positions/BTCUSD/target", json={"net": 1})
    t = r.get_json()
    assert r.status_code == 200 and t["order"]["reduce_only"] and t["order"]["post_only"], t
    assert t["order"]["state"] == "open" and t["price"] > 80_000.5, t
    assert client.post("/api/positions/BTCUSD/target", json={"net": 0}).status_code == 400
    assert client.post("/api/positions/XYZUSD/target", json={}).status_code == 400
    assert client.delete(f"/api/orders/{t['order']['order_id']}").status_code == 200
    r = client.post("/api/positions/BTCUSD/close", json={})
    exit_peg = r.get_json()["peg"]
    assert r.status_code == 200 and exit_peg["kind"] == "exit" and exit_peg["price"] == 80_000.0
    assert ex.positions, "an exit is a resting limit, not a market order"
    feed.publish("BTCUSD", 80_000.5, bid=80_000.0, ask=80_000.5, funding_rate=0.01)
    ex.poll()
    pegger.tick()
    assert not ex.positions and pegger.views()[0]["state"] == "filled"
    assert client.post("/api/positions/BTCUSD/close", json={}).status_code == 400
    assert client.get("/api/candles/BTCUSD?resolution=1h").get_json()["candles"]
    assert client.get("/api/candles/BTCUSD?resolution=7m").status_code == 400
    assert len(client.get("/api/candles/BTCUSD?resolution=15m&bars=2").get_json()["candles"]) == 2
    assert len(client.get("/api/candles/BTCUSD?resolution=15m&bars=-5").get_json()["candles"]) == 1
    assert client.get("/api/candles/NOPE").status_code == 400
    assert client.get("/api/ticks/BTCUSD?window=6h").get_json()["ticks"] == []
    assert client.get("/api/ticks/BTCUSD?window=3h").status_code == 400
    assert client.get("/api/ticks/NOPE").status_code == 400
    assert client.get("/api/history/nope").status_code == 404
    assert client.get("/api/history/fills").get_json()["rows"] == []
    assert client.post("/api/paper/reset", json={"equity": 50}).status_code == 400
    real_reset, asked = ex.reset, []
    ex.reset = lambda equity=None, clear_history=False: (asked.append(clear_history), real_reset(equity, clear_history))[1]
    assert client.post("/api/paper/reset", json={"equity": 50, "confirm": "RESET"}).status_code == 200
    assert ex.balance == 50 and asked == [True], asked      # a reset from the console always clears the ledger
    ex.reset = real_reset
    specs = client.get("/api/instruments").get_json()
    assert specs["BTCUSD"]["max_leverage"] == 200 and specs["BTCUSD"]["leverage"] == 20
    assert client.get("/api/broker_ping").get_json()["ok"]
    assert client.get("/api/signal/BTCUSD").get_json() == {"symbol": "BTCUSD", "ready": False,
                                                          "connected": False}
    assert client.get("/api/signal/NOPE").status_code == 400
    from book_signal import BookSignal
    _Console.signal = BookSignal("wss://unused", INSTRUMENTS)
    client = create_app(_Console()).test_client()
    assert client.get("/api/signal/btcusd").get_json()["ready"] is False
    assert "BTCUSD" in _Console.signal._watched, "asking subscribes"
    assert client.get("/api/book/BTCUSD?window=1m").get_json() == {"symbol": "BTCUSD", "seconds": 60,
                                                                   "recorded_s": 0}
    assert client.get("/api/book/BTCUSD?window=2h").status_code == 400
    # Momentum: no source yet is "not ready"; minutes outside 1 ... 60 are refused.
    assert client.get("/api/momentum/BTCUSD").get_json() == {"symbol": "BTCUSD", "window_s": 60,
                                                             "trades": 0, "ready": False}
    for bad in ("0", "61", "x", "-5"):
        assert client.get(f"/api/momentum/BTCUSD?minutes={bad}").status_code == 400, bad
    assert client.get("/api/momentum/NOPE").status_code == 400
    from momentum import Momentum
    _Console.momentum = Momentum(None)             # its query is checked on the scratch database
    client = create_app(_Console()).test_client()
    assert client.get("/api/momentum/btcusd?minutes=5").get_json() == {"symbol": "BTCUSD", "window_s": 300,
                                                                     "trades": 0, "ready": False}


    # The strategy: off is reported as off, never as an error.
    assert client.get("/api/algo").get_json()["status"] == "not_running"
    assert client.get("/api/algo/trades").get_json() == {"trades": []}
    assert snap["algo"] is None
    assert client.post("/api/algo/threshold", json={"symbol": "BTCUSD", "threshold": 1}).get_json()["code"] == "not_running"
    assert client.post("/api/algo/smooth", json={"symbol": "BTCUSD", "smooth": 0.02}).get_json()["code"] == "not_running"
    assert client.get("/api/smooth/btcusd?window=15m").get_json()["points"] == []
    assert client.get("/api/smooth/btcusd?window=7m").status_code == 400
    assert client.get("/api/algo/pivots/btcusd").get_json()["smooth_pivots"] == []


def check_venue_and_telemetry_read_the_database(db):
    """The paper venue, fill telemetry and broker pings keep nothing between calls:
    a second venue on the same database sees every change without `load()`,
    an order that left the book is answered from `orders`, and the voucher cap
    and the stats are sums over rows."""
    import charges as _charges
    from fast_limit import FastLimitTelemetry
    from live_server import BrokerLatencyProbe

    a, _ = venue(db=db)
    b, _ = venue(equity=1.0, db=db)                    # never loaded: it reads the database
    filled = take(a, "BTCUSD", "buy", 3)
    assert b.positions_view()[0]["size"] == 3, b.positions_view()
    assert close(b.account()["balance"], a.account()["balance"])
    assert not a._recent and not b._recent, "with a database no order history is held in memory"
    assert b.order_status(filled["order_id"])["state"] == "closed"
    flatten(b, "BTCUSD")
    assert a.positions_view() == [] and "BTCUSD" not in a.positions
    resting = a.place_order("BTCUSD", "buy", 1, "limit_order", limit_price=70_000)
    assert [o["order_id"] for o in b.open_orders()] == [resting["order_id"]]
    b.cancel_order(resting["order_id"])
    assert a.open_orders() == [] and a.order_status(resting["order_id"])["state"] == "cancelled"
    b.set_leverage("BTCUSD", 7)
    assert a.get_leverage("BTCUSD") == 7

    # The scalper voucher cap is the month's `fee_voucher` ledger rows, summed.
    v, _ = venue(db=db, scalper_offer=True)
    db.execute("INSERT INTO deltabot.ledger (mode, kind, amount, balance, created_at) "
               "VALUES ('TEST', 'fee_voucher', %s, 0, now())", (_charges.SCALPER_VOUCHER_CAP - 0.01,))
    take(v, "BTCUSD", "buy", 10)
    flatten(v, "BTCUSD")
    last = db.execute("SELECT amount FROM deltabot.ledger WHERE kind = 'fee_voucher' ORDER BY id DESC LIMIT 1",
                      fetch=True)[0]["amount"]
    assert close(last, 0.01, 1e-6), last
    assert not v._vouchers, "with a database no voucher total is held in memory"

    # Fill telemetry: one row per fill, stats summed by the database.
    t = FastLimitTelemetry(db, "CHECKTEL")
    t.record_fill("BTCUSD", "buy", 1, 80_000.0, 4.0, "fast_limit")
    t.record_fill("BTCUSD", "sell", 1, 80_010.0, 300.0, "sniper_maker", is_maker=True)
    st = FastLimitTelemetry(db, "CHECKTEL").stats()  # a fresh instance: nothing held
    assert st["total_orders"] == 2 and st["sub_100ms_fills"] == 1 and st["maker_fills"] == 1, st
    assert close(st["avg_latency_ms"], 152.0) and st["max_latency_ms"] == 300.0, st
    assert FastLimitTelemetry(db, "CHECKTEL2").stats()["total_orders"] == 0

    # Broker pings: one row per probe, a day kept.
    db.execute("INSERT INTO deltabot.broker_pings (latency_ms, at) VALUES (1, now() - interval '2 days')")
    probe = BrokerLatencyProbe(lambda: True, db, ttl_s=0)
    probe.snapshot()
    snap = BrokerLatencyProbe(lambda: True, db, ttl_s=3600).snapshot()
    assert snap["ok"] and snap["samples"] == 2 and snap["failures"] == 0, snap
    assert db.execute("SELECT count(*) AS n FROM deltabot.broker_pings WHERE at < now() - interval '1 day'",
                      fetch=True)[0]["n"] == 0
    assert not probe._samples, "with a database no samples are held in memory"

    # Auto orders: a finished one is written to `pegs` and dropped from memory.
    from pegger import Pegger
    pg = Pegger(a, a.feed, INSTRUMENTS, requote_s=0.0, db=db)
    peg = pg.submit("BTCUSD", "buy", 1)
    assert peg["state"] == "working" and pg.views()[0]["peg_id"] == peg["peg_id"]
    pg.cancel(peg["peg_id"])
    pg.tick()
    assert not pg._pegs, "with a database finished auto orders are not held in memory"
    shown = pg.views()
    assert shown[0]["peg_id"] == peg["peg_id"] and shown[0]["state"] == "cancelled", shown
    assert Pegger(a, a.feed, INSTRUMENTS, db=db)._next == peg["peg_id"] + 1, "ids carry on across runs"


def check_momentum_reads_the_database(db):
    """Momentum keeps no ticks of its own: every count and window is read back from `deltabot.ticks`."""
    from datetime import datetime, timezone

    from momentum import KEEP_S, Momentum

    def put(symbol, rows):
        db.execute_many("INSERT INTO deltabot.ticks (symbol, price, traded_at) VALUES %s",
                        [(symbol, p, datetime.fromtimestamp(t - ago, timezone.utc)) for p, ago in rows])

    t = time.time()
    m = Momentum(db)
    assert m.read("MOMO", 60) == {"symbol": "MOMO", "window_s": 60, "trades": 0, "ready": False}
    # Every up-move to Long, every down-move to Short, as many points as it moved.
    put("MOMO", [(p, 5 - i) for i, p in enumerate([80_000, 80_012, 80_005, 80_005, 80_010])])
    r = m.read("MOMO", 60)
    assert r["call"] == "LONG" and r["long_pts"] == 17 and r["short_pts"] == 7 and r["lead_pts"] == 10, r
    assert r["trades"] == 5 and r["ups"] == 2 and r["downs"] == 1, r
    put("MOMO", [(79_990, 0.5)])                         # -20: Short leads
    r = m.read("MOMO", 60)
    assert r["call"] == "SHORT" and r["short_pts"] == 27 and r["lead_pts"] == -10, r
    put("MOMO", [(80_000, 0.2)])                         # +10: level
    assert m.read("MOMO", 60)["call"] == "EVEN"
    # The window is the caller's: a move 10 minutes back shows in 15m, not in 1m.
    put("MOMOALT", [(3000, 600), (3030, 599), (3030, 1)])
    assert m.read("MOMOALT", 60)["ready"] is False
    r = m.read("MOMOALT", 900)
    assert r["window_s"] == 900 and r["long_pts"] == 30 and r["call"] == "LONG", r
    assert m.read("MOMOALT", 10 ** 6)["window_s"] == KEEP_S
    # An hour later every trade has aged out.
    assert m.read("MOMOALT", 3600, wall=t + KEEP_S + 1)["ready"] is False



def check_fast_limit_orders():
    """Verify the Ultra-Fast Limit Order system fills orders in milliseconds (<1000ms),
    with full slippage bounds, strict limit_order compliance, and real-time latency telemetry."""
    from fast_limit import FastLimitEngine, FastLimitTelemetry
    ex, feed = venue()
    engine = FastLimitEngine(ex, INSTRUMENTS, default_slippage_ticks=1, default_maker_timeout_s=0.15)

    feed.publish("BTCUSD", 78000.25, bid=78000.0, ask=78000.5, funding_rate=0.01)

    # 1. Fast Limit Buy: Ask is 78000.5, tick 0.5. Limit = 78001.0, fills immediately at 78000.5 (price improvement)
    t0 = time.perf_counter()
    order_buy = engine.place_fast_limit("BTCUSD", "buy", 2, slippage_ticks=1)
    lat_buy = (time.perf_counter() - t0) * 1000.0

    assert order_buy["order_type"] == "limit_order", "must strictly be a limit_order"
    assert order_buy["post_only"] is False, "fast limit order must not be post_only"
    assert order_buy["state"] == "closed", "must fill immediately on arrival"
    assert order_buy["limit_price"] == 78001.0, f"limit price must bound slippage, got {order_buy['limit_price']}"
    assert order_buy["avg_fill_price"] == 78000.5, f"fill must get price improvement at touch, got {order_buy['avg_fill_price']}"
    assert order_buy["sub_second"] is True, "must fill in less than 1 second"
    assert lat_buy < 1000.0, f"fill latency must be < 1000ms, got {lat_buy:.2f}ms"

    # 2. Fast Limit Sell: Bid is 78000.0. Reduce-only exit fills immediately at 78000.0
    t1 = time.perf_counter()
    order_sell = engine.place_fast_limit("BTCUSD", "sell", 2, slippage_ticks=1, reduce_only=True)
    lat_sell = (time.perf_counter() - t1) * 1000.0

    assert order_sell["order_type"] == "limit_order"
    assert order_sell["state"] == "closed"
    assert order_sell["avg_fill_price"] == 78000.0
    assert order_sell["sub_second"] is True
    assert lat_sell < 1000.0

    # 3. Sniper sub-second fallback (< 500ms)
    t2 = time.perf_counter()
    order_sniper = engine.place_sniper_limit(
        "BTCUSD", "buy", 1, maker_price_fn=lambda s, side: 78000.0, maker_timeout_s=0.15,
    )
    lat_sniper = (time.perf_counter() - t2) * 1000.0

    assert order_sniper["order_type"] == "limit_order"
    assert order_sniper["state"] == "closed"
    assert order_sniper["sub_second"] is True
    assert lat_sniper < 1000.0, f"sniper elapsed time must be < 1s, got {lat_sniper:.2f}ms"

    # Clean up position
    ex.place_order("BTCUSD", "sell", 1, limit_price=78000.0, reduce_only=True, reason="cleanup")

    # 4. Telemetry check
    st = engine.telemetry.stats()
    assert st["total_orders"] == 3
    assert st["sub_second_fills"] == 3
    assert st["sub_second_pct"] == 100.0
    assert st["avg_latency_ms"] < 1000.0
    assert st["sub_second_guarantee"] is True


def check_hhll_on_the_paper_venue():
    """The strategy through the real paper venue: it trades the smoothed line's pivots (LL -> long, HH -> the long
    closed and a short opened) at the second that confirms them, every order a crossing limit filled on the real
    prints; the prints' own pivots are not traded."""
    from fast_limit import FastLimitEngine
    from hhll import HHLL
    ex, feed = venue()
    algo = HHLL(feed, ex, INSTRUMENTS, FastLimitEngine(ex, INSTRUMENTS), symbols=("BTCUSD", "NOPEUSD"), trades_on="smoothed", execution_mode="fast_limit")
    assert algo.symbols == ("BTCUSD",)
    algo.x["BTCUSD"] = 0.12                                    # 96 points at 80,000
    assert algo._build("BTCUSD")
    # cost = taker fee with GST both sides + the 1.0 spread
    assert close(algo.cost["BTCUSD"], 80_000 * 2 * 0.0005 * GST + 1.0) and algo.zz["BTCUSD"].R == 0.12
    t0 = 1_700_000_000_000                                     # long past: the line's quiet-second clock stays off

    def tick(p, n=1):
        for _ in range(n):
            feed.publish("BTCUSD", p, bid=p - 0.5, ask=p + 0.5, funding_rate=0.01)
            feed._handle_trade({"symbol": "BTCUSD", "price": p, "timestamp": (t0 + tick.n * 1000) * 1000})
            tick.n += 1
            algo.step()
    tick.n = 0

    tick(80_000.0, 60)
    tick(80_300.0)                                             # the prints' LL at 80,000: not traded
    assert [x["type"] for x in algo.pivot_list("BTCUSD")] == ["LL"] and not ex.positions_view()
    while not algo.smooth_pivot_list("BTCUSD"):
        tick(80_300.0)
    p = ex.positions["BTCUSD"]                                 # the line's LL -> long, at the real ask
    assert p.size > 0 and p.entry_price == 80_300.5 and p.leverage == 10.0, p
    st = algo.status()["symbols"]["BTCUSD"]
    assert st["side"] == "LONG" and st["smooth_last_pivot"]["type"] == "LL" and st["trades_on"] == "smoothed"
    assert algo.st["BTCUSD"]["entry_price"] == 80_300.5 != st["smooth_last_pivot"]["confirmed_price"]
    tick(80_300.0, 60)
    tick(79_900.0)                                             # the prints' HH: not traded
    assert ex.positions["BTCUSD"].size > 0
    while len(algo.smooth_pivot_list("BTCUSD")) < 2:
        tick(79_900.0)
    p = ex.positions["BTCUSD"]                                 # the line's HH -> short, at the real bid
    assert p.size < 0 and p.entry_price == 79_899.5, p
    assert algo.st["BTCUSD"]["dir"] == -1 and algo.halt["BTCUSD"] is None
    assert [x["type"] for x in algo.smooth_pivot_list("BTCUSD")] == ["LL", "HH"]
    assert not ex.open_orders()                                # nothing left resting
    assert rejects_value(algo.set_threshold, "BTCUSD", 0) and rejects_value(algo.set_threshold, "ADAUSD", 0.1)
    assert algo.set_threshold("BTCUSD", 0.25) == 0.25 and algo.zz["BTCUSD"].R == 0.25 and not algo.pivot_list("BTCUSD")
    assert ex.positions["BTCUSD"].size < 0                     # a new threshold waits for its next pivot


def check_crossing_fills_are_honest():
    """A crossing limit fills as it would on the venue: never at a touch the market has since traded past, level
    by level for its size, never through its limit, and what the limit leaves rests."""
    from fast_limit import FastLimitEngine
    from hhll import HHLL
    ex, feed = venue()
    depth = lambda **m: feed._on_message(None, json.dumps({"type": "l2_updates", "symbol": "BTCUSD", **m}))  # noqa: E731
    trade = lambda p: feed._handle_trade({"symbol": "BTCUSD", "price": p})  # noqa: E731

    # 1. Top of book only, and a print has run past it (12 Nov: bought at 3983.95 under a 3984.37 print).
    trade(80_040.0)                                            # the 80,000.5 ask is not there any more
    stale = ex.place_order("BTCUSD", "buy", 10, "limit_order", limit_price=80_001.0)
    assert stale["state"] == "open" and "BTCUSD" not in ex.positions, "a limit at the old ask rests; it is not filled"
    ex.poll()
    assert ex.orders, "and nothing fills it while the market stays above it"
    ex.cancel_order(stale["order_id"])
    engine = FastLimitEngine(ex, INSTRUMENTS, default_slippage_ticks=1)
    got = engine.place_fast_limit("BTCUSD", "buy", 10)         # priced off the print, it crosses, at the print
    assert got["limit_price"] == 80_040.5 and got["state"] == "closed" and got["avg_fill_price"] == 80_040.0, got
    trade(79_960.0)                                            # and the same on the way down
    got = engine.place_fast_limit("BTCUSD", "sell", 10, reduce_only=True)
    assert got["limit_price"] == 79_959.5 and got["avg_fill_price"] == 79_960.0 and "BTCUSD" not in ex.positions

    # 2. The live book: an order larger than the touch takes the next levels too, at their prices.
    depth(action="snapshot", sequence_no=1, bids=[["79999.5", "4"], ["79999", "6"], ["79990", "50"]],
          asks=[["80000.5", "3"], ["80001", "5"], ["80010", "50"]])
    trade(80_000.5)                                            # a print at the touch moves nothing
    balance = ex.balance
    got = ex.place_order("BTCUSD", "buy", 8, "limit_order", limit_price=80_005.0)
    vwap = (3 * 80_000.5 + 5 * 80_001.0) / 8
    assert got["state"] == "closed" and close(got["avg_fill_price"], vwap), got
    fee_in = 8 * 0.001 * vwap * 0.0005 * GST
    assert close(ex.balance, balance - fee_in) and ex.positions["BTCUSD"].size == 8

    # 3. The limit stops it short: part fills, the rest rests at the limit with its margin reserved.
    got = ex.place_order("BTCUSD", "buy", 12, "limit_order", limit_price=80_001.0)
    assert got["state"] == "open" and got["unfilled_size"] == 4 and close(got["avg_fill_price"], vwap), got
    assert ex.positions["BTCUSD"].size == 16 and got["reserved_margin"] > 0
    rest = charges.order_quote(BTC, "buy", 4, 80_001.0, 10.0, position_size=16)["required"]
    assert close(got["reserved_margin"], rest) and close(ex.account()["order_margin"], rest)
    depth(action="update", sequence_no=2, bids=[], asks=[["80000.5", "0"], ["80001", "9"]])
    ex.poll()                                                  # the levels it took are no new sellers: it waits
    assert ex.order_status(got["order_id"])["unfilled_size"] == 4 and ex.positions["BTCUSD"].size == 16
    depth(action="update", sequence_no=3, bids=[], asks=[["80001", "0"]])
    ex.poll()                                                  # the book stands clear of it (ask 80,010)
    assert ex.order_status(got["order_id"])["unfilled_size"] == 4
    depth(action="update", sequence_no=4, bids=[], asks=[["80001", "9"]])
    ex.poll()                                                  # an ask comes back to its price: the rest fills, as maker
    done = ex.order_status(got["order_id"])
    assert done["state"] == "closed" and done["unfilled_size"] == 0 and ex.positions["BTCUSD"].size == 20
    assert close(done["avg_fill_price"], (8 * vwap + 4 * 80_001.0) / 12)
    fees = 8 * 0.001 * vwap * 0.0005 * GST + 4 * 0.001 * 80_001.0 * 0.0002 * GST
    assert close(done["paid_commission"], fees) and close(ex.balance, balance - fee_in - fees)
    assert close(ex.positions["BTCUSD"].entry_price, (16 * vwap + 4 * 80_001.0) / 20)

    # 4. A reduce-only exit that the book cannot take whole: the part is closed, the rest can be pulled.
    got = ex.place_order("BTCUSD", "sell", 20, "limit_order", limit_price=79_999.0, reduce_only=True)
    assert got["state"] == "open" and got["unfilled_size"] == 10 and got["reserved_margin"] == 0.0, got
    assert close(got["avg_fill_price"], (4 * 79_999.5 + 6 * 79_999.0) / 10) and ex.positions["BTCUSD"].size == 10
    pulled = ex.cancel_order(got["order_id"])
    assert pulled["state"] == "cancelled" and pulled["unfilled_size"] == 10 and not ex.orders
    # A print newer than the book and under its bid: the bids it traded through are gone, the limit rests.
    trade(79_995.0)
    got = ex.place_order("BTCUSD", "sell", 10, "limit_order", limit_price=79_999.0, reduce_only=True)
    assert got["state"] == "open" and got["unfilled_size"] == 10, got
    ex.cancel_order(got["order_id"])
    got = engine.place_fast_limit("BTCUSD", "sell", 10, reduce_only=True)      # 79,994.5: short of the 79,990 bid
    assert got["state"] == "open" and ex.positions["BTCUSD"].size == 10, got
    ex.cancel_order(got["order_id"])
    depth(action="update", sequence_no=5, bids=[["79999.5", "0"], ["79999", "0"], ["79994", "30"]], asks=[])
    got = engine.place_fast_limit("BTCUSD", "sell", 10, reduce_only=True)      # the book has caught up: at its bid
    assert got["state"] == "closed" and got["avg_fill_price"] == 79_994.0 and "BTCUSD" not in ex.positions

    # A resting limit is filled by what trades after it came to rest, not by a print from before.
    ex, feed = venue()
    trade(79_999.5)                                            # the last trade was at the bid
    got = ex.place_order("BTCUSD", "buy", 10, "limit_order", limit_price=80_000.0)    # inside the spread, above it
    ex.poll()
    assert got["state"] == "open" and ex.orders and "BTCUSD" not in ex.positions, "an old print fills nothing"
    trade(80_000.0)
    ex.poll()
    assert ex.orders, "a print at its price is not enough: the queue ahead is unknown"
    time.sleep(0.002)
    trade(79_999.5)
    ex.poll()                                                  # a new print through it: filled at its limit, as maker
    assert not ex.orders and ex.positions["BTCUSD"].entry_price == 80_000.0

    # An order takes the round trip to be matched: it meets the book of that moment, not the one it was priced off.
    ex, feed = venue(order_latency=0.05)
    depth(action="snapshot", sequence_no=1, bids=[["79999.5", "40"]], asks=[["80000.5", "40"]])
    moved = dict(action="update", sequence_no=2, bids=[], asks=[["80000.5", "0"], ["80003", "40"]])
    threading.Timer(0.01, depth, kwargs=moved).start()
    started = time.monotonic()
    got = ex.place_order("BTCUSD", "buy", 10, "limit_order", limit_price=80_001.0)   # the ask left while it travelled
    assert time.monotonic() - started >= 0.05 and got["state"] == "open" and "BTCUSD" not in ex.positions, got
    ex.cancel_order(got["order_id"])
    got = ex.place_order("BTCUSD", "buy", 10, "limit_order", limit_price=80_003.5)
    assert got["state"] == "closed" and got["avg_fill_price"] == 80_003.0, got
    # A print past the touch as it arrives: the book that shows that trade is the one it meets, a moment later.
    trade(80_020.0)
    late = dict(action="update", sequence_no=3, bids=[], asks=[["80003", "0"], ["80020.5", "40"]])
    threading.Timer(0.08, depth, kwargs=late).start()
    got = ex.place_order("BTCUSD", "buy", 5, "limit_order", limit_price=80_021.0)
    assert got["state"] == "closed" and got["avg_fill_price"] == 80_020.5, got
    depth(action="update", sequence_no=4, bids=[], asks=[["80020.5", "0"], ["80003", "40"]])
    # A cancel does not beat a fill the book has already made.
    got = ex.place_order("BTCUSD", "sell", 10, "limit_order", limit_price=80_010.0, reduce_only=True)
    assert got["state"] == "open"
    depth(action="update", sequence_no=5, bids=[["80010", "5"]], asks=[["80003", "0"], ["80011", "40"]])
    got = ex.cancel_order(got["order_id"])
    assert got["state"] == "closed" and got["avg_fill_price"] == 80_010.0 and ex.positions["BTCUSD"].size == 5, got

    # 5. The strategy on a print that outruns the book: it buys at the print or worse, never at the stale ask.
    ex, feed = venue()
    algo = HHLL(feed, ex, INSTRUMENTS, FastLimitEngine(ex, INSTRUMENTS), symbols=("BTCUSD",), execution_mode="fast_limit")
    algo.x["BTCUSD"] = 0.12
    assert algo._build("BTCUSD") and algo.zz["BTCUSD"].R == 0.12
    t0 = 1_700_000_000_000
    for i in range(60):
        feed._handle_trade({"symbol": "BTCUSD", "price": 80_000.0, "timestamp": (t0 + i * 1000) * 1000})
        algo.step()
    i = 60
    while not algo.smooth_pivot_list("BTCUSD"):                # the line's LL; the book still shows 79,999.5 / 80,000.5
        feed._handle_trade({"symbol": "BTCUSD", "price": 80_100.0, "timestamp": (t0 + i * 1000) * 1000})
        algo.step()
        i += 1
    p = ex.positions["BTCUSD"]
    assert p.size > 0 and p.entry_price == 80_100.0 and algo.halt["BTCUSD"] is None, p


def rejects_value(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return True
    return False


CHECKS = [
    check_fees_and_margin, check_scalper_offer, check_maker_limit_and_post_only, check_leverage,
    check_insufficient_margin, check_brackets, check_funding, check_liquidation,
    check_flip_and_reduce_only, check_rest_of_api, check_auto_pricing,
    check_auto_pricing_against_a_venue, check_old_brackets_become_stop_limits,
    check_database_round_trip, check_http_routes, check_fast_limit_orders, check_hhll_on_the_paper_venue,
    check_crossing_fills_are_honest,
]


def main() -> int:
    for module in ("instruments", "charges", "feed", "book_signal", "data_handler", "ist", "move_risk", "momentum", "fast_limit", "hhll", "tickfilter", "delta_login"):
        if module == "delta_login":
            continue                          # its __main__ logs in; the signing check is below
        print(f"[self-check] {module}")
        runpy.run_path(str(ROOT / f"{module}.py"), run_name="__main__")
    import hashlib
    import hmac

    from delta_login import DeltaClient
    expected = hmac.new(b"s", b"GET1700000000/v2/profile", hashlib.sha256).hexdigest()
    assert DeltaClient("k", "s")._sign("GET", "/v2/profile", "", "", "1700000000") == expected
    for check in CHECKS:
        print(f"[check] {check.__name__}")
        check()
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
