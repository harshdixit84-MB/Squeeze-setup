// GET /api/backtest?symbol=TCS&tf=W&years=10
// Logs in to Angel One, downloads daily candles for ONE stock, runs the 20/50 EMA backtest (see _engine.js).
// Needs 4 environment variables in Vercel: ANGEL_API_KEY, ANGEL_CLIENT_CODE (or ANGEL_CLIENT_ID), ANGEL_PIN, ANGEL_TOTP_SECRET.
const OTPAuth = require("otpauth");
const { istNow, fixSplits, runBacktest, summarize, WARMUP } = require("./_engine");

const BASE = "https://apiconnect.angelbroking.com";
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
    const tf = req.query.tf === "M" ? "M" : "W";
    const years = Math.min(Math.max(parseInt(req.query.years || "10", 10), 1), 20);
    if (!/^[A-Z0-9&\-]+$/.test(symbol)) { res.status(400).json({ error: "valid symbol required" }); return; }

    const today = istNow();
    const jwt = await angelLogin();
    const token = await findToken(jwt, symbol);
    // one extra year is downloaded only so the 50 EMA is warmed up when the 10-year window starts
    let daily = await fetchDaily(jwt, token, years + (tf === "M" ? 5 : 1.2));
    // today's candle is still forming until the market has settled (after 16:00 IST)
    if (daily.length && daily[daily.length - 1].date >= today.date && today.dow >= 1 && today.dow <= 5 && today.minutes < 16 * 60) daily.pop();
    if (daily.length < 100) {
      res.status(200).json({ symbol, tf, years, bars: 0, trades: [], summary: null, adjustments: [], note: "Not enough price history at Angel One" });
      return;
    }
    const fixed = fixSplits(daily);
    const from = new Date(Date.now() - years * 365 * 86400000).toISOString().slice(0, 10);
    const out = runBacktest(fixed.rows, tf, { today, from });
    const note = out.bars < WARMUP + 2 ? "Not enough history for the 20/50 EMA (needs 50+ " + (tf === "M" ? "months" : "weeks") + ")" : undefined;
    res.status(200).json({
      symbol, tf, years, bars: out.bars,
      firstDate: fixed.rows[0].date, lastDate: fixed.rows[fixed.rows.length - 1].date,
      trades: out.trades, summary: summarize(out.trades), adjustments: fixed.notes, note,
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
};
