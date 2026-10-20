import pandas as pd

STOP_PCT = 0.20      # entry se -20% stop
TARGET_PCT = 0.30    # +30% exit
TRAIL_PCT = 0.70     # +70% what-if check


def add_emas(df, fast=20, slow=50):
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
    return df


def find_crossovers(df):
    """True on the bar where fast EMA closes above slow EMA after being <= before."""
    above = df["ema_fast"] > df["ema_slow"]
    prev_above = above.shift(1, fill_value=False)
    return above & ~prev_above


def whatif_70(df, entry_idx, exit_idx, entry_price):
    """After the +30% exit, would price have hit +70% before falling to the -20% stop?
    Tracks from the exit bar onward. Returns (reached_70, bars_to_70)."""
    stop = entry_price * (1 - STOP_PCT)
    goal = entry_price * (1 + TRAIL_PCT)
    for i in range(exit_idx, len(df)):
        hi, lo = df["high"].iloc[i], df["low"].iloc[i]
        if lo <= stop:          # stop checked first (conservative)
            return False, None
        if hi >= goal:
            return True, i - entry_idx
    return False, None


def run_backtest(df, unit="W"):
    """df columns: date, open, high, low, close (one row per week or month, sorted ascending).
    unit: 'W' for weekly, 'M' for monthly (only affects column labels)."""
    df = add_emas(df.reset_index(drop=True))
    cross = find_crossovers(df)
    trades = []
    i = 0
    n = len(df)

    while i < n - 1:
        # need a fresh crossover on bar i; entry at open of bar i+1
        if not cross.iloc[i]:
            i += 1
            continue

        entry_idx = i + 1
        entry_price = df["open"].iloc[entry_idx]
        stop = entry_price * (1 - STOP_PCT)
        target = entry_price * (1 + TARGET_PCT)
        exit_idx, exit_price, reason = None, None, None

        for j in range(entry_idx, n):
            o, h, l = df["open"].iloc[j], df["high"].iloc[j], df["low"].iloc[j]
            # gap handling on bars after entry bar
            if j > entry_idx:
                if o <= stop:
                    exit_idx, exit_price, reason = j, o, "Stop -20% (gap)"
                    break
                if o >= target:
                    exit_idx, exit_price, reason = j, o, "Target +30% (gap)"
                    break
            hit_stop, hit_target = l <= stop, h >= target
            if hit_stop and hit_target:      # both in same bar -> assume stop first
                exit_idx, exit_price, reason = j, stop, "Stop -20% (same bar as target)"
                break
            if hit_stop:
                exit_idx, exit_price, reason = j, stop, "Stop -20%"
                break
            if hit_target:
                exit_idx, exit_price, reason = j, target, "Target +30%"
                break

        if exit_idx is None:   # still open at last bar
            exit_idx = n - 1
            exit_price = df["close"].iloc[exit_idx]
            reason = "Open (marked at last close)"

        peak = df["high"].iloc[entry_idx:exit_idx + 1].max()
        pl_pct = (exit_price / entry_price - 1) * 100

        reached_70, bars_to_70 = (False, None)
        if reason.startswith("Target"):
            reached_70, bars_to_70 = whatif_70(df, entry_idx, exit_idx, entry_price)

        trades.append({
            "Trade #": len(trades) + 1,
            "Entry Date": df["date"].iloc[entry_idx],
            "Entry Price": round(entry_price, 2),
            "Exit Date": df["date"].iloc[exit_idx],
            "Exit Price": round(exit_price, 2),
            "Exit Reason": reason,
            "Peak Gain %": round((peak / entry_price - 1) * 100, 1),
            "P/L %": round(pl_pct, 1),
            "P/L Rs (1 share)": round(exit_price - entry_price, 2),
            f"Duration ({'weeks' if unit == 'W' else 'months'})": exit_idx - entry_idx,
            "Reached +70%?": "Yes" if reached_70 else ("No" if reason.startswith("Target") else "-"),
            f"Bars to +70% ({unit})": bars_to_70,
        })

        # next signal must be a NEW crossover after this exit bar
        i = exit_idx + 1
        while i < n - 1 and not cross.iloc[i]:
            i += 1

    return pd.DataFrame(trades)


def summary(trades):
    closed = trades[~trades["Exit Reason"].str.startswith("Open")]
    if closed.empty:
        return {}
    return {
        "Total trades": len(trades),
        "Win %": round((closed["P/L %"] > 0).mean() * 100, 1),
        "Avg P/L %": round(closed["P/L %"].mean(), 1),
        "Best %": closed["P/L %"].max(),
        "Worst %": closed["P/L %"].min(),
    }


if __name__ == "__main__":
    # quick test with Yahoo data (Angel API wala fetch Step 2 mein)
    import yfinance as yf
    raw = yf.download("RELIANCE.NS", period="10y", interval="1wk", auto_adjust=True)
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.reset_index().rename(columns={"Date": "date", "Open": "open",
                                             "High": "high", "Low": "low", "Close": "close"})
    t = run_backtest(raw[["date", "open", "high", "low", "close"]], unit="W")
    print(t.to_string(index=False))
    print(summary(t))
