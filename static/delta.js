/* Delta · manual trading console
 *
 * One snapshot stream drives the board, the account and the positions - the
 * server pushes a fresh one on every feed tick; the
 * history tabs are fetched only while visible; the ticket asks the server for
 * a cost preview (fees, GST, margin, liquidation) as it is filled in, so the
 * number shown before an order is the number the venue charges.
 *
 * Every order is a limit. With no price typed the server auto-prices it (the
 * best maker price, re-priced until it fills) and it shows as an auto order.
 */
(() => {
  "use strict";

  const body = document.body;
  const POLL_MS = Math.max(500, Number(body.dataset.pollMs) || 1500);
  const LIVE = body.dataset.mode === "LIVE";

  const IST = "Asia/Kolkata";
  const istTime = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const istShort = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST, hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const istWhen = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const istStamp = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hour12: false,
  });

  const $ = (id) => document.getElementById(id);
  const ticket = $("ticket");
  const el = {
    feedPill: $("feedPill"), feedLabel: $("feedLabel"), clock: $("clock"),
    pingPill: $("pingPill"), pingLadder: $("pingLadder"), pingValue: $("pingValue"),
    symbolPicker: $("symbolPicker"), specLine: $("specLine"),
    boardPrice: $("boardPrice"), boardMeta: $("boardMeta"),
    veil: $("chartVeil"), liveWindows: $("liveWindows"), tip: $("chartTip"), tipTime: $("tipTime"), tipBody: $("tipBody"),
    positions: $("positions"), openOrders: $("openOrders"),
    countPositions: $("countPositions"), countOrders: $("countOrders"), closeAll: $("closeAll"),
    ticketSymbol: $("ticketSymbol"), ticketLeverageNow: $("ticketLeverageNow"),
    autoPrice: $("autoPrice"), sizeHint: $("sizeHint"),
    leverageInput: $("leverageInput"), leverageDown: $("leverageDown"), leverageUp: $("leverageUp"),
    leveragePresets: $("leveragePresets"), sizePcts: $("sizePcts"),
    leverageValue: $("leverageValue"), leverageApply: $("leverageApply"),
    pvPrice: $("pvPrice"), pvNotional: $("pvNotional"), pvMargin: $("pvMargin"), pvFee: $("pvFee"),
    pvExit: $("pvExit"), pvRequired: $("pvRequired"), pvAvailable: $("pvAvailable"), pvLiq: $("pvLiq"),
    ticketError: $("ticketError"), buyButton: $("buyButton"), sellButton: $("sellButton"), exitButton: $("exitButton"),
    accountMode: $("accountMode"), equity: $("equity"), balance: $("balance"),
    available: $("available"), unrealised: $("unrealised"),
    positionMargin: $("positionMargin"), orderMargin: $("orderMargin"),
    periodPnl: $("periodPnl"), periodFees: $("periodFees"), periodFunding: $("periodFunding"),
    periodNet: $("periodNet"), resetPaper: $("resetPaper"),
    feedDot: $("feedDot"), feedMeta: $("feedMeta"), brokerDot: $("brokerDot"), brokerMeta: $("brokerMeta"),
    dbMeta: $("dbMeta"), uptime: $("uptime"), toast: $("toast"),
    quoteSymbol: $("quoteSymbol"), priceMain: $("priceMain"), pricePaise: $("pricePaise"),
    priceChange: $("priceChange"), priceAsOf: $("priceAsOf"),
    pnlPoints: $("pnlPoints"), pnlUsd: $("pnlUsd"),
    readKind: $("readKind"), readAt: $("readAt"), readO: $("readO"), readH: $("readH"),
    readL: $("readL"), readC: $("readC"), barCount: $("barCount"),
    sizeDown: $("sizeDown"), sizeUp: $("sizeUp"),
  };

  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
  const PALETTE = {
    upFill: v("--up-fill", "#34d399"), downFill: v("--down-fill", "#fb7185"),
    up: v("--up", "#34d399"), down: v("--down", "#fb7185"),
    grid: v("--grid", "rgba(148,163,184,.1)"), axis: v("--axis", "#8b97a9"),
    crosshair: v("--crosshair", "rgba(203,213,225,.34)"),
    iris: v("--iris", "#a3b05f"), amber: v("--amber", "#fbbf24"),
    upLine: v("--up-line", "rgba(52,211,153,.38)"), downLine: v("--down-line", "rgba(251,113,133,.38)"),
    gridSoft: v("--grid-strong", "rgba(190,200,150,.03)"), crossDot: v("--cross-dot", "#fff"),
    badgeInk: v("--ink-badge", "#05060b"),
    crossLabel: v("--line-bright", "#2c3044"), crossInk: v("--ink", "#eef0fb"),
    mono: v("--mono", "monospace"), pivotLine: "rgba(220,225,255,.32)", smooth: v("--smooth", "#38bdf8"),
    smoothPivot: "rgba(56,189,248,.35)",
  };

  let specs = {};               // symbol -> instrument spec (fees, tick, leverage)
  // The charted symbol is remembered per browser, honoured only while it is still offered.
  const SYMBOL_KEY = "delta.symbol";
  let selected = el.symbolPicker.value || null;
  try {
    const kept = localStorage.getItem(SYMBOL_KEY);
    if (kept && [...el.symbolPicker.options].some((o) => o.value === kept)) selected = kept;
  } catch (error) { /* not remembered */ }
  let resolution = "live";
  let liveWindow = "15m";
  let snapshot = null;
  let view = "positions";
  let period = "1D";
  let busy = false;
  let pressing = false;         // a button is half-clicked: do not swap it out yet

  /* ---------- formatting ------------------------------------------------- */
  const num = (value) => (value === null || value === undefined || value === "" ? null
    : Number.isFinite(Number(value)) ? Number(value) : null);

  function decimals(symbol) {
    const tick = (specs[symbol] || {}).tick_size || "0.0001";
    const dot = tick.indexOf(".");
    return dot < 0 ? 0 : tick.replace(/0+$/, "").length - dot - 1;
  }

  function px(value, symbol) {
    const n = num(value);
    if (n === null) return "—";
    let digits = symbol ? decimals(symbol) : (Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 4 : 6);
    if (symbol && digits === 0 && Math.abs(n) < 1) digits = 6;
    return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function usd(value, signed = false, digits = 2) {
    const n = num(value);
    if (n === null) return "—";
    const sign = signed && n > 0 ? "+" : n < 0 ? "-" : "";
    const abs = Math.abs(n);
    const d = abs !== 0 && abs < 0.01 ? Math.max(digits, 4) : digits;
    return `${sign}$${abs.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d })}`;
  }

  const pct = (value, digits = 4) => (num(value) === null ? "—" : `${Number(value).toFixed(digits)}%`);
  const cls = (value) => {
    const n = num(value);
    return n === null || Math.abs(n) < 1e-12 ? "neutral" : n > 0 ? "positive" : "negative";
  };
  function tone(node, value) {
    node.classList.remove("positive", "negative", "neutral");
    node.classList.add(cls(value));
  }
  const when = (stamp) => (stamp ? istStamp.format(new Date(stamp)) : "—");
  const esc = (text) => String(text ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function duration(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m ${s % 60}s`;
  }

  // Rows are re-rendered on every tick: skip the identical ones, and hold off
  // while a pointer is down so a click lands on the button it started on.
  function setRows(node, html) {
    if (pressing || node.rendered === html) return;
    node.rendered = html;
    node.innerHTML = html;
  }
  document.addEventListener("pointerdown", () => { pressing = true; }, true);
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => { window.setTimeout(() => { pressing = false; }); }, true);
  }

  function toast(message) {
    el.toast.textContent = message;
    el.toast.classList.add("is-visible");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => el.toast.classList.remove("is-visible"), 4200);
  }

  const BASE = document.body.dataset.base || "";   // "/v2" when a version is served through v1's port
  // v3: the stream's `trades` are SENSEX-like filtered ticks (tickfilter.py), for the chart only. The price shown
  // and every P&L stay on the real prints, which each snapshot carries (`quotes.last`, `mark`).
  const FILTERED_TICKS = document.body.dataset.ticks === "filtered";

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 15000);
    const init = { cache: "no-store", signal: controller.signal, ...options };
    if (options.json !== undefined) {
      init.method = init.method || "POST";
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(options.json);
      delete init.json;
    }
    try {
      const response = await fetch(BASE + path, init);
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error((payload && payload.error) || `HTTP ${response.status}`);
      return payload;
    } finally {
      window.clearTimeout(timer);
    }
  }

  /* ---------- HH/LL strategy panel (rail module 01) --------------------- */
  // The plain zigzag's one box: the reversal, in % of the extreme, that prints a pivot. On v3 (SENSEX-like ticks)
  // it is in Lune's unit, x SENSEX's round-trip cost (`data-cost-pct` % of price): 18 there is SENSEX's 18.
  const leg = { applied: null, dirty: false, busy: false };
  const legBox = document.querySelector(".leg-control");
  const XCOST = !!legBox && legBox.dataset.unit === "x_cost";
  const COST_PCT = legBox ? Number(legBox.dataset.costPct) || 0 : 0;
  const LEG_MIN = XCOST ? 0.5 : 0.001, LEG_MAX = XCOST ? 100 : 50;
  const legPct = (v) => (XCOST ? v * COST_PCT : v);
  const legUnit = (v) => (XCOST ? `${legShow(v)}× (${legShow(legPct(v))}%)` : `${legShow(v)}%`);

  function legValue() {
    const v = Number($("legInput").value);
    return $("legInput").value !== "" && Number.isFinite(v) ? v : null;
  }

  function legShow(v) {
    return v === null || v === undefined ? "—" : String(Number(Number(v).toFixed(4)));
  }

  function legRefresh() {
    const input = $("legInput");
    if (!input) return;
    const v = legValue();
    const valid = v !== null && v >= LEG_MIN && v <= LEG_MAX;
    leg.dirty = valid && leg.applied !== null && Math.abs(v - leg.applied) > 1e-12;
    $("legSet").disabled = !leg.dirty || leg.busy;
    input.closest(".leg-control").toggleAttribute("data-dirty", leg.dirty);
    for (const b of document.querySelectorAll("#legPresets button")) {
      b.setAttribute("aria-pressed", String(v !== null && Math.abs(Number(b.dataset.leg) - v) < 1e-12));
    }
    const q = snapshot && (snapshot.quotes || {})[selected];
    const ref = q ? num(q.last) || num(q.mark) : null;
    $("legHint").textContent = !valid
      ? (XCOST ? "0.5 to 100 × SENSEX's cost." : "0.001 to 50 %.")
      : `${legShow(legPct(v))}% down from the high prints HH · ${legShow(legPct(v))}% up from the low prints LL` +
        (XCOST ? ` · ${legShow(v)} × SENSEX's cost, as SENSEX's ${legShow(v)} on Lune` : "") +
        (ref ? ` · ≈ ${px((ref * legPct(v)) / 100, selected)} now` : "") + (leg.dirty ? " · not applied yet" : "");
  }

  function legStatus(text, tone) {
    const s = $("legStatus");
    if (!s) return;
    s.hidden = !text;
    s.textContent = text || "";
    s.dataset.tone = tone || "";
  }

  function legStep(direction) {
    const v = legValue() ?? leg.applied ?? (XCOST ? 18 : 0.05);
    const step = XCOST ? (v < 2 || (direction < 0 && v <= 2) ? 0.5 : 1)
      : v < 0.1 || (direction < 0 && v <= 0.1) ? 0.01 : v < 1 || (direction < 0 && v <= 1) ? 0.05 : 0.5;
    const next = Math.min(LEG_MAX, Math.max(LEG_MIN, Math.round((v + direction * step) * 1000) / 1000));
    $("legInput").value = legShow(next);
    legStatus("");
    legRefresh();
  }

  async function applyLeg() {
    const v = legValue();
    if (!selected || v === null || !leg.dirty || leg.busy) return;
    leg.busy = true;
    legRefresh();
    legStatus("Redrawing the pivot chain with the new threshold…", "");
    try {
      const r = await api("/api/algo/threshold", { json: { symbol: selected, threshold: v } });
      leg.applied = Number(r.threshold);
      $("legInput").value = legShow(r.threshold);
      legStatus(`Threshold ${legUnit(r.threshold)} applied; the pivot chain was redrawn with it.`, "good");
    } catch (error) {
      legStatus(error.message, "bad");
    } finally {
      leg.busy = false;
      legRefresh();
    }
  }

  // The pivot list is fetched only when it changes (a new pivot, a new leg, another symbol); in between only the
  // running extreme moves, and it comes with the status.
  const pivotFeed = { key: null, list: [], smooth: [], busy: false };
  async function loadPivots(s) {
    const lp = s.last_pivot, sp = s.smooth_last_pivot;
    const key = `${selected}:${s.threshold}:${s.pivot_count}:${lp ? `${lp.type}@${lp.at}` : "-"}:` +
      `${s.smooth}:${s.smooth_pivot_count}:${sp ? `${sp.type}@${sp.at}` : "-"}`;
    if (key !== pivotFeed.key && !pivotFeed.busy) {
      pivotFeed.busy = true;
      try {
        const r = await api(`/api/algo/pivots/${selected}`);
        pivotFeed.list = r.pivots || [];
        pivotFeed.smooth = r.smooth_pivots || [];
        pivotFeed.key = key;
      } catch (error) {
        pivotFeed.key = null;                 // retried on the next status
      } finally {
        pivotFeed.busy = false;
      }
    }
    const mine = pivotFeed.key && pivotFeed.key.startsWith(`${selected}:`);
    chart.setPivots(mine ? pivotFeed.list : [], s.running || null, mine ? pivotFeed.smooth : [], s.smooth_running || null);
    smoothFollow(s.smooth);
  }

  function renderStrategy() {
    const panel = $("strategy");
    if (!panel) return;
    const algo = snapshot && snapshot.algo;
    const s = algo && algo.symbols && selected ? algo.symbols[selected] : null;
    const set = (id, text, cls) => { const e = $(id); e.textContent = text; e.className = cls || ""; };
    $("strategySymbol").textContent = selected || "—";
    const state = $("strategyState");
    if (!s) {
      chart.setPivots([], null);
      state.textContent = algo ? "NOT TRADED" : "STRATEGY OFF";
      state.dataset.tone = "off";
      for (const id of ["stPivot", "stSwing", "stPosition", "stCost", "stTrades", "stNet"]) set(id, "—");
      $("legLive").textContent = "LIVE —";
      return;
    }
    loadPivots(s);
    const m = algo.metrics || {};
    const open = s.phase === "open";
    const w = s.working;
    const working = w ? `${w.kind === "exit" ? "EXITING" : "ENTERING"} · MAKER ${w.side.toUpperCase()}${w.price != null ? ` @ ${px(w.price, selected)}` : ""}` : null;
    state.textContent = working || (open ? `${s.side} OPEN` : s.halt ? s.halt.toUpperCase() : "WATCHING");
    state.dataset.tone = open || working ? "open" : s.halt ? "warn" : "idle";
    state.title = s.halt || "";
    // The strategy trades the smoothed line's pivots: its last one and its swing.
    const tp = s.trades_on === "smoothed" ? s.smooth_last_pivot : s.last_pivot;
    const sw = s.trades_on === "smoothed" ? s.smooth_swing : s.swing;
    set("stPivot", tp ? `${s.trades_on === "smoothed" ? "SMOOTHED " : ""}${tp.type} ${px(tp.price, selected)}` : "—");
    set("stSwing", sw ? sw.toUpperCase() : "—");
    set("stPosition", open ? `${s.side} @ ${px(s.entry_price, selected)}${s.lock != null ? ` · LOCK ${px(s.lock, selected)}` : ""}` : "FLAT", open ? (s.side === "LONG" ? "positive" : "negative") : "");
    set("stCost", num(s.cost) === null ? "—" : px(s.cost, selected));
    set("stTrades", `${m.trades || 0} · ${m.wins || 0}`);
    const net = num(m.net_usd);
    set("stNet", net === null ? "—" : usd(net, true), net > 0 ? "positive" : net < 0 ? "negative" : "");
    const input = $("legInput");
    const changed = leg.applied === null || Math.abs(leg.applied - s.threshold) > 1e-12 || input.dataset.symbol !== selected;
    leg.applied = num(s.threshold);
    $("legLive").textContent = `LIVE ${legUnit(s.threshold)}${num(s.threshold_pts) ? ` · ${px(s.threshold_pts, selected)}` : ""}`;
    // the operator's typing wins; otherwise the box follows the live threshold
    if (changed && document.activeElement !== input && !leg.dirty) {
      input.value = legShow(s.threshold);
      input.dataset.symbol = selected;
    }
    legRefresh();
  }

  {
    const input = $("legInput");       // the script is deferred: the page is parsed
    if (input) {
      $("legSet").addEventListener("click", applyLeg);
      $("legDown").addEventListener("click", () => legStep(-1));
      $("legUp").addEventListener("click", () => legStep(1));
      input.addEventListener("input", () => { legStatus(""); legRefresh(); });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); applyLeg(); }
        else if (e.key === "Escape") { input.value = legShow(leg.applied); legStatus(""); legRefresh(); input.blur(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); legStep(1); }
        else if (e.key === "ArrowDown") { e.preventDefault(); legStep(-1); }
      });
      for (const b of document.querySelectorAll("#legPresets button")) {
        b.addEventListener("click", () => { input.value = b.dataset.leg; legStatus(""); legRefresh(); });
      }
    }
  }

  /* ---------- chart ------------------------------------------------------ */
  const MAX_TICKS = 20000;      // the Live view's history plus what streams in after it
  const MAX_BARS = 5000;        // candles kept as reloads add to what can be scrolled back through
  const MIN_BARS = 10;          // zoomed all the way in on candles
  const MIN_SPAN = 10e3, MAX_SPAN = 86400e3;    // the Live view's zoom limits, in ms
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const tick = (price, at) => ({ at, time: new Date(at).toISOString(),
    open: price, high: price, low: price, close: price });

  // Every trade lands in its bar, opening the next bar when it crosses one.
  function extend(bars, step, price, at) {
    const c = bars[bars.length - 1];
    if (!c || !step) return;
    const start = Date.parse(c.time);
    if (at < start) return;
    if (at >= start + step) {
      const bar = start + Math.floor((at - start) / step) * step;
      bars.push({ time: new Date(bar).toISOString(), at: bar, open: price, high: price,
        low: price, close: price, volume: 0 });
    } else {
      c.close = price;
      c.high = Math.max(c.high, price);
      c.low = Math.min(c.low, price);
    }
  }

  // The smoothed line over the Live view's trades (v1; shown, never traded). The server runs the same code
  // (hhll.py `Smoother`, tests/check_smoother.js compares them) on every print and prints the line's own HH/LL
  // pivots on it; the chart draws the server's line and goes on from where it stands with the prints since, or,
  // with no server line (the strategy off), runs it here over the trades it holds. Run on whole seconds, each on its time-weighted price (each print held until the next):
  //   1. the ratchet: rising, the line holds the highest price since it turned up and follows every new high at
  //      once; it turns down only when price is the threshold (`pct` % of price) below that high, then holds the
  //      lowest, mirror: a move back smaller than the threshold is never shown;
  //   2. two 15 s poles round every step and turn, so the line has no jumps and no corners.
  // Causal: a point once drawn never moves; a new threshold redraws the line from the trades held.
  const SMOOTH_ROUND_S = 30, SMOOTH_GAP_MS = 3600e3;
  class Smoother {
    constructor(pct) { this.pct = pct; this.at = []; this.v = []; this.sec = null; }

    // Closes the open second (`st.sec` + 1) of a state {s: [ratchet, pole 1, pole 2], m (+1 rising / -1 falling), sec, x, mark, area}:
    // its time-weighted price, `x` held from `mark` to its end, steps the line.
    close(st) {
      const end = (st.sec + 2) * 1000;
      const x = (st.area + st.x * (end - st.mark)) / 1000, s = st.s;
      st.sec += 1; st.mark = end; st.area = 0;
      const h = this.pct / 100;
      if (st.m > 0) {
        if (x > s[0]) s[0] = x;
        else if (x <= s[0] * (1 - h)) { st.m = -1; s[0] = x; }
      } else if (x < s[0]) s[0] = x;
      else if (x >= s[0] * (1 + h)) { st.m = 1; s[0] = x; }
      const a = 1 - Math.exp(-2 / SMOOTH_ROUND_S);
      s[1] += a * (s[0] - s[1]);
      s[2] += a * (s[1] - s[2]);
      return s[2];
    }

    // A print: the seconds before its own close (each value stored at its second's end); a late print, from a
    // second already closed, changes nothing.
    feed(price, at) {
      const sec = Math.floor(at / 1000);
      if (this.sec === null || sec - this.sec > SMOOTH_GAP_MS / 1000) {
        Object.assign(this, { s: [price, price, price], m: 1, sec: sec - 1, x: price, mark: at, area: price * (at - sec * 1000) });
        return;
      }
      if (sec <= this.sec) return;
      while (this.sec + 1 < sec) { const v = this.close(this); this.at.push((this.sec + 1) * 1000); this.v.push(v); }
      if (at > this.mark) { this.area += this.x * (at - this.mark); this.mark = at; }
      this.x = price;
    }

    // The seconds ended since the last print, up to `now`, on its price: drawn, never stored.
    tail(now) {
      if (this.sec === null) return [];
      const st = { s: this.s.slice(), m: this.m, sec: this.sec, x: this.x, mark: this.mark, area: this.area }, out = [];
      while ((st.sec + 2) * 1000 <= now && out.length < 3600) {
        const v = this.close(st);
        out.push([(st.sec + 1) * 1000, v]);
      }
      return out;
    }

    // The server's line ({pct, points, state, fed}), to go on from with the prints after `fed`.
    static load(payload) {
      const sm = new Smoother(payload.pct);
      sm.at = payload.points.map((p) => p[0]);
      sm.v = payload.points.map((p) => p[1]);
      const st = payload.state;
      Object.assign(sm, { s: st.s.slice(), m: st.m, sec: st.sec, x: st.x, mark: st.mark, area: st.area });
      sm.fed = payload.fed;
      sm.from = -Infinity;          // never rebuilt from the chart's trades: the next load replaces it
      return sm;
    }

    // The line's value at `t` (the last second ended by then).
    value(t) {
      let lo = 0, hi = this.at.length - 1;
      if (hi < 0 || t < this.at[0]) return null;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (this.at[m] <= t) lo = m; else hi = m - 1; }
      return this.v[lo];
    }

    trim(before) {
      let n = 0;
      while (n < this.at.length && this.at[n] < before) n += 1;
      if (n > 600) { this.at.splice(0, n); this.v.splice(0, n); }
    }
  }

  // The view, panned and zoomed by drag, wheel, pinch and keys:
  //   candles  `span` bars wide (null: as many as fit at 6px) ending `offset` bars before the newest
  //   Live     `liveSpan` ms wide ending at `liveTo` (epoch ms; null: now)
  //   both     the price range scaled by `yZoom` (dragging the price scale)
  // An offset of 0 / a null liveTo follows the market; double-click or End returns there.
  class CandleChart {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.candles = [];
      this.ticks = [];          // the selected symbol's websocket trades, for the Live view
      this.live = false;
      this.lines = [];
      this.geometry = null;
      this.hover = null;
      this.hoverX = null;
      this.hoverY = null;
      this.pointers = new Map();
      this.drag = null;
      this.window = 15 * 60e3;  // the Live view opens on the last `window` ms
      // The smoothed line (v1): null until `setSmooth`; `showTick` / `showSmooth` toggle the two Live lines and
      // their pivots. `smoothPct` is the server's threshold; `smoothServer` whether the line came from it.
      this.smooth = null;
      this.smoothPct = null;
      this.smoothServer = false;
      this.showTick = true;
      this.showSmooth = false;
      this.resetView();
      this.onView = null;       // (view) after each draw: the candles in view, and the one read out
      new ResizeObserver(() => this.draw()).observe(canvas.parentElement);
      canvas.addEventListener("mousemove", (event) => this.onMove(event));
      canvas.addEventListener("mouseleave", () => this.unhover());
      canvas.addEventListener("keydown", (event) => this.onKey(event));
      canvas.addEventListener("wheel", (event) => this.onWheel(event), { passive: false });
      canvas.addEventListener("pointerdown", (event) => this.onDown(event));
      canvas.addEventListener("pointermove", (event) => this.onDrag(event));
      for (const type of ["pointerup", "pointercancel"]) canvas.addEventListener(type, (event) => this.onUp(event));
      canvas.addEventListener("dblclick", (event) => this.onDouble(event));
    }

    get window() { return this._window; }
    set window(ms) { this._window = ms; this.liveSpan = ms; this.liveTo = null; }

    resetView() {
      this.span = null;
      this.offset = 0;
      this.liveSpan = this.window;
      this.liveTo = null;
      this.yZoom = 1;
    }

    unhover() {
      this.hover = null; this.hoverX = null; this.hoverY = null;
      el.tip.hidden = true;
      this.draw();
    }

    local(event) {
      const rect = this.canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    // Candles: the view moves by whole and part bars, never past the oldest or the newest.
    setOffset(offset) {
      const g = this.geometry;
      const slots = g ? g.slots : 1;
      this.offset = clamp(offset, 0, Math.max(0, this.candles.length - slots));
      if (this.offset < 1e-6) this.offset = 0;
    }

    // Live: a view ending at a fixed time stays there as trades stream; reaching now, it follows again.
    setLiveTo(to) { this.liveTo = to >= Date.now() ? null : to; }

    // Dragged `dx` px: right shows older, left newer.
    panBy(dx) {
      const g = this.geometry;
      if (!g) return;
      if (this.live) this.setLiveTo((this.liveTo ?? Date.now()) - dx / g.plotW * this.liveSpan);
      else if (g.stride) this.setOffset(this.offset + dx / g.stride);
      this.draw();
    }

    // Zoomed by `factor` (above 1: out) about the point at `x`, which keeps its
    // place; a view following the market zooms about its right edge and keeps following.
    zoomAt(factor, x) {
      const g = this.geometry;
      if (!g) return;
      const following = this.live ? this.liveTo === null : this.offset === 0;
      const r = following ? 1 : clamp((x - g.left) / g.plotW, 0, 1);
      if (this.live) {
        const at = (this.liveTo ?? Date.now()) - this.liveSpan * (1 - r);
        const span = clamp(this.liveSpan * factor, MIN_SPAN, MAX_SPAN);
        if (!following) this.setLiveTo(at + span * (1 - r));
        this.liveSpan = span;
      } else {
        const n = this.candles.length, was = g.slots;
        let span = Math.round(was * factor);
        if (span === was) span += factor > 1 ? 1 : -1;
        span = clamp(span, MIN_BARS, Math.max(MIN_BARS, n));
        // bars right of the point: (1 - r) of the view before, (1 - r) of it after
        const right = this.offset + was * (1 - r);
        this.span = span;
        this.offset = clamp(right - span * (1 - r), 0, Math.max(0, n - Math.min(span, n)));
        if (this.offset < 1e-6) this.offset = 0;
      }
      this.draw();
    }

    onWheel(event) {
      if (!this.geometry) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.geometry.plotW : 1;
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) this.panBy(-event.deltaX * unit);
      else if (event.deltaY) this.zoomAt(Math.exp(event.deltaY * unit * 0.002), this.local(event).x);
      this.onMove(event);
    }

    onDown(event) {
      if (event.button !== 0 || !this.geometry) return;
      this.canvas.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, this.local(event));
      if (this.pointers.size === 2) {           // a second finger: pinch
        const [a, b] = [...this.pointers.values()];
        this.pinch = Math.hypot(a.x - b.x, a.y - b.y);
        this.drag = null;
        return;
      }
      const { x, y } = this.local(event);
      this.drag = { x, y, last: x, gutter: x > this.geometry.right, yZoom: this.yZoom, moved: false };
    }

    onDrag(event) {
      if (!this.pointers.has(event.pointerId)) return;
      const p = this.local(event);
      this.pointers.set(event.pointerId, p);
      if (this.pinch && this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        if (dist > 0) this.zoomAt(this.pinch / dist, (a.x + b.x) / 2);
        this.pinch = dist;
        return;
      }
      const d = this.drag;
      if (!d) return;
      if (!d.moved && Math.abs(p.x - d.x) < 3 && Math.abs(p.y - d.y) < 3) return;   // a click, not a drag
      if (!d.moved) { d.moved = true; this.hover = null; this.hoverY = null; el.tip.hidden = true; }
      this.canvas.style.cursor = d.gutter ? "ns-resize" : "grabbing";
      if (d.gutter) {               // the price scale: drag down to squeeze the candles, up to stretch
        this.yZoom = clamp(d.yZoom * Math.exp((p.y - d.y) / 200), 0.05, 20);
        this.draw();
      } else {
        this.panBy(p.x - d.last);
        d.last = p.x;
      }
    }

    onUp(event) {
      if (!this.pointers.delete(event.pointerId)) return;
      if (this.pointers.size < 2) this.pinch = null;
      if (this.pointers.size) return;
      this.drag = null;
      this.canvas.style.cursor = "";
      // released outside the chart: the capture swallowed its mouseleave
      const { x, y } = this.local(event);
      if (x < 0 || y < 0 || x > this.canvas.clientWidth || y > this.canvas.clientHeight) this.unhover();
    }

    // Double-click: the price scale goes back to fitting the candles; anywhere else the whole view resets.
    onDouble(event) {
      if (!this.geometry) return;
      if (this.local(event).x > this.geometry.right) this.yZoom = 1;
      else this.resetView();
      this.draw();
    }

    // The keyboard walks the readout across the bars, as the pointer does, panning
    // at either edge; + and - zoom, End returns to the newest bars.
    onKey(event) {
      const g = this.geometry;
      if (!g) return;
      const key = event.key;
      if (["+", "=", "-", "_"].includes(key)) {
        event.preventDefault();
        this.zoomAt(key === "+" || key === "=" ? 0.8 : 1.25, g.left + g.plotW / 2);
        return;
      }
      if (key === "End") { event.preventDefault(); this.resetView(); this.draw(); return; }
      const candles = g.candles;
      if (!candles.length || !["ArrowLeft", "ArrowRight", "Escape"].includes(key)) return;
      event.preventDefault();
      el.tip.hidden = true;
      this.hoverY = null;
      this.hoverX = null;
      if (key === "Escape") this.hover = null;
      else {
        const from = this.hover === null ? candles.length - 1 : this.hover;
        const next = from + (key === "ArrowRight" ? 1 : -1);
        // a bar cut by the edge counts as off it: the view pans to show it whole
        const first = g.cut ? 1 : 0;
        if (!this.live && next < first && this.offset < this.candles.length - g.slots) {
          this.setOffset(Math.floor(this.offset) + 1);
          this.hover = 0;
        } else if (!this.live && next >= candles.length && this.offset > 0) {
          this.setOffset(Math.ceil(this.offset) - 1);
          this.hover = g.slots - 1;
        } else this.hover = clamp(next, 0, candles.length - 1);
      }
      this.draw();
    }

    // Changing resolution (or coming back from Live) opens on the newest bars;
    // a reload keeps the bars scrolled back to, and the newest trade: the
    // venue's candles lag it by a few seconds.
    setCandles(candles, step) {
      candles = candles || [];
      const same = !this.live && step === this.step && this.candles.length > 0;
      if (!same) this.resetView();
      this.live = false;
      const first = candles.length ? Date.parse(candles[0].time) : Infinity;
      const kept = same ? this.candles.filter((c) => Date.parse(c.time) < first) : [];
      const before = this.candles.length ? Date.parse(this.candles[this.candles.length - 1].time) : Infinity;
      this.candles = kept.concat(candles).slice(-MAX_BARS);
      this.step = step;
      if (!this.candles.length) this.lastTrade = null;
      else if (this.lastTrade) extend(this.candles, step, ...this.lastTrade);
      if (same) this.grew(this.candles.filter((c) => Date.parse(c.time) > before).length);
      this.draw();
    }

    // New bars arrived: a view scrolled back stays on the bars it showed, and a
    // bar picked by the keyboard stays picked (a pointer's pick follows the pointer).
    grew(added) {
      if (added <= 0) return;
      if (this.offset > 0) this.offset += added;
      else if (this.hover !== null && this.hoverX === null) this.hover = Math.max(0, this.hover - added);
    }

    // The Live view: one point per trade, no venue candles.
    showTicks() {
      if (!this.live) { this.resetView(); this.live = true; }
      this.candles = this.ticks;
      this.draw();
    }

    // The stored history goes first; trades streamed since it was read stay after it.
    seedTicks(rows) {
      const newest = rows.length ? rows[rows.length - 1][1] : -Infinity;
      const ticks = rows.map(([price, at]) => tick(price, at));
      ticks.push(...this.ticks.filter((t) => t.at > newest));
      this.ticks = ticks.slice(-MAX_TICKS);
      if (this.live) this.candles = this.ticks;
      this.smoothSync();
      this.draw();
    }

    // The line is fed once per print: a reload that brings older trades than it began on redraws it from them
    // (here only: a server line is replaced by the next load); otherwise only prints newer than the last one fed
    // go in, so the line already drawn stays as it was.
    smoothSync(rebuild = false) {
      if (this.smoothPct === null) return;
      const t = this.ticks;
      if (!t.length && !this.smoothServer) { this.smooth = null; return; }
      if (!this.smoothServer && (rebuild || !this.smooth || t[0].at < this.smooth.from)) {
        this.smooth = new Smoother(this.smoothPct);
        this.smooth.from = t[0].at;
        this.smooth.fed = -Infinity;
      }
      const sm = this.smooth;
      let i = t.length;
      while (i > 0 && t[i - 1].at > sm.fed) i -= 1;
      for (; i < t.length; i += 1) sm.feed(t[i].close, t[i].at);
      if (t.length) sm.fed = Math.max(sm.fed, t[t.length - 1].at);
      if (!this.smoothServer && t.length) sm.trim(t[0].at - 60e3);
    }

    // The server's line (`/api/smooth`), or null: none there, so it is run here at `pct`.
    setSmoothSeries(payload, pct) {
      this.smoothServer = !!(payload && payload.state);
      if (this.smoothServer) {
        this.smoothPct = payload.pct;
        this.smooth = Smoother.load(payload);
        this.smoothSync();
      } else if (pct !== this.smoothPct || !this.smooth) {
        this.smoothPct = pct;
        this.smoothSync(true);
      }
      this.draw();
    }

    setSmooth({ tick, smooth }) {
      this.showTick = tick;
      this.showSmooth = smooth;
      this.draw();
    }

    // A new symbol starts with no trades of its own, on the newest bars.
    clear() {
      this.seeded = null;
      this.ticks = [];
      this.smooth = null;
      this.smoothServer = false;
      this.smoothPivots = [];
      this.smoothRunning = null;
      this.lastTrade = null;
      this.candles = this.live ? this.ticks : [];
      this.resetView();
      this.draw();
    }

    trade(price, at) {
      this.ticks.push(tick(price, at));
      if (this.ticks.length > MAX_TICKS) this.ticks.splice(0, this.ticks.length - MAX_TICKS);
      if (this.smooth) { this.smooth.feed(price, at); this.smooth.fed = Math.max(this.smooth.fed, at); }
      else if (this.smoothPct !== null) this.smoothSync();
      this.lastTrade = [price, at];
      if (!this.live) this.bar(price, at);
      if (!this.frame) {
        this.frame = window.requestAnimationFrame(() => { this.frame = null; this.draw(); });
      }
    }

    bar(price, at) {
      const n = this.candles.length;
      extend(this.candles, this.step, price, at);
      this.grew(this.candles.length - n);
    }

    // The prints' pivots and running swing, and the smoothed line's.
    setPivots(pivots, running, smoothPivots = [], smoothRunning = null) {
      const k = (list, run) => `${list.length}:${list.length ? list[list.length - 1].at : 0}:${run ? `${run.at}:${run.price}:${run.trigger}` : 0}`;
      const key = `${k(pivots, running)}|${k(smoothPivots, smoothRunning)}`;
      if (key === this.pivotsKey) return;
      this.pivotsKey = key;
      this.pivots = pivots;
      this.running = running;
      this.smoothPivots = smoothPivots;
      this.smoothRunning = smoothRunning;
      this.draw();
    }

    setLines(lines) {
      const key = JSON.stringify(lines);
      if (key === this.linesKey) return;
      this.linesKey = key;
      this.lines = lines;
      this.draw();
    }

    onMove(event) {
      const g = this.geometry;
      if (!g || (this.drag && this.drag.moved) || this.pinch) return;
      const { x, y } = this.local(event);
      if (!this.drag) this.canvas.style.cursor = x > g.right ? "ns-resize" : "";
      const { candles, at } = g;
      let index = -1;
      if (!candles.length || x < g.left || x > g.right) index = -1;
      else if (this.live) {
        for (let i = 0, best = 12; i < candles.length; i += 1) {   // nearest trade within 12px
          const d = Math.abs(at(i) - x);
          if (d <= best) { best = d; index = i; }
        }
      } else index = Math.round(g.index(x));
      this.hover = index >= 0 && index < candles.length ? index : null;
      this.hoverX = x >= g.left && x <= g.right ? x : null;
      this.hoverY = y;
      this.draw();
      if (this.hover === null) { el.tip.hidden = true; return; }
      const c = candles[this.hover];
      el.tipTime.textContent = istStamp.format(new Date(c.time));
      const sv = this.live && this.showSmooth && this.smooth ? this.smooth.value(c.at) : null;
      let tipBody = this.live
        ? `Trade ${px(c.close, selected)}${sv === null ? "" : `  Smoothed ${px(sv, selected)}`}`
        : `O ${px(c.open, selected)}  H ${px(c.high, selected)}  ` +
          `L ${px(c.low, selected)}  C ${px(c.close, selected)}`;
      el.tipBody.textContent = tipBody;
      el.tip.hidden = false;
      const w = el.tip.offsetWidth, width = this.canvas.clientWidth;
      el.tip.style.left = `${Math.min(Math.max(8, x - w / 2), width - w - 8)}px`;
      el.tip.style.top = "10px";
    }

    draw() {
      const canvas = this.canvas;
      const stage = canvas.parentElement;
      const ratio = window.devicePixelRatio || 1;
      const width = stage.clientWidth, height = stage.clientHeight;
      if (!width || !height) return;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      const ctx = this.ctx;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const narrow = width < 620;
      const pad = { top: 20, right: narrow ? 66 : 78, bottom: 28, left: 14 };
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const right = width - pad.right;
      const now = Date.now();
      const all = this.candles;
      const newest = all[all.length - 1];

      // What is in view. Candles: whole bars from the right edge, plus the one
      // cut by the left edge while the view sits between bars. Live: the trades
      // inside the window, and the one before it so the line starts at the edge.
      let candles, x, index = () => -1, slots = 1, stride = 0, start = 0, cut = false;
      const to = this.liveTo ?? now, from = to - this.liveSpan;
      this.liveFrom = from;
      const tx = (t) => pad.left + (Math.max(t, from) - from) / this.liveSpan * plotW;
      if (!this.live) {
        slots = Math.max(1, Math.min(this.span || Math.max(24, Math.floor(plotW / 6)), all.length));
        this.offset = clamp(this.offset, 0, Math.max(0, all.length - slots));
        const whole = Math.floor(this.offset), part = this.offset - whole;
        const end = all.length - whole;
        start = Math.max(0, end - slots - (part > 0 ? 1 : 0));
        cut = part > 0 && start === end - slots - 1;
        candles = all.slice(start, end);
        stride = plotW / slots;
        const edge = right + part * stride, k = candles.length;
        x = (i) => edge - (k - i - 0.5) * stride;
        index = (px_) => k - 0.5 - (edge - px_) / stride;
      } else {
        let stop = all.length;
        while (stop > 0 && all[stop - 1].at > to) stop -= 1;
        const inside = all.findIndex((c) => c.at >= from);
        candles = inside < 0 || inside >= stop ? all.slice(Math.max(0, stop - 1), stop)
          : all.slice(Math.max(0, inside - 1), stop);
        x = (i) => tx(candles[i].at);
      }
      this.geometry = { left: pad.left, right, plotW, stride, slots, cut, candles, at: x, index };
      if (!candles.length) { if (this.onView) this.onView(null); return; }
      const bodyW = Math.max(1, Math.min(11, stride * 0.62));

      let low = Infinity, high = -Infinity;
      for (const c of candles) { low = Math.min(low, c.low); high = Math.max(high, c.high); }
      const near = this.lines.map((line) => line.price);
      // Lines close to the candles widen the scale so they show; a far-away
      // liquidation price is not allowed to flatten the chart into a line.
      const span0 = (high - low) || Math.max(high * 0.001, 1e-8);
      for (const price of near) {
        if (price >= low - span0 * 0.6 && price <= high + span0 * 0.6) {
          low = Math.min(low, price); high = Math.max(high, price);
        }
      }
      const span = (high - low) || Math.max(high * 0.001, 1e-8);
      low -= span * 0.08; high += span * 0.08;
      if (this.yZoom !== 1) {
        const mid = (low + high) / 2, half = (high - low) / 2 * this.yZoom;
        low = mid - half; high = mid + half;
      }
      const y = (p) => pad.top + (high - p) / (high - low) * plotH;

      // The last price is the newest trade (Live) or the forming candle's close,
      // wherever the view is. Its badge sits in the scale's gutter, so a scale
      // label landing under it is skipped; panned off the scale, neither shows.
      const following = this.live ? this.liveTo === null : this.offset === 0;
      const lastBar = candles[candles.length - 1];
      const lastPrice = newest.close;
      const endColour = this.live ? (lastBar.close >= candles[0].close ? PALETTE.up : PALETTE.down) : null;
      const rising = this.live ? lastPrice >= candles[0].close : newest.close >= newest.open;
      const lastShown = lastPrice >= low && lastPrice <= high;
      const lastY = lastShown ? y(lastPrice) : -1e9;

      ctx.font = `${narrow ? 9.5 : 10.5}px ${PALETTE.mono}`;
      ctx.textBaseline = "middle";
      ctx.lineWidth = 1;
      const rows = plotH < 340 ? 4 : 6;
      for (let step = 0; step <= rows; step += 1) {
        const value = high - (high - low) * (step / rows);
        const gy = Math.round(y(value)) + 0.5;
        ctx.strokeStyle = PALETTE.grid;
        ctx.beginPath(); ctx.moveTo(pad.left, gy); ctx.lineTo(right, gy); ctx.stroke();
        if (Math.abs(gy - lastY) < 13) continue;
        ctx.fillStyle = PALETTE.axis;
        ctx.textAlign = "right";
        ctx.fillText(px(value, selected), width - 12, gy);
      }

      // The plot is clipped to its box: a bar half off either edge is cut there.
      ctx.save();
      ctx.beginPath(); ctx.rect(pad.left, 0, plotW, height); ctx.clip();
      ctx.fillStyle = PALETTE.axis;
      ctx.textAlign = "center";
      if (this.live) {
        const format = this.liveSpan > 3600e3 ? istShort : istTime;
        for (let k = 1; k < 6; k += 1) {
          const t = from + this.liveSpan * k / 6, gx = Math.round(tx(t)) + 0.5;
          ctx.strokeStyle = PALETTE.gridSoft;
          ctx.beginPath(); ctx.moveTo(gx, pad.top); ctx.lineTo(gx, height - pad.bottom); ctx.stroke();
          ctx.fillText(format.format(new Date(t)), tx(t), height - 12);
        }
      } else {
        // labelled by the bar's place in the whole series, so labels move with the bars as they pan
        const every = Math.max(1, Math.floor(slots / 6));
        candles.forEach((c, i) => {
          if ((start + i) % every) return;
          const label = istShort.format(new Date(c.time));
          const half = ctx.measureText(label).width / 2 + 2;
          const cx = x(i);
          if (cx - half < pad.left || cx + half > right) return;
          ctx.strokeStyle = PALETTE.gridSoft;
          ctx.beginPath(); ctx.moveTo(Math.round(cx) + 0.5, pad.top); ctx.lineTo(Math.round(cx) + 0.5, height - pad.bottom); ctx.stroke();
          ctx.fillText(label, cx, height - 12);
        });
      }

      const trace = (points, at, value) => {
        ctx.beginPath();
        points.forEach((c, i) => (i ? ctx.lineTo(at(c), y(value(c))) : ctx.moveTo(at(c), y(value(c)))));
        ctx.stroke();
      };
      if (this.live) {
        // no trade since the last one in view: the price held until the view's end (or now)
        const trades = [...candles, { at: Math.min(now, to), close: lastBar.close }];
        ctx.lineJoin = "round";
        if (this.showTick) {
          ctx.strokeStyle = endColour;
          ctx.lineWidth = 1.5;
          trace(trades, (c) => tx(c.at), (c) => c.close);
          if (following) {
            ctx.fillStyle = endColour;
            ctx.beginPath(); ctx.arc(tx(now), y(lastBar.close), 3, 0, Math.PI * 2); ctx.fill();
          }
        }
        // The smoothed line, over the trades and under the pivots: the seconds in view (one per pixel at most),
        // then the seconds since the last print, on its price, up to now.
        const sm = this.showSmooth ? this.smooth : null;
        if (sm && sm.at.length) {
          let i = 0, hi = sm.at.length - 1;
          while (i < hi) { const m = (i + hi) >> 1; if (sm.at[m] < from) i = m + 1; else hi = m; }
          i = Math.max(0, i - 1);
          let end = i, top = sm.at.length;
          while (end < top) { const m = (end + top) >> 1; if (sm.at[m] <= to) end = m + 1; else top = m; }
          const pts = [], every = Math.max(1, Math.floor((end - i) / plotW));
          for (; i < end; i += every) pts.push([sm.at[i], sm.v[i]]);
          const lastAt = sm.at[sm.at.length - 1];
          if (pts.length && pts[pts.length - 1][0] < lastAt && lastAt <= to) pts.push([lastAt, sm.v[sm.v.length - 1]]);
          for (const p of sm.tail(Math.min(now, to))) if (p[0] > lastAt) pts.push(p);
          if (pts.length > 1) {
            ctx.strokeStyle = PALETTE.smooth;
            ctx.lineWidth = 2.25;
            trace(pts, (p) => tx(p[0]), (p) => p[1]);
            this.smoothEnd = following ? pts[pts.length - 1][1] : null;
            if (following && !this.showTick) {
              ctx.fillStyle = PALETTE.smooth;
              ctx.beginPath(); ctx.arc(tx(pts[pts.length - 1][0]), y(this.smoothEnd), 3, 0, Math.PI * 2); ctx.fill();
            }
          } else this.smoothEnd = null;
        } else this.smoothEnd = null;
        ctx.lineWidth = 1;
      } else candles.forEach((c, i) => {
        const up = c.close >= c.open;
        const cx = x(i);
        ctx.strokeStyle = up ? PALETTE.up : PALETTE.down;
        ctx.beginPath();
        ctx.moveTo(Math.round(cx) + 0.5, y(c.high));
        ctx.lineTo(Math.round(cx) + 0.5, y(c.low));
        ctx.stroke();
        const top = y(Math.max(c.open, c.close)), bottom = y(Math.min(c.open, c.close));
        ctx.fillStyle = up ? PALETTE.upFill : PALETTE.downFill;
        ctx.fillRect(cx - bodyW / 2, top, bodyW, Math.max(1.5, bottom - top));
      });

      // HH/LL pivots: the zigzag through them, each marked where its extreme printed; the swing in
      // progress runs from the last pivot to the running extreme (dashed: not a pivot yet). Live has two chains:
      // the prints' (the strategy's, shown with TICK) and the smoothed line's own (shown with SMOOTHED: blue
      // zigzag, blue tags, dashes on the line; never traded). Candles show the prints'.
      const chains = [];
      if (!this.live || this.showTick) chains.push({ piv: this.pivots || [], running: this.running, smooth: false });
      if (this.live && this.showSmooth) {
        chains.push({ piv: this.smoothPivots || [], running: this.smoothRunning, smooth: true });
      }
      let at;
      if (this.live) at = (t) => (t >= from && t <= to ? tx(t) : null);
      else {
        const times = candles.map((c) => c.at ?? Date.parse(c.time));
        const last = times[times.length - 1] + (this.step || (times.length > 1 ? times[1] - times[0] : 60e3));
        at = (t) => {
          if (!(t >= times[0] && t < last)) return null;
          let lo = 0, hi = times.length - 1;
          while (lo < hi) { const m = (lo + hi + 1) >> 1; if (times[m] <= t) lo = m; else hi = m - 1; }
          return x(lo);
        };
      }
      const placed = [];            // tags of both chains: none overprints another
      for (const { piv, running, smooth } of chains) {
        const shown = [];
        for (const p of piv) {
          const px_ = p.at == null ? null : at(p.at);
          if (px_ !== null && p.price >= low && p.price <= high) shown.push({ ...p, x: px_, y: y(p.price) });
        }
        if (!shown.length) continue;
        const run = running && running.at != null ? at(running.at) : null;
        ctx.lineWidth = 1;
        ctx.strokeStyle = smooth ? PALETTE.smoothPivot : PALETTE.pivotLine;
        ctx.beginPath();
        shown.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.stroke();
        if (run !== null) {
          const tail = shown[shown.length - 1];
          ctx.setLineDash([3, 4]);
          ctx.beginPath(); ctx.moveTo(tail.x, tail.y); ctx.lineTo(run, y(running.price)); ctx.stroke();
          // The confirmation level, a chandelier stop: R back from the running extreme. It moves only with the
          // extreme; the first print (smoothed: second of the line) to reach it confirms the pivot (the white
          // dash) and flips the side.
          const trig = running.trigger;
          if (trig != null && trig >= low && trig <= high) {
            const ty = Math.round(y(trig)) + 0.5, ink = smooth ? PALETTE.smooth : "rgba(255,255,255,.7)";
            ctx.strokeStyle = ink;
            ctx.beginPath(); ctx.moveTo(run, ty); ctx.lineTo(right, ty); ctx.stroke();
            ctx.fillStyle = ink;
            ctx.font = `700 9px ${PALETTE.mono}`;
            ctx.textAlign = "right";
            const kind = trig < running.price ? "HH" : "LL";
            ctx.fillText(`${smooth ? "SMOOTHED " : ""}${kind} at ${px(trig, selected)}`, right - 4, smooth ? ty + 8 : ty - 6);
          }
          ctx.setLineDash([]);
        }
        // Where each pivot was confirmed: a short white dash on the print (smoothed: on the line) that confirmed
        // it; the smoothed line's dashes sit in a blue halo.
        for (const [stroke, w] of smooth ? [[PALETTE.smooth, 4], ["#fff", 2]] : [["#fff", 2]]) {
          ctx.strokeStyle = stroke;
          ctx.lineWidth = w;
          ctx.beginPath();
          for (const p of shown) {
            const cx = p.confirmed_at == null ? null : at(p.confirmed_at);
            if (cx === null || p.confirmed_price == null || p.confirmed_price < low || p.confirmed_price > high) continue;
            const cy = y(p.confirmed_price);
            ctx.moveTo(cx - 5, cy); ctx.lineTo(cx + 5, cy);
          }
          ctx.stroke();
        }
        ctx.lineWidth = 1;
        // Every pivot gets its marker, a triangle pointing at the extreme. Tags go on newest first, wherever
        // they fit without overprinting another, so the latest pivots are always readable.
        ctx.font = `700 9px ${PALETTE.mono}`;
        ctx.textAlign = "center";
        const tagW = ctx.measureText("HH").width + 8, tagH = 13;
        for (let i = shown.length - 1; i >= 0; i -= 1) {
          const p = shown[i], hh = p.type === "HH", colour = hh ? PALETTE.down : PALETTE.up, dir = hh ? -1 : 1;
          ctx.fillStyle = colour;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y + dir * 2);
          ctx.lineTo(p.x - 4, p.y + dir * 8);
          ctx.lineTo(p.x + 4, p.y + dir * 8);
          ctx.closePath();
          ctx.fill();
          const bx = p.x - tagW / 2, by = hh ? p.y - 11 - tagH : p.y + 11;
          if (placed.some((q) => bx < q.x + tagW + 2 && q.x < bx + tagW + 2 && by < q.y + tagH + 1 && q.y < by + tagH + 1)) continue;
          placed.push({ x: bx, y: by });
          if (smooth) ctx.fillStyle = PALETTE.smooth;
          ctx.fillRect(bx, by, tagW, tagH);
          ctx.fillStyle = PALETTE.badgeInk;
          ctx.fillText(p.type, p.x, by + tagH / 2 + 0.5);
        }
      }

      ctx.restore();

      const colours = { entry: PALETTE.iris, liq: PALETTE.amber, sl: PALETTE.down, tp: PALETTE.up };
      for (const line of this.lines) {
        if (line.price < low || line.price > high) continue;
        const ly = Math.round(y(line.price)) + 0.5;
        ctx.strokeStyle = colours[line.kind];
        ctx.setLineDash(line.kind === "entry" ? [5, 4] : [2, 3]);
        ctx.beginPath(); ctx.moveTo(pad.left, ly); ctx.lineTo(right, ly); ctx.stroke();
        ctx.setLineDash([]);
        if (Math.abs(ly - lastY) < 12) continue;
        ctx.fillStyle = colours[line.kind];
        ctx.textAlign = "right";
        ctx.fillText(px(line.price, selected), width - 12, ly);
      }

      // The last price: a dashed line across the plot and its badge on the scale.
      const badge = (by, text, fill, ink, glow) => {
        const bw = pad.right - 14, bh = 20, bx = right + 8;
        ctx.save();
        ctx.beginPath();
        ctx.roundRect(bx, by - bh / 2, bw, bh, 6);
        ctx.fillStyle = fill;
        if (glow) { ctx.shadowColor = glow; ctx.shadowBlur = 12; }
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.fillStyle = ink;
        ctx.font = `700 ${narrow ? 9.5 : 10}px ${PALETTE.mono}`;
        ctx.textAlign = "center";
        ctx.fillText(text, bx + bw / 2, by + 0.5);
        ctx.restore();
      };
      if (lastShown) {
        ctx.save();
        ctx.setLineDash([3, 5]);
        ctx.strokeStyle = rising ? PALETTE.upLine : PALETTE.downLine;
        ctx.beginPath(); ctx.moveTo(pad.left, Math.round(lastY) + 0.5); ctx.lineTo(right, Math.round(lastY) + 0.5); ctx.stroke();
        ctx.restore();
        badge(lastY, px(lastPrice, selected), rising ? PALETTE.up : PALETTE.down, PALETTE.badgeInk,
          rising ? PALETTE.upLine : PALETTE.downLine);
      }
      // The smoothed line's value now, on the scale, unless it would sit on the last price's badge.
      const se = this.live && this.showSmooth ? this.smoothEnd : null;
      if (se !== null && se !== undefined && se >= low && se <= high && Math.abs(y(se) - lastY) >= 21) {
        badge(y(se), px(se, selected), PALETTE.smooth, PALETTE.badgeInk);
      }

      // The crosshair: through the bar (or trade) under the pointer, else the
      // pointer itself; the price and the time it points at labelled on the scales.
      const hovered = this.hover !== null && this.hover < candles.length;
      const inPlot = this.hoverY !== null && this.hoverY >= pad.top && this.hoverY <= height - pad.bottom;
      const cx = hovered ? Math.round(x(this.hover)) + 0.5 : this.hoverX !== null ? Math.round(this.hoverX) + 0.5 : null;
      if (cx !== null && (hovered || inPlot)) {
        ctx.save();
        ctx.strokeStyle = PALETTE.crosshair;
        ctx.setLineDash([3, 5]);
        ctx.beginPath(); ctx.moveTo(cx, pad.top); ctx.lineTo(cx, height - pad.bottom);
        if (inPlot) {
          ctx.moveTo(pad.left, Math.round(this.hoverY) + 0.5); ctx.lineTo(right, Math.round(this.hoverY) + 0.5);
        }
        ctx.stroke();
        ctx.restore();
        if (hovered) {
          ctx.fillStyle = PALETTE.crossDot;
          ctx.beginPath(); ctx.arc(cx, y(candles[this.hover].close), 3, 0, Math.PI * 2); ctx.fill();
        }
        if (inPlot) badge(this.hoverY, px(high - (this.hoverY - pad.top) / plotH * (high - low), selected),
          PALETTE.crossLabel, PALETTE.crossInk);
        const t = hovered ? new Date(candles[this.hover].time)
          : new Date(this.live ? from + (cx - pad.left) / plotW * this.liveSpan : NaN);
        if (!Number.isNaN(t.getTime())) {
          const label = (this.live ? istStamp : istWhen).format(t);
          ctx.font = `${narrow ? 9.5 : 10.5}px ${PALETTE.mono}`;
          const lw = ctx.measureText(label).width + 14, lh = 18;
          const lx = clamp(cx - lw / 2, 0, width - lw), ly = height - 12 - lh / 2;
          ctx.fillStyle = PALETTE.crossLabel;
          ctx.beginPath(); ctx.roundRect(lx, ly, lw, lh, 5); ctx.fill();
          ctx.fillStyle = PALETTE.crossInk;
          ctx.textAlign = "center";
          ctx.fillText(label, lx + lw / 2, ly + lh / 2 + 0.5);
        }
      }

      if (this.onView) {
        // the range and the move are of what is in view; the readout's LATEST is the newest bar anywhere
        const end = following ? lastPrice : lastBar.close;
        let lo = Infinity, hi = -Infinity;
        for (const c of candles) { lo = Math.min(lo, c.low); hi = Math.max(hi, c.high); }
        this.onView({ live: this.live, count: candles.length, open: candles[0].open, last: end,
          lo: Math.min(lo, end), hi: Math.max(hi, end),
          read: hovered ? candles[this.hover] : newest, hovered });
      }
    }
  }

  const chart = new CandleChart($("chart"));

  /* ---------- the quote deck: price, range, readout ---------------------- */
  // The big figure: whole part, then its decimals set smaller.
  function setPrice(value) {
    const text = px(value, selected);
    const cut = text.lastIndexOf(".");
    el.priceMain.textContent = cut < 0 ? text : text.slice(0, cut);
    el.pricePaise.textContent = cut < 0 ? "" : text.slice(cut);
  }

  const signedPx = (n) => `${n > 0 ? "+" : n < 0 ? "-" : ""}${px(Math.abs(n), selected)}`;
  function toneUpDown(node, value) {
    node.classList.remove("up", "down", "flat");
    node.classList.add(value > 0 ? "up" : value < 0 ? "down" : "flat");
  }

  // After every draw: the range of what is on the chart (the pin clamped to
  // its track), the move since the first bar in view, and the readout - the
  // bar under the pointer, or the newest one.
  chart.onView = (view) => {
    if (!view) {
      for (const node of [el.readO, el.readH, el.readL, el.readC]) node.textContent = "—";
      el.readC.classList.remove("up", "down");
      el.readKind.textContent = "LATEST"; el.readAt.textContent = "—";
      el.barCount.textContent = chart.live ? "0 trades" : "0 bars";
      return;
    }
    const { lo, hi, open, last, read, hovered } = view;
    const diff = last - open;
    el.priceChange.textContent = `${signedPx(diff)} · ${diff > 0 ? "+" : ""}${(open ? diff / open * 100 : 0).toFixed(2)}% on the chart`;
    toneUpDown(el.priceChange, diff);

    const values = [read.open, read.high, read.low, read.close];
    [el.readO, el.readH, el.readL, el.readC].forEach((node, i) => { node.textContent = px(values[i], selected); });
    el.readC.classList.remove("up", "down");
    if (!view.live) el.readC.classList.add(read.close >= read.open ? "up" : "down");
    el.readKind.textContent = hovered ? (view.live ? "SELECTED TRADE" : "SELECTED BAR") : "LATEST";
    el.readAt.textContent = istStamp.format(new Date(read.time));
    el.barCount.textContent = `${view.count.toLocaleString("en-US")} ${view.live ? (view.count === 1 ? "trade" : "trades") : (view.count === 1 ? "bar" : "bars")}`;
  };

  // The symbol is set at the size its column was drawn for, scaled down to
  // fit a longer name, never up.
  function fitSymbol() {
    const h = el.quoteSymbol;
    h.style.fontSize = "";
    const text = document.createRange();
    text.selectNodeContents(h);
    const room = h.clientWidth, need = text.getBoundingClientRect().width;
    if (room > 0 && need > room) {
      h.style.fontSize = `${Math.floor(parseFloat(getComputedStyle(h).fontSize) * (room / need) * 0.98 * 10) / 10}px`;
    }
  }
  window.addEventListener("resize", fitSymbol);
  if (document.fonts) document.fonts.ready.then(fitSymbol);
  const UNIT_MS = { m: 60e3, h: 3600e3, d: 86400e3, w: 604800e3 };
  const stepMs = (r) => parseInt(r, 10) * UNIT_MS[r.slice(-1)];

  const tickView = () => resolution === "live";

  // The smoothed line (v1): the server's, over the Live window, again with each reload of the trades and whenever
  // its threshold changes; with no server line it is run here over the trades.
  const SMOOTH_UI = !!el.liveWindows, SMOOTH_DEFAULT = 0.3;
  const smoothFeed = { busy: false, again: false };
  async function loadSmooth() {
    if (!SMOOTH_UI || !selected || !tickView()) return;
    if (smoothFeed.busy) { smoothFeed.again = true; return; }
    smoothFeed.busy = true;
    const asked = `${selected}/${liveWindow}`;
    try {
      const r = await api(`/api/smooth/${selected}?window=${liveWindow}`);
      if (asked === `${selected}/${liveWindow}` && tickView()) chart.setSmoothSeries(r, num(r.pct) ?? SMOOTH_DEFAULT);
      else smoothFeed.again = true;
    } catch (error) {
      if (!chart.smooth) chart.setSmoothSeries(null, chart.smoothPct ?? SMOOTH_DEFAULT);
    } finally {
      smoothFeed.busy = false;
      if (smoothFeed.again) { smoothFeed.again = false; loadSmooth(); }
    }
  }
  // Each status: the box follows the server's threshold (unless being typed in), and a new one reloads the line.
  let smoothFollow = () => {};

  async function loadChart() {
    if (!selected) return;
    if (tickView()) {
      chart.showTicks();
      // The window's history from the database, again each minute in case the stream missed any.
      const asked = `${selected}/${liveWindow}`;
      if (chart.seeded !== asked || Date.now() - chart.seededAt > 60e3) {
        try {
          const payload = await api(`/api/ticks/${selected}?window=${liveWindow}`);
          if (asked !== `${selected}/${liveWindow}` || !tickView()) return;
          chart.seeded = asked;
          chart.seededAt = Date.now();
          chart.seedTicks(payload.ticks);
        } catch (error) { /* the stream still fills it */ }
        loadSmooth();
      }
      el.veil.classList.toggle("is-hidden", chart.ticks.length > 0);
      if (!chart.ticks.length) el.veil.querySelector("strong").textContent = "Waiting for trades";
      return;
    }
    const asked = `${selected}/${resolution}`;
    try {
      const payload = await api(`/api/candles/${selected}?resolution=${resolution}`);
      if (asked !== `${selected}/${resolution}`) return;
      chart.setCandles(payload.candles, stepMs(resolution));
      el.veil.classList.toggle("is-hidden", payload.candles.length > 0);
      if (!payload.candles.length) el.veil.querySelector("strong").textContent = "No candles";
    } catch (error) {
      if (asked !== `${selected}/${resolution}`) return;
      el.veil.classList.remove("is-hidden");
      el.veil.querySelector("strong").textContent = "Chart unavailable";
    }
  }

  /* ---------- the selected instrument ------------------------------------ */
  function renderSpec() {
    const s = specs[selected];
    el.ticketSymbol.textContent = selected || "—";
    if (!s) { el.specLine.textContent = "Perpetual future"; return; }
    const unit = selected.replace(/USD$/, "");
    const hours = Math.round(s.funding_interval / 3600);
    el.specLine.textContent =
      `1 contract = ${Number(s.contract_value)} ${unit} · tick ${s.tick_size} · up to ${s.max_leverage}x · ` +
      `maker ${(s.maker_fee * 100).toFixed(3)}% / taker ${(s.taker_fee * 100).toFixed(3)}% + ` +
      `${Math.round(s.gst_rate * 100)}% GST · funding every ${hours}h`;
    const max = Math.max(1, Math.floor(s.max_leverage));
    el.leverageInput.max = String(max);
    for (const b of el.leveragePresets.querySelectorAll("[data-lev]")) b.hidden = Number(b.dataset.lev) > max;
    const lev = num(s.leverage);
    if (lev !== null && !el.leverageInput.matches(":focus")) {
      el.leverageInput.value = String(lev);
      markLeverage();
    }
    el.leverageValue.textContent = lev === null ? "—" : `${lev}x`;
    el.ticketLeverageNow.textContent = lev === null ? "" : `${lev}x set`;
  }

  function renderBoard() {
    if (!snapshot || !selected) return;
    const q = (snapshot.quotes || {})[selected];
    if (!q) {
      setPrice(null);
      el.boardMeta.textContent = "no quote yet";
    } else {
      // The big number is the last trade - it moves on every one. The mark
      // (what stops and liquidation use) Delta publishes only every ~2 s.
      setPrice(q.last ?? q.mark);
      const funding = num(q.funding_rate);
      el.boardMeta.textContent =
        `last traded · mark ${px(q.mark, selected)} · bid ${px(q.bid, selected)} · ask ${px(q.ask, selected)} · funding ` +
        `${funding === null ? "—" : pct(funding)}` + (q.stale ? " · STALE" : "");
    }
    const position = (snapshot.positions || []).find((p) => p.symbol === selected);
    el.symbolPicker.dataset.side = position ? position.direction.toLowerCase() : "flat";
    renderHold(position);
    const lines = [];
    if (position) {
      lines.push({ kind: "entry", price: position.entry_price });
      if (num(position.liquidation_price)) lines.push({ kind: "liq", price: position.liquidation_price });
      if (num(position.stop_loss)) lines.push({ kind: "sl", price: position.stop_loss });
      if (num(position.take_profit)) lines.push({ kind: "tp", price: position.take_profit });
    }
    chart.setLines(lines);
    updateSizeHint();
  }

  // The selected symbol's open P&L above the buy / sell / exit buttons: points (the last trade
  // against entry, on the position's side) and USD. It moves on every trade, not just on Delta's ~2 s mark.
  function renderHold(p) {
    el.exitButton.disabled = !p;
    const q = p && snapshot ? (snapshot.quotes || {})[p.symbol] : null;
    const price = num(q && q.last) ?? num(p && p.mark);
    const cv = num((specs[p && p.symbol] || {}).contract_value);
    const points = p && price !== null ? (price - p.entry_price) * (p.direction === "LONG" ? 1 : -1) : null;
    const upnl = p && price !== null && cv !== null ? (price - p.entry_price) * p.size * cv : p ? p.unrealised : null;
    el.pnlPoints.textContent = points === null ? "—" : signedPx(points);
    el.pnlUsd.textContent = p ? usd(upnl, true, 4) : "—";
    tone(el.pnlPoints, points);
    tone(el.pnlUsd, upnl);
  }

  /* ---------- tables ----------------------------------------------------- */
  function renderPositions(positions) {
    el.countPositions.textContent = positions.length;
    if (!positions.length) {
      setRows(el.positions, '<tr><td colspan="9" class="empty">No open positions.</td></tr>');
      return;
    }
    setRows(el.positions, positions.map((p) => {
      const s = p.symbol;
      const fees = num(p.fees_paid);
      return `<tr>
        <td data-label="Symbol"><button type="button" class="link" data-pick="${esc(s)}">${esc(s)}</button>
          <span class="side-chip" data-side="${p.direction.toLowerCase()}">${p.direction}</span></td>
        <td data-label="Contracts" class="tabular" title="notional ${usd(p.notional)}">${p.contracts}</td>
        <td data-label="Entry" class="tabular">${px(p.entry_price, s)}</td>
        <td data-label="Mark" class="tabular">${px(p.mark, s)}</td>
        <td data-label="Margin · lev." class="tabular">${usd(p.margin, false, 4)}<small> ${p.leverage == null ? "" : `${p.leverage}x`}</small></td>
        <td data-label="Liq. price" class="tabular liq">${px(p.liquidation_price, s)}</td>
        <td data-label="Unrealised" class="tabular ${cls(p.unrealised)}">${usd(p.unrealised, true, 4)}` +
        `${num(p.roe_pct) === null ? "" : `<small> ${Number(p.roe_pct).toFixed(2)}%</small>`}</td>
        <td data-label="Funding · fees" class="tabular"><span class="${cls(p.realized_funding)}">${usd(p.realized_funding, true, 4)}</span>` +
        `${fees === null ? "" : ` · <span class="negative">${usd(-fees, true, 4)}</span>`}</td>
        <td class="cell-action"><span class="actions"><button type="button" class="ghost tabular" data-bracket="${esc(s)}"
             data-sl="${p.stop_loss ?? ""}" data-tp="${p.take_profit ?? ""}" title="Edit stop-loss / take-profit">` +
        `${p.stop_loss == null && p.take_profit == null ? "SL / TP" : `${px(p.stop_loss, s)} / ${px(p.take_profit, s)}`}</button>` +
        `<button type="button" class="ghost tabular" data-target="${esc(s)}" title="Rest a take-profit limit that nets this much after every fee">+$ net</button>` +
        `<button type="button" class="ghost danger" data-close="${esc(s)}">Close</button></span></td>
      </tr>`;
    }).join(""));
  }

  function orderType(o) {
    if (o.stop_order_type === "stop_loss_order") return "Stop-loss";
    if (o.stop_order_type === "take_profit_order") return "Take-profit";
    return o.order_type === "limit_order" ? "Limit" : "Market";
  }

  function pegRow(g) {
    const flags = ["post-only", g.requotes ? `re-priced ${g.requotes}×` : "",
      g.rr && !g.take_profit ? `TP +$${g.rr[0]}${g.rr[1] ? ` / SL −$${g.rr[1]}` : ""} on fill` : "",
      g.stop_loss ? `SL ${px(g.stop_loss, g.symbol)}` : "", g.take_profit ? `TP ${px(g.take_profit, g.symbol)}` : "",
      g.note || ""].filter(Boolean).join(" · ");
    return `<tr>
        <td data-label="Placed" class="tabular">${when(g.created_at)}</td>
        <td data-label="Symbol">${esc(g.symbol)}</td>
        <td data-label="Type">Auto ${g.kind}</td>
        <td data-label="Side" class="${g.side === "buy" ? "positive" : "negative"}">${esc(String(g.side).toUpperCase())}</td>
        <td data-label="Size" class="tabular">${g.filled} / ${g.size}</td>
        <td data-label="Price / trigger" class="tabular">${g.price == null ? "pricing…" : px(g.price, g.symbol)}</td>
        <td data-label="Flags" class="note">${esc(flags)}</td>
        <td data-label="Reserved" class="tabular">—</td>
        <td class="cell-action"><button type="button" class="ghost danger" data-cancel-peg="${g.peg_id}">Cancel</button></td>
      </tr>`;
  }

  function renderOpenOrders(orders, pegs) {
    const working = pegs.filter((g) => g.state === "working");
    const owned = new Set(working.map((g) => g.order_id).filter(Boolean));
    orders = orders.filter((o) => !owned.has(o.order_id));
    el.countOrders.textContent = working.length + orders.length;
    if (!working.length && !orders.length) {
      setRows(el.openOrders, '<tr><td colspan="9" class="empty">No open orders.</td></tr>');
      return;
    }
    setRows(el.openOrders, working.map(pegRow).join("") + orders.map((o) => {
      const flags = [o.reduce_only ? "reduce-only" : "", o.post_only ? "post-only" : "",
        o.bracket_stop_loss ? `SL ${px(o.bracket_stop_loss, o.symbol)}` : "",
        o.bracket_take_profit ? `TP ${px(o.bracket_take_profit, o.symbol)}` : ""].filter(Boolean).join(" · ");
      const size = o.stop_order_type ? "whole position" : o.size;
      return `<tr>
        <td data-label="Placed" class="tabular">${when(o.created_at)}</td>
        <td data-label="Symbol">${esc(o.symbol)}</td>
        <td data-label="Type">${orderType(o)}</td>
        <td data-label="Side" class="${o.side === "buy" ? "positive" : "negative"}">${esc(String(o.side).toUpperCase())}</td>
        <td data-label="Size" class="tabular">${size}</td>
        <td data-label="Price / trigger" class="tabular">${px(o.stop_price ?? o.limit_price, o.symbol)}</td>
        <td data-label="Flags">${flags || "—"}</td>
        <td data-label="Reserved" class="tabular">${o.reserved_margin == null ? "—" : usd(o.reserved_margin, false, 4)}</td>
        <td class="cell-action"><button type="button" class="ghost danger" data-cancel="${esc(o.order_id)}">Cancel</button></td>
      </tr>`;
    }).join(""));
  }

  const HISTORY = {
    orders: (r) => `<tr>
      <td data-label="Placed" class="tabular">${when(r.created_at)}</td>
      <td data-label="Symbol">${esc(r.symbol)}</td>
      <td data-label="Type">${orderType(r)}${r.reduce_only ? " · RO" : ""}</td>
      <td data-label="Side" class="${r.side === "buy" ? "positive" : "negative"}">${esc(String(r.side).toUpperCase())}</td>
      <td data-label="Size" class="tabular">${r.size}</td>
      <td data-label="Limit / trigger" class="tabular">${px(r.stop_price ?? r.limit_price, r.symbol)}</td>
      <td data-label="Fill price" class="tabular">${px(r.avg_fill_price, r.symbol)}</td>
      <td data-label="Fee" class="tabular">${r.paid_commission == null ? "—" : usd(r.paid_commission, false, 4)}</td>
      <td data-label="State"><span class="state-chip" data-state="${esc(r.state)}">${esc(r.state)}</span></td>
      <td data-label="Note" class="note">${esc(r.note || "")}</td></tr>`,
    fills: (r) => `<tr>
      <td data-label="Time" class="tabular">${when(r.created_at)}</td>
      <td data-label="Symbol">${esc(r.symbol)}</td>
      <td data-label="Side" class="${r.side === "buy" ? "positive" : "negative"}">${esc(String(r.side).toUpperCase())}</td>
      <td data-label="Size" class="tabular">${r.size}</td>
      <td data-label="Price" class="tabular">${px(r.price, r.symbol)}</td>
      <td data-label="Notional" class="tabular">${usd(r.notional)}</td>
      <td data-label="Role">${esc(r.role || "—")}${num(r.fee_rate) ? ` ${(r.fee_rate * 100).toFixed(3)}%` : ""}</td>
      <td data-label="Fee" class="tabular">${r.fee == null ? "—" : usd(r.fee, false, 6)}</td>
      <td data-label="GST" class="tabular">${r.gst == null ? "—" : usd(r.gst, false, 6)}</td>
      <td data-label="Charged" class="tabular negative">${r.commission == null ? "—" : usd(r.commission, false, 6)}</td>
      <td data-label="Realised" class="tabular ${cls(r.realized_pnl)}">${r.realized_pnl == null ? "—" : usd(r.realized_pnl, true, 4)}</td></tr>`,
    trades: (r) => `<tr>
      <td data-label="Closed" class="tabular">${when(r.closed_at)}</td>
      <td data-label="Symbol">${esc(r.symbol)}</td>
      <td data-label="Side"><span class="side-chip" data-side="${esc(String(r.direction).toLowerCase())}">${esc(r.direction)}</span></td>
      <td data-label="Contracts" class="tabular">${r.contracts}</td>
      <td data-label="Entry" class="tabular">${px(r.entry_price, r.symbol)}</td>
      <td data-label="Exit" class="tabular">${px(r.exit_price, r.symbol)}</td>
      <td data-label="Leverage" class="tabular">${r.leverage == null ? "—" : `${r.leverage}x`}</td>
      <td data-label="Gross" class="tabular ${cls(r.gross_pnl)}">${usd(r.gross_pnl, true, 4)}</td>
      <td data-label="Fees" class="tabular negative">${usd(-r.fees, true, 4)}</td>
      <td data-label="Funding" class="tabular ${cls(r.funding)}">${usd(r.funding, true, 4)}</td>
      <td data-label="Net" class="tabular ${cls(r.net_pnl)}"><strong>${usd(r.net_pnl, true, 4)}</strong></td>
      <td data-label="Reason">${esc(r.reason || "")}</td></tr>`,
    ledger: (r) => `<tr>
      <td data-label="Time" class="tabular">${when(r.created_at)}</td>
      <td data-label="Kind">${esc(r.kind)}</td>
      <td data-label="Symbol">${esc(r.symbol || "—")}</td>
      <td data-label="Amount" class="tabular ${cls(r.amount)}">${usd(r.amount, true, 6)}</td>
      <td data-label="Balance" class="tabular">${usd(r.balance, false, 4)}</td>
      <td data-label="Detail" class="note">${esc(r.note || "")}</td></tr>`,
  };
  const HISTORY_COLS = { orders: 10, fills: 11, trades: 12, ledger: 6 };

  async function pollHistory() {
    if (!HISTORY[view]) return;
    const table = view;
    const node = $(`history-${table}`);
    try {
      const data = await api(`/api/history/${table}?limit=200`);
      if (table !== view) return;
      node.innerHTML = data.rows.length ? data.rows.map(HISTORY[table]).join("")
        : `<tr><td colspan="${HISTORY_COLS[table]}" class="empty">Nothing yet.</td></tr>`;
    } catch (error) {
      node.innerHTML = `<tr><td colspan="${HISTORY_COLS[table]}" class="empty">Unavailable: ${esc(error.message)}</td></tr>`;
    }
  }

  function showView(next) {
    view = next;
    for (const seg of document.querySelectorAll("#viewTabs .seg")) {
      seg.setAttribute("aria-selected", String(seg.dataset.view === next));
    }
    for (const node of document.querySelectorAll(".view")) node.hidden = node.id !== `view-${next}`;
    pollHistory();
  }

  /* ---------- account and system ----------------------------------------- */
  function renderAccount(data) {
    const a = data.account || {};
    el.accountMode.textContent = a.mode === "LIVE" ? "LIVE" : "paper";
    el.equity.textContent = usd(a.equity, false, 4);
    el.balance.textContent = usd(a.balance, false, 4);
    el.available.textContent = usd(a.available, false, 4);
    el.unrealised.textContent = usd(a.unrealised, true, 4);
    tone(el.unrealised, a.unrealised);
    el.positionMargin.textContent = usd(a.position_margin, false, 4);
    el.orderMargin.textContent = usd(a.order_margin, false, 4);
    const w = (data.pnl || {})[period];
    el.periodPnl.textContent = w ? usd(w.pnl, true, 4) : "—";
    el.periodFees.textContent = w ? usd(-w.fees, true, 4) : "—";
    el.periodFunding.textContent = w ? usd(w.funding, true, 4) : "—";
    el.periodNet.textContent = w ? usd(w.net, true, 4) : (a.mode === "LIVE" ? "see Delta" : "—");
    for (const [node, value] of [[el.periodPnl, w && w.pnl], [el.periodFees, w && -w.fees],
      [el.periodFunding, w && w.funding], [el.periodNet, w && w.net]]) tone(node, value);
    el.resetPaper.hidden = a.mode === "LIVE";

    const feed = data.feed || {};
    const stale = (feed.stale_symbols || []).length;
    const state = !feed.connected ? "error" : stale ? "fallback" : "live";
    el.feedPill.dataset.state = state;
    el.feedLabel.textContent = state === "live" ? "Feed live" : state === "fallback" ? "Feed partial" : "Feed down";
    el.feedDot.dataset.health = state === "live" ? "ok" : state === "fallback" ? "warn" : "down";
    el.feedMeta.textContent = `${(feed.messages || 0).toLocaleString()} messages · ${feed.symbols || 0} symbols` +
      (stale ? ` · stale: ${feed.stale_symbols.join(", ")}` : "");
    el.dbMeta.textContent = data.database || "—";
    el.uptime.textContent = duration(data.uptime_s);
  }

  const PING_TIERS = [
    { max: 200, quality: "good", level: 4 }, { max: 300, quality: "good", level: 3 },
    { max: 600, quality: "fair", level: 2 }, { max: Infinity, quality: "poor", level: 1 },
  ];
  async function pollPing() {
    let data = null;
    try { data = await api("/api/broker_ping"); } catch (error) { data = null; }
    const latency = data ? num(data.latency_ms) : null;
    const t = latency === null ? { quality: "down", level: 0 } : PING_TIERS.find((x) => latency <= x.max);
    el.pingPill.dataset.quality = t.quality;
    el.pingLadder.dataset.level = String(t.level);
    el.pingValue.textContent = latency === null ? "no answer" : `${Math.round(latency)} ms`;
    el.brokerDot.dataset.health = latency === null ? "down" : t.quality === "good" ? "ok" : "warn";
    el.brokerMeta.textContent = latency === null ? `No answer${data && data.error ? ` · ${data.error}` : ""}`
      : `${Math.round(latency)} ms round trip` + (data.median_ms ? ` · median ${Math.round(data.median_ms)} ms` : "");
  }

  /* ---------- the ticket ------------------------------------------------- */
  const field = (name) => ticket.elements[name];
  // The preview prices the side last pointed at (buy until then).
  let previewSide = "buy";

  function ticketPayload(side = previewSide) {
    const payload = {
      symbol: selected, side, order_type: "limit_order",
      size: Number(field("size").value),
      reduce_only: false,
    };
    if (field("limit_price").value.trim()) payload.limit_price = field("limit_price").value.trim();
    if (field("stop_loss").value) payload.stop_loss = field("stop_loss").value;
    if (field("take_profit").value) payload.take_profit = field("take_profit").value;
    return payload;
  }

  function updateSizeHint() {
    const s = specs[selected];
    const q = snapshot && (snapshot.quotes || {})[selected];
    const size = Number(field("size").value);
    if (!s || !q || !Number.isFinite(size) || size <= 0) { el.sizeHint.textContent = "—"; return; }
    const units = size * Number(s.contract_value);
    el.sizeHint.textContent = `= ${units.toLocaleString("en-US", { maximumFractionDigits: 8 })} ` +
      `${selected.replace(/USD$/, "")} · ≈ ${usd(units * q.mark)} at mark`;
  }

  function clearPreview(message) {
    for (const node of [el.pvPrice, el.pvNotional, el.pvMargin, el.pvFee, el.pvExit,
      el.pvRequired, el.pvAvailable, el.pvLiq]) node.textContent = "—";
    if (message !== undefined) el.pvPrice.textContent = message;
    el.autoPrice.textContent = field("limit_price").value.trim() ? "manual" : "auto";
  }

  let previewTimer = null;
  let previewSeq = 0;
  function schedulePreview() {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(preview, 250);
  }

  async function preview() {
    if (!selected) return;
    const payload = ticketPayload();
    if (!Number.isInteger(payload.size) || payload.size < 1) { clearPreview(); return; }
    const seq = ++previewSeq;
    try {
      const q = await api("/api/quote", { json: payload });
      if (seq !== previewSeq) return;
      const s = selected;
      el.pvPrice.textContent = `${px(q.price, s)} · ${q.auto ? "auto, " : ""}${q.role} ${(q.fee_rate * 100).toFixed(3)}%`;
      el.autoPrice.textContent = q.auto ? `auto · ${px(q.price, s)}` : "manual";
      el.pvNotional.textContent = usd(q.notional, false, 4);
      el.pvMargin.textContent = q.opening_contracts ? `${usd(q.margin, false, 4)} @ ${q.leverage}x` : "none (reduces)";
      el.pvFee.textContent = `${usd(q.fee_total, false, 4)} (${usd(q.fee, false, 4)} + ${usd(q.gst, false, 4)})` +
        (q.closing_free ? " · close free (scalper)" : "");
      el.pvExit.textContent = q.opening_contracts ? usd(q.exit_fee_estimate, false, 4) : "—";
      el.pvRequired.textContent = usd(q.required, false, 4);
      el.pvAvailable.textContent = usd(q.available, false, 4);
      el.pvRequired.className = q.available != null && q.required > q.available ? "negative" : "";
      el.pvLiq.textContent = q.liquidation_price == null ? "—" : px(q.liquidation_price, s);
      // Chance the price reaches it, from recent volatility (move_risk.py).
      const risk = q.liquidation_risk;
      el.pvLiq.title = "";
      if (risk) {
        el.pvLiq.textContent += ` · ${risk.distance_bps} bps away, hit ${(risk.touch_15m * 100).toFixed(0)}% in 15m / ` +
          `${(risk.touch_1h * 100).toFixed(0)}% in 1h`;
        el.pvLiq.title = `Typical 15-minute move now: ±${risk.move_15m_bps} bps`;
      }
      el.pvLiq.className = risk && risk.touch_15m >= 0.2 ? "negative" : "";
      if (q.leverage > q.leverage_cap + 1e-9) {
        el.pvMargin.textContent += ` · max ${q.leverage_cap.toFixed(2)}x at this size`;
        el.pvMargin.className = "negative";
      } else {
        el.pvMargin.className = "";
      }
    } catch (error) {
      if (seq !== previewSeq) return;
      clearPreview(error.message);
    }
  }

  function showError(message) {
    el.ticketError.hidden = !message;
    el.ticketError.textContent = message || "";
  }

  // The size is remembered in this browser: a reload, a fill or an exit keeps it.
  const SIZE_KEY = "delta.size";
  try {
    const kept = Number(localStorage.getItem(SIZE_KEY));
    if (Number.isInteger(kept) && kept >= 1) field("size").value = String(kept);
  } catch (error) { /* not remembered */ }
  function keepSize() {
    const size = Number(field("size").value);
    if (!Number.isInteger(size) || size < 1) return;
    try { localStorage.setItem(SIZE_KEY, String(size)); } catch (error) { /* not remembered */ }
  }
  ticket.addEventListener("input", () => { showError(""); keepSize(); updateSizeHint(); schedulePreview(); });
  ticket.addEventListener("change", schedulePreview);
  ticket.addEventListener("submit", (event) => event.preventDefault());   // only the buttons send
  for (const button of [el.buyButton, el.sellButton]) {
    const point = () => { if (previewSide !== button.dataset.side) { previewSide = button.dataset.side; schedulePreview(); } };
    button.addEventListener("pointerenter", point);
    button.addEventListener("focus", point);
    button.addEventListener("click", () => send(button.dataset.side));
  }
  el.exitButton.addEventListener("click", () => { if (selected) exitPosition(selected); });

  // TP · NET USD: a new position from flat gets a take-profit priced from its
  // real fill to net this much after every charge (the pegger's target entry).
  // Remembered in this browser, as the size is.
  const TP_KEY = "delta.tpNet";
  try { field("tp_net").value = localStorage.getItem(TP_KEY) || ""; } catch (error) { /* not remembered */ }
  field("tp_net").addEventListener("input", () => {
    try { localStorage.setItem(TP_KEY, field("tp_net").value); } catch (error) { /* not remembered */ }
  });
  // The net target for an order from flat, null when off; throws when it cannot apply.
  function tpNet(payload) {
    const raw = field("tp_net").value.trim();
    const net = raw === "" ? 0 : Number(raw);
    if (!Number.isFinite(net) || net < 0) throw new Error("TP · NET USD must be 0 or more.");
    const held = snapshot && (snapshot.positions || []).some((p) => p.symbol === payload.symbol);
    if (!net || held) return null;         // off, or the order adds to / reduces a position
    if (payload.limit_price) throw new Error("TP · NET USD needs the limit on auto: clear the limit price, or set TP · NET to 0.");
    if (payload.stop_loss || payload.take_profit) throw new Error("Use TP · NET USD or the STOP-LOSS / TAKE-PROFIT prices, not both.");
    return net;
  }

  async function send(side) {
    if (busy || !selected) return;
    const payload = ticketPayload(side);
    if (!Number.isInteger(payload.size) || payload.size < 1) { showError("Size must be a whole number of contracts."); return; }
    let net;
    try { net = tpNet(payload); } catch (error) { showError(error.message); return; }
    if (net) payload.rr = [net];
    const words = `${payload.side.toUpperCase()} ${payload.size} ${selected} ` +
      (payload.limit_price ? `limit ${payload.limit_price}` : "auto-priced limit (post-only, best bid/ask)") +
      (net ? ` · TP +$${net} net` : "");
    if (LIVE && !window.confirm(`LIVE ORDER with real money:\n\n${words}\n\nSend it to Delta Exchange?`)) return;
    busy = true;
    ticket.dataset.working = "";
    el.buyButton.disabled = el.sellButton.disabled = true;
    try {
      const result = await api("/api/orders", { json: payload });
      showError("");
      const o = result.order, g = result.peg;
      if (g) {
        toast(g.state === "filled" ? `${words}: filled @ ${px(g.avg_fill_price, selected)}`
          : `${words}: working @ ${g.price == null ? "next tick" : px(g.price, selected)}`);
      } else {
        toast(o.state === "closed"
          ? `${words}: filled @ ${px(o.avg_fill_price, selected)}, fee ${usd(o.paid_commission, false, 4)}`
          : `${words}: ${o.state}`);
      }
      field("stop_loss").value = "";
      field("take_profit").value = "";
      await poll();
      pollHistory();
    } catch (error) {
      showError(error.message);
    } finally {
      busy = false;
      delete ticket.dataset.working;
      el.buyButton.disabled = el.sellButton.disabled = false;
      sizeToPct();
    }
  }

  // The size stepper: one contract either way, never under one. Stepping or
  // typing a size drops a picked share.
  for (const [button, by] of [[el.sizeDown, -1], [el.sizeUp, 1]]) {
    button.addEventListener("click", () => {
      setSizePct(null);
      const now = Math.floor(Number(field("size").value));
      field("size").value = String(Math.max(1, (Number.isFinite(now) ? now : 1) + by));
      ticket.dispatchEvent(new Event("input"));
    });
  }
  field("size").addEventListener("input", (event) => { if (event.isTrusted) setSizePct(null); });

  // Size as a share of available: price one contract, then fill the share with whole contracts.
  // Kept reserve leaves small margin buffer so 100% fits within available margin and fees.
  // Lot size defaults to 100% always: the size follows available, leverage, or symbol.
  const SIZE_RESERVE_USD = 10;
  const PCT_KEY = "delta.sizePct";
  let sizePct = 100;
  function setSizePct(pct) {
    sizePct = pct != null ? Number(pct) : null;
    for (const b of el.sizePcts.querySelectorAll("[data-pct]")) {
      b.setAttribute("aria-pressed", String(sizePct !== null && Number(b.dataset.pct) === sizePct));
    }
    try {
      if (sizePct !== null) localStorage.setItem(PCT_KEY, String(sizePct));
      else localStorage.removeItem(PCT_KEY);
    } catch (error) { /* not remembered */ }
  }
  try {
    const saved = localStorage.getItem(PCT_KEY);
    setSizePct(saved !== null && saved !== "" ? Number(saved) : 100);
  } catch (error) {
    setSizePct(100);
  }

  // `loud`: on a click, say why it cannot size; on a refresh, keep the size quietly.
  // While the symbol holds a position or a working order, its margin is out of
  // available: a refresh then keeps the size, and resizes once flat again.
  const engaged = () => Boolean(snapshot && selected && (
    (snapshot.positions || []).some((p) => p.symbol === selected) ||
    (snapshot.orders || []).some((o) => o.symbol === selected) ||
    (snapshot.pegs || []).some((g) => g.symbol === selected && g.state === "working")));
  async function sizeToPct(loud = false) {
    if (!sizePct || !selected || busy || (!loud && engaged())) return;
    const pct = sizePct, asked = selected;
    try {
      const q = await api("/api/quote", { json: { ...ticketPayload(), size: 1 } });
      if (pct !== sizePct || asked !== selected) return;
      const per = q.opening_contracts ? q.required : q.notional / q.leverage + 2 * q.fee_total;
      const reserve = Math.min(SIZE_RESERVE_USD, Math.max(0.5, q.available * 0.01));
      const usable = Math.max(0, q.available - reserve);
      if (!(per > 0) || !(usable >= per)) {
        if (loud) showError(`Not enough available above the $${reserve.toFixed(2)} kept free for one contract.`);
        return;
      }
      const size = String(Math.max(1, Math.floor(usable * pct / 100 / per)));
      if (field("size").value === size) return;
      field("size").value = size;
      ticket.dispatchEvent(new Event("input"));
    } catch (error) {
      if (loud) showError(error.message);
    }
  }
  el.sizePcts.addEventListener("click", (event) => {
    const pct = Number(event.target.dataset.pct);
    if (!pct) return;
    setSizePct(pct);
    sizeToPct(true);
  });

  // Leverage: typed, stepped one at a time or picked from the presets, then SET
  // (or Enter). A preset shows pressed while the box holds its value.
  function markLeverage() {
    for (const b of el.leveragePresets.querySelectorAll("[data-lev]")) {
      b.setAttribute("aria-pressed", String(Number(b.dataset.lev) === Number(el.leverageInput.value)));
    }
  }
  function nudgeLeverage(value) {
    const max = Number(el.leverageInput.max) || Infinity;
    el.leverageInput.value = String(Math.min(max, Math.max(1, Math.round(value))));
    markLeverage();
  }
  el.leverageDown.addEventListener("click", () => nudgeLeverage((Number(el.leverageInput.value) || 1) - 1));
  el.leverageUp.addEventListener("click", () => nudgeLeverage((Number(el.leverageInput.value) || 0) + 1));
  el.leveragePresets.addEventListener("click", (event) => {
    if (event.target.dataset.lev) nudgeLeverage(Number(event.target.dataset.lev));
  });
  el.leverageInput.addEventListener("input", markLeverage);
  el.leverageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); el.leverageApply.click(); }
  });
  el.leverageApply.addEventListener("click", async () => {
    const leverage = Number(el.leverageInput.value);
    if (!Number.isFinite(leverage) || leverage < 1) { showError("Leverage must be at least 1x."); return; }
    if (LIVE && !window.confirm(`Set ${selected} leverage to ${leverage}x on Delta Exchange?`)) return;
    try {
      const result = await api(`/api/leverage/${selected}`, { json: { leverage } });
      specs[selected] = { ...specs[selected], leverage: result.leverage };
      renderSpec();
      showError("");
      toast(`${selected} leverage set to ${result.leverage}x`);
      schedulePreview();
      sizeToPct();
      poll();
    } catch (error) {
      showError(error.message);
    }
  });

  /* ---------- row actions ------------------------------------------------ */
  // Exits at once, no confirmation: an auto-priced reduce-only limit, followed until flat.
  async function exitPosition(symbol) {
    try {
      const r = await api(`/api/positions/${symbol}/close`, { json: {} });
      toast(r.peg.state === "filled" ? `${symbol} exited @ ${px(r.peg.avg_fill_price, symbol)}`
        : `${symbol} exit working @ ${r.peg.price == null ? "next tick" : px(r.peg.price, symbol)}`);
      await poll();
      sizeToPct();
    } catch (error) { toast(`Exit failed: ${error.message}`); }
  }

  el.positions.addEventListener("click", async (event) => {
    const pick = event.target.closest("[data-pick]");
    if (pick) { selectSymbol(pick.dataset.pick); return; }
    const closeBtn = event.target.closest("[data-close]");
    if (closeBtn) { exitPosition(closeBtn.dataset.close); return; }
    const targetBtn = event.target.closest("[data-target]");
    if (targetBtn) {
      const symbol = targetBtn.dataset.target;
      const net = window.prompt(`${symbol}: take profit at a net gain of (USD, after entry + exit fees incl. GST):`, "1");
      if (net === null) return;
      try {
        const r = await api(`/api/positions/${symbol}/target`, { json: { net: Number(net) } });
        toast(`${symbol} target $${r.net} net: limit resting @ ${px(r.price, symbol)}`);
        await poll();
      } catch (error) { toast(`Target failed: ${error.message}`); }
      return;
    }
    const bracket = event.target.closest("[data-bracket]");
    if (bracket) {
      const symbol = bracket.dataset.bracket;
      const sl = window.prompt(`${symbol} stop-loss price (blank = none):`, bracket.dataset.sl || "");
      if (sl === null) return;
      const tp = window.prompt(`${symbol} take-profit price (blank = none):`, bracket.dataset.tp || "");
      if (tp === null) return;
      try {
        await api(`/api/positions/${symbol}/bracket`, { json: { stop_loss: sl.trim() || null, take_profit: tp.trim() || null } });
        toast(`${symbol} bracket updated`);
        await poll();
      } catch (error) { toast(`Bracket failed: ${error.message}`); }
    }
  });

  el.openOrders.addEventListener("click", async (event) => {
    const peg = event.target.closest("[data-cancel-peg]");
    if (peg) {
      try {
        await api(`/api/pegs/${peg.dataset.cancelPeg}`, { method: "DELETE" });
        toast("Auto order cancelled");
        await poll();
      } catch (error) { toast(`Cancel failed: ${error.message}`); }
      return;
    }
    const button = event.target.closest("[data-cancel]");
    if (!button) return;
    try {
      await api(`/api/orders/${button.dataset.cancel}`, { method: "DELETE" });
      toast("Order cancelled");
      await poll();
    } catch (error) { toast(`Cancel failed: ${error.message}`); }
  });

  el.closeAll.addEventListener("click", async () => {
    if (!window.confirm(`Exit EVERY open position with auto-priced limits${LIVE ? " (LIVE)" : ""}?`)) return;
    try {
      const r = await api("/api/positions/ALL/close", { json: {} });
      const failed = r.results.filter((x) => x.error);
      toast(failed.length ? `Some exits failed: ${failed.map((x) => `${x.symbol}: ${x.error}`).join("; ")}`
        : `Exiting ${r.results.length} position(s)`);
      await poll();
    } catch (error) { toast(`Exit all failed: ${error.message}`); }
  });

  el.resetPaper.addEventListener("click", async () => {
    const amount = window.prompt("Reset the paper account. Every position and order is dropped and the ledger, trades and P&L history are cleared. The HH/LL strategy starts over too: its trades and stats are cleared (its threshold stays).\n\nNew starting balance (USD):", "500");
    if (amount === null) return;
    if (window.prompt('Type RESET to confirm') !== "RESET") return;
    try {
      await api("/api/paper/reset", { json: { equity: amount, confirm: "RESET" } });
      toast(`Paper account reset to $${amount}; ledger and HH/LL strategy cleared`);
      await poll();
      pollHistory();
    } catch (error) { toast(`Reset failed: ${error.message}`); }
  });

  /* ---------- performance ledger ----------------------------------------- */
  // Closed positions over a date range, by close day (IST) and by symbol, net
  // of fees and funding. Read from the newest HISTORY_MAX closed trades.
  const HISTORY_MAX = 500;
  const istDay = new Intl.DateTimeFormat("en-CA", { timeZone: IST, year: "numeric", month: "2-digit", day: "2-digit" });
  const dayOf = (stamp) => istDay.format(new Date(stamp));
  const filters = $("pnlFilters");
  const today = dayOf(Date.now());
  $("pnlEnd").value = today;
  $("pnlStart").value = `${today.slice(0, 8)}01`;
  $("pnlMode").value = LIVE ? "LIVE" : "TEST";
  let historyRequest = 0;

  async function refreshPerformance() {
    $("pnlStart").setCustomValidity($("pnlStart").value > $("pnlEnd").value ? "Start date must not follow end date." : "");
    if (!filters.reportValidity()) return;
    const requestId = ++historyRequest;
    $("pnlStatus").textContent = "Loading trade history…";
    $("pnlResults").hidden = true;
    $("pnlApply").disabled = true;
    try {
      const data = await api(`/api/history/trades?limit=${HISTORY_MAX}`);
      if (requestId !== historyRequest) return;
      const start = $("pnlStart").value, end = $("pnlEnd").value;
      const mode = $("pnlMode").value, symbol = $("pnlSymbol").value;
      const rows = data.rows.filter((r) => {
        const day = dayOf(r.closed_at);
        return day >= start && day <= end && (mode === "ALL" || r.mode === mode) && (!symbol || r.symbol === symbol);
      });
      const net = (r) => num(r.net_pnl) || 0;
      const total = rows.reduce((sum, r) => sum + net(r), 0);
      const wins = rows.filter((r) => net(r) > 0).length;
      $("historyTotal").textContent = usd(total, true);
      toneUpDown($("historyTotal"), total);
      $("historyTrades").textContent = rows.length;
      $("historyWinRate").textContent = rows.length ? `${Math.round(wins / rows.length * 100)}%` : "—";
      const group = (key) => {
        const out = new Map();
        for (const r of rows) {
          const k = key(r), g = out.get(k) || { key: k, trades: 0, net: 0 };
          g.trades += 1; g.net += net(r);
          out.set(k, g);
        }
        return [...out.values()];
      };
      const daily = group((r) => dayOf(r.closed_at)).sort((a, b) => (a.key < b.key ? -1 : 1));
      const symbols = group((r) => r.symbol).sort((a, b) => b.net - a.net);
      const cumulative = [0];
      for (const day of daily) cumulative.push(cumulative[cumulative.length - 1] + day.net);
      const bottom = Math.min(...cumulative), top = Math.max(...cumulative);
      $("historyLine").setAttribute("d", cumulative.map((value, i) =>
        `${i ? "L" : "M"}${10 + i * 200 / Math.max(1, cumulative.length - 1)},${top === bottom ? 20 : 35 - (value - bottom) * 30 / (top - bottom)}`).join(" "));
      toneUpDown($("historyCurve"), total);
      for (const [id, list] of [["dailyPnl", [...daily].reverse()], ["symbolPnl", symbols]]) {
        const fragment = document.createDocumentFragment();
        const largest = Math.max(1e-9, ...list.map((g) => Math.abs(g.net)));
        for (const g of list) {
          const tr = document.createElement("tr");
          for (const value of [g.key, g.trades, usd(g.net, true)]) {
            const td = document.createElement("td"); td.textContent = value; tr.appendChild(td);
          }
          toneUpDown(tr.lastElementChild, g.net);
          tr.lastElementChild.style.setProperty("--magnitude", `${Math.abs(g.net) / largest * 100}%`);
          fragment.appendChild(tr);
        }
        $(id).replaceChildren(fragment);
      }
      $("pnlResults").hidden = !rows.length;
      const capped = data.rows.length >= HISTORY_MAX ? ` · from the newest ${HISTORY_MAX} closed trades` : "";
      $("pnlStatus").textContent = rows.length
        ? `${start} → ${end} · ${mode === "ALL" ? "All execution modes" : mode === "TEST" ? "Paper" : "Live"} · ${symbol || "All symbols"}${capped}`
        : "No closed trades for this selection. Try another period or symbol.";
    } catch (error) {
      if (requestId === historyRequest) $("pnlStatus").textContent = "Trade history unavailable. Apply filters to retry.";
    } finally {
      if (requestId === historyRequest) $("pnlApply").disabled = false;
    }
  }
  filters.addEventListener("submit", (event) => { event.preventDefault(); refreshPerformance(); });
  filters.addEventListener("input", () => $("pnlStart").setCustomValidity(""));
  document.querySelector(".periods").addEventListener("click", (event) => {
    const days = event.target.closest("[data-days]")?.dataset.days;
    if (!days) return;
    const last = dayOf(Date.now()), first = new Date(`${last}T00:00:00Z`);
    if (days === "month") first.setUTCDate(1);
    else first.setUTCDate(first.getUTCDate() - Number(days) + 1);
    $("pnlStart").value = first.toISOString().slice(0, 10);
    $("pnlEnd").value = last;
    refreshPerformance();
  });

  /* ---------- selection, tabs and polling -------------------------------- */
  async function selectSymbol(symbol) {
    if (!symbol) return;
    selected = symbol;
    el.symbolPicker.value = symbol;
    el.quoteSymbol.textContent = symbol;
    fitSymbol();
    el.priceAsOf.textContent = "—";
    el.priceChange.textContent = "Waiting for a quote";
    toneUpDown(el.priceChange, 0);
    try { localStorage.setItem(SYMBOL_KEY, symbol); } catch (error) { /* not remembered */ }
    el.veil.classList.remove("is-hidden");
    el.veil.querySelector("strong").textContent = "Loading candles";
    chart.clear();
    renderSpec();
    renderBoard();
    schedulePreview();
    sizeToPct();
    await loadChart();
  }

  el.symbolPicker.addEventListener("change", () => selectSymbol(el.symbolPicker.value));

  for (const tab of document.querySelectorAll("#resolutionTabs [data-resolution]")) {
    tab.addEventListener("click", () => {
      resolution = tab.dataset.resolution;
      for (const other of document.querySelectorAll("#resolutionTabs [data-resolution]")) {
        other.setAttribute("aria-selected", String(other === tab));
      }
      el.liveWindows.hidden = !tickView();
      el.veil.classList.remove("is-hidden");
      el.veil.querySelector("strong").textContent = "Loading candles";
      loadChart();
    });
  }
  for (const tab of document.querySelectorAll("#liveWindows [data-window]")) {
    tab.addEventListener("click", () => {
      liveWindow = tab.dataset.window;
      chart.window = stepMs(liveWindow);
      for (const other of document.querySelectorAll("#liveWindows [data-window]")) {
        other.setAttribute("aria-selected", String(other === tab));
      }
      el.veil.classList.remove("is-hidden");
      el.veil.querySelector("strong").textContent = "Loading trades";
      chart.draw();
      loadChart();
    });
  }
  // The smoothed line's controls (every version: the strategy trades its pivots): TICK and SMOOTHED show or hide the two Live lines
  // with their pivots (remembered per browser); the box is the smoother's threshold in % of price, the server's
  // (Enter or leaving the box applies it: the line and its pivots are redrawn with it).
  if (SMOOTH_UI) {
    const KEY = "delta.smooth", MIN = 0.001, MAX = 1;
    const conf = { tick: true, smooth: true };
    try {
      const kept = JSON.parse(localStorage.getItem(KEY) || "null");
      if (kept && typeof kept.tick === "boolean") conf.tick = kept.tick;
      if (kept && typeof kept.smooth === "boolean") conf.smooth = kept.smooth;
    } catch (error) { /* not remembered */ }
    const bar = document.createElement("div");
    bar.className = "desk-frames smooth-tools";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Live chart lines");
    const toggle = (key, text, title) => {
      const b = document.createElement("button");
      Object.assign(b, { type: "button", className: "seg", textContent: text, title });
      b.dataset.line = key;
      b.addEventListener("click", () => { conf[key] = !conf[key]; apply(); });
      return b;
    };
    const tickBtn = toggle("tick", "TICK", "Show or hide the websocket trades line and its own HH/LL pivots (shown, not traded)");
    const smoothBtn = toggle("smooth", "SMOOTHED", "Show or hide the smoothed line and its HH/LL pivots: the strategy trades these, at their white dashes");
    const label = document.createElement("label");
    label.className = "smooth-box";
    label.title = "The smallest move the smoothed line shows, in % of price: it follows price at once while price keeps " +
      "going, and holds flat through any move back smaller than this. Higher is smoother. " +
      `${MIN} to ${MAX}. Enter applies: the line and its pivots are redrawn.`;
    const input = document.createElement("input");
    Object.assign(input, { type: "number", inputMode: "decimal", min: String(MIN), max: String(MAX), step: "0.005",
      value: String(SMOOTH_DEFAULT), id: "smoothPct" });
    input.setAttribute("aria-label", "Smoothed line threshold, % of price");
    label.append("SMOOTH THRESHOLD · %", input);
    bar.append(tickBtn, smoothBtn, label);
    el.liveWindows.append(bar);
    const legend = document.querySelector(".board-foot .legend");
    if (legend) {
      const item = document.createElement("span");
      item.innerHTML = '<i class="swatch swatch--smooth"></i>SMOOTHED · ITS HH / LL (TRADED)';
      legend.append(item);
    }
    function apply() {
      tickBtn.setAttribute("aria-pressed", String(conf.tick));
      smoothBtn.setAttribute("aria-pressed", String(conf.smooth));
      label.toggleAttribute("data-off", !conf.smooth);
      chart.setSmooth(conf);
      try { localStorage.setItem(KEY, JSON.stringify(conf)); } catch (error) { /* not remembered */ }
    }
    let applied = null, busy = false;
    const value = () => {
      const v = Number(input.value);
      return input.value !== "" && Number.isFinite(v) && v >= MIN && v <= MAX ? v : null;
    };
    smoothFollow = (pct) => {
      const v = num(pct);
      if (v === null) return;
      const changed = v !== applied;
      applied = v;
      if (changed && document.activeElement !== input && !busy) { input.value = String(v); input.removeAttribute("aria-invalid"); }
      if (chart.smoothServer && v !== chart.smoothPct) loadSmooth();
    };
    async function send() {
      const v = value();
      input.toggleAttribute("aria-invalid", v === null);
      if (v === null || v === applied || busy || !selected) return;
      busy = true;
      label.setAttribute("data-busy", "");
      try {
        const r = await api("/api/algo/smooth", { json: { symbol: selected, smooth: v } });
        applied = num(r.smooth);
        input.value = String(applied);
        await loadSmooth();
        toast(`Smoothing threshold ${applied}% applied; the smoothed line and its pivots were redrawn with it.`);
      } catch (error) {
        input.setAttribute("aria-invalid", "");
        toast(error.message);
      } finally {
        busy = false;
        label.removeAttribute("data-busy");
      }
    }
    input.addEventListener("input", () => input.toggleAttribute("aria-invalid", value() === null));
    input.addEventListener("change", send);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); send(); }
      else if (e.key === "Escape" && applied !== null) { input.value = String(applied); input.removeAttribute("aria-invalid"); input.blur(); }
    });
    apply();
  }

  for (const tab of document.querySelectorAll("#viewTabs .seg")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  }
  for (const tab of document.querySelectorAll("#periodTabs .seg")) {
    tab.addEventListener("click", () => {
      period = tab.dataset.period;
      for (const other of document.querySelectorAll("#periodTabs .seg")) {
        other.setAttribute("aria-selected", String(other === tab));
      }
      if (snapshot) renderAccount(snapshot);
    });
  }

  // An auto order that stops working says so once: filled, or why not.
  const pegStates = new Map();
  function announcePegs(pegs) {
    for (const g of pegs) {
      const was = pegStates.get(g.peg_id);
      pegStates.set(g.peg_id, g.state);
      if (was !== "working" || g.state === "working") continue;
      const what = `Auto ${g.kind} ${String(g.side).toUpperCase()} ${g.size} ${g.symbol}`;
      toast(g.state === "filled" && g.filled
        ? `${what}: filled @ ${px(g.avg_fill_price, g.symbol)}${g.note ? ` · ${g.note}` : ""}`
        : `${what}: ${g.state}, ${g.filled}/${g.size} filled${g.note ? ` · ${g.note}` : ""}`);
    }
  }

  function render(data) {
    snapshot = data;
    announcePegs(snapshot.pegs || []);
    renderAccount(snapshot);
    renderPositions(snapshot.positions || []);
    renderOpenOrders(snapshot.orders || [], snapshot.pegs || []);
    renderStrategy();
    renderBoard();
  }

  function unreachable() {
    el.feedPill.dataset.state = "error";
    el.feedLabel.textContent = "Console unreachable";
  }

  // One-off refresh, right after an action, without waiting for the next tick.
  async function poll() {
    try { render(await api("/api/snapshot")); } catch (error) { unreachable(); }
  }

  // Every tick the server's feed sees, and every trade. EventSource reconnects by itself after a
  // dropped connection; one the browser gave up on (an HTTP error) is reopened.
  // Closed while the tab is hidden: a phone keeps background tabs, and each open stream holds one of
  // the browser's six connections to this host, which the next page load then waits behind.
  let live = null;
  function stream() {
    if (live || document.hidden) return;
    const source = live = new EventSource(BASE + "/api/stream");
    source.onmessage = (event) => render(JSON.parse(event.data));
    source.addEventListener("trades", (event) => {
      for (const [symbol, price, at] of JSON.parse(event.data)) {
        if (symbol !== selected) continue;
        chart.trade(price, at);
        if (resolution === "live") el.veil.classList.add("is-hidden");
        if (FILTERED_TICKS) continue;     // not a real print: the snapshot right after it prices and marks
        setPrice(price);
        el.priceAsOf.textContent = `${istTime.format(new Date(at))} IST · live`;
        const q = snapshot && (snapshot.quotes || {})[symbol];
        if (q) q.last = price;
        if (snapshot) renderHold((snapshot.positions || []).find((p) => p.symbol === symbol));
      }
    });
    source.onerror = () => {
      unreachable();
      if (source.readyState === EventSource.CLOSED && live === source) {
        live = null;
        window.setTimeout(stream, POLL_MS);
      }
    };
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (live) live.close();
      live = null;
    } else if (!live) {
      poll();
      loadChart();
      stream();
    }
  });

  async function loadSpecs() {
    try {
      specs = await api("/api/instruments");
      renderSpec();
    } catch (error) {
      toast(`Instrument specs unavailable: ${error.message}`);
    }
  }

  window.setInterval(() => { el.clock.textContent = istTime.format(new Date()); }, 1000);



  (async function boot() {
    await loadSpecs();
    await poll();
    await selectSymbol(selected);
    pollPing();
    stream();
    window.setInterval(pollPing, 5000);
    window.setInterval(loadChart, 5000);
    window.setInterval(pollHistory, 5000);
    refreshPerformance();
    window.setInterval(() => { if (!document.hidden) { preview(); sizeToPct(); } }, 4000);
  })();
})();
