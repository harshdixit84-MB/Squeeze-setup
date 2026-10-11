"""Viru Trend Dynamics (VTD) BUY signals on the 4-hour chart - a Python copy of your Pine script's logic (default settings, long side).
Signal = confirmed 4H candle close where the trend slope is up and the close is above the upper band, while no virtual trade is open.
Each signal becomes a virtual trade (entry = that close, stop, 1:3 target, ATR trailing stop) and stays in site/data/vtd_list.json until you delete it;
its status (OPEN / TARGET / SL / TRAIL ...) and live stop are refreshed on every scan.
Prices: ./data4h/ (1-hour candles joined into 4-hour bars). Stock list: the Squeeze tab of your Google Sheet."""
import glob, json, math, os
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd
from run_backtest import fix_splits
from scan_ema_cross import read_sheet

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data4h"), os.environ.get("OUT_DIR", "site/data")
LIST = f"{OUT_DIR}/vtd_list.json"
IST = timezone(timedelta(hours=5, minutes=30))
# --- VTD settings (same defaults as the Pine script) ---
TREND_LEN, OFFSET, SIGMA, DEV_LEN, BAND, SLOPE_LEN, MIN_SLOPE, ATR_LEN = 34, 0.85, 6.0, 34, 0.65, 3, 0.08, 14
STOP_LOOKBACK, MIN_ATR, MAX_ATR = 12, 0.75, 3.0
RR, TRAIL_ON, ACTIVATION_R, TRAIL_MULT = 3.0, True, 2.0, 2.0
LONG_ONLY, TICK, MIN_BARS, CATCH_UP_BARS = True, 0.05, 100, 4


def to_4h(df, now):
    """1-hour candles -> NSE 4-hour bars (09:15-13:15 and 13:15-15:30); drops a bar that has not finished yet."""
    t = df["date"]
    mins = np.where(t.dt.hour * 60 + t.dt.minute < 13 * 60 + 15, 9 * 60 + 15, 13 * 60 + 15)
    df = df.assign(bar=t.dt.normalize() + pd.to_timedelta(mins, unit="m"))
    g = df.groupby("bar").agg(Open=("open", "first"), High=("high", "max"), Low=("low", "min"), Close=("close", "last"), Volume=("volume", "sum"))
    g.index.name = "date"
    end = pd.Series([s + (pd.Timedelta(hours=4) if s.hour == 9 else pd.Timedelta(hours=2, minutes=15)) for s in g.index], index=g.index)
    return g[end <= now]


def alma(x, length, offset, sigma):
    m, s = math.floor(offset * (length - 1)), length / sigma
    w = np.exp(-((np.arange(length) - m) ** 2) / (2 * s * s))
    w /= w.sum()
    out = np.full(len(x), np.nan)
    for t in range(length - 1, len(x)):
        out[t] = np.dot(x[t - length + 1:t + 1], w)
    return out


def rma(x, n):
    out, a = np.full(len(x), np.nan), 1.0 / n
    if len(x) >= n:
        out[n - 1] = np.mean(x[:n])
        for i in range(n, len(x)):
            out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def simulate(d):
    """Walks the bars exactly like the Pine script (confirmed closes, stop checked before trailing). Returns (trades, indicator dict)."""
    o, h, l, c = (d[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    n = len(c)
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(prev_c), h - l, np.maximum(h - l, np.maximum(abs(h - prev_c), abs(l - prev_c))))
    atr = rma(tr, ATR_LEN)
    basis = alma(c, TREND_LEN, OFFSET, SIGMA)
    dev = pd.Series(c).rolling(DEV_LEN).std(ddof=0).to_numpy()
    upper, lower = basis + dev * BAND, basis - dev * BAND
    prior_low = pd.Series(l).rolling(STOP_LOOKBACK).min().shift(1).to_numpy()
    prior_high = pd.Series(h).rolling(STOP_LOOKBACK).max().shift(1).to_numpy()
    times = [str(x)[:16] for x in d.index]
    trades, pos = [], None
    for t in range(n):
        exited = False
        if pos is not None and t > pos["i"]:
            hit_stop, hit_target = l[t] <= pos["stop"], h[t] >= pos["target"]
            ex = None
            if hit_stop:
                ex = ("TRAIL SL" if pos["trailed"] else "SL", min(o[t], pos["stop"]))
            elif hit_target:
                ex = ("TARGET", pos["target"])
            else:
                pos["extreme"] = max(pos["extreme"], h[t])
                if TRAIL_ON and (pos["extreme"] - pos["entry"]) / pos["risk"] >= ACTIVATION_R:
                    pos["trail_active"] = True
                nxt = pos["stop"]
                if pos["trail_active"]:
                    cand = math.floor((pos["extreme"] - atr[t] * TRAIL_MULT) / TICK + 1e-9) * TICK
                    nxt = max(pos["stop"], cand)
                if c[t] <= nxt:
                    ex = ("TRAIL CLOSE", c[t])
                elif nxt > pos["stop"]:
                    pos["trailed"], pos["stop"] = True, nxt
            if ex:
                pos.update(status=ex[0], exit_price=float(ex[1]), exit_time=times[t], exit_i=t)
                trades.append(pos)
                pos, exited = None, True
        ready = not (np.isnan(basis[t - SLOPE_LEN]) if t >= SLOPE_LEN else True) and not np.isnan(dev[t]) and not np.isnan(prior_low[t]) and atr[t] > 0
        if ready:
            slope = (basis[t] - basis[t - SLOPE_LEN]) / atr[t]
            bull = slope > MIN_SLOPE and c[t] > upper[t]
            if pos is None and not exited and bull:
                r = min(max(c[t] - prior_low[t], MIN_ATR * atr[t]), MAX_ATR * atr[t])
                r = max(TICK, math.ceil(r / TICK - 1e-9) * TICK)
                pos = dict(i=t, time=times[t], entry=float(c[t]), risk=float(r), stop=float(c[t] - r), sl0=float(c[t] - r), target=float(c[t] + r * RR),
                           atr=float(atr[t]), extreme=float(c[t]), trailed=False, trail_active=False, status="OPEN")
    if pos is not None:
        trades.append(pos)
    return trades, dict(n=n, times=times, last_close=float(c[-1]))


def main():
    now = datetime.now(IST).replace(tzinfo=None)
    sheet = read_sheet()
    state = json.load(open(LIST)) if os.path.exists(LIST) else {}
    tracked = {f"{s['symbol']}|{s['signal_time']}": s for s in state.get("stocks", [])}
    removed = state.get("removed", {})
    scanned, new, last_bar, notes, errors = 0, 0, None, [], []
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        key = os.path.basename(f)[:-4]
        if key.startswith("_") or (sheet and key not in sheet):
            continue
        try:
            d = to_4h(pd.read_csv(f, parse_dates=["date"]), now)
            if len(d) < MIN_BARS:
                continue
            d = fix_splits(d, key, notes)
            trades, ind = simulate(d)
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__}: {e}")
            continue
        scanned += 1
        sym, name, industry = sheet.get(key, (key, "", ""))
        last_bar = ind["times"][-1] if last_bar is None else max(last_bar, ind["times"][-1])
        for tr in trades:
            k = f"{sym}|{tr['time']}"
            fresh = tr["i"] >= ind["n"] - CATCH_UP_BARS                # signal on one of the latest candles
            if k not in tracked:
                if not fresh or tr["time"] <= removed.get(sym, ""):
                    continue
                tracked[k] = dict(symbol=sym, tv=sym.replace("&", "_").replace("-", "_"), company=name, industry=industry, signal_time=tr["time"])
                new += 1
            closed = tr["status"] != "OPEN"
            ref = tr["exit_price"] if closed else ind["last_close"]
            tracked[k].update(entry=round(tr["entry"], 2), sl0=round(tr["sl0"], 2), target=round(tr["target"], 2), risk_pct=round(100 * tr["risk"] / tr["entry"], 2),
                              atr=round(tr["atr"], 2), status=tr["status"], cur_sl=round(tr["stop"], 2), last_close=round(ind["last_close"], 2), last_time=ind["times"][-1],
                              exit_price=round(tr["exit_price"], 2) if closed else None, exit_time=tr.get("exit_time"),
                              pnl_pct=round(100 * (ref / tr["entry"] - 1), 2), pnl_r=round((ref - tr["entry"]) / tr["risk"], 2), trail_active=tr["trail_active"])
    stocks = sorted(tracked.values(), key=lambda e: e["signal_time"], reverse=True)
    out = dict(updated=datetime.now(IST).strftime("%d %b %Y %H:%M IST"), data_time=last_bar, stocks_scanned=scanned, new_count=new, stocks=stocks, removed=removed,
               log=[f"{new} new signals, {len(stocks)} kept, {scanned} scanned, last 4H bar {last_bar}"] + errors[:10] + [f"split fixes: {len(notes)}"])
    json.dump(out, open(LIST, "w"))
    print(out["log"][0], "|", len(errors), "errors")


if __name__ == "__main__":
    main()
