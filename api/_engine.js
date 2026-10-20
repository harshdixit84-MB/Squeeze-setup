"use strict";
// Backtest engine (no network). The file name starts with "_" so Vercel does not publish it as an endpoint.
//
// Rules (agreed with the user):
//  - Signal : 20 EMA closes above 50 EMA on a COMPLETE weekly (or monthly) bar, after being at/below it the bar before.
//  - Entry  : open of the next bar (first trading day of the next week / month).
//  - Exit   : whichever hits first, checked on DAILY candles - stop = entry -20%, target = entry +30%.
//  - What-if: after the +30% exit, keep watching - entry -20% first => "No (SL hit)", entry +70% first => "Yes" (trail target hit).
//  - Next trade only on a fresh crossover after the trade has closed. Trades are numbered 1, 2, 3 ...
// When stop and target are both inside ONE daily candle the order is unknowable, so the stop is assumed first (conservative).

const STOP_PCT = 0.2, TARGET_PCT = 0.3, TRAIL_PCT = 0.7, WARMUP = 50;
const DAY = 86400000;
const ms = (s) => Date.parse(s + "T00:00:00Z");

function istNow(now = Date.now()) {
  const d = new Date(now + 5.5 * 3600000);
  return { date: d.toISOString().slice(0, 10), minutes: d.getUTCHours() * 60 + d.getUTCMinutes(), dow: d.getUTCDay() };
}

function periodKey(dateStr, tf) {
  if (tf === "M") return dateStr.slice(0, 7);
  const d = new Date(ms(dateStr));
  return new Date(d.getTime() - ((d.getUTCDay() + 6) % 7) * DAY).toISOString().slice(0, 10); // back to Monday
}

// whole weeks (or months) between two dates
function elapsed(a, b, tf) {
  if (tf === "M") {
    const [ya, ma] = a.slice(0, 7).split("-").map(Number), [yb, mb] = b.slice(0, 7).split("-").map(Number);
    return (yb - ya) * 12 + (mb - ma);
  }
  return Math.round((ms(periodKey(b, "W")) - ms(periodKey(a, "W"))) / (7 * DAY));
}

// Angel prices may not be adjusted for splits/bonus: a >25% overnight gap is treated as one and earlier prices are scaled
// (same rule as backtest/run_backtest.py).
function fixSplits(daily) {
  const rows = daily.map((d) => ({ ...d }));
  const notes = [];
  for (let pass = 0; pass < 20; pass++) {
    let k = -1, f = 1;
    for (let i = 1; i < rows.length; i++) {
      const g = rows[i].open / rows[i - 1].close;
      if (g < 0.75 || g > 1.35) { k = i; f = g; break; }
    }
    if (k < 0) break;
    for (let i = 0; i < k; i++) {
      rows[i].open *= f; rows[i].high *= f; rows[i].low *= f; rows[i].close *= f;
    }
    notes.push({ date: rows[k].date, overnightChangePct: +((f - 1) * 100).toFixed(1) });
  }
  return { rows, notes };
}

function aggregate(daily, tf, today) {
  const bars = [];
  let cur = null;
  daily.forEach((d, k) => {
    const key = periodKey(d.date, tf);
    if (!cur || cur.key !== key) {
      cur = { key, date: d.date, open: d.open, high: d.high, low: d.low, close: d.close, d0: k, d1: k };
      bars.push(cur);
    } else {
      cur.high = Math.max(cur.high, d.high);
      cur.low = Math.min(cur.low, d.low);
      cur.close = d.close;
      cur.d1 = k;
    }
  });
  const nowKey = periodKey(today.date, tf);
  const weekend = tf === "W" && (today.dow === 0 || today.dow === 6);
  bars.forEach((b) => { b.complete = b.key < nowKey || weekend; });   // the running week / month is not complete
  return bars;
}

function ema(values, span) {
  const a = 2 / (span + 1);
  const out = [values[0]];
  for (let i = 1; i < values.length; i++) out.push(a * values[i] + (1 - a) * out[i - 1]);
  return out;
}

// walks daily candles from the entry day; returns where the trade ended
function simulate(daily, k0, entry) {
  const stop = entry * (1 - STOP_PCT), target = entry * (1 + TARGET_PCT);
  for (let k = k0; k < daily.length; k++) {
    const c = daily[k];
    if (k > k0) {
      if (c.open <= stop) return { k, price: c.open, reason: "Stop -20% (gap)" };
      if (c.open >= target) return { k, price: c.open, reason: "Target +30% (gap)" };
    }
    const hitStop = c.low <= stop, hitTarget = c.high >= target;
    if (hitStop && hitTarget) return { k, price: stop, reason: "Stop -20% (same day as target)" };
    if (hitStop) return { k, price: stop, reason: "Stop -20%" };
    if (hitTarget) return { k, price: target, reason: "Target +30%" };
  }
  return null;
}

// after the +30% exit: does price reach +70% (trail target) before falling to entry -20% (stoploss)?
function whatIf(daily, from, entry) {
  const stop = entry * (1 - STOP_PCT), goal = entry * (1 + TRAIL_PCT);
  for (let k = from; k < daily.length; k++) {
    const c = daily[k];
    if (k > from) {
      if (c.open <= stop) return { result: "SL", k };
      if (c.open >= goal) return { result: "Trail", k };
    }
    const hitStop = c.low <= stop, hitGoal = c.high >= goal;
    if (hitStop) return { result: "SL", k };        // also when both in one day: stop first
    if (hitGoal) return { result: "Trail", k };
  }
  return { result: "Running", k: null };
}

// daily: [{date:'YYYY-MM-DD', open, high, low, close}] ascending, already split-fixed.
// opts: { today: istNow()-style object, from: 'YYYY-MM-DD' (only trades entered on/after this date are reported) }
function runBacktest(daily, tf, opts = {}) {
  const today = opts.today || istNow();
  const bars = aggregate(daily, tf, today);
  const nc = bars.filter((b) => b.complete).length;           // complete bars are always a prefix
  const closes = bars.slice(0, nc).map((b) => b.close);
  if (nc < WARMUP + 2) return { trades: [], bars: nc };
  const fast = ema(closes, 20), slow = ema(closes, 50);
  const cross = (i) => i >= WARMUP && i < nc && fast[i] > slow[i] && !(fast[i - 1] > slow[i - 1]);
  const unit = tf === "M" ? "months" : "weeks";
  const trades = [];
  let i = 0;
  while (i < nc) {
    if (!cross(i)) { i++; continue; }
    const eb = bars[i + 1];
    if (!eb) break;                                            // signal on the last bar, entry bar has not started yet
    const k0 = eb.d0, entry = daily[k0].open;
    let x = simulate(daily, k0, entry), open = false;
    if (!x) { x = { k: daily.length - 1, price: daily[daily.length - 1].close, reason: "Open (marked at last close)" }; open = true; }

    let peak = 0;
    for (let k = k0; k <= x.k; k++) peak = Math.max(peak, daily[k].high);
    let reached70 = "-", barsTo70 = null;
    if (x.reason.startsWith("Target")) {
      const w = whatIf(daily, x.k, entry);
      if (w.result === "Trail") { reached70 = "Yes"; barsTo70 = elapsed(daily[k0].date, daily[w.k].date, tf); }
      else reached70 = w.result === "SL" ? "No (SL hit)" : "Running";
    }
    trades.push({
      entryDate: daily[k0].date,
      entryPrice: +entry.toFixed(2),
      exitDate: daily[x.k].date,
      exitPrice: +x.price.toFixed(2),
      reason: x.reason,
      peakGainPct: +((peak / entry - 1) * 100).toFixed(1),
      plPct: +((x.price / entry - 1) * 100).toFixed(1),
      plRs: +(x.price - entry).toFixed(2),
      duration: elapsed(daily[k0].date, daily[x.k].date, tf),
      durationUnit: unit,
      reached70,
      barsTo70,
    });
    if (open) break;
    // next signal: a fresh crossover on or after the bar in which the trade closed (the signal is only known at that bar's close)
    i = bars.findIndex((b) => b.d0 <= x.k && x.k <= b.d1);
  }
  const shown = trades.filter((t) => !opts.from || t.entryDate >= opts.from);
  shown.forEach((t, n) => { t.trade = n + 1; });
  return { trades: shown.map((t) => ({ trade: t.trade, ...t })), bars: nc };
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

module.exports = { istNow, periodKey, elapsed, fixSplits, aggregate, ema, simulate, whatIf, runBacktest, summarize, WARMUP };
