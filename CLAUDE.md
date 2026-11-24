# Delta bot: standing orders from the operator

- 12 Nov 2026, operator's order: gold only. DELTA_SYMBOLS is XAUTUSD (the cheapest contract on Delta India: 1 bp a
  side, maker or taker). The strategy Lune trades runs on it, unchanged (`hhll.py`: Lune's `Pivot` / `ZigZag`
  verbatim; HH short, LL long, stop-and-reverse on the confirming print, crossing limits). Its one setting is the
  console's threshold box, x round-trip cost (default 20, Lune's SENSEX). Paper only: main.py does not start it in
  LIVE. Lune itself is not to be changed for this.
- 12 Nov 2026, later, operator's order: confirmation distance = swing, always, and adaptive. Lune's plain zigzag
  (`ZigZag` verbatim); at each pivot R for the next leg is set to `VOL_K` (4) x the mean 5-minute high-low range of
  the last `VOL_BARS` (48 = 4 h), never under the console box x cost (the floor, box 3). R never changes inside a
  leg and no past pivot is touched: never repaints. Picked on the 5 days of archived gold ticks (2-4 and 11-12 Nov),
  realistic fills (170/400 ms, 0.05 slip): +136 points, 30 trades, 4/5 days up; its neighbours (k 3.5-4.5) alike.
  Floor, k and lookback are tunable in the console (ADAPTIVE R row; saved in algo_state, a change rebuilds the
  chain); 21:17 the operator set 4 x 1 h. In-sample on 5 days: not evidence; rerun `adapt` tests when 2-3 weeks of ticks are archived. (Tried and dropped:
  repainting chop filter; Lune's forced pivots; fixed R 8.7: -54 points on the same days.)
- 15 Nov 2026, operator's order: an HH's white dash (confirming print) above the LL's, as often as can be, never
  repainting, never stalling. `ZigZag` has a dash gate: a pivot prints by the threshold only on a tick beyond the
  last pivot's dash; a swing too small for that prints nothing; a break of the last pivot still forces the next one
  (the only pairs out of order, and what keeps the chain from stalling). Archived gold ticks (2-4, 11-14 Nov, 4 x
  24 h, floor 3): dashes in order 47% -> 70% of pairs, 81 -> 56 pivots, points not worse (in-sample, 5 days: not
  evidence). 100% is impossible: price that leaves a dash and never returns has no print on its right side. Tried,
  no better: breakeven confirms, breaks set 1.5-3 R away (to 77% in order, worse points and losses). Live from
  15 Nov 06:58 until the order below replaced it (kept in data/backups/hhll-dash-gate-20261115.tgz).
- 15 Nov 2026, later, operator's order: big clean legs, the white dash as near its pivot as can be, no whipsaw in
  chop, no forced pivots, in every market; never repainting, never stalling, always alternating. What runs: the
  plain one-threshold zigzag, adaptive R as above (`ZigZag`; no dash gate, no forced prints, no `forced` / `level`).
  A pivot is the swing's true extreme, printed R back from it, or back at the last pivot's price when R has grown
  since (so no print is ever traded through before the next). The dash is R from its pivot and cannot be nearer:
  a pivot confirmed at a fraction f of the swing is traded through before the next one about 1 - f of the time
  (archived gold ticks: f 0.15 / 0.3 / 0.5 / 0.75 -> 60% / 49% / 32% / 13% of pivots), and each of those must be
  forced, stalled or repainted. So dash distance = smallest leg = R; the console's k moves both together (2-4,
  11-14 Nov, 24 h, floor 3: k 3 / 4 / 5 / 6 / 8 -> 152 / 84 / 57 / 53 / 35 pivots, median leg 16 / 21 / 25 / 27 /
  29, median dash 9 / 11 / 14 / 16 / 19 points, +8 / +157 / +163 / -43 / -75 points; in-sample, not evidence). k is
  left at the operator's 4. Tried for chop, no use (sign flips between neighbours): R raised 1.25-2x after a
  short leg; R never under 0.5-1x the last 1-4 h range.
- 16 Nov 2026, operator's order: absolutely big legs, never repainting, always alternating, pivots at the true
  extreme, no delayed and no forced pivots, confirm time as low as big legs allow, no choppy pivots. The plain
  zigzag above stays (it is the only one doing all the hard rules; confirm distance = smallest leg = R, so big
  legs cost confirm time). Changed: R = 5 x the mean 5-min range of the last 1 h (VOL_K 5, VOL_BARS 12; was the
  operator's 4 x 24 h), and a console change (box, k, lookback) applies from the next pivot instead of rebuilding
  the chain, so no printed pivot ever moves. All archived gold ticks (1-3, 10-16 Nov), realistic fills: 1 h gives
  the biggest leg per point of confirm distance (median leg / dash 2.0-2.8 at 0.5-1 h vs 1.7-1.9 at 1.5-24 h);
  5 x 1 h: 29 pivots, median leg 40, dash 17, confirm 19 min, +91 points, DD 61 (4 x 24 h: 103 pivots, leg 21,
  dash 12.5, 10 min, +114, DD 111). Points flip sign between lookback neighbours (45 min / 1.5 h): not evidence.
  Tried, no better: R from a quantile of recent small swings, R raised in chop by the 1-h efficiency ratio.
  Then, operator: "big legs, not quick ones, ignore these small legs": the box (floor) set to 15 x cost (about 14
  points), so no leg is shorter: 5 x 1 h, box 15 -> 26 pivots, median leg 48, smallest quarter >= 32 (was 27),
  dash 18, confirm 30 min, +74 points, DD 72 (box 20 / 25 / 30: leg25 stays 32, points worse). Restarts no
  longer rebuild the chain: the printed pivots and the leg's R are stored in algo_state (`hhll_chain`) on every
  pivot and continued at start (16 Nov 20:13 one restart recomputed the chain; the original 32 pivots were
  restored from the console's copy at 20:14).
  Then, operator: "more big legs, no frequent changing of pivots, absolutely clean": box 40 x cost (about 38
  points), k 8 x 1 h (VOL_K 8). Archived gold ticks: every leg >= 49 points, smallest quarter >= 65, median 70,
  1.5 pivots a day, no leg shorter than its R, dash 38, confirm about 4.5 h (median), +119 points, DD 45. A plateau:
  box 40 at k 6-10, 1 h or 24 h, all alike (+40 to +131); box 50 the same legs, worse points; box 20-30 still
  leaves 30-point legs. Few trades (about 10 on these days): in-sample, not evidence.
  Then, operator, on rule 1: "once printed never changes; if logic changes then pivots should change". So a
  console change (box, k, lookback) redraws the chain with the new logic, and a restart continues the stored chain
  only if it was drawn by the same logic (`LOGIC` version + box, k, lookback, stored with it); otherwise it is
  redrawn. Bump `LOGIC` in hhll.py whenever the pivot rule itself changes. 20:23 redrawn at box 40, 8 x 1 h:
  LL 3818.98, HH 3887.01, LL 3823.71 (16 Nov 16:24), the paper LONG on it.
  Then, operator: "lower legs, about 10, and a box so I can edit it myself". The console box is now the minimum
  leg in price points (MIN LEG · POINTS; `x` in algo_state is points, `LOGIC` zigzag-adaptive-2), R = the larger
  of it and k x the 5-min range. The operator set k 1 x 5 h themself; box 10 points: R is mostly 10 (archived
  ticks at box 10: k 0.5 -> 25 pivots a day, median leg 19, confirm 11 min, -146 points; k 4 x 1 h -> 6 a day,
  median 30, confirm 24 min, +12). Don't overwrite algo_state behind the console: the operator edits it live.
- 15 Nov 2026, 07:15-07:20, tried and withdrawn on the operator's order ("restore to last one"): every pivot
  delayed so its white dash is at the tip of the next pivot, then HH long, LL short. The rule above (HH short, LL
  long, dash gate) is back; the delayed code is kept in data/backups/hhll-tip-delay-20261115.tgz. What it was
  (`SWING`, `TIP`, `HHLL.held`): a pivot needs a
  swing of 1.5 x R (R = the adaptive R above), is confirmed 0.15 of that swing off its tip (Lune's `ZigZag(R, leg)`,
  unchanged), and is printed one pivot late, on the tick that confirms the next: that tick is its dash and the
  trade. Never repaints, always alternates; a broken tip prints the next pivot at once (the stop, reversed).
  Archived gold ticks (2-4, 11-14 Nov, 4 x 24 h, floor 3), realistic fills: dash to the next tip 14.4 -> 3.7
  points (median), 67% within 5; 214 pivots (was 57, 126 forced), +150 points, 7 of 8 days up (the old rule: +237);
  neighbours (swing 1.5-2.5, tip 0.1-0.2) +60 to +215. At swing 1.0 x R it loses (-130 to -280, ~400 trades). In-sample: not evidence.
  Tried, no use: the old pivots with only the print delayed (dash still 13-14 points off the next tip, -270 to
  -340 points). A dash exactly on the tip is impossible: no print knows a tip before price has left it.
- 15 Nov 2026, operator's order: v2 and v3 are removed, only v1 stays (this directory, delta.service, port 5001,
  database Delta_Manual: adaptive R, the operator's last setting kept in algo_state). Their code, unit files and
  databases (Delta_Manual_v2, Delta_Manual_v3) are archived in data/backups/delta-v2-v3-removed-20261115.tgz and
  data/backups/Delta_Manual_v{2,3}_removed_20261115.dump. The V1/V2/V3 header buttons are gone from the code; the
  running process shows them until its next restart (not restarted for this: a restart rebuilds the pivot chain).
  The 12 Nov 13:06 IST code (fixed 3, honest fills) stays archived in data/backups/v1-1pm-20261112.tgz.
- 15 Nov 2026, 18:57 IST, operator's order: V2 deployed beside V1, same port. V2 = /home/ubuntu/Documents/delta-v2
  (delta-v2.service, 127.0.0.1:5002, database Delta_Manual_v2, seeded with V1's last day of ticks), served through V1
  at :5001/v2/ (V1's `V2_UPSTREAM` proxy, delta.service.d/v2.conf; V2 mounts itself at `DELTA_BASE=/v2`). Header
  V1 / V2 buttons switch. V2's strategy (`delta-v2/hhll.py`): dash gate (an HH prints R back from its high only at
  or above the last LL's dash, mirror for LL), loss print LOSS 2 x R beyond the last dash, trade stop STOP 1.5 x R
  against the leg's entry -> reversed once and held to the next pivot; adaptive R as V1 (4 x 24 h, floor 3).
  Never repaints, always alternates, the loss print bounds every wait. Tests on archived gold ticks: 78% dashes in
  order, 72% wins, +308 points, DD 42; leave-one-day-out about +142; BTC/ETH/SOL at their fees no edge. In-sample,
  8 falling days: not evidence. V1's strategy is unchanged; it was restarted once for the proxy (chain rebuilt
  from the last day of ticks, its open paper trade re-attached). Paper only, both.
- 12 Nov 2026, 00:05, operator's order: the strat tournament is removed completely. strat.py, its results
  (data/strat), and the CLAUDE.md, main.py and tests/run_checks.py that went with it are archived in
  data/backups/strat-tournament-20261112.tgz. (No strategy ran from then until the gold order above.) Earlier strategies (HH/LL) stay archived in data/backups/hhll-strategy-20261111.tgz.
- Tournament verdict at removal: ~380k shadow trades over 14 symbols, 137 candidates with n >= 40, none positive
  after charges. Best family fflow (maker) at -1.3 bps; gold about -2.7 bps (≈ zero gross minus its 2.36 bps fee).
- Paper (`TRADE_MODE=TEST`) on live data, the symbol in DELTA_SYMBOLS (delta.service.d/maker.conf), 10x. Real money
  (`TRADE_MODE=LIVE`) needs the operator's say-so.
- Paper fills must stay honest: a resting maker order fills only when the book reaches it or a print trades
  through it, never on a print at it.
- 12 Nov 2026, operator's order: the paper must trade as live would. A crossing order is matched 170 ms after it
  is sent (PAPER_ORDER_LATENCY_MS, the round trip to Delta), against the live level-by-level book for its size,
  never at a price the market has traded past; a part fill leaves the rest resting. Orders are priced off the
  true touch (`Quote.take`). Paper results before this (12 Nov, about 12:30 IST) were filled at stale prices,
  about 0.28 points too good a fill, and are not evidence.
- 17 Nov 2026, operator's order: an HH's white dash at least 10 points above the LL's dash, 70% of the time or more,
  by any means. What runs (`ZigZag`, `LOGIC` zigzag-dashgap-3): the dash gap. An HH prints only on a tick
  DASH_GAP (10) or more above the last LL's dash, either R back from its high or back within DASH_SLACK (0.3) of that
  gate once the high has cleared it by DASH_MIN (2); mirror for LL. Price DASH_STOP (40) past the last dash prints the
  next pivot anyway (the stop: the only pairs out of order, the losing trades). Never repaints, always alternates;
  price may trade past a printed pivot before the next prints. Archived gold ticks (3-4, 11-16 Nov, box 15, 5 x 5 h):
  31/36 pairs in order (86%; the plain zigzag 22%), 5 stop prints, +187 points at the dashes less 1 a round trip
  (wins +12.7, losses -41.1), DD 78, confirm 3.5 min median, every day >= 67%. Stop 35-45 / swing 1-3: 77-89%. A
  random walk gives stop / (stop + gap + slack + swing) = 76%. In-sample: rerun when more ticks are archived.
- 17 Nov 2026, later, operator's order (replaces the gap-10 rule above): an HH's white dash at least N points above
  the LL's after charges, 70% of the time or more, drawdowns controlled; N 1 to 10, the console's DASH GAP N box
  (`set_gap`, /api/algo/gap; saved in algo_state as `n`, default 7; `LOGIC` zigzag-dashgap-5). gap = N + the round
  trip's cost (`cost_pts`, about 0.94); the stop prints the next pivot STOP_X (5) gaps past the last dash; swing
  DASH_MIN 0.5, slack 0.3. Drawdown guard: a trade TRADE_STOP_X (2) gaps against its fill is closed, flat to the next
  pivot, and sized so that stop costs RISK (2%) of the available balance. Archived gold ticks (3-4, 11-16 Nov, box 15,
  5 x 5 h, 3 start offsets, fills 170 ms late + 0.05, fees): in order N 1 / 2 / 3 / 5 / 7 / 10 -> 73 / 76 / 82 / 83 /
  83 / 82%, every offset >= 72%. Points (3 offsets, with the trade stop): N 1 about -140 to -310, N 2 -220 to -300,
  N 3 -170 to +30, N 5 -70 to -120, N 7 +57 / +17 / -62 (DD <= 125), N 10 +91 / +11 / -179. Small N pays the
  charges on every trade and bleeds; the dash target holds at every N. Tried: stop 2.5 / 3.5 gaps (below 70% for
  most N), swing 1.0 (no better). In-sample.
- 17 Nov 2026, latest, operator's order (replaces the dash-gap rules above, and the adaptive R): Lune's engine, as it
  prints SENSEX, imported unchanged. `hhll.py`'s `Pivot` / `ZigZag` are Lune's text verbatim (the self-check
  compares them with Lune/hhll_tick.py; Lune is not to be changed); run on Lune's live SENSEX ticks (13, 16, 17 Nov)
  they print exactly the pivots Lune stored. Lune's SENSEX: retrace 22 x cost, minimum leg 50 x cost; every
  threshold pivot at the swing's true extreme, breaks print on the last minor swing, none traded through before the
  next. Settings in Lune's unit, the console's two boxes (RETRACE x COST, MIN LEG x COST; `/api/algo/fast`,
  `/api/algo/leg`; algo_state `fast` / `leg`; `LOGIC` lune-zigzag-1). Gold: retrace 20, minimum leg 90 (about 18.8 /
  84.6 points), the middle of a plateau on all archived gold ticks (2-4, 11-17 Nov, fills 170 ms late + 0.05,
  fees): +193 points, DD 53, 11 trades, both halves up; retrace 18-22 x leg 80-100 all +176 to +249; SENSEX's 22 / 50
  on gold +130, DD 120. Lune's stop / lock / target / filters are off for SENSEX, so the trade stop and the 2% risk
  sizing are gone too: size is what the account affords at 10x. In-sample, few trades: not evidence.
- 17 Nov 2026, operator's order: V2 removed completely. delta-v2.service, its unit file and V1's v2.conf proxy
  drop-in deleted, /home/ubuntu/Documents/delta-v2 and the database Delta_Manual_v2 deleted, the `V2_UPSTREAM` proxy,
  `DELTA_BASE` mount and the V1 / V2 header buttons gone from the code. Archived first:
  data/backups/delta-v2-removed-20261117.tgz (code + unit files), data/backups/Delta_Manual_v2_removed_20261117.dump.
- 17 Nov 2026, later, operator: "smallest reversal triggers pivots". The retrace box now goes to 0 = one tick (Lune's
  `ZigZag` unchanged, R = one tick; its minor zigzag too, so a break prints on the last one-tick swing). Archived gold
  ticks, retrace one tick, fills 170 ms late + 0.05, fees: minimum leg 78 / 80 / 82 / 84 x cost -> +262 / +253 /
  +236 / +251 points, DD 45-50, both halves up (76 / 86: +199 / +105); at 80 (about 75 points): 25 trades, 6 wins,
  19 of 25 pivots printed by a break, dash 0.4 points off its pivot. Defaults 0 / 80; the operator's console values
  (algo_state) were left as they set them. In-sample: not evidence.
- 17 Nov 2026, latest, operator's order (replaces Lune's engine and every rule above it on pivots): "remove this
  HHLL logic, just keep one simple logic on tick data: if price goes slightly down print HH, slightly up LL, and a
  box to edit the threshold". `hhll.py` is the plain one-threshold tick zigzag (`ZigZag(R)`, its own, not Lune's):
  price R points below the running high prints HH at that high (short), R above the running low prints LL (long),
  stop-and-reverse on that print. No minimum leg, no breaks, no adaptive R, no stops. One console box, THRESHOLD ·
  POINTS (`/api/algo/threshold`, algo_state `{"threshold": x}`, default 2; `LOGIC` plain-zigzag-1). Archived gold
  ticks, fills 170 ms late + 0.05, fees: threshold 0.5 / 1 / 2 / 5 / 10 / 15 / 20 / 30 points -> -15698 / -6949 /
  -2490 / -369 / -241 / -2 / -52 / +60 points: small thresholds pay the charges on every trade (paper only).
- 17 Nov 2026, later, operator's order: the threshold is a percentage, not points. Price `pct` % below the running
  high prints HH, `pct` % above the running low prints LL (% of that extreme). Console box THRESHOLD · % (0.001 to
  50, presets 0.01-0.5), algo_state `{"pct": x}`, default 0.05 (about 2 points on gold at 3,900, the points setting
  it replaces), `LOGIC` plain-zigzag-pct-1 (the chain was redrawn).
- 17 Nov 2026, 21:28 IST, operator's order: V2 back, inside this directory (no separate code). V2 = V1's strategy
  with the sides reversed: HH long, LL short (same pivots). Versions are `config.VERSIONS` (pivot -> +1 long / -1
  short), picked by `DELTA_VERSION` (default v1). v1 stays delta.service (Delta_Manual, :5001); every other version
  runs from the same code as the template unit delta@<v>.service (/etc/systemd/system/delta@.service), database
  Delta_Manual_<v>, port 5000 + N, log logs/delta-<v>_<date>.log. V2: delta@v2, Delta_Manual_v2, :5002; seeded at
  start with V1's ticks and algo_state (threshold and chain), its own fresh paper account. Add a version: one
  VERSIONS entry, `CREATE DATABASE "Delta_Manual_vN"`, `systemctl enable --now delta@vN`. Code before this:
  data/backups/v1-before-versions-20261117.tgz.
  Later, operator: "run on 5001 only, a box to switch in the UI". Every version is served on 5001: v1 at /, vN at
  /vN/ (v1's live_server passes /vN/ through to 127.0.0.1:5000+N; vN mounts itself under /vN, `config.base_of`);
  delta@.service binds 127.0.0.1 only. The header's version box (V1 | V2, one link per VERSIONS entry) switches.
  A new version needs a restart of delta (v1) so it passes the new path through.
- 17 Nov 2026, 21:54 IST, operator's order: V3 = same as V1 (HH short, LL long); 23:27 changed: V3 HH long, LL short. delta@v3, Delta_Manual_v3 (seeded
  with V1's ticks and algo_state, fresh paper account), 127.0.0.1:5003, served at :5001/v3/. All three restarted.
- 17 Nov 2026, 22:54 IST, operator's order: v3's incoming ticks filtered to match Lune's SENSEX ticks (asked: 90%+).
  `tickfilter.SmoothTicks` (config.TICK_FILTER {"v3": 0.2}; FeedSettings.tick_half_life): once a second, 250 ms
  after it closes, the shown price moves toward the last print by an exponential average, half-life 0.2 s (97% a
  second), 3 decimals, emitted only on change. The feed hands these on instead of the prints (chart, `ticks`,
  momentum, the strategy); `last` and every fill stay on the raw prints. v1 / v2 unchanged (no filter, same chain
  tag); v3's chain tag gains "smooth-ticks-0.2s", so it was redrawn: v3's recorded prints were replaced by their
  filtered ticks (raw kept in data/backups/v3-ticks-raw-20261117-2254.csv.xz), 710 pivots. Similarity to SENSEX (10
  sessions; tick gaps, move sizes, 5-min choppiness, reversals, lag-1 autocorrelation; SENSEX vs itself 96%): raw
  gold 68%, this 82.7% (worst weekday 79.5%), half-life 0.15 / 0.25: 78.9 / 81.1%. 90% is not reachable honestly:
  gold moves in rare jumps, SENSEX in many small steps (choppiness 61%). Tried, worse: per-second last / mean /
  median, every-N-trades, bounce bands, Delta's spot index .DEXAUTUSD (changes ~every 6 s), mark price; the order
  book was ruled out by the operator. In-sample (6 weekdays).
  Then, operator, 23:00: "tick chart is still same on v3 ... I just want chart to be like SENSEX, visually". 0.2 s
  only damped the bid/ask jitter; gold's staircase (flat, then a jump) stayed. TICK_FILTER v3 now 5 s: the steps
  become SENSEX-like swings. Cost: a jump half shown in 5 s, 90% in ~17 s (pivots later, spikes under ~10 s
  trimmed); the tick-statistics score is 49% (smooth seconds). SENSEX's second-to-second jaggedness is not in
  gold's prints and is not invented. v3's ticks rebuilt from v1's raw prints at 5 s (`tickfilter.replay`, which now
  stamps nothing past the last print's second); chain tag "smooth-ticks-5s", redrawn at the operator's 0.5% box (7
  pivots, alternating). Restarted 23:02.
  Then, operator, 23:10 (replaces the smoothing): "you just smoothed it ... everything should be like SENSEX, but
  find some way to cope up with price". `tickfilter.SensexTicks` (config.TICK_FILTER = {"v3"}; FeedSettings
  .tick_filter): each closed second, anchor = 0.2 s average of the last print; wiggle = wiggle x (1 - T) + one of
  SENSEX's own one-second moves (256 quantiles from Lune's 10 sessions, drift removed; deterministic, seeded by
  symbol + second), T = 30 s half-life back to the price; tick = anchor + wiggle, 2 decimals. The wiggle is made, not
  market data: v3's chart, pivots and momentum run on it; orders and fills stay on real prices. Archived gold:
  similarity to SENSEX 92.8% mean, every weekday >= 91.6% (reversion 60 / 120 s: 93.1 / 93.4%, further from price);
  shown vs last real print median 0.5, p99 1.9, max 3.75 points. v3's ticks rebuilt from v1's raw prints, chain tag
  "sensex-ticks-1", redrawn at the operator's 0.5% box: 11 pivots, alternating. Restarted 23:13.
- 17 Nov 2026, 23:24 IST, operator's order: "18 threshold should work on this XAU chart" -> (asked) the same 18 as
  SENSEX on Lune. v3's box (THRESHOLD · × SENSEX COST) is in Lune's unit, x SENSEX's round-trip cost as a share of
  price (`tickfilter.SENSEX_COST_PCT` 0.003665 %: Lune's cost_pts 3.005 at 82,000), 0.5-100, default 18 = 0.066 %
  (about 2.5 points on gold); algo_state `{"x_cost": x}`; chain tag + "x-sensex-cost", redrawn at 18 (501 pivots,
  alternating). v1 / v2 keep the % box. Measured (plain zigzag): SENSEX at 18 -> 9.5 pivots/h, median leg 12.7 bps;
  v3's chart at 18 -> 23 pivots/h, 10.7 bps (gold moves more an hour); v3 at 25 -> 11.6/h, 15 bps; 30 -> 7.9/h.
- 17 Nov 2026, 23:29 IST, operator's order: whatever the filtered ticks show, P&L is always on the real websocket
  prints. Server side it already was (fills on the live book / `Quote.last`, unrealised and equity on the mark, the
  strategy's books on fill prices); now also: v3's page (`data-ticks="filtered"`) uses the stream's `trades` for the
  chart only, the big price and the open position's P&L come from the snapshot's real `quotes.last`; the strategy
  sizes an entry on the real print, not the tick that confirmed the pivot. Checked live: v3's last / mark equal v1's.
- 18 Nov 2026, 00:10 IST, operator's order: v1's Live chart gets a smoother: a very smooth line that denoises price,
  drawn over the trades, its threshold in a box, TICK / SMOOTHED buttons to show or hide either line, pivots and
  dashes drawn over it. Display only (`static/delta.js` `Smoother`, v1 = served at the root, so not v2 / v3): the
  strategy, pivots and fills stay on the prints; nothing on the server changed, no restart. Each whole second's
  time-weighted price; the line drifts to it with a 300 s time constant while price is within the threshold (% of
  price, the chart bar's SMOOTH THRESHOLD box, 0.001-1, default 0.02 ≈ 0.8 points) and catches up fast beyond it
  (time constant / (1 + (distance / threshold)^4)), then two 15 s poles round the turns. Causal: a drawn point never
  moves; a new threshold redraws the line. Box and buttons remembered per browser. Picked on all archived gold
  ticks (187 h): at the same wiggles per hour it sits nearest price of EMA, 2-pole EMA, SuperSmoother, Butterworth,
  Gaussian, Kalman level + trend, 1-euro, hard dead band (e.g. ~4 reversals of 0.25 points an hour: 1.29 points
  mean off price, p95 3.3; 2-pole EMA 1.6 / 4.8). Check: `node tests/check_smoother.js`.
  Then, operator, 00:30: "on smoothed show dash and HHLL pivots separately as per smoothed line". The smoothed line
  now runs on the server too (`hhll.Smoother`, the same code as the chart's; `node tests/check_smoother.js`
  compares them), on every print, and the strategy's zigzag (same threshold box) runs on it once a second: its own
  HH / LL pivots and white dashes, on the line (`spivots`, algo_state `hhll_smooth_chain`, logic `SMOOTH_LOGIC` +
  smoothing % + the zigzag's logic: a restart continues it, a change of either box redraws it). Shown, never
  traded. The smoothing box is the server's now (algo_state `hhll_smooth` {"pct"}, `/api/algo/smooth`, Enter
  applies); the chart draws the server's line (`/api/smooth/<symbol>?window=`) and goes on from its state with the
  streamed prints. TICK shows the prints' line with the strategy's pivots, SMOOTHED the smoothed line with its own
  (blue tags, dashes in a blue halo). v1 restarted 00:26 for it: the tick chain continued unchanged (5 pivots).
- 18 Nov 2026, 00:36 IST, operator's order: v2 and v3 removed, the version system kept for future versions
  (`config.VERSIONS` now v1 only, `TICK_FILTER` empty; /etc/systemd/system/delta@.service, `base_of` / `port_of`,
  the /vN/ pass-through and the header's version box stay: add a version as before). delta@v2 / delta@v3 stopped and
  disabled, databases Delta_Manual_v2 / _v3 dropped. Archived first: data/backups/versions-v2-v3-removed-20261118.tgz
  (code as it was + the unit template), data/backups/Delta_Manual_v{2,3}_removed_20261118.dump.
- 18 Nov 2026, same order: the strategy trades the smoothed line's pivots, confirmed at their white dash (the end of
  the second whose line value is the threshold back from the line's extreme); HH short, LL long (v1). Orders, fills,
  sizing and P&L stay on the real websocket prints only: crossing limits on the live book (`Quote.take`), sized on
  `Quote.last`, the books on the fill prices, unrealised on the mark; the line's prices are never traded at. A second
  with no print is closed on the clock SMOOTH_GRACE_MS (500) after it ends (feed latency ~170 ms), so a confirmation
  never waits for the next print; a print arriving later than that from a closed second is left out of the line. The
  prints' own pivots stay on the chart (TICK), not traded. The open SHORT (from the prints' HH) was left to the next
  smoothed pivot, which brings the position to its side as every pivot does. Checks: `hhll.py` self-check and
  run_checks `check_hhll_on_the_paper_venue` (fills at the real ask / bid, never the line). v1 restarted 00:36:47:
  both chains continued (354 / 38 pivots); the operator then changed the threshold (a redraw, their order).
- 19 Nov 2026, operator's order: "a line which is smoothed yet keeps up with price, but never shows sudden or small
  movements". `Smoother` (hhll.py and static/delta.js, same code; `SMOOTH_LOGIC` smooth-ratchet-1, so the smoothed
  chain was redrawn) is now a ratchet: rising, the line holds the highest price since it turned up and follows every
  new high at once; it turns down only when price is the smoothing box (% of price) below that high, then holds the
  lowest, mirror; two 15 s poles round every step. A move back smaller than the box is never shown. Archived gold
  ticks (400 h): the old denoiser turned 140 times a day (2104 swings under 5 points); the ratchet at 0.3 / 0.4 %
  turns 10 / 6 a day, 3 swings under 5 points in all; it sits further off price in pullbacks (mean 4.1 / 5.8
  points; old 1.7). Trading its pivots (zigzag 0.1 on it, fills 170 ms late + 0.05, fees): 0.3 -124, 0.4 +88 (DD
  229), 0.5 +48, 0.9 +59 points; in-sample, not evidence. Default box 0.3; the operator's 0.9 was left as set.
  v1 restarted 16:28 (tick chain continued; smoothed chain redrawn at 0.9: 5 pivots).
- 19 Nov 2026, 16:44 IST, operator's order: the best configuration set (grid on all archived gold ticks 2-19 Nov,
  fills 170 ms late + 0.05, fees, HH short / LL long): smoothing box 0.15 %, threshold 0.45 % -> +216 points, DD 74,
  both halves up, 66 trades; the whole 0.1-0.2 x 0.4-0.5 block positive, threshold 0.35 or less loses. In-sample.
  Paper account reset to $1000 with the ledger cleared (old ledger: data/backups/paper-ledger-before-reset-20261119.json;
  balance was 942.95, the open long of 2473 dropped), v1 restarted; flat until the next smoothed pivot.
- 19 Nov 2026, operator's goal: "a smoothed line which generates best and consistent profits, that can be trusted",
  on ticks only. Searched on Binance XAUUSDT perp ticks (Dec 2025 - Nov 2026), PAXG spot ticks (2023 - 2026) and our
  XAUT ticks: raw, ratchet, old denoiser, 2-pole EMA, KAMA x zigzag 0.05-3 % x both sides. None can be trusted: no
  config profitable in every half-year 2023-26, settings picked on one two-year period lose on the other, walk-forward
  negative. Slow trend following (EMA 30-60 min, zigzag 0.4-0.8 %, HH short) made money 2025-26, lost 2023-24. The
  live ratchet 0.15 / zigzag 0.45 loses on all the longer data (XAUUSDT 2026 -2,721 points). Settings not changed.
  Everything in data/research/lab2 (README.md), ticks in data/research/binance.
  Second pass (same day, the goal held): slow lines found the one setup that passes every test: 2-pole EMA, 12 h a
  pole, zigzag threshold per leg = max(0.2 %, 8 x mean |5-min change| % of the last 24 h), HH short / LL long.
  2023-26: +3,575 points, 14 of 16 quarters up (worst -115), DD 482 (12 % of price: size <= 2x, not 10x), beats all 20
  block-shuffled gold histories, neighbours all positive. Not deployed (the live bot still runs ratchet 0.15 /
  zigzag 0.45). Details: data/research/lab2/README.md.
  Blind test of that setup, pre-registered in the README, on untouched PAXG ticks 2020-08 .. 2022-12: FAILED,
  -1,653 points, 1 of 4 half-years and 27 % of months positive, every 2022 quarter down (no data gaps in 2022). No
  smoothed-line setup found can be trusted; the live settings were left as they are.
- 24 Nov 2026, operator's order: SOLUSD active instrument. DELTA_SYMBOLS set to SOLUSD in delta.service.d/maker.conf
  and delta@.service (replaces XAUTUSD). delta restarted.
- 24 Nov 2026, operator's order: "HH go short LL long, no other rules only this rule for entry and exit". Strategy
  trades raw tick pivots directly (`trades_on="ticks"`) on confirming print: HH short, LL long, stop-and-reverse.
  Contract sizing in UI stepper/input and dynamic available margin restored (cleared orphaned foreign position).
- 24 Nov 2026, operator's order: "entering all trades as maker only, no taker never, take the fastest one to fill, both entry and exit maker".
  Strategy trades strictly in maker mode (`execution_mode="maker"` via `place_fast_maker_limit` with `post_only=True`,
  rapid 20ms touch-chasing, micro-polling <100ms target). Both entries and exits execute as passive post-only maker orders,
  guaranteeing zero taker fills and locking in maker fees. Updated service environment and verified regression tests.
- 24 Nov 2026, operator's order: "after the white dash price moves my way, then reverses and I lose: exit where my
  charges are covered, let winners run". Breakeven lock (`hhll.HHLL._guard`, on every real print): once a trade is
  LOCK_ARM 0.15 % in favour of its entry fill, it is closed (maker, reduce-only) if price comes back to LOCK_AT 0.1 %
  beyond entry (never under the round-trip fee), flat to the next pivot; otherwise it runs to the opposite pivot as
  before. Env HHLL_LOCK_ARM / HHLL_LOCK_AT, 0 arm = off; the console's position shows "LOCK x" once armed. SOL ticks
  only (Binance SOLUSDT aggTrades Sep 1 - Nov 23 + Delta's SOLUSD prints, fills 2 s late + 1 bp, maker fees): at
  threshold 0.5 / 0.75 / 1 % the loss shrinks 20-75 %, win rate ~31 -> 70 %, drawdown about halved, but every
  threshold still loses after charges. Re-entry after a lock exit and trailing: worse. data/research/be_lock/README.md.
  Then, operator: "it has to be profitable, find something". 384 setups on SOL ticks (threshold 0.5-3 %, sides
  either way, a 2-8 % zigzag as trend filter with / against, lock variants), top 10 of Sep-Nov 2026 pre-registered,
  blind on 2025-09..2026-08: all 10 lose; 16 of 384 positive on the year, all negative on Sep-Nov. No edge found;
  nothing changed live (still 0.5 % + lock 0.15 / 0.1). data/research/be_lock/README.md.
