"""Finds stocks where the 20 EMA crossed ABOVE the 50 EMA on the LATEST trading day AND that day is a green candle (close > open).
Stock list: the "Squeeze" tab of your Google Sheet (same list fetch_data.py downloads prices for).
Every stock found is KEPT in site/data/ema_cross_list.json until you delete it from the dashboard (its prices are refreshed daily).
Reads prices from ./data/ for the "20/50 Crossover" tab on the dashboard."""
import glob, io, json, os
from datetime import datetime
import numpy as np, pandas as pd, requests
from run_backtest import load, fix_splits
from fetch_data import SHEET_CSV

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
FAST, SLOW, MIN_BARS = 20, 50, 100


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


def main():
    sheet = read_sheet()
    state = json.load(open(LIST)) if os.path.exists(LIST) else {}
    tracked = {s["symbol"]: s for s in state.get("stocks", [])}
    removed = state.get("removed", {})                      # symbol -> signal date the user deleted (so it is not re-added)
    metrics, cands, last_date, scanned = {}, [], None, 0
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        key = os.path.basename(f)[:-4]
        if key.startswith("_") or (sheet and key not in sheet):
            continue
        df = load(f)
        if len(df) < MIN_BARS:
            continue
        d = fix_splits(df, key, []).copy()
        scanned += 1
        last_date = d.index[-1] if last_date is None else max(last_date, d.index[-1])
        c = d["Close"]
        e20, e50 = c.ewm(span=FAST, adjust=False).mean(), c.ewm(span=SLOW, adjust=False).mean()
        sym, name, ind = sheet.get(key, (key, "", ""))
        vol_avg = d["Volume"].iloc[-21:-1].mean()
        m = dict(date=d.index[-1], last_close=round(float(c.iloc[-1]), 2), day_pct=round(100 * (float(c.iloc[-1]) / float(c.iloc[-2]) - 1), 2),
                 ema20=round(float(e20.iloc[-1]), 2), ema50=round(float(e50.iloc[-1]), 2), above=bool(e20.iloc[-1] > e50.iloc[-1]),
                 volx=round(float(d["Volume"].iloc[-1] / vol_avg), 1) if vol_avg and np.isfinite(vol_avg) and vol_avg > 0 else None,
                 open=round(float(d["Open"].iloc[-1]), 2), company=name, industry=ind)
        metrics[sym] = m
        if e20.iloc[-1] > e50.iloc[-1] and e20.iloc[-2] <= e50.iloc[-2] and c.iloc[-1] > d["Open"].iloc[-1]:
            cands.append((sym, m))
    new = 0
    for sym, m in cands:
        if m["date"] != last_date:                             # stale / suspended stock
            continue
        sd = str(m["date"].date())
        if removed.get(sym) == sd or (sym in tracked and tracked[sym].get("signal_date") == sd):
            continue
        tracked[sym] = dict(symbol=sym, tv=sym.replace("&", "_").replace("-", "_"), company=m["company"], industry=m["industry"],
                            signal_date=sd, signal_close=m["last_close"], signal_open=m["open"], signal_day_pct=m["day_pct"], signal_volx=m["volx"])
        new += 1
    for sym, e in tracked.items():                             # refresh prices of everything we are keeping
        m = metrics.get(sym)
        if m:
            e.update(last_date=str(m["date"].date()), last_close=m["last_close"], day_pct=m["day_pct"], ema20=m["ema20"], ema50=m["ema50"], above=m["above"],
                     since_pct=round(100 * (m["last_close"] / e["signal_close"] - 1), 2))
    stocks = sorted(tracked.values(), key=lambda e: (e["signal_date"], e.get("since_pct") or 0), reverse=True)
    json.dump(dict(updated=datetime.now().strftime("%d %b %Y %H:%M"), data_date=str(last_date.date()) if last_date is not None else state.get("data_date"),
                   stocks_scanned=scanned, new_today=new, stocks=stocks, removed=removed), open(LIST, "w"))
    print(f"{new} new 20/50 crossovers (green candle) on the latest day, {len(stocks)} stocks kept in the list, {scanned} scanned (data up to {last_date})")


if __name__ == "__main__":
    main()
