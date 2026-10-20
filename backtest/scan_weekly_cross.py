"""Weekly 20/50 EMA crossover list for the "Weekly Crossover" tab on the dashboard.
Weekly candles are built from the daily prices in ./data/ (downloaded by fetch_data.py from Angel One). Only closed weeks are used for the EMAs.
For every stock of the Squeeze list:
  * take the LATEST weekly crossover (20 EMA closes above 50 EMA after being at/below it the week before)
  * entry = open of the first trading day of the next week (same as the Calculate backtest)
  * SKIP the stock once any daily close since the entry is +30% or more above the entry, or -20% or more below it
  * otherwise the setup is still active and the stock is listed; it is "fresh" when the crossover happened in the last closed week
    (if that next week has no price yet, the entry is still pending: "entry at next week's open")
The list is rebuilt from scratch on every run (nothing to delete by hand): a stock leaves it by itself when it hits +30% / -20%."""
import glob, json, os
from datetime import datetime, timedelta, timezone
import pandas as pd
from run_backtest import load, fix_splits
from scan_ema_cross import read_sheet

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
FAST, SLOW, WARMUP = 20, 50, 50                 # same warm-up as api/_engine.js
TARGET_PCT, STOP_PCT = 0.30, 0.20
IST = timezone(timedelta(hours=5, minutes=30))


def weekly(d):
    """Daily -> weekly candles keyed by the Monday of the week, plus the first / last trading day of each week."""
    key = (d.index - pd.to_timedelta(d.index.weekday, unit="D")).normalize()
    g = d.groupby(key)
    w = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(), "Close": g["Close"].last()})
    w["first_day"] = d.index.to_series().groupby(key).min()
    w["last_day"] = d.index.to_series().groupby(key).max()
    return w


def analyse(d, today):
    """Returns (status, info). status: 'no_signal' | 'hit_target' | 'hit_stop' | 'active'."""
    w = weekly(d)
    now_key = pd.Timestamp(today.date()) - timedelta(days=today.weekday())
    last_day = d.index[-1]
    # the running week counts as closed once its Friday candle (or a weekend) has come
    running_done = today.weekday() >= 5 or last_day.weekday() == 4
    complete = [(k < now_key) or running_done for k in w.index]
    nc = sum(complete)                                  # closed weeks are always a prefix
    if nc < WARMUP + 2:
        return "no_signal", None
    c = w["Close"].iloc[:nc]
    e20, e50 = c.ewm(span=FAST, adjust=False).mean(), c.ewm(span=SLOW, adjust=False).mean()
    cross = [i for i in range(WARMUP, nc) if e20.iloc[i] > e50.iloc[i] and not e20.iloc[i - 1] > e50.iloc[i - 1]]
    if not cross:
        return "no_signal", None
    i = cross[-1]
    info = dict(signal_week=str(w.index[i].date()), signal_date=str(w["last_day"].iloc[i].date()), fresh=(i == nc - 1),
                last_date=str(last_day.date()), last_close=round(float(d["Close"].iloc[-1]), 2),
                ema20=round(float(e20.iloc[-1]), 2), ema50=round(float(e50.iloc[-1]), 2))
    if i + 1 >= len(w):                                 # the entry week has no candle yet
        info.update(entry_date=None, entry_price=None, since_pct=None, target_price=None, stop_price=None)
        return "active", info
    entry_day = w["first_day"].iloc[i + 1]
    entry = float(d.loc[entry_day, "Open"])
    closes = d.loc[entry_day:, "Close"]
    info.update(entry_date=str(entry_day.date()), entry_price=round(entry, 2), target_price=round(entry * (1 + TARGET_PCT), 2),
                stop_price=round(entry * (1 - STOP_PCT), 2), since_pct=round((info["last_close"] / entry - 1) * 100, 2))
    if (closes >= entry * (1 + TARGET_PCT)).any():
        return "hit_target", info
    if (closes <= entry * (1 - STOP_PCT)).any():
        return "hit_stop", info
    return "active", info


def main():
    today = datetime.now(IST).replace(tzinfo=None)
    sheet = read_sheet()
    found, counts, last_date, scanned = [], {"no_signal": 0, "hit_target": 0, "hit_stop": 0, "active": 0}, None, 0
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        key = os.path.basename(f)[:-4]
        if key.startswith("_") or (sheet and key not in sheet):
            continue
        df = load(f)
        if len(df) < 250:
            continue
        d = fix_splits(df, key, [])
        scanned += 1
        last_date = d.index[-1] if last_date is None else max(last_date, d.index[-1])
        status, info = analyse(d, today)
        sym, name, ind = sheet.get(key, (key, "", ""))
        found.append((status, d.index[-1], dict(symbol=sym, tv=sym.replace("&", "_").replace("-", "_"), company=name, industry=ind, **(info or {}))))
    stocks = []
    for status, ld, row in found:
        if status != "no_signal" and ld != last_date:   # stale / suspended stock: its last price is old, so the skip rule cannot be trusted
            continue
        counts[status] += 1
        if status == "active":
            stocks.append(row)
    stocks.sort(key=lambda r: r["symbol"])                              # three stable sorts: A-Z, then newest signal first, then fresh ones on top
    stocks.sort(key=lambda r: r["signal_week"], reverse=True)
    stocks.sort(key=lambda r: not r["fresh"])
    out = dict(updated=datetime.now(IST).strftime("%d %b %Y %H:%M"), data_date=str(last_date.date()) if last_date is not None else None,
               scanned=scanned, active=counts["active"], fresh=sum(1 for r in stocks if r["fresh"]), skipped_target=counts["hit_target"],
               skipped_stop=counts["hit_stop"], no_signal=counts["no_signal"], stocks=stocks)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(out, open(f"{OUT_DIR}/weekly_cross_list.json", "w"))
    print(f"Weekly crossover: {scanned} scanned, {len(stocks)} active ({out['fresh']} fresh), skipped {counts['hit_target']} at +30% and {counts['hit_stop']} at -20% (data up to {last_date})")


if __name__ == "__main__":
    main()
