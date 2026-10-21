// GET /api/backtest?symbol=TCS&years=10
// Logs in to Angel One, downloads daily candles for ONE stock, runs the 20/50 EMA backtest (see _engine.js) on the WEEKLY and the
// MONTHLY chart from that one download, and saves the result (see _store.js) so it never has to be calculated again until new data comes.
// Needs 4 environment variables in Vercel: ANGEL_API_KEY, ANGEL_CLIENT_CODE (or ANGEL_CLIENT_ID), ANGEL_PIN, ANGEL_TOTP_SECRET.
const OTPAuth = require("otpauth");
const { istNow, fixSplits, runBacktest, summarize, stats, expectedKey, WARMUP } = require("./_engine");
const store = require("./_store");

const BASE = process.env.ANGEL_BASE || "https://apiconnect.angelbroking.com";   // ANGEL_BASE is only for tests
const sleep = (n) => new Promise((r) => setTimeout(r, n));

function need(name, ...alts) {
  for (const n of [name, ...alts]) if (process.env[n]) return process.env[n];
  throw new Error(`Vercel environment variable ${[name, ...alts].join(" / ")} is missing`);
}

function hdrs(jwt) {
  const h = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-UserType": "USER",
    "X-SourceID": "WEB",
    "X-ClientLocalIP": "127.0.0.1",
    "X-ClientPublicIP": "127.0.0.1",
    "X-MACAddress": "00:00:00:00:00:00",
    "X-PrivateKey": need("ANGEL_API_KEY"),
  };
  if (jwt) h.Authorization = `Bearer ${jwt}`;
  return h;
}

// Angel limits requests per second; retry a few times before giving up
async function post(path, body, jwt, tries = 4) {
  let last = null;
  for (let attempt = 0; attempt < tries; attempt++) {
    const r = await fetch(BASE + path, { method: "POST", headers: hdrs(jwt), body: JSON.stringify(body) });
    let j = null;
    try { j = await r.json(); } catch (e) { j = { status: false, message: "Angel sent an unreadable reply (HTTP " + r.status + ")" }; }
    if (j && j.status !== false) return j;
    last = j;
    await sleep(1200 * (attempt + 1));
  }
  throw new Error("Angel One: " + ((last && last.message) || "request failed") + " [" + path.split("/").pop() + "]");
}

async function angelLogin() {
  const totp = new OTPAuth.TOTP({
    secret: OTPAuth.Secret.fromBase32(need("ANGEL_TOTP_SECRET")),
    digits: 6,
    period: 30,
  }).generate();
  const res = await post("/rest/auth/angelbroking/user/v1/loginByPassword", {
    clientcode: need("ANGEL_CLIENT_CODE", "ANGEL_CLIENT_ID"),
    password: need("ANGEL_PIN"),
    totp,
  }, null, 1);                                         // no retry on login: repeated wrong attempts can lock the account
  if (!res.data || !res.data.jwtToken) throw new Error("Angel login failed: " + res.message);
  return res.data.jwtToken;
}

async function findToken(jwt, symbol) {
  const res = await post("/rest/secure/angelbroking/order/v1/searchScrip", { exchange: "NSE", searchscrip: symbol }, jwt);
  const list = res.data || [];
  const hit = list.find((x) => x.tradingsymbol === `${symbol}-EQ`) || list.find((x) => x.tradingsymbol === `${symbol}-BE`);
  if (!hit) throw new Error("Symbol not found on Angel One: " + symbol);
  return hit.symboltoken;
}

const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;

async function fetchDaily(jwt, token, years) {
  const end = new Date(), start = new Date(end.getTime() - years * 365 * 86400000);
  const WINDOW = 1900 * 86400000;                      // Angel allows at most 2000 days per daily request
  const rows = new Map();
  for (let from = start.getTime(); from < end.getTime(); from += WINDOW) {
    const to = Math.min(from + WINDOW - 86400000, end.getTime());
    const res = await post("/rest/secure/angelbroking/historical/v1/getCandleData", {
      exchange: "NSE", symboltoken: token, interval: "ONE_DAY", fromdate: fmt(new Date(from)), todate: fmt(new Date(to)),
    }, jwt);
    for (const c of res.data || []) {
      if (!(c[1] > 0 && c[2] > 0 && c[3] > 0 && c[4] > 0)) continue;
      rows.set(c[0].slice(0, 10), { date: c[0].slice(0, 10), open: c[1], high: c[2], low: c[3], close: c[4] });
    }
    await sleep(400);
  }
  return [...rows.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  try {
    const symbol = String(req.query.symbol || "").toUpperCase();
    const years = Math.min(Math.max(parseInt(req.query.years || "10", 10), 1), 15);
    if (!/^[A-Z0-9&\-]+$/.test(symbol)) { res.status(400).json({ error: "valid symbol required" }); return; }

    const today = istNow();
    const keys = { W: expectedKey(today, "W"), M: expectedKey(today, "M") };   // newest closed weekly / monthly candle today

    // already calculated (with the 20% / 10% targets too) and no new weekly / monthly candle has closed since: give back the saved
    // result, do not call Angel again (?force=1 skips this check). A result saved before the 20% / 10% targets existed is calculated again.
    if (store.configured() && req.query.force !== "1") {
      let old = null;
      try { old = await store.loadOne(symbol); } catch (e) { /* storage trouble must not block a calculation */ }
      if (old && old.alt && old.keys && old.keys.W >= keys.W && old.keys.M >= keys.M) { res.status(200).json({ ...old, saved: true, fromStore: true }); return; }
    }

    const jwt = await angelLogin();
    const token = await findToken(jwt, symbol);
    // 5 extra years are downloaded only so the 50 EMA is warmed up (the monthly chart needs it most) when the 10-year window starts
    const daily = await fetchDaily(jwt, token, years + 5);
    // today's candle is still forming until the market has settled (after 16:00 IST)
    if (daily.length && daily[daily.length - 1].date >= today.date && today.dow >= 1 && today.dow <= 5 && today.minutes < 16 * 60) daily.pop();
    if (daily.length < 100) { res.status(200).json({ symbol, years, error: "Not enough price history at Angel One" }); return; }

    const fixed = fixSplits(daily);
    const from = new Date(Date.now() - years * 365 * 86400000).toISOString().slice(0, 10);
    // target 30% is the main result (record.W / record.M); the stop is always -20%. The same daily prices are run again with a
    // 20% and a 10% target (record.alt) so the targets can be compared.
    const one = (tf, target) => {
      const out = runBacktest(fixed.rows, tf, { today, from, target });
      return {
        bars: out.bars, trades: out.trades, summary: summarize(out.trades), stats: stats(out.trades),
        note: out.bars < WARMUP + 2 ? "Not enough history for the 20/50 EMA (needs 50+ " + (tf === "M" ? "months" : "weeks") + ")" : undefined,
      };
    };
    const alt = {};
    for (const t of [0.2, 0.1]) alt[Math.round(t * 100)] = { W: one("W", t), M: one("M", t) };
    const record = {
      symbol, years, savedAt: new Date().toISOString(), keys,
      firstDate: fixed.rows[0].date, asOf: fixed.rows[fixed.rows.length - 1].date,   // asOf = last daily candle used
      adjustments: fixed.notes, W: one("W", 0.3), M: one("M", 0.3), alt,
    };
    let saved = false, saveError;
    if (!store.configured()) saveError = "Storage is not connected in Vercel, so this result is not saved";
    else { try { await store.save(symbol, record); saved = true; } catch (e) { saveError = e.message; } }
    res.status(200).json({ ...record, saved, saveError });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
};
