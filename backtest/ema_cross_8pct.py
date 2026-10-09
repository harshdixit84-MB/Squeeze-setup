"""EMA 20/50 cross backtest on the prices saved in ./data/ . Writes simple tables to site/data/ema_cross_*.json
SETUP day: the 20-day average (EMA) crosses above the 50-day average AND that day is a green candle (close > open).
ENTRY: next day's open.
TARGET: 8% above the entry price ("HIT"). If the day's high reaches it, the trade is closed there (or at the open if it gaps above).
STOPLOSS: a daily close below the 50-day average -> sell at the next day's open ("STOP").
One trade per stock at a time (a new cross is ignored while a trade is open). Cost 0.2% per round trip.
Same rules as ema_cross_8pct_strategy.pine, so the TradingView chart and this table can be compared."""
import glob, io, json, os
from datetime import datetime
import numpy as np, pandas as pd
from run_backtest import load, fix_splits, COST, YEARS

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
FAST, SLOW, LONG, TARGET, WARMUP = 20, 50, 200, 0.08, 100
HIT_WITHIN = (5, 10, 20, 40)


def split_groups(sheet):
    """The Google Sheet lists the midcap names A-Z first, then the smallcap names A-Z again.
    The point where the list jumps from Y/Z back to A is the split. Returns {file_name: 'Midcap'|'Smallcap'}."""
    names = sheet["Company Name"].astype(str).str.strip().str.lower().tolist()
    syms = sheet["Symbol"].astype(str).str.strip().tolist()
    cut = next((i for i in range(1, len(names)) if names[i][:1] == "a" and names[i - 1][:1] in ("y", "z")), None)
    if cut is None:
        return {}
    return {s.replace("&", "_"): ("Midcap" if i < cut else "Smallcap") for i, s in enumerate(syms)}


def read_groups():
    try:
        import requests
        from fetch_data import SHEET_CSV
        r = requests.get(SHEET_CSV, timeout=60)
        r.raise_for_status()
        g = split_groups(pd.read_csv(io.StringIO(r.text)))
        print(f"::notice::Midcap/Smallcap split read from the Google Sheet: {len(g)} stocks")
        return g
    except Exception as ex:
        print(f"::warning::Could not read the Google Sheet for the midcap/smallcap split ({ex}); all stocks marked 'Unknown'")
        return {}


def run_symbol(sym, df, cutoff, group):
    d = df.copy()
    c = d["Close"]
    e20, e50, e200 = (c.ewm(span=n, adjust=False).mean().values for n in (FAST, SLOW, LONG))
    o, h, cl = d["Open"].values, d["High"].values, c.values
    idx, n = d.index, len(d)
    cross = np.r_[False, (e20[1:] > e50[1:]) & (e20[:-1] <= e50[:-1])]
    sig = cross & (cl > o)
    rows, busy_until = [], -1
    for i in np.where(sig)[0]:
        if i < WARMUP or i < busy_until or i + 1 >= n or idx[i] < cutoff:
            continue
        e = i + 1
        entry = o[e]
        tgt = entry * (1 + TARGET)
        k, why, stop_next, px = e, None, False, None
        while k < n:
            if stop_next:
                px, why = o[k], "STOP"
                break
            if h[k] >= tgt:
                px, why = max(tgt, o[k]), "HIT"
                break
            if cl[k] < e50[k]:
                stop_next = True
            k += 1
        if why is None:                      # still running on the last day of data
            k, px, why = n - 1, cl[n - 1], "OPEN"
        busy_until = n if why == "OPEN" else k
        gross = px / entry - 1
        rows.append(dict(stock=sym, group=group, signal_date=str(idx[i].date()), entry_date=str(idx[e].date()),
                         entry_price=round(float(entry), 2), target=round(float(tgt), 2),
                         exit_date=str(idx[k].date()), exit_price=round(float(px), 2), result=why,
                         pct=round(gross * 100, 2), pct_net=round((gross - COST) * 100, 2),
                         bars=int(k - e), days=int((idx[k] - idx[e]).days),
                         above_ema200=bool(cl[i] > e200[i])))
    return rows


def block(t):
    """All the numbers for one group of trades."""
    if t.empty:
        return dict(trades=0)
    done = t[t["result"] != "OPEN"]
    hit, stop = done[done["result"] == "HIT"], done[done["result"] == "STOP"]
    out = dict(trades=int(len(t)), still_open=int((t["result"] == "OPEN").sum()), closed=int(len(done)),
               hit=int(len(hit)), stopped=int(len(stop)),
               hit_rate=round(len(hit) / len(done) * 100, 1) if len(done) else None,
               avg_days_to_hit=round(float(hit["days"].mean()), 1) if len(hit) else None,
               median_days_to_hit=float(hit["days"].median()) if len(hit) else None,
               avg_bars_to_hit=round(float(hit["bars"].mean()), 1) if len(hit) else None,
               avg_days_to_stop=round(float(stop["days"].mean()), 1) if len(stop) else None,
               avg_loss_when_stopped=round(float(stop["pct"].mean()), 2) if len(stop) else None,
               avg_profit_per_trade_after_cost=round(float(done["pct_net"].mean()), 2) if len(done) else None)
    if len(hit):
        out["hits_within_bars"] = {str(x): round(float((hit["bars"] <= x).mean() * 100), 1) for x in HIT_WITHIN}
    if len(stop):
        loss = abs(out["avg_loss_when_stopped"])
        out["hit_rate_needed_to_break_even"] = round(loss / (TARGET * 100 + loss) * 100, 1)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = pd.Timestamp.today().normalize() - pd.DateOffset(years=YEARS)
    groups, notes, rows, n_stocks = read_groups(), [], [], 0
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        sym = os.path.basename(f)[:-4]
        if sym.startswith("_"):
            continue
        df = load(f)
        if len(df) < 300:
            continue
        rows += run_symbol(sym, fix_splits(df, sym, notes), cutoff, groups.get(sym, "Unknown"))
        n_stocks += 1
    if not rows:
        raise SystemExit("No EMA 20/50 crosses found - check that data/ has price files.")
    T = pd.DataFrame(rows)
    T["year"] = pd.to_datetime(T["signal_date"]).dt.year
    summary = dict(updated=datetime.now().strftime("%d %b %Y %H:%M"), stocks_tested=n_stocks, years=YEARS,
                   rules=f"EMA{FAST} crosses above EMA{SLOW} on a green candle; buy next open; target +{int(TARGET * 100)}%; "
                         f"sell next open after a daily close below EMA{SLOW}",
                   overall=block(T),
                   by_group={g: block(x) for g, x in T.groupby("group")},
                   by_year={int(y): block(x) for y, x in T.groupby("year")},
                   by_trend={("Above 200-day EMA" if k else "Below 200-day EMA"): block(x) for k, x in T.groupby("above_ema200")},
                   split_fixes=len(notes))
    stocks = []
    for s, x in T.groupby("stock"):
        b = block(x)
        stocks.append(dict(stock=s, group=x["group"].iloc[0], trades=b["trades"], hit=b["hit"], stopped=b["stopped"],
                           hit_rate=b["hit_rate"], avg_days_to_hit=b["avg_days_to_hit"],
                           avg_profit_per_trade_after_cost=b["avg_profit_per_trade_after_cost"]))
    stocks.sort(key=lambda r: (-(r["hit_rate"] or 0), -r["trades"]))
    json.dump(summary, open(f"{OUT_DIR}/ema_cross_summary.json", "w"), indent=1)
    json.dump(stocks, open(f"{OUT_DIR}/ema_cross_stocks.json", "w"))
    T.drop(columns=["year"]).to_json(f"{OUT_DIR}/ema_cross_trades.json", orient="records")
    print(json.dumps(summary, indent=1)[:4000])


if __name__ == "__main__":
    main()
