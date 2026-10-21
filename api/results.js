// GET /api/results                -> { configured, updated, stocks: { SYMBOL: light result } }   (the four numbers per chart + dates)
// GET /api/results?symbol=TCS     -> { configured, result: the whole saved result of that stock (trades of both charts) or null }
// Reads what /api/backtest saved (see _store.js), so the dashboard can show it without calculating again.
const store = require("./_store");

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (!store.configured()) { res.status(200).json({ configured: false, stocks: {}, result: null }); return; }
  try {
    const symbol = String(req.query.symbol || "").toUpperCase();
    if (symbol) {
      if (!/^[A-Z0-9&\-]+$/.test(symbol)) { res.status(400).json({ error: "valid symbol required" }); return; }
      res.status(200).json({ configured: true, result: await store.loadOne(symbol) });
      return;
    }
    const s = await store.loadSummary();
    res.status(200).json({ configured: true, updated: s.updated, stocks: s.stocks });
  } catch (e) {
    res.status(500).json({ configured: true, error: e.message, stocks: {}, result: null });
  }
};
