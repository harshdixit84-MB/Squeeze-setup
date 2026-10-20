"""Monthly Supertrend (10, 3) flips for the "Monthly Supertrend" tab on the dashboard.
Monthly candles are built from the daily prices in ./data/ (downloaded by fetch_data.py from Angel One). Only fully closed months are used.
Signal = Supertrend turned GREEN (or RED) on the last closed month.
  "SP pullback": a kept stock whose trend is still the same colour and whose price is back within 3% of the Supertrend line
  green flip: skipped if that month's candle is a hammer or has a long upper wick
  red flip  : mirror image - skipped if it is a shooting star or has a long lower wick
Every stock found is KEPT in site/data/supertrend_list.json until you delete it (or tick Buy, which moves it to the positions table).
Prices, 52-week high/low and open-position prices are refreshed on every run. Stock list: the Squeeze tab of your Google Sheet."""
import glob, json, os
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd
from run_backtest import load, fix_splits
from scan_ema_cross import read_sheet
from fetch_data import SHEET_CSV
import io, requests

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
LIST, POS = f"{OUT_DIR}/supertrend_list.json", f"{OUT_DIR}/st_positions.json"
ATR_LEN, MULT, MIN_MONTHS = 10, 3.0, 14
MONTHS_BACK = int(os.environ.get("MONTHS_BACK", "1"))     # 1 = only the last closed month; use 3 once to also pick up flips of the last 3 months
PULLBACK_PCT = 3.0                                        # "SP pullback": price is within 3% of the Supertrend line again
WICK_MAX = 0.40                                           # a wick longer than 40% of the candle's high-low range counts as "long"
HAMMER_RATIO = 2.0                                        # hammer / shooting star: long wick >= 2x the body, tiny wick on the other side
IST = timezone(timedelta(hours=5, minutes=30))


def supertrend(h, l, c, n=ATR_LEN, m=MULT):
    """Standard Supertrend (Wilder ATR, same as TradingView). Returns (direction array: 1 green / -1 red, line array)."""
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc))))
    atr = np.full(len(c), np.nan)
    if len(c) >= n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, len(c)):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    hl2 = (h + l) / 2
    ub, lb = hl2 + m * atr, hl2 - m * atr
    d, line = np.ones(len(c), int), np.full(len(c), np.nan)
    for i in range(n, len(c)):
        if i > n:
            if not (ub[i] < ub[i - 1] or c[i - 1] > ub[i - 1]): ub[i] = ub[i - 1]
            if not (lb[i] > lb[i - 1] or c[i - 1] < lb[i - 1]): lb[i] = lb[i - 1]
            d[i] = d[i - 1]
            if d[i - 1] == -1 and c[i] > ub[i - 1]: d[i] = 1
            elif d[i - 1] == 1 and c[i] < lb[i - 1]: d[i] = -1
        else:
            d[i] = 1 if c[i] > hl2[i] else -1
        line[i] = lb[i] if d[i] == 1 else ub[i]
    return d, line


def candle_ok(o, h, l, c, side):
    rng = h - l
    if rng <= 0:
        return False
    body, up, lo = abs(c - o), h - max(o, c), min(o, c) - l
    big = max(body, rng * 0.02)
    if side == "green":
        return up / rng <= WICK_MAX and not (lo >= HAMMER_RATIO * big and up / rng < 0.15)
    return lo / rng <= WICK_MAX and not (up >= HAMMER_RATIO * big and lo / rng < 0.15)


def monthly(d, now):
    """Daily -> closed monthly candles. Returns frame with Open/High/Low/Close and 'last_day' (last trading day of that month)."""
    g = d.groupby(d.index.to_period("M"))
    m = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(), "Close": g["Close"].last(),
                      "last_day": g.apply(lambda x: x.index.max())})
    return m[m.index < pd.Period(now, "M")]               # the month that is still running is not a candle yet


def last_close(sym):
    """(last close, its date) from the saved Angel One prices in ./data/, or None when there is no file for the stock."""
    path = f"{DATA_DIR}/{sym.replace('&', '_')}.csv"
    if not os.path.exists(path):
        return None
    try:
        d = pd.read_csv(path, usecols=["date", "close"]).dropna().tail(1)
        return round(float(d["close"].iloc[0]), 2), str(d["date"].iloc[0])[:10]
    except Exception:
        return None


def save_squeeze_page():
    """Writes the whole Squeeze tab (every row, any series) to site/data/squeeze_list.json for the "Squeeze List" page. Keeps the old file if the sheet can't be read.
    Each row also gets the last close ("price") and its date ("price_date") from ./data/; when a stock has no price file in this run
    (e.g. the morning refresh with no saved prices) the price already in the old file is kept instead of being wiped."""
    try:
        r = requests.get(SHEET_CSV, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text)).dropna(subset=["Symbol"])
        rows = [dict(symbol=str(x["Symbol"]).strip(), tv=str(x["Symbol"]).strip().replace("&", "_").replace("-", "_"),
                     company=str(x.get("Company Name", "")).strip(), industry=str(x.get("Industry", "")).strip(), series=str(x.get("Series", "")).strip())
                for _, x in df.iterrows()]
        try:
            old = {s["symbol"]: s for s in json.load(open(f"{OUT_DIR}/squeeze_list.json")).get("stocks", [])}
        except Exception:
            old = {}
        for row in rows:
            p = last_close(row["symbol"])
            if p is None and old.get(row["symbol"], {}).get("price") is not None:
                p = (old[row["symbol"]]["price"], old[row["symbol"]].get("price_date"))
            if p is not None:
                row["price"], row["price_date"] = p
        if len(rows) > 50:
            dates = [x["price_date"] for x in rows if x.get("price_date")]
            json.dump(dict(updated=datetime.now(IST).strftime("%d %b %Y %H:%M"), count=len(rows), price_date=max(dates) if dates else None, stocks=rows),
                      open(f"{OUT_DIR}/squeeze_list.json", "w"))
            print(f"Squeeze list page: {len(rows)} stocks, {len(dates)} with a price")
    except Exception as ex:
        print(f"::warning::Could not refresh the Squeeze list page ({ex})")


def main():
    save_squeeze_page()
    sheet = read_sheet()
    state = json.load(open(LIST)) if os.path.exists(LIST) else {}
    tracked = {(s["symbol"], s["side"]): s for s in state.get("stocks", [])}
    removed = state.get("removed", {})                    # "SYMBOL|side" -> signal date the user deleted (so it is not added back)
    now = datetime.now(IST).replace(tzinfo=None)
    metrics, found, scanned, last_date = {}, [], 0, None
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        key = os.path.basename(f)[:-4]
        if key.startswith("_") or (sheet and key not in sheet):
            continue
        df = load(f)
        if len(df) < 250:
            continue
        d = fix_splits(df, key, []).copy()
        scanned += 1
        last_date = d.index[-1] if last_date is None else max(last_date, d.index[-1])
        sym, name, ind = sheet.get(key, (key, "", ""))
        yr = d.tail(252)
        px = float(d["Close"].iloc[-1])
        m = monthly(d, now)
        if len(m) < MIN_MONTHS:
            continue
        dr, line = supertrend(m["High"].to_numpy(float), m["Low"].to_numpy(float), m["Close"].to_numpy(float))
        metrics[sym] = dict(date=d.index[-1], last_close=round(px, 2), high52=round(float(yr["High"].max()), 2),
                            low52=round(float(yr["Low"].min()), 2), st_now="green" if dr[-1] == 1 else "red", st_line=round(float(line[-1]), 2))
        for k in range(1, MONTHS_BACK + 1):
            i = len(m) - k
            if i <= ATR_LEN or dr[i] == dr[i - 1]:
                continue
            side = "green" if dr[i] == 1 else "red"
            r = m.iloc[i]
            if candle_ok(r["Open"], r["High"], r["Low"], r["Close"], side):
                found.append((sym, side, name, ind, r, str(m.index[i])))
    new = 0
    for sym, side, name, ind, r, month in found:
        sd = str(r["last_day"].date())
        if removed.get(f"{sym}|{side}") == sd or (sym, side) in tracked and tracked[(sym, side)]["signal_date"] == sd:
            continue
        tracked[(sym, side)] = dict(symbol=sym, tv=sym.replace("&", "_").replace("-", "_"), side=side, company=name, industry=ind, signal_month=month,
                                    signal_date=sd, signal_close=round(float(r["Close"]), 2), signal_open=round(float(r["Open"]), 2),
                                    signal_high=round(float(r["High"]), 2), signal_low=round(float(r["Low"]), 2))
        new += 1
    for (sym, side), e in tracked.items():                # refresh everything we are keeping
        m = metrics.get(sym)
        if m:
            e.update(last_date=str(m["date"].date()), last_close=m["last_close"], high52=m["high52"], low52=m["low52"], st_now=m["st_now"],
                     st_line=m["st_line"], since_pct=round(100 * (m["last_close"] / e["signal_close"] - 1), 2))
            # gap between price and the Supertrend line, measured on the trend side (green: price above the line, red: price below it)
            gap = 100 * ((m["last_close"] - m["st_line"]) if side == "green" else (m["st_line"] - m["last_close"])) / m["st_line"]
            e["gap_pct"] = round(gap, 2)
            e["pullback"] = bool(m["st_now"] == side and 0 <= gap <= PULLBACK_PCT)    # trend still the same colour and price is back near the line
    stocks = sorted(tracked.values(), key=lambda e: (e["signal_date"], e["symbol"]), reverse=True)
    json.dump(dict(updated=now.strftime("%d %b %Y %H:%M"), data_date=str(last_date.date()) if last_date is not None else state.get("data_date"),
                   stocks_scanned=scanned, new_count=new, stocks=stocks, removed=removed), open(LIST, "w"))
    if os.path.exists(POS):                               # keep open positions priced
        p = json.load(open(POS))
        for e in p.get("open", []):
            if e["symbol"] in metrics:
                e["last_close"] = metrics[e["symbol"]]["last_close"]
        p["updated"] = now.strftime("%d %b %Y %H:%M")
        json.dump(p, open(POS, "w"))
    print(f"{new} new monthly Supertrend flips, {len(stocks)} stocks kept in the list, {scanned} scanned (data up to {last_date})")


if __name__ == "__main__":
    main()
