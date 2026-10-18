"""20/50 EMA crossover on the MONTHLY chart: the 20-month EMA crossed ABOVE the 50-month EMA in the latest fully closed month AND that
monthly candle is green (close > open). Monthly candles are built from the daily prices in ./data/ (Angel One has no monthly candle);
the month that is still running is not counted, same as the Monthly Supertrend tab.
Stock list: the "Squeeze" tab of your Google Sheet (same list fetch_data.py downloads prices for).
Every stock found is KEPT in site/data/ema_cross_list.json until you delete it from the dashboard (its prices are refreshed every trading day).
Feeds the "20/50 Crossover" tab on the dashboard."""
import glob, io, json, os
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd, requests
from run_backtest import load, fix_splits
from fetch_data import SHEET_CSV

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
FAST, SLOW, MIN_BARS = 20, 50, 52            # 52 closed months = the 50-month average plus the two months needed to see a cross
IST = timezone(timedelta(hours=5, minutes=30))


def read_sheet():
    """Returns {file_name: (symbol, company, industry)} for the EQ rows of the Squeeze tab, or {} if the sheet can't be read."""
    try:
        r = requests.get(SHEET_CSV, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        if "Series" in df.columns:
            df = df[df["Series"].astype(str).str.strip() == "EQ"]
        out = {}
        for _, x in df.dropna(subset=["Symbol"]).iterrows():
            s = str(x["Symbol"]).strip()
            out[s.replace("&", "_")] = (s, str(x.get("Company Name", "")).strip(), str(x.get("Industry", "")).strip())
        print(f"::notice::Stock list read from the Squeeze tab: {len(out)} stocks")
        return out
    except Exception as ex:
        print(f"::warning::Could not read the Squeeze tab ({ex}); scanning every price file found")
        return {}


LIST = f"{OUT_DIR}/ema_cross_list.json"


def monthly(d, now):
    """Daily -> closed monthly candles (Open/High/Low/Close/Volume + the last trading day of each month)."""
    g = d.groupby(d.index.to_period("M"))
    m = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(), "Close": g["Close"].last(),
                      "Volume": g["Volume"].sum(), "last_day": d.index.to_series().groupby(d.index.to_period("M")).max()})
    return m[m.index < pd.Period(now, "M")]                  # the running month is not a finished candle


def main():
    now = datetime.now(IST).replace(tzinfo=None)
    sheet = read_sheet()
    state = json.load(open(LIST)) if os.path.exists(LIST) else {}
    if state.get("timeframe") != "monthly":                  # the old list held DAILY crossovers - start fresh for the monthly strategy
        state = {}
    tracked = {s["symbol"]: s for s in state.get("stocks", [])}
    removed = state.get("removed", {})                      # symbol -> signal date the user deleted (so it is not re-added)
    metrics, cands, last_date, scanned = {}, [], None, 0
    diag_any, diag_green, above_now = {}, {}, 0               # context numbers: how many stocks crossed in each recent month
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        key = os.path.basename(f)[:-4]
        if key.startswith("_") or (sheet and key not in sheet):
            continue
        df = load(f)
        if len(df) < 250:
            continue
        d = fix_splits(df, key, []).copy()
        m = monthly(d, now)
        if len(m) < MIN_BARS:
            continue
        scanned += 1
        last_date = d.index[-1] if last_date is None else max(last_date, d.index[-1])
        c = m["Close"]
        e20, e50 = c.ewm(span=FAST, adjust=False).mean(), c.ewm(span=SLOW, adjust=False).mean()
        sym, name, ind = sheet.get(key, (key, "", ""))
        dc = d["Close"]
        cx, gr = (e20 > e50) & (e20.shift() <= e50.shift()), c > m["Open"]
        for p in m.index[-8:]:
            if cx[p]:
                diag_any[str(p)] = diag_any.get(str(p), 0) + 1
                diag_green[str(p)] = diag_green.get(str(p), 0) + int(bool(gr[p]))
        above_now += int(e20.iloc[-1] > e50.iloc[-1])
        metrics[sym] = dict(date=d.index[-1], last_close=round(float(dc.iloc[-1]), 2), day_pct=round(100 * (float(dc.iloc[-1]) / float(dc.iloc[-2]) - 1), 2),
                            ema20=round(float(e20.iloc[-1]), 2), ema50=round(float(e50.iloc[-1]), 2), above=bool(e20.iloc[-1] > e50.iloc[-1]))
        if e20.iloc[-1] > e50.iloc[-1] and e20.iloc[-2] <= e50.iloc[-2] and c.iloc[-1] > m["Open"].iloc[-1]:
            vol_avg = m["Volume"].iloc[-7:-1].mean()
            cands.append((sym, dict(month=m.index[-1], sdate=str(m["last_day"].iloc[-1].date()), company=name, industry=ind, daily_last=d.index[-1],
                                    close=round(float(c.iloc[-1]), 2), open=round(float(m["Open"].iloc[-1]), 2),
                                    chg=round(100 * (float(c.iloc[-1]) / float(c.iloc[-2]) - 1), 2),
                                    volx=round(float(m["Volume"].iloc[-1] / vol_avg), 1) if vol_avg and np.isfinite(vol_avg) and vol_avg > 0 else None)))
        metrics[sym]["month"] = m.index[-1]
    latest_month = max((v["month"] for v in metrics.values()), default=None)
    new = 0
    for sym, x in cands:
        if x["month"] != latest_month or x["daily_last"] != last_date:          # stale / suspended stock
            continue
        if removed.get(sym) == x["sdate"] or (sym in tracked and tracked[sym].get("signal_date") == x["sdate"]):
            continue
        tracked[sym] = dict(symbol=sym, tv=sym.replace("&", "_").replace("-", "_"), company=x["company"], industry=x["industry"],
                            signal_date=x["sdate"], signal_month=str(x["month"]), signal_close=x["close"], signal_open=x["open"],
                            signal_day_pct=x["chg"], signal_volx=x["volx"])
        new += 1
    for sym, e in tracked.items():                             # refresh prices of everything we are keeping
        m = metrics.get(sym)
        if m:
            e.update(last_date=str(m["date"].date()), last_close=m["last_close"], day_pct=m["day_pct"], ema20=m["ema20"], ema50=m["ema50"], above=m["above"],
                     since_pct=round(100 * (m["last_close"] / e["signal_close"] - 1), 2))
    stocks = sorted(tracked.values(), key=lambda e: (e["signal_date"], e.get("since_pct") or 0), reverse=True)
    latest_signal = max((x["sdate"] for _, x in cands if x["month"] == latest_month), default=None)
    json.dump(dict(timeframe="monthly", updated=datetime.now(IST).strftime("%d %b %Y %H:%M"), data_date=str(last_date.date()) if last_date is not None else state.get("data_date"),
                   latest_month=str(latest_month) if latest_month is not None else None, latest_signal=latest_signal,
                   stocks_scanned=scanned, new_today=new, stocks=stocks, removed=removed,
                   diag=dict(crossed_any_candle_by_month=dict(sorted(diag_any.items())), crossed_green_by_month=dict(sorted(diag_green.items())), above_now=above_now)), open(LIST, "w"))
    print(f"{new} new monthly 20/50 crossovers (green candle) for {latest_month}, {len(stocks)} stocks kept in the list, {scanned} scanned (data up to {last_date})")


if __name__ == "__main__":
    main()
