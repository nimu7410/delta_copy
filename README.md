# Delta Exchange India — manual trading console

Orders are placed by hand from the console, and in TEST by one strategy (`hhll.py`, below). The service
loads XAUTUSD, the gold perpetual (`DELTA_SYMBOLS` to change).

**Limit orders only - there are no market orders anywhere.**

- Buy / Sell with the price left blank: the console prices it - a post-only
  limit at the best bid (buy) or best ask (sell), one tick inside when the
  spread is wider than a tick - and re-prices it each time someone shows a
  better price, until it fills. Every fill is a maker fill.
- Exit (per position, or Exit all): the same, reduce-only, followed until flat.
- An entry follows the market at most `PEG_MAX_DRIFT_BPS` (default 30 = 0.30%)
  from where it started, then stops instead of chasing.
- Stop-loss / take-profit are stop-limits: a take-profit's limit is its
  trigger, a stop-loss's sits `STOP_LIMIT_BUFFER_BPS` (default 50 = 0.5%) past
  its trigger. If the market gaps past that, it rests at its limit.
- Type a limit price to place a plain limit at that price instead.

- Layout: on desktop the console is one screen, no page scroll - chart,
  positions (and history tabs) on the left, the order ticket on the right,
  equity / available / unrealised in the top bar. Wallet, realised P&L by
  window and system health are the Account tab.
- Live chart: the SMOOTHED line, a causal denoiser over the trades (`hhll.Smoother` on the server, the same
  code in `static/delta.js`), with its own HH / LL pivots and dashes (the strategy's zigzag run on the line). The
  strategy trades these pivots, at their dashes; every fill and P&L is on the real prints. TICK / SMOOTHED show or hide each line with its pivots; the SMOOTH THRESHOLD box (% of price,
  stored on the server, Enter applies) sets how big a move it treats as noise.
- BUY / SELL / EXIT (bottom of the ticket, as on Lune's desk): BUY and SELL
  send the ticket long or short at once; EXIT squares off the selected
  symbol's position (enabled while one is open). The preview prices the side
  last pointed at. On tablets and phones they are docked at the bottom of the
  screen.

- Service: `delta.service` (systemd, enabled, restarts on failure)
- Console: http://127.0.0.1:5001 (also on the Tailscale address, port 5001)
- Database: `Delta_Manual` (its own; the retired bot's `Delta_v1` is untouched)
- Mode: `TRADE_MODE=TEST` (paper venue on the live Delta India order book).
  Set `TRADE_MODE=LIVE` in the unit to trade real money (needs API credentials,
  see `delta_login.py`).

- Momentum (top bar, `momentum.py`): over the last N minutes (the box, 1-60),
  every websocket trade's price against the one before it - a move up adds
  its size in price points to Long, a move down to Short. The side with more
  points is the call (LONG / SHORT, EVEN on a tie). Read with one query over
  `ticks` each time; nothing is held in memory.

How the paper venue fills, so that it trades as a live account would:

- An order is matched `PAPER_ORDER_LATENCY_MS` (default 170, the round trip to Delta from this host) after it
  is sent, against the live order book of that moment (`l2_updates`: every level, as it changes)
- A crossing limit takes the book level by level for its size, never through its limit and never at a price
  the market has since traded past; what the book inside its limit cannot fill rests at the limit
- A resting limit fills at its own price once the book comes to it or a later trade prints through it (a print
  at it, or one from before it rested, fills nothing)
- The book is the venue's, without our orders: an order does not use up the levels it takes for the next one

What the paper venue charges, per product from Delta's own catalogue:

- Trading fee: maker or taker rate × notional, plus 18% GST on the fee
- Scalper Offer: no closing fee when a position is opened and closed within 30 min
  (BTCUSD, ETHUSD) or 15 min (other futures); on BTCUSD the opening fee (ex GST)
  also comes back as a voucher, up to $100 a month. `PAPER_SCALPER_OFFER=0` turns it off
- Isolated margin: entry notional / leverage. Leverage per symbol, up to the
  product maximum, and lower above `max_leverage_notional` (Delta's margin scaling)
- Order margin: margin + estimated entry and exit fees (incl. GST)
- Funding: every funding interval of the product, at the live rate, on the mark notional
- Liquidation: when mark crosses the liquidation price, the position is closed at
  the bankruptcy price and its margin is forfeited
- Brackets: stop-loss / take-profit on the mark price, closing the whole position
  with a stop-limit

Everything lands in `Delta_Manual`: `orders`, `fills` (fee and GST split),
`ledger` (every balance movement), `trades`, and the paper account itself.
Everything lives in the database, not in the process. With a database
connected nothing is kept in memory between calls except short-lived working
state:

- `ticks`: every websocket trade (a version in `config.TICK_FILTER`: the SENSEX-like filtered ticks of `tickfilter.py`, at most one a second) (chart Live view, momentum, the strategy's pivot
  chain at start). Rows older than a day are deleted
  every minute; `archive_ticks.py` keeps finished hours in `data/ticks`.
- Paper venue: every call re-reads the wallet, positions, open orders and
  leverage (`paper_account`, `paper_positions`, `orders`, `leverage`) and
  writes its changes back before returning; how a finished order ended and the
  monthly scalper-voucher total are read from `orders` / `ledger`.
- Strategy (`hhll.py`, TEST only: the plain zigzag on XAUTUSD's smoothed line; the line the threshold below its
  running high prints HH (short), above its running low LL (long), reversed at that second with crossing limits on
  the real book; the console's
  one box, the threshold in % of the extreme; printed pivots never move, a console change redraws the chain): its round
  trips in `hhll_trades`, its threshold in `algo_state` (`hhll_cost_x`), its pivot chain in `algo_state`
  (`hhll_chain`), continued at start while the logic stands. `strat_trades`, `lag_trades`, `algo_trades` and `pivots` hold the retired strategies' history (code in
  `data/backups`).
- Auto orders: finished ones in `pegs`. Order-book flow: the last minute of
  `book_seconds`. Broker ping: `broker_pings` (a day kept). LIVE leverage:
  `leverage`.

Short-lived working state that stays in memory: live quotes, the feed's
2000-trade hand-off buffer, the strategy's tapes, shadow positions and open trade, auto orders
while their order is working, the current second's order-book trades, writes
waiting for a retry, and caches of a few seconds.

```bash
sudo systemctl status|restart|stop delta
journalctl -u delta -f
.venv/bin/python tests/run_checks.py
.venv/bin/python strat.py report        # the live tournament so far
```

The old automated strategy is archived at
`~/Documents/delta_automated_backup_2026-10-25.tar.gz`.
