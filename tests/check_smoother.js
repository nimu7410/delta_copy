// The chart's smoothed line (static/delta.js `Smoother`): node tests/check_smoother.js
const fs = require("fs"), assert = require("assert");
const src = fs.readFileSync(`${__dirname}/../static/delta.js`, "utf8");
const Smoother = new Function(`${src.slice(src.indexOf("const SMOOTH_ROUND_S"), src.indexOf("// The view, panned"))}; return Smoother;`)();

// 3900 with 0.3-point bid/ask bounce for 20 min, a 10-point jump, 20 min more; a print every 250 ms, but none
// for 5 s after the 6000th
const t0 = 1.8e12, prints = [];
for (let i = 0; i < 9600; i += 1) prints.push([(i < 4800 ? 3900 : 3910) + (i % 2) * 0.3, t0 + i * 250 + (i >= 6000) * 5000]);
const s = new Smoother(0.02);
prints.slice(0, 6000).forEach(([p, t]) => s.feed(p, t));
const before = s.v.slice(), tail = s.tail(prints[6000][1]);
assert(tail.length >= 4, "the silence is drawn");
prints.slice(6000).forEach(([p, t]) => s.feed(p, t));
assert.deepStrictEqual(s.v.slice(0, before.length), before, "a drawn point never moves");
tail.forEach(([t, v]) => assert.strictEqual(s.value(t), v, "the drawn tail is what is stored later"));
const at = (min) => s.value(t0 + min * 60e3);
assert(Math.abs(at(19.9) - 3900.15) < 0.05, `settles on price in the bounce: ${at(19.9)}`);
let wig = 0;
for (let i = 2; i < 1100; i += 1) wig += Math.sign(s.v[i] - s.v[i - 1]) * Math.sign(s.v[i - 1] - s.v[i - 2]) < 0;
assert(wig === 0, `the bounce makes no wiggle: ${wig}`);
assert(at(21) > 3906 && at(21) < 3910.3 && Math.abs(at(30) - 3910.15) < 0.2, `follows a jump: ${at(21)} ${at(30)}`);
for (let i = 1; i < s.v.length; i += 1) assert(s.v[i] <= 3910.31 && s.v[i] >= 3899.99, "never outside the prints");
// The server's line (hhll.Smoother, whose pivots the chart draws on it) is the same line
const { execFileSync } = require("child_process"), path = require("path");
const root = path.join(__dirname, "..");
const py = fs.existsSync(`${root}/.venv/bin/python`) ? `${root}/.venv/bin/python` : "python3";
const server = JSON.parse(execFileSync(py, ["-c", `
import json, sys
from hhll import Smoother
s = Smoother(0.02)
out = [p for price, at in json.load(sys.stdin) for p in s.feed(price, at)]
print(json.dumps({"line": out, "state": s.state(), "fed": s.fed}))`], { cwd: root, input: JSON.stringify(prints) }));
assert.strictEqual(server.line.length, s.at.length, "as many seconds");
server.line.forEach(([t, v], i) => {
  assert.strictEqual(t, s.at[i]);
  assert(Math.abs(v - s.v[i]) < 1e-9, `second ${i}: server ${v}, chart ${s.v[i]}`);
});
// ... and the chart, loaded with the server's line part way, goes on exactly as if it had run it all
const half = new Smoother(0.02), cut = 5000;
prints.slice(0, cut).forEach(([p, t]) => half.feed(p, t));
const loaded = Smoother.load({ pct: 0.02, points: half.at.map((t, i) => [t, half.v[i]]), fed: prints[cut - 1][1],
  state: { s: half.s, m: half.m, sec: half.sec, x: half.x, mark: half.mark, area: half.area } });
prints.slice(cut).forEach(([p, t]) => loaded.feed(p, t));
assert.deepStrictEqual(loaded.v, s.v, "loaded and continued = run whole");
console.log("smoother ok");
