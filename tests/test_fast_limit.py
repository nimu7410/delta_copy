"""Comprehensive tests for the Ultra-Fast Limit Order Execution System.

Tests:
1. Fast marketable limit order fill speed (< 1000 ms, single-digit ms in practice).
2. Strict limit price protection (order_type == "limit_order", no market order violations).
3. Sub-second sniper execution (maker-taker with micro-timeout < 500 ms).
4. Real-time live feed execution benchmarks and latency telemetry.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import replace
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ist
from config import SETTINGS
from delta_login import DeltaClient
from fast_limit import FastLimitEngine, FastLimitTelemetry, get_fast_limit_price
from feed import MarketFeed, Quote
import instruments as instrument_catalogue
from paper_engine import PaperExchange

def setup_venue():
    client = DeltaClient("", "")
    insts = instrument_catalogue.load(client, ["BTCUSD"])
    feed = MarketFeed(["BTCUSD"], settings=SETTINGS.feed)
    ex = PaperExchange(insts, feed, replace(SETTINGS.paper, order_latency=0.0), db=None)   # latency: run_checks
    return insts, feed, ex


def test_fast_marketable_limit_execution():
    """Verify fast limit order places a limit_order and fills immediately (<1000ms)."""
    insts, feed, ex = setup_venue()
    engine = FastLimitEngine(ex, insts, default_slippage_ticks=1)

    # Mock order book: Bid 78000.0, Ask 78000.5
    feed._quotes["BTCUSD"] = Quote(
        mark=78000.25, bid=78000.0, ask=78000.5, last=78000.0,
        funding_rate=0.01, stamp=time.monotonic(),
    )

    # 1. Fast Limit Buy: Ask is 78000.5, tick is 0.5. Limit = 78000.5 + 0.5 = 78001.0
    t0 = time.perf_counter()
    order_buy = engine.place_fast_limit("BTCUSD", "buy", 5, slippage_ticks=1)
    latency_buy_ms = (time.perf_counter() - t0) * 1000.0

    assert order_buy["order_type"] == "limit_order", "must strictly be a limit_order"
    assert order_buy["post_only"] is False, "marketable limit must not be post_only"
    assert order_buy["state"] == "closed", "must fill immediately upon arrival"
    assert order_buy["limit_price"] == 78001.0, f"limit price must cap slippage, got {order_buy['limit_price']}"
    assert order_buy["avg_fill_price"] == 78000.5, f"fill price must get price improvement at touch (78000.5), got {order_buy['avg_fill_price']}"
    assert order_buy["sub_second"] is True, "must fill in less than 1 second"
    assert latency_buy_ms < 1000.0, f"execution latency must be < 1000ms, got {latency_buy_ms:.2f}ms"
    assert order_buy["fill_latency_ms"] < 1000.0

    print(f"✓ Fast Limit Buy filled in {order_buy['fill_latency_ms']:.2f} ms @ {order_buy['avg_fill_price']} (< 1s: YES)")

    # 2. Fast Limit Sell (flatten position)
    t1 = time.perf_counter()
    order_sell = engine.place_fast_limit("BTCUSD", "sell", 5, slippage_ticks=1, reduce_only=True)
    latency_sell_ms = (time.perf_counter() - t1) * 1000.0

    assert order_sell["order_type"] == "limit_order"
    assert order_sell["state"] == "closed"
    assert order_sell["avg_fill_price"] == 78000.0, f"sell fill price must be at bid touch, got {order_sell['avg_fill_price']}"
    assert order_sell["sub_second"] is True
    assert latency_sell_ms < 1000.0

    print(f"✓ Fast Limit Sell filled in {order_sell['fill_latency_ms']:.2f} ms @ {order_sell['avg_fill_price']} (< 1s: YES)")


def test_sniper_sub_second_fallback():
    """Verify sniper attempts maker, times out within sub-second threshold, and sweeps book in < 1s."""
    insts, feed, ex = setup_venue()
    # 150ms micro-timeout
    engine = FastLimitEngine(ex, insts, default_slippage_ticks=1, default_maker_timeout_s=0.15)

    feed._quotes["BTCUSD"] = Quote(
        mark=78000.25, bid=78000.0, ask=78000.5, last=78000.0,
        funding_rate=0.01, stamp=time.monotonic(),
    )

    def maker_price_fn(sym, side):
        return 78000.0 if side == "buy" else 78000.5

    t0 = time.perf_counter()
    # Resting buy at bid 78000.0 will not fill passively because opposite touch is at 78000.5
    order = engine.place_sniper_limit("BTCUSD", "buy", 2, maker_price_fn=maker_price_fn, maker_timeout_s=0.15)
    total_elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert order["order_type"] == "limit_order"
    assert order["state"] == "closed", "sniper sweep must guarantee fill"
    assert order["execution_type"] == "sniper_sweep"
    assert order["sub_second"] is True
    assert total_elapsed_ms < 1000.0, f"sniper total time must be strictly < 1000ms, got {total_elapsed_ms:.1f}ms"
    # Expected: ~150ms timeout + ~10ms sweep = ~160-200ms
    assert 120.0 <= total_elapsed_ms <= 800.0, f"sniper elapsed time out of expected range: {total_elapsed_ms:.1f}ms"

    print(f"✓ Sniper Maker-Taker Fallback filled in {total_elapsed_ms:.2f} ms total (< 1s: YES, state: closed)")


def test_telemetry_accuracy():
    """Verify telemetry correctly records sub-second fill rates, counts, and averages."""
    telemetry = FastLimitTelemetry()
    telemetry.record_fill("BTCUSD", "buy", 10, 78000.0, 4.5, "fast_limit", is_maker=False)
    telemetry.record_fill("BTCUSD", "sell", 10, 78050.0, 12.2, "fast_limit", is_maker=False)
    telemetry.record_fill("BTCUSD", "buy", 5, 78020.0, 180.0, "sniper_maker", is_maker=True)

    stats = telemetry.stats()
    assert stats["total_orders"] == 3
    assert stats["sub_second_fills"] == 3
    assert stats["sub_second_pct"] == 100.0
    assert stats["maker_fills"] == 1
    assert stats["marketable_fills"] == 2
    assert abs(stats["avg_latency_ms"] - ((4.5 + 12.2 + 180.0) / 3)) < 0.1
    assert stats["sub_second_guarantee"] is True

    print(f"✓ Telemetry stats verified: {stats}")


def test_realtime_burst_execution():
    """Execute a rapid burst of 25 consecutive limit orders and confirm 100% fill in < 1 second."""
    insts, feed, ex = setup_venue()
    engine = FastLimitEngine(ex, insts, default_slippage_ticks=1)

    latencies = []
    base_price = 78100.0

    for i in range(25):
        side = "buy" if i % 2 == 0 else "sell"
        px = base_price + (i * 0.5)
        feed._quotes["BTCUSD"] = Quote(
            mark=px, bid=px - 0.5, ask=px, last=px, funding_rate=0.01, stamp=time.monotonic(),
        )
        t0 = time.perf_counter()
        order = engine.place_fast_limit("BTCUSD", side, 1, reduce_only=(i % 2 != 0))
        lat_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(lat_ms)
        assert order["state"] == "closed"
        assert lat_ms < 1000.0, f"order {i} took {lat_ms:.2f}ms >= 1000ms"

    max_lat = max(latencies)
    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)

    print(f"✓ 25/25 Burst Limit Orders Filled: 100% in <1s (Min: {min_lat:.2f}ms, Avg: {avg_lat:.2f}ms, Max: {max_lat:.2f}ms)")
    assert max_lat < 1000.0


def test_fast_maker_limit_execution():
    """Verify fast maker limit places post_only limit order, fills in <100ms with maker fee."""
    insts, feed, ex = setup_venue()
    engine = FastLimitEngine(ex, insts, default_maker_timeout_s=0.25)

    # Mock order book: Bid 78000.0, Ask 78000.5. A trade prints through the bid just after the order rests
    # (a print at it does not fill, and neither does one from before it was there).
    feed._quotes["BTCUSD"] = Quote(
        mark=78000.25, bid=78000.0, ask=78000.5, last=77999.5,
        funding_rate=0.01, stamp=time.monotonic(),
    )
    threading.Timer(0.02, feed._handle_trade, ({"symbol": "BTCUSD", "price": 77999.5},)).start()

    t0 = time.perf_counter()
    order_buy = engine.place_fast_maker_limit("BTCUSD", "buy", 5, timeout_s=0.25)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    assert order_buy["order_type"] == "limit_order", "must strictly be a limit_order"
    assert order_buy["post_only"] is True, "maker limit must be post_only"
    assert order_buy["is_maker"] is True, "must be recorded as maker"
    assert order_buy["state"] == "closed", "must fill on arrival/trade match"
    assert order_buy["limit_price"] == 78000.0, f"limit price must be at bid touch (78000.0), got {order_buy['limit_price']}"
    assert lat_ms < 100.0, f"fill latency must be < 100ms, got {lat_ms:.2f}ms"
    assert order_buy["sub_100ms"] is True, "must record sub_100ms in telemetry"

    # Verify maker fee was charged (0.02% + 18% GST = 0.0236%)
    # 5 contracts * 0.001 * 78000 = $390 notional. Fee = $390 * 0.000236 = $0.09204
    commission = order_buy.get("paid_commission")
    assert commission is not None
    assert abs(commission - 0.09204) < 0.001, f"maker fee must be ~0.092 USD, got {commission}"

    print(f"✓ Fast Maker Limit Buy filled in {order_buy['fill_latency_ms']:.2f} ms @ {order_buy['avg_fill_price']} (< 100ms: YES, fee: ${commission:.4f})")


def test_hhll_maker_integration():
    """Verify HHLL trades strictly in maker mode (post-only fills, maker fees, zero taker fills)."""
    from hhll import HHLL
    insts, feed, ex = setup_venue()
    engine = FastLimitEngine(ex, insts, default_maker_timeout_s=0.25)
    algo = HHLL(feed, ex, insts, engine, symbols=("BTCUSD",), execution_mode="maker", trades_on="ticks")
    algo.x["BTCUSD"] = 0.05
    assert algo.execution_mode == "maker"

    # Set quote: Bid 78000.0, Ask 78000.5
    feed._quotes["BTCUSD"] = Quote(
        mark=78000.25, bid=78000.0, ask=78000.5, last=78000.0,
        funding_rate=0.01, stamp=time.monotonic(),
    )

    # 1. Maker Entry: BUY 2 contracts
    feed._quotes["BTCUSD"] = Quote(
        mark=78000.25, bid=78000.0, ask=78000.5, last=77999.5,
        funding_rate=0.01, stamp=time.monotonic(),
    )
    threading.Timer(0.02, feed._handle_trade, ({"symbol": "BTCUSD", "price": 77999.5},)).start()
    q, v, f = algo._cross("BTCUSD", "buy", 2, reduce_only=False, reason="test maker entry")
    assert q == 2, f"expected 2 contracts filled, got {q}"
    order = list(ex._recent.values())[-1]
    assert order["post_only"] is True, "entry order must strictly be post_only"
    assert abs(order["paid_commission"] - 0.036816) < 1e-5, f"must pay maker fee, got {order['paid_commission']}"
    assert f > 0, "must record fee"
    print(f"✓ HHLL Maker Mode Entry: {q} contracts filled as post-only maker (fee: ${f:.4f})")

    # 2. Maker Exit: SELL 2 contracts (reduce_only=True)
    feed._quotes["BTCUSD"] = Quote(
        mark=78010.25, bid=78010.0, ask=78010.5, last=78011.0,
        funding_rate=0.01, stamp=time.monotonic(),
    )
    threading.Timer(0.02, feed._handle_trade, ({"symbol": "BTCUSD", "price": 78011.0},)).start()
    q_exit, v_exit, f_exit = algo._cross("BTCUSD", "sell", 2, reduce_only=True, reason="test maker exit")
    assert q_exit == 2, f"expected 2 contracts exited, got {q_exit}"
    exit_order = list(ex._recent.values())[-1]
    assert exit_order["post_only"] is True, "exit order must strictly be post_only"
    assert exit_order["reduce_only"] is True, "exit order must be reduce_only"
    print(f"✓ HHLL Maker Mode Exit: {q_exit} contracts filled as post-only maker (fee: ${f_exit:.4f})")


if __name__ == "__main__":
    print("\n--- RUNNING FAST LIMIT TESTS ---")
    test_fast_marketable_limit_execution()
    test_fast_maker_limit_execution()
    test_sniper_sub_second_fallback()
    test_telemetry_accuracy()
    test_realtime_burst_execution()
    test_hhll_maker_integration()
    print("\n--- ALL FAST LIMIT TESTS PASSED ---")
