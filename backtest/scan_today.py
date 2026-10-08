"""Finds stocks that match the two chosen setups on the LATEST trading day and writes site/data/candidates.json
(with price history so the page can draw a chart with stoploss and targets)."""
import glob, json, os
from datetime import datetime
import numpy as np, pandas as pd
from run_backtest import load, fix_splits
from compare_setups import indic, signals, SETUPS, STOP_ATR

DATA_DIR, OUT_DIR = os.environ.get("DATA_DIR", "data"), os.environ.get("OUT_DIR", "site/data")
CHOSEN = ["high_break", "strong_any_mkt"]
LOOKBACK = int(os.environ.get("LOOKBACK", "1"))      # how many latest trading days to look at
CHART_DAYS = 100


def main():
    comp = json.load(open(f"{OUT_DIR}/compare.json"))
    rec = {r["setup"]: r for r in comp["setups"] if r["exit"] == "t2"}
    npath = f"{DATA_DIR}/_NIFTY50.csv"
    nret = (lambda n: n["Close"] / n["Close"].shift(20) - 1)(load(npath)) if os.path.exists(npath) else pd.Series(dtype=float)
    cands, last_date, n = [], None, 0
    for f in sorted(glob.glob(f"{DATA_DIR}/*.csv")):
        sym = os.path.basename(f)[:-4]
        if sym.startswith("_"): continue
        df = load(f)
        if len(df) < 300: continue
        d = indic(fix_splits(df, sym, []).copy(), nret)
        n += 1
        last_date = max(last_date, d.index[-1]) if last_date is not None else d.index[-1]
        sig = signals(d)
        hits = {}
        for k in CHOSEN:
            s = (sig[k] & d["eligible"]).fillna(False).values[-LOOKBACK:]
            if s.any(): hits[k] = len(d) - LOOKBACK + int(np.where(s)[0][-1])
        if not hits: continue
        i = max(hits.values())
        r = d.iloc[i]
        atr = r["atr"]
        if not np.isfinite(atr) or atr <= 0: continue
        c, risk = float(r["Close"]), STOP_ATR * float(atr)
        tail = d.iloc[max(0, i - CHART_DAYS + 1): i + 1]
        cands.append(dict(
            stock=sym, signal_date=str(d.index[i].date()), setups=[SETUPS[k][0] for k in hits],
            close=round(c, 2), stoploss=round(c - risk, 2), risk_pct=round(100 * risk / c, 1),
            target1=round(c + 2 * risk, 2), target2=round(c + 3 * risk, 2),
            volx=round(float(r["volx"]), 1), below_high=round(100 * (1 - float(r["near"])), 1),
            ret20=round(100 * float(r["ret20"]), 1),
            nret20=None if not np.isfinite(r["nret20"]) else round(100 * float(r["nret20"]), 1),
            reason=" ".join(SETUPS[k][1] for k in hits),
            record=[dict(setup=SETUPS[k][0], times_6y=rec[k]["trades"], win_rate=rec[k]["win_rate"],
                         avg_profit=rec[k]["avg_profit"], first=rec[k]["first_profit"], last=rec[k]["last_profit"],
                         avg_days=rec[k]["avg_days"]) for k in hits if k in rec],
            chart=dict(d=[str(x.date()) for x in tail.index], c=[round(float(x), 2) for x in tail["Close"]],
                       m=[None if not np.isfinite(x) else round(float(x), 2) for x in tail["sma50"]])))
    cands.sort(key=lambda x: (-len(x["setups"]), -x["volx"]))
    json.dump(dict(updated=datetime.now().strftime("%d %b %Y %H:%M"), data_date=str(last_date.date()) if last_date is not None else None,
                   stocks_scanned=n, candidates=cands), open(f"{OUT_DIR}/candidates.json", "w"))
    print(f"{len(cands)} candidates out of {n} stocks (data up to {last_date})")


if __name__ == "__main__":
    main()
