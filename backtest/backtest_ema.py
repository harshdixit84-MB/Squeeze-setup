"""Reference (pure Python) version of the 20/50 EMA backtest. It mirrors api/_engine.js line by line and exists only so the
dashboard engine can be cross-checked (see parity_check.py). The dashboard itself runs api/backtest.js.

Rules: weekly (or monthly) 20 EMA closes above 50 EMA -> entry at next bar open -> exit on DAILY candles at +30% target or
entry -20% stop, whichever hits first -> what-if: after the +30% exit, entry -20% first = "No (SL hit)", +70% first = "Yes".
Next trade only on a fresh crossover after the trade closed."""
import datetime as dt

STOP_PCT, TARGET_PCT, TRAIL_PCT, WARMUP = 0.20, 0.30, 0.70, 50


def _d(s):
    return dt.date.fromisoformat(s)


def period_key(date_str, tf):
    if tf == "M":
        return date_str[:7]
    d = _d(date_str)
    return (d - dt.timedelta(days=d.weekday())).isoformat()      # back to Monday


def elapsed(a, b, tf):
    if tf == "M":
        return (int(b[:4]) - int(a[:4])) * 12 + (int(b[5:7]) - int(a[5:7]))
    return round((_d(period_key(b, "W")) - _d(period_key(a, "W"))).days / 7)


def fix_splits(daily):
    """A >25% overnight gap is treated as a split/bonus and earlier prices are scaled (same rule as run_backtest.py)."""
    rows, notes = [dict(r) for r in daily], []
    for _ in range(20):
        k, f = -1, 1.0
        for i in range(1, len(rows)):
            g = rows[i]["open"] / rows[i - 1]["close"]
            if g < 0.75 or g > 1.35:
                k, f = i, g
                break
        if k < 0:
            break
        for i in range(k):
            for c in ("open", "high", "low", "close"):
                rows[i][c] *= f
        notes.append({"date": rows[k]["date"], "overnightChangePct": round((f - 1) * 100, 1)})
    return rows, notes


def aggregate(daily, tf, today):
    bars, cur = [], None
    for k, d in enumerate(daily):
        key = period_key(d["date"], tf)
        if cur is None or cur["key"] != key:
            cur = dict(key=key, date=d["date"], open=d["open"], high=d["high"], low=d["low"], close=d["close"], d0=k, d1=k)
            bars.append(cur)
        else:
            cur["high"], cur["low"], cur["close"], cur["d1"] = max(cur["high"], d["high"]), min(cur["low"], d["low"]), d["close"], k
    now_key = period_key(today["date"], tf)
    weekend = tf == "W" and today["dow"] in (0, 6)
    for b in bars:
        b["complete"] = b["key"] < now_key or weekend
    return bars


def ema(values, span):
    a = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def simulate(daily, k0, entry):
    stop, target = entry * (1 - STOP_PCT), entry * (1 + TARGET_PCT)
    for k in range(k0, len(daily)):
        c = daily[k]
        if k > k0:
            if c["open"] <= stop:
                return k, c["open"], "Stop -20% (gap)"
            if c["open"] >= target:
                return k, c["open"], "Target +30% (gap)"
        hit_stop, hit_target = c["low"] <= stop, c["high"] >= target
        if hit_stop and hit_target:
            return k, stop, "Stop -20% (same day as target)"
        if hit_stop:
            return k, stop, "Stop -20%"
        if hit_target:
            return k, target, "Target +30%"
    return None


def what_if(daily, start, entry):
    stop, goal = entry * (1 - STOP_PCT), entry * (1 + TRAIL_PCT)
    for k in range(start, len(daily)):
        c = daily[k]
        if k > start:
            if c["open"] <= stop:
                return "SL", k
            if c["open"] >= goal:
                return "Trail", k
        if c["low"] <= stop:
            return "SL", k
        if c["high"] >= goal:
            return "Trail", k
    return "Running", None


def run_backtest(daily, tf, today, from_date=None):
    bars = aggregate(daily, tf, today)
    nc = sum(1 for b in bars if b["complete"])
    if nc < WARMUP + 2:
        return []
    closes = [b["close"] for b in bars[:nc]]
    fast, slow = ema(closes, 20), ema(closes, 50)
    cross = lambda i: WARMUP <= i < nc and fast[i] > slow[i] and not fast[i - 1] > slow[i - 1]
    trades, i = [], 0
    while i < nc:
        if not cross(i):
            i += 1
            continue
        if i + 1 >= len(bars):
            break
        k0 = bars[i + 1]["d0"]
        entry = daily[k0]["open"]
        x, is_open = simulate(daily, k0, entry), False
        if x is None:
            x, is_open = (len(daily) - 1, daily[-1]["close"], "Open (marked at last close)"), True
        k, price, reason = x
        peak = max(daily[j]["high"] for j in range(k0, k + 1))
        reached, to70 = "-", None
        if reason.startswith("Target"):
            res, kk = what_if(daily, k, entry)
            if res == "Trail":
                reached, to70 = "Yes", elapsed(daily[k0]["date"], daily[kk]["date"], tf)
            else:
                reached = "No (SL hit)" if res == "SL" else "Running"
        trades.append(dict(entryDate=daily[k0]["date"], entryPrice=round(entry, 2), exitDate=daily[k]["date"],
                           exitPrice=round(price, 2), reason=reason, peakGainPct=round((peak / entry - 1) * 100, 1),
                           plPct=round((price / entry - 1) * 100, 1), plRs=round(price - entry, 2),
                           duration=elapsed(daily[k0]["date"], daily[k]["date"], tf), reached70=reached, barsTo70=to70))
        if is_open:
            break
        i = next(n for n, b in enumerate(bars) if b["d0"] <= k <= b["d1"])
    shown = [t for t in trades if not from_date or t["entryDate"] >= from_date]
    for n, t in enumerate(shown, 1):
        t["trade"] = n
    return shown


def to_table(trades, tf="W"):
    """Same columns as the dashboard table."""
    import pandas as pd
    unit = "months" if tf == "M" else "weeks"
    cols = {"trade": "Trade", "entryDate": "Entry date", "entryPrice": "Entry", "exitDate": "Exit date", "exitPrice": "Exit",
            "reason": "Exit reason", "peakGainPct": "Peak gain %", "plPct": "P/L %", "plRs": "P/L Rs (1 share)",
            "duration": f"Duration ({unit})", "reached70": "Reached +70%?", "barsTo70": f"{unit} to +70%"}
    return pd.DataFrame(trades)[list(cols)].rename(columns=cols)
