const OTPAuth = require("otpauth");

const BASE = "https://apiconnect.angelbroking.com";
const STOP_PCT = 0.2, TARGET_PCT = 0.3, TRAIL_PCT = 0.7, WARMUP = 50;

function hdrs(jwt) {
  const h = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-UserType": "USER",
    "X-SourceID": "WEB",
    "X-ClientLocalIP": "127.0.0.1",
    "X-ClientPublicIP": "127.0.0.1",
    "X-MACAddress": "00:00:00:00:00:00",
    "X-PrivateKey": process.env.ANGEL_API_KEY,
  };
  if (jwt) h.Authorization = `Bearer ${jwt}`;
  return h;
}

async function post(path, body, jwt) {
  const r = await fetch(BASE + path, { method: "POST", headers: hdrs(jwt), body: JSON.stringify(body) });
  return r.json();
}

async function angelLogin() {
  const totp = new OTPAuth.TOTP({
    secret: OTPAuth.Secret.fromBase32(process.env.ANGEL_TOTP_SECRET),
    digits: 6,
    period: 30,
  }).generate();
  const res = await post("/rest/auth/angelbroking/user/v1/loginByPassword", {
    clientcode: process.env.ANGEL_CLIENT_CODE,
    password: process.env.ANGEL_PIN,
    totp,
  });
  if (!res.status) throw new Error("Angel login failed: " + res.message);
  return res.data.jwtToken;
}

async function findToken(jwt, symbol) {
  const res = await post("/rest/secure/angelbroking/order/v1/searchScrip",
    { exchange: "NSE", searchscrip: symbol }, jwt);
  const list = res.data || [];
  const hit = list.find((x) => x.tradingsymbol === `${symbol}-EQ`) ||
              list.find((x) => x.tradingsymbol === `${symbol}-BE`);
  if (!hit) throw new Error("Symbol not found on Angel: " + symbol);
  return hit.symboltoken;
}

const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) =>
  `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;

async function fetchDaily(jwt, token, years) {
  const end = new Date();
  const start = new Date(end.getTime() - years * 365 * 86400000);
  const WINDOW = 1900 * 86400000; // Angel allows max 2000 days per daily request
  const rows = new Map();
  for (let from = start.getTime(); from < end.getTime(); from += WINDOW) {
    const to = Math.min(from + WINDOW - 86400000, end.getTime());
    const res = await post("/rest/secure/angelbroking/historical/v1/getCandleData", {
      exchange: "NSE",
      symboltoken: token,
      interval: "ONE_DAY",
      fromdate: fmt(new Date(from)),
      todate: fmt(new Date(to)),
    }, jwt);
    for (const c of res.data || []) {
      rows.set(c[0].slice(0, 10), { date: c[0].slice(0, 10), open: c[1], high: c[2], low: c[3], close: c[4] });
    }
  }
  return [...rows.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
}

function periodKey(dateStr, tf) {
  if (tf === "M") return dateStr.slice(0, 7);
  const d = new Date(dateStr + "T00:00:00Z");
  const diff = (d.getUTCDay() + 6) % 7; // back to Monday
  return new Date(d.getTime() - diff * 86400000).toISOString().slice(0, 10);
}

function aggregate(daily, tf) {
  const bars = [];
  let cur = null, curKey = null;
  for (const d of daily) {
    const k = periodKey(d.date, tf);
    if (k !== curKey) {
      if (cur) bars.push(cur);
      cur = { date: d.date, open: d.open, high: d.high, low: d.low, close: d.close };
      curKey = k;
    } else {
      cur.high = Math.max(cur.high, d.high);
      cur.low = Math.min(cur.low, d.low);
      cur.close = d.close;
    }
  }
  if (cur) {
    // drop the still-running (incomplete) period
    const today = new Date().toISOString().slice(0, 10);
    const dow = new Date().getUTCDay();
    const running = tf === "M"
      ? curKey === today.slice(0, 7)
      : curKey === periodKey(today, "W") && dow >= 1 && dow <= 5;
    if (!running) bars.push(cur);
  }
  return bars;
}

function ema(values, span) {
  const a = 2 / (span + 1);
  const out = [values[0]];
  for (let i = 1; i < values.length; i++) out.push(a * values[i] + (1 - a) * out[i - 1]);
  return out;
}

function whatIf70(bars, exitIdx, entryPrice) {
  const stop = entryPrice * (1 - STOP_PCT), goal = entryPrice * (1 + TRAIL_PCT);
  for (let i = exitIdx; i < bars.length; i++) {
    if (bars[i].low <= stop) return null;
    if (bars[i].high >= goal) return i;
  }
  return null;
}

function runBacktest(bars, tf) {
  const n = bars.length;
  const closes = bars.map((b) => b.close);
  const fast = ema(closes, 20), slow = ema(closes, 50);
  const cross = bars.map((_, i) => i >= WARMUP && fast[i] > slow[i] && !(fast[i - 1] > slow[i - 1]));
  const unit = tf === "M" ? "months" : "weeks";
  const trades = [];
  let i = 0;
  while (i < n - 1) {
    if (!cross[i]) { i++; continue; }
    const entryIdx = i + 1;
    const entry = bars[entryIdx].open;
    const stop = entry * (1 - STOP_PCT), target = entry * (1 + TARGET_PCT);
    let exitIdx = null, exitPrice = null, reason = null;

    for (let j = entryIdx; j < n; j++) {
      const b = bars[j];
      if (j > entryIdx) {
        if (b.open <= stop) { exitIdx = j; exitPrice = b.open; reason = "Stop -20% (gap)"; break; }
        if (b.open >= target) { exitIdx = j; exitPrice = b.open; reason = "Target +30% (gap)"; break; }
      }
      const hitStop = b.low <= stop, hitTarget = b.high >= target;
      if (hitStop && hitTarget) { exitIdx = j; exitPrice = stop; reason = "Stop -20% (same bar as target)"; break; }
      if (hitStop) { exitIdx = j; exitPrice = stop; reason = "Stop -20%"; break; }
      if (hitTarget) { exitIdx = j; exitPrice = target; reason = "Target +30%"; break; }
    }
    if (exitIdx === null) { exitIdx = n - 1; exitPrice = bars[n - 1].close; reason = "Open (marked at last close)"; }

    let peak = 0;
    for (let k = entryIdx; k <= exitIdx; k++) peak = Math.max(peak, bars[k].high);
    const isTarget = reason.startsWith("Target");
    let reached70 = "-", barsTo70 = null;
    if (isTarget) {
      const idx = whatIf70(bars, exitIdx, entry);
      reached70 = idx === null ? "No" : "Yes";
      barsTo70 = idx === null ? null : idx - entryIdx;
    }
    trades.push({
      trade: trades.length + 1,
      entryDate: bars[entryIdx].date,
      entryPrice: +entry.toFixed(2),
      exitDate: bars[exitIdx].date,
      exitPrice: +exitPrice.toFixed(2),
      reason,
      peakGainPct: +((peak / entry - 1) * 100).toFixed(1),
      plPct: +((exitPrice / entry - 1) * 100).toFixed(1),
      plRs: +(exitPrice - entry).toFixed(2),
      duration: exitIdx - entryIdx,
      durationUnit: unit,
      reached70,
      barsTo70,
    });
    i = exitIdx + 1;
    while (i < n - 1 && !cross[i]) i++;
  }
  return trades;
}

function summarize(trades) {
  const closed = trades.filter((t) => !t.reason.startsWith("Open"));
  if (!closed.length) return null;
  const pl = closed.map((t) => t.plPct);
  return {
    totalTrades: trades.length,
    winPct: +((pl.filter((x) => x > 0).length / pl.length) * 100).toFixed(1),
    avgPlPct: +(pl.reduce((a, b) => a + b, 0) / pl.length).toFixed(1),
    bestPct: Math.max(...pl),
    worstPct: Math.min(...pl),
  };
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  try {
    const symbol = String(req.query.symbol || "").toUpperCase();
    const tf = req.query.tf === "M" ? "M" : "W";
    const years = Math.min(Math.max(parseInt(req.query.years || "10", 10), 1), 20);
    if (!/^[A-Z0-9&\-]+$/.test(symbol)) {
      res.status(400).json({ error: "valid symbol required" });
      return;
    }
    const jwt = await angelLogin();
    const token = await findToken(jwt, symbol);
    const daily = await fetchDaily(jwt, token, years);
    const bars = aggregate(daily, tf);
    if (bars.length < WARMUP + 5) {
      res.status(200).json({ symbol, tf, bars: bars.length, trades: [], summary: null,
        note: "Not enough history for 20/50 EMA" });
      return;
    }
    const trades = runBacktest(bars, tf);
    res.status(200).json({ symbol, tf, bars: bars.length, trades, summary: summarize(trades) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
};
