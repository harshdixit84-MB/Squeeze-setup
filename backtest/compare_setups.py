"""Compares 5 buy-only swing setups on the same stocks, with the same stoploss, costs and exits.
Each setup is judged separately on the FIRST 3 years and the LAST 3 years: a setup is only trusted if it works in both.
Exits tested: target 2x risk, target 3x risk, or a trailing stoploss that follows the price up.
Risk = 2 x the stock's usual daily move (ATR14) below the entry price."""
import glob, json, os
from datetime import datetime
import numpy as np, pandas as pd
from run_backtest import load, fix_splits, PRICE_MIN, PRICE_MAX, MIN_TURNOVER, COST, YEARS

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
COOL, STOP_ATR, TRAIL_ATR = 10, 2.0, 3.0
EXITS = {"t2": ("Target = 2x the risk", 20), "t3": ("Target = 3x the risk", 20),
         "trail": ("Stoploss follows the price up", 40)}
SETUPS = {
    "high_break": ("Breakout near 1-year high, heavy volume",
                   "Stock in an uptrend closes at a fresh 20-day high, within 5% of its 1-year high, on 2x normal volume."),
    "pullback": ("Dip in a strong stock",
                 "Stock in an uptrend dips at least 4%, touches its 20-day average and bounces back up with a green day."),
    "strong_weak_mkt": ("Strong while market is weak",
                        "Market (Nifty) fell over 20 days, yet the stock rose 8%+ and beat Nifty by 10+ points, near its 1-year high."),
    "strong_any_mkt": ("Strong stock beating the market",
                       "Stock rose 8%+ in 20 days, beat Nifty by 10+ points, near its 1-year high, on 1.5x volume (any market)."),
    "tight_base": ("Tight base breakout",
                   "Uptrending stock stayed within an 8% range for 10 days, then closes above it on 1.5x volume, near its 1-year high."),
    "base_any": ("Yardstick: buy any stock on a random day",
                 "Not a setup. Buys any eligible stock on any day (every 10 days per stock) with the same stoploss and exits. A real setup must beat this."),
    "base_up": ("Yardstick: buy any stock in an uptrend",
                "Not a setup. Buys any eligible stock whose price is above its 50-day and 200-day averages, every 10 days per stock."),
}


def indic(d, nret20):
    c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
    d["sma20"], d["sma50"], d["sma200"] = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    d["hi252"] = c.rolling(252).max()
    d["near"] = c / d["hi252"]
    d["hi20p"], d["hi10p"] = c.shift(1).rolling(20).max(), c.shift(1).rolling(10).max()
    d["vol20"] = v.rolling(20).mean()
    d["volx"] = v / d["vol20"].shift(1)
    pc = c.shift(1)
    d["atr"] = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).rolling(14).mean()
    d["ret20"] = c / c.shift(20) - 1
    d["nret20"] = nret20.reindex(d.index).ffill()
    d["rng10p"] = ((h.rolling(10).max() - l.rolling(10).min()) / c).shift(1)
    d["pull"] = d["hi10p"] / pc - 1
    d["fwd_c10"] = c.shift(-10) / c - 1
    d["fwd_hi"] = h[::-1].rolling(10, min_periods=10).max()[::-1].shift(-1) / c - 1
    d["eligible"] = c.between(PRICE_MIN, PRICE_MAX) & (d["vol20"] * c >= MIN_TURNOVER)
    return d


def signals(d):
    c, up = d["Close"], (d["Close"] > d["sma50"]) & (d["sma50"] > d["sma200"])
    rs = d["ret20"] - d["nret20"]
    return {
        "base_any": d["eligible"], "base_up": up,
        "high_break": up & (d["near"] >= 0.95) & (c > d["hi20p"]) & (d["volx"] >= 2) & (c > d["Open"]),
        "pullback": up & (d["near"] >= 0.80) & (d["pull"] >= 0.04) & (c > d["Open"]) & (c > d["sma20"]) & (d["Low"] <= d["sma20"] * 1.01),
        "strong_weak_mkt": (d["nret20"] < 0) & (d["ret20"] >= 0.08) & (rs >= 0.10) & (d["near"] >= 0.90) & (c > d["sma50"]),
        "strong_any_mkt": (rs >= 0.10) & (d["ret20"] >= 0.08) & (d["near"] >= 0.95) & (c > d["sma50"]) & (d["volx"] >= 1.5),
        "tight_base": up & (d["near"] >= 0.90) & (d["rng10p"] <= 0.08) & (c > d["hi10p"]) & (d["volx"] >= 1.5),
    }


def sim(o, h, l, c, e, risk, kind, atr):
    entry, stop = o[e], o[e] - risk
    mult = {"t2": 2, "t3": 3}.get(kind)
    tgt = entry + mult * risk if mult else None
    last = min(e + EXITS[kind][1], len(o) - 1)
    px, end, hc = c[last], last, entry
    for k in range(e, last + 1):
        if k > e and o[k] <= stop: px, end = o[k], k; break
        if l[k] <= stop: px, end = stop, k; break
        if tgt and h[k] >= tgt: px, end = tgt, k; break
        if kind == "trail":
            hc = max(hc, c[k]); stop = max(stop, hc - TRAIL_ATR * atr)
    return round((px / entry - 1 - COST) * 100, 2), end - e


def stats(t, kind):
    if t.empty:
        return dict(trades=0, win_rate=0, avg_profit=0, avg_win=0, avg_loss=0, avg_days=0)
    p = t[f"p_{kind}"]
    w, l = p[p > 0], p[p <= 0]
    return dict(trades=int(len(p)), win_rate=round((p > 0).mean() * 100, 1), avg_profit=round(p.mean(), 2),
                avg_win=round(w.mean(), 2) if len(w) else 0, avg_loss=round(l.mean(), 2) if len(l) else 0,
                avg_days=round(t[f"d_{kind}"].mean(), 1))


def verdict(f, l):
    if min(f["trades"], l["trades"]) < 30: return "Too few trades to judge"
    a, b = f["avg_profit"], l["avg_profit"]
    if a > 0.5 and b > 0.5: return "Works in both periods"
    if a > 0 and b > 0: return "Small profit in both periods"
    if a > 0: return "Worked before, not recently"
    if b > 0: return "Worked only recently"
    return "Does not work"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = pd.Timestamp.today().normalize() - pd.DateOffset(years=YEARS)
    mid = cutoff + pd.DateOffset(years=YEARS // 2)
    npath = f"{DATA_DIR}/_NIFTY50.csv"
    nret = (lambda n: n["Close"] / n["Close"].shift(20) - 1)(load(npath)) if os.path.exists(npath) else pd.Series(dtype=float)
    rows, base = [], []
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        sym = os.path.basename(f)[:-4]
        if sym.startswith("_"): continue
        df = load(f)
        if len(df) < 300: continue
        d = indic(fix_splits(df, sym, []).copy(), nret)
        o, h, l, c = (d[x].values for x in ("Open", "High", "Low", "Close"))
        base.append(d.loc[d["eligible"] & (d.index >= cutoff), "fwd_hi"].dropna())
        for key, sig in signals(d).items():
            last_i = -999
            for i in np.where((sig & d["eligible"]).fillna(False).values)[0]:
                if i - last_i < COOL: continue
                last_i = i
                atr = d["atr"].values[i]
                if i + 2 >= len(d) or d.index[i] < cutoff or not np.isfinite(atr) or atr <= 0: continue
                e, risk = i + 1, STOP_ATR * atr
                if risk / o[e] > 0.12 or not (PRICE_MIN <= o[e] <= PRICE_MAX * 1.1): continue
                r = d.iloc[i]
                row = dict(setup=key, stock=sym, signal_date=str(d.index[i].date()), entry_date=str(d.index[e].date()),
                           entry_price=round(o[e], 2), stoploss=round(o[e] - risk, 2), fwd_hi=r["fwd_hi"], fwd_c10=r["fwd_c10"],
                           reason="" if key.startswith("base") else (f"{SETUPS[key][1]} On {d.index[i].date()} it closed at {c[i]:.1f}, {100 * (1 - r['near']):.1f}% below its "
                                   f"1-year high, on {r['volx']:.1f}x normal volume. 20-day change: stock {r['ret20']:+.0%}, market {r['nret20']:+.0%}."))
                for kind in EXITS:
                    row[f"p_{kind}"], row[f"d_{kind}"] = sim(o, h, l, c, e, risk, kind, atr)
                rows.append(row)
    if not rows:
        raise SystemExit("No setups found - check that data/ has price files.")
    T = pd.DataFrame(rows)
    T["first"] = pd.to_datetime(T["signal_date"]) < mid
    baseline = round((pd.concat(base) >= 0.10).mean() * 100, 1)
    out = []
    T["year"] = pd.to_datetime(T["signal_date"]).dt.year
    ref = {k: T[T["setup"] == "base_any"][f"p_{k}"].mean() for k in EXITS}
    for key, (label, desc) in SETUPS.items():
        s = T[T["setup"] == key]
        fh, fc = s["fwd_hi"].dropna(), s["fwd_c10"].dropna()
        bm = round((fh >= 0.10).mean() * 100, 1) if len(fh) else 0
        p8 = round((fh >= 0.08).mean() * 100, 1) if len(fh) else 0
        best_avg = round(fh.mean() * 100, 1) if len(fh) else 0
        best_med = round(fh.median() * 100, 1) if len(fh) else 0
        day10 = round(fc.mean() * 100, 1) if len(fc) else 0
        for kind, (xl, _) in EXITS.items():
            a, f, l = stats(s, kind), stats(s[s["first"]], kind), stats(s[~s["first"]], kind)
            out.append(dict(setup=key, label=label, desc=desc, exit=kind, exit_label=xl, **a,
                            first_profit=f["avg_profit"], first_trades=f["trades"], last_profit=l["avg_profit"],
                            last_trades=l["trades"], big_move=bm, reach8=p8, best_avg=best_avg, best_med=best_med, day10=day10, verdict=verdict(f, l),
                            edge=round(a["avg_profit"] - ref[kind], 2),
                            by_year={int(y): round(g[f"p_{kind}"].mean(), 2) for y, g in s.groupby("year")}))
    json.dump(dict(updated=datetime.now().strftime("%d %b %Y %H:%M"), baseline_big_move=baseline,
                   split_date=str(mid.date()), market_data=bool(len(nret)), setups=out), open(f"{OUT_DIR}/compare.json", "w"), indent=1)
    keep = ["setup", "stock", "entry_date", "entry_price", "stoploss", "p_t2", "p_t3", "p_trail", "reason"]
    recent = T[~T["setup"].str.startswith("base")].sort_values("entry_date").groupby("setup").tail(400)[keep]
    json.dump(recent.to_dict("records"), open(f"{OUT_DIR}/compare_trades.json", "w"))
    for r in out:
        print(f"{r['label'][:38]:38} {r['exit']:5} n={r['trades']:5} win={r['win_rate']:5}% avg={r['avg_profit']:6}% "
              f"first={r['first_profit']:6}% last={r['last_profit']:6}%  {r['verdict']}")


if __name__ == "__main__":
    main()
