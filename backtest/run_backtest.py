"""Squeeze + NR7 breakout backtest on the prices saved in ./data/ . Writes simple tables to site/data/*.json
SETUP day: narrowest High-Low range of last 7 days AND price swings (Bollinger width) in the lowest 10% of last 6 months,
           price between 50 and 500, daily traded value >= Rs 50 lakh.
ENTRY: within next 5 days a close above (buy) / below (sell) that quiet day's range, on above-average volume. Enter next open.
EXIT: stoploss = tighter of quiet-day opposite side or 1.5 x average daily move (never closer than 0.5x);
      target = 2x or 3x the amount risked; or sell after 15 days. Same-day clash = stoploss assumed first. Cost 0.2%."""
import glob, json, os
from datetime import datetime
import numpy as np, pandas as pd

YEARS = 6
PRICE_MIN, PRICE_MAX, MIN_TURNOVER = 50, 500, 5_000_000
BB_LEN, SQ_WIN, SQ_PCT, TRIG_WIN, MAX_HOLD = 20, 126, 0.10, 5, 15
ATR_STOP, ATR_MIN, RRS, COST = 1.5, 0.5, (2, 3), 0.002
BLAST_N, BLAST_MOVE = 10, 0.10
DATA_DIR, OUT_DIR = "data", "site/data"


def load(path):
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    df.columns = [c.title() for c in df.columns]
    return df[["Open", "High", "Low", "Close", "Volume"]].astype(float).dropna()


def fix_splits(df, sym, notes):
    """Angel prices may not be adjusted for splits/bonus. A >25% overnight jump is treated as one and smoothed out."""
    df = df.copy()
    for _ in range(20):
        gap = df["Open"] / df["Close"].shift(1)
        bad = gap[(gap < 0.75) | (gap > 1.35)]
        if bad.empty:
            break
        k, f = bad.index[0], float(bad.iloc[0])
        before = df.index < k
        df.loc[before, ["Open", "High", "Low", "Close"]] *= f
        df.loc[before, "Volume"] /= f
        notes.append({"stock": sym, "date": str(k.date()), "overnight_change_pct": round((f - 1) * 100, 1)})
    return df


def prep(df):
    d = df.copy()
    c = d["Close"]
    mid, sd = c.rolling(BB_LEN).mean(), c.rolling(BB_LEN).std()
    d["bbw"] = 4 * sd / mid
    d["bbw_pct"] = d["bbw"].rolling(SQ_WIN).apply(lambda x: (x[:-1] <= x[-1]).mean(), raw=True)
    rng = d["High"] - d["Low"]
    d["nr7"] = (rng > 0) & (rng <= rng.rolling(7).min())
    pc = c.shift(1)
    tr = pd.concat([rng, (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["vol20"] = d["Volume"].rolling(20).mean()
    d["sma50"] = c.rolling(50).mean()
    n = BLAST_N
    d["fwd_hi"] = d["High"][::-1].rolling(n, min_periods=n).max()[::-1].shift(-1) / c - 1
    d["fwd_lo"] = d["Low"][::-1].rolling(n, min_periods=n).min()[::-1].shift(-1) / c - 1
    d["eligible"] = c.between(PRICE_MIN, PRICE_MAX) & (d["vol20"] * c >= MIN_TURNOVER)
    d["signal"] = d["eligible"] & d["nr7"] & (d["bbw_pct"] <= SQ_PCT)
    return d


def simulate(d, e, side, risk, rr):
    o, h, l, c = (d[x].values for x in ("Open", "High", "Low", "Close"))
    entry = o[e]
    stop = entry - risk if side == "buy" else entry + risk
    tgt = entry + rr * risk if side == "buy" else entry - rr * risk
    last = min(e + MAX_HOLD, len(d) - 1)
    px, why = c[last], "Time ran out"
    for k in range(e, last + 1):
        if side == "buy":
            if k > e and o[k] <= stop: px, why = o[k], "Stoploss hit"; break
            if l[k] <= stop: px, why = stop, "Stoploss hit"; break
            if h[k] >= tgt: px, why = tgt, "Target hit"; break
        else:
            if k > e and o[k] >= stop: px, why = o[k], "Stoploss hit"; break
            if h[k] >= stop: px, why = stop, "Stoploss hit"; break
            if l[k] <= tgt: px, why = tgt, "Target hit"; break
    last = k if why != "Time ran out" else last
    gross = (px - entry) if side == "buy" else (entry - px)
    return round((gross / entry - COST) * 100, 2), why, last - e


def run_symbol(sym, df, cutoff):
    d = prep(df).reset_index().rename(columns={"index": "date"})
    sigs, trades = [], []
    for i in np.where(d["signal"].values)[0]:
        if i + 2 >= len(d) or d["date"].iloc[i] < cutoff:
            continue
        r = d.iloc[i]
        hi, lo = r["High"], r["Low"]
        sigs.append(dict(stock=sym, date=r["date"], fwd_hi=r["fwd_hi"], fwd_lo=r["fwd_lo"]))
        side = j = None
        for k in range(i + 1, min(i + TRIG_WIN, len(d) - 2) + 1):
            vol_ok = d["Volume"].iloc[k] > d["vol20"].iloc[k]
            if vol_ok and d["Close"].iloc[k] > hi: side, j = "buy", k; break
            if vol_ok and d["Close"].iloc[k] < lo: side, j = "sell", k; break
        if side is None:
            continue
        e, atr = j + 1, d["atr"].iloc[j]
        entry = d["Open"].iloc[e]
        if not np.isfinite(atr) or atr <= 0:
            continue
        if side == "buy":
            stop = min(max(lo, entry - ATR_STOP * atr), entry - ATR_MIN * atr); risk = entry - stop
        else:
            stop = max(min(hi, entry + ATR_STOP * atr), entry + ATR_MIN * atr); risk = stop - entry
        if risk <= 0 or not (PRICE_MIN <= entry <= PRICE_MAX * 1.1):
            continue
        volx = d["Volume"].iloc[j] / d["vol20"].iloc[j]
        up = d["Close"].iloc[j] > d["sma50"].iloc[j]
        row = dict(stock=sym, direction=side, signal_date=str(r["date"].date()), entry_date=str(d["date"].iloc[e].date()),
                   entry_price=round(entry, 2), stoploss=round(stop, 2), target_2x=round(entry + (2 if side == "buy" else -2) * risk, 2),
                   with_trend=bool(up == (side == "buy")),
                   reason=(f"Price swings were in the quietest {r['bbw_pct']:.0%} of the last 6 months and "
                           f"{r['date'].date()} was the smallest-range day of 7. On {d['date'].iloc[j].date()} the price closed "
                           f"{'above' if side == 'buy' else 'below'} that quiet day's range with {volx:.1f}x normal volume. "
                           f"Stock was {'above' if up else 'below'} its 50-day average."))
        for rr in RRS:
            p, why, days = simulate(d, e, side, risk, rr)
            row[f"pct_{rr}"], row[f"how_{rr}"], row[f"days_{rr}"] = p, why, days
        trades.append(row)
    base = d.loc[d["eligible"] & (d["date"] >= cutoff), ["fwd_hi", "fwd_lo"]].dropna()
    return sigs, trades, base


def verdict(n, avg):
    if n < 30: return "Too few trades to judge"
    if avg > 0.5: return "Looks good"
    if avg > 0: return "Barely profitable"
    return "Does not work"


def stats(t, rr):
    if t.empty:
        return dict(trades=0, win_rate=0, avg_profit=0, avg_win=0, avg_loss=0, avg_days=0, verdict="No trades")
    p = t[f"pct_{rr}"]
    w, l = p[p > 0], p[p <= 0]
    return dict(trades=len(p), win_rate=round((p > 0).mean() * 100, 1), avg_profit=round(p.mean(), 2),
                avg_win=round(w.mean(), 2) if len(w) else 0, avg_loss=round(l.mean(), 2) if len(l) else 0,
                avg_days=round(t[f"days_{rr}"].mean(), 1), verdict=verdict(len(p), p.mean()))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = pd.Timestamp.today().normalize() - pd.DateOffset(years=YEARS)
    S, T, B, notes, n_stocks = [], [], [], [], 0
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        sym = os.path.basename(f)[:-4]
        if sym.startswith("_"):
            continue
        df = load(f)
        if len(df) < 300: continue
        a, b, c = run_symbol(sym, fix_splits(df, sym, notes), cutoff)
        S += a; T += b; B.append(c); n_stocks += 1
    if not S:
        raise SystemExit("No setups found - check that data/ has price files.")
    sigs, trades, base = pd.DataFrame(S), pd.DataFrame(T), pd.concat(B)
    sigs["year"] = pd.to_datetime(sigs["date"]).dt.year

    def pcts(df):
        return dict(up=round((df["fwd_hi"] >= BLAST_MOVE).mean() * 100, 1), down=round((df["fwd_lo"] <= -BLAST_MOVE).mean() * 100, 1),
                    either=round(((df["fwd_hi"] >= BLAST_MOVE) | (df["fwd_lo"] <= -BLAST_MOVE)).mean() * 100, 1))
    results = []
    for side, label in (("buy", "Buy (price went up)"), ("sell", "Sell (price went down)")):
        sub = trades[trades["direction"] == side] if not trades.empty else trades
        for fl, sel in (("All trades", sub), ("Only when stock was already moving that way (50-day trend)", sub[sub["with_trend"]] if not sub.empty else sub)):
            for rr in RRS:
                results.append(dict(direction=label, filter=fl, target=f"Profit {rr}x the amount risked", **stats(sel, rr)))
    summary = dict(updated=datetime.now().strftime("%d %b %Y %H:%M"), stocks_tested=n_stocks, years=YEARS,
                   times_appeared=int(len(sigs)), trades_taken=int(len(trades)),
                   by_year=[dict(year=int(y), count=int(n)) for y, n in sigs.groupby("year").size().items()],
                   big_move=dict(after_setup=pcts(sigs), normal_days=pcts(base), days=BLAST_N, move=int(BLAST_MOVE * 100)),
                   results=results, split_fixes=len(notes))
    st = []
    for s, g in sigs.groupby("stock"):
        t = trades[trades["stock"] == s] if not trades.empty else trades
        st.append(dict(stock=s, times_appeared=int(len(g)), trades=int(len(t)),
                       win_rate=round((t["pct_2"] > 0).mean() * 100, 1) if len(t) else None,
                       avg_profit=round(t["pct_2"].mean(), 2) if len(t) else None))
    st.sort(key=lambda x: -x["times_appeared"])
    json.dump(summary, open(f"{OUT_DIR}/summary.json", "w"), indent=1)
    json.dump(trades.to_dict("records"), open(f"{OUT_DIR}/trades.json", "w"))
    json.dump(st, open(f"{OUT_DIR}/stocks.json", "w"))
    json.dump(notes, open(f"{OUT_DIR}/price_fixes.json", "w"), indent=1)
    print(json.dumps(summary, indent=1)[:3000])


if __name__ == "__main__":
    main()
