"""Real-time live feed test for fast limit orders.

Tests fast limit execution against live streaming WebSocket market data from Delta Exchange India.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dataclasses import replace
from config import SETTINGS
from delta_login import DeltaClient, WS_URL
from fast_limit import FastLimitEngine
from feed import MarketFeed
import instruments as instrument_catalogue
from paper_engine import PaperExchange


def test_realtime_with_live_market_data():
    print("\n--- CONNECTING TO LIVE DELTA FEED FOR REAL-TIME FAST LIMIT TEST ---")
    client = DeltaClient("", "")
    insts = instrument_catalogue.load(client, ["BTCUSD"])
    feed_cfg = replace(SETTINGS.feed, ws_url=WS_URL)
    feed = MarketFeed(["BTCUSD"], settings=feed_cfg)
    feed.start()

    try:
        # Await real live quote from Delta Exchange WebSocket
        print("Awaiting live BTCUSD market quote from WebSocket...")
        t_wait_start = time.monotonic()
        quote = None
        while time.monotonic() - t_wait_start < 15.0:
            quote = feed.quote("BTCUSD")
            if quote and quote.ask and quote.bid:
                break
            time.sleep(0.2)

        if quote is None:
            print("Warning: Live WebSocket feed timed out; fallback to cached mark")
            quote = feed.quote("BTCUSD")

        assert quote is not None, "Failed to receive quote from feed"
        print(f"✓ LIVE BTCUSD QUOTE: Bid={quote.bid}, Ask={quote.ask}, Mark={quote.mark}")

        ex = PaperExchange(insts, feed, SETTINGS.paper, db=None)
        engine = FastLimitEngine(ex, insts, default_slippage_ticks=1)

        print("\nExecuting 10 Real-Time Fast Limit Orders against Live Market Data:")
        latencies = []
        for i in range(1, 11):
            side = "buy" if i % 2 != 0 else "sell"
            reduce_only = (i % 2 == 0)

            t0 = time.perf_counter()
            order = engine.place_fast_limit("BTCUSD", side, 1, reduce_only=reduce_only)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)

            assert order["order_type"] == "limit_order"
            assert order["state"] == "closed"
            assert elapsed_ms < 1000.0, f"Order {i} exceeded 1000ms: {elapsed_ms:.2f}ms"
            assert order["fill_latency_ms"] < 1000.0

            print(f"  Order #{i:02d}: {side.upper()} 1 @ {order.get('avg_fill_price')} | Latency: {order['fill_latency_ms']:.3f} ms | State: {order['state']} | <1s: YES")

        avg_lat = sum(latencies) / len(latencies)
        max_lat = max(latencies)
        min_lat = min(latencies)

        print(f"\n✓ REAL-TIME TEST RESULTS (10/10 Orders):")
        print(f"  - 100% Filled in < 1 Second: YES")
        print(f"  - Min Fill Latency: {min_lat:.3f} ms")
        print(f"  - Average Fill Latency: {avg_lat:.3f} ms")
        print(f"  - Max Fill Latency: {max_lat:.3f} ms")
        print("✓ Real-time test completed successfully!")

    finally:
        feed.stop()


if __name__ == "__main__":
    test_realtime_with_live_market_data()
