// TEMPORARY live check of the real /api/backtest handler (real Angel One login + candles), called the way Vercel calls it.
const fs = require("fs"), handler = require("../api/backtest.js");
const call = (symbol, tf) => new Promise(async (resolve) => {
  const t0 = Date.now(); let status = 0;
  const res = { setHeader() {}, status(s) { status = s; return this; }, json(b) { resolve({ status, ms: Date.now() - t0, body: b }); } };
  try { await handler({ query: { symbol, tf, years: "10" } }, res); } catch (e) { resolve({ status: -1, ms: Date.now() - t0, body: { error: String(e) } }); }
});
(async () => {
  const out = {};
  for (const sym of ["SRF", "FEDERALBNK", "HEXT", "M&MFIN", "GVT&D", "ATUL", "HFCL"]) {
    out[sym] = {};
    for (const tf of ["W", "M"]) {
      const r = await call(sym, tf), b = r.body;
      out[sym][tf] = { http: r.status, seconds: +(r.ms / 1000).toFixed(1), error: b.error, bars: b.bars, from: b.firstDate, to: b.lastDate, note: b.note, adjustments: b.adjustments, summary: b.summary, trades: (b.trades || []).length, firstTrades: (b.trades || []).slice(0, 5), lastTrade: (b.trades || []).slice(-1)[0] };
    }
  }
  fs.writeFileSync("bt_check/result.json", JSON.stringify(out, null, 1));
  console.log("done");
})();
