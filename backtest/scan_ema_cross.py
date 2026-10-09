"""Finds stocks where the 20 EMA crossed ABOVE the 50 EMA on the LATEST trading day AND that day is a green candle (close > open).
Stock list: the "Squeeze" tab of your Google Sheet (same list fetch_data.py downloads prices for).
Reads prices from ./data/ and writes site/data/ema_cross_today.json for the "20/50 Crossover" tab on the dashboard."""
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


def main():
    sheet = read_sheet()
    rows, last_date, scanned = [], None, 0
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
        crossed = e20.iloc[-1] > e50.iloc[-1] and e20.iloc[-2] <= e50.iloc[-2]
        green = d["Close"].iloc[-1] > d["Open"].iloc[-1]
        if not (crossed and green):
            continue
        vol_avg = d["Volume"].iloc[-21:-1].mean()
        sym, name, ind = sheet.get(key, (key, "", ""))
        rows.append(dict(
            date=d.index[-1], symbol=sym, tv=sym.replace("&", "_").replace("-", "_"), company=name, industry=ind,
            open=round(float(d["Open"].iloc[-1]), 2), close=round(float(c.iloc[-1]), 2),
            day_pct=round(100 * (float(c.iloc[-1]) / float(c.iloc[-2]) - 1), 2),
            ema20=round(float(e20.iloc[-1]), 2), ema50=round(float(e50.iloc[-1]), 2),
            volx=round(float(d["Volume"].iloc[-1] / vol_avg), 1) if vol_avg and np.isfinite(vol_avg) and vol_avg > 0 else None))
    # only keep stocks whose last candle is the latest trading day (skips stale / suspended stocks)
    rows = [r for r in rows if r["date"] == last_date]
    for r in rows:
        r["date"] = str(r["date"].date())
    rows.sort(key=lambda r: -r["day_pct"])
    json.dump(dict(updated=datetime.now().strftime("%d %b %Y %H:%M"), data_date=str(last_date.date()) if last_date is not None else None,
                   stocks_scanned=scanned, stocks=rows), open(f"{OUT_DIR}/ema_cross_today.json", "w"))
    print(f"{len(rows)} stocks crossed 20 EMA above 50 EMA on a green candle, out of {scanned} scanned (data up to {last_date})")


if __name__ == "__main__":
    main()
