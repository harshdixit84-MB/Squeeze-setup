"""Downloads 1-HOUR candles for the stocks in your Squeeze sheet from Angel One into ./data4h/ (Angel has no 4-hour candle, so scan_vtd.py
joins them into NSE 4-hour bars: 09:15-13:15 and 13:15-15:30, the same as TradingView).
Incremental: files restored from the GitHub cache only get the last few days refreshed. Same 4 secrets as fetch_data.py."""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pandas as pd, requests
from fetch_data import note, pace, stock_list, SCRIP_URL, COLS, _lock, _count

OUT, DAYS_BACK, KEEP_DAYS, WORKERS = "data4h", 200, 230, 3
IST = timezone(timedelta(hours=5, minutes=30))


def get_candles(api, token, start, end):
    for attempt in range(5):
        try:
            pace()
            r = api.getCandleData({"exchange": "NSE", "symboltoken": token, "interval": "ONE_HOUR",
                                   "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")})
            if r and r.get("status"):
                return r.get("data") or []
            if attempt == 4:
                note(f"1-hour candle request failed for token {token}: {str(r)[:200]}", "warning")
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return []


def fetch_one(api, name, token, start_all, end):
    path = f"{OUT}/{name.replace('&', '_')}.csv"
    old, start = None, start_all
    if os.path.exists(path):
        old = pd.read_csv(path, parse_dates=["date"])
        if len(old):
            start = max(start_all, old["date"].max().to_pydatetime() - timedelta(days=4))
    new = pd.DataFrame(get_candles(api, token, start, end), columns=COLS)
    if len(new):
        new["date"] = pd.to_datetime(new["date"]).dt.tz_localize(None)       # keeps India wall-clock time (09:15, 10:15 ...)
    df = pd.concat([old, new]) if old is not None else new
    if df.empty:
        return False
    df = df.drop_duplicates("date", keep="last").sort_values("date")
    df = df[df["date"] >= end - timedelta(days=KEEP_DAYS)]
    if len(df) < 300:
        return False
    df.to_csv(path, index=False)
    return True


def main():
    try:
        import pyotp
        from SmartApi import SmartConnect
    except Exception as e:
        sys.exit(f"Could not load Angel One library: {type(e).__name__}: {e}")
    key, cid, pin, totp = (os.environ.get(k) for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET"))
    if not all([key, cid, pin, totp]):
        sys.exit("Missing one of the 4 secrets (ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET).")
    api = SmartConnect(api_key=key)
    login = api.generateSession(cid, pin, pyotp.TOTP(totp).now())
    if not login or not login.get("status"):
        sys.exit(f"Angel One login failed: {login.get('message') if login else 'no reply'}")
    note("Angel One login OK (1-hour candles)")
    master = requests.get(SCRIP_URL, timeout=120).json()
    tokens = {m["name"]: m["token"] for m in master if m.get("exch_seg") == "NSE" and str(m.get("symbol", "")).endswith("-EQ")}
    syms = stock_list()
    note(f"{len(syms)} stocks in list, {sum(s in tokens for s in syms)} found at Angel One")
    os.makedirs(OUT, exist_ok=True)
    end = datetime.now(IST).replace(tzinfo=None)
    start = end - timedelta(days=DAYS_BACK)

    def work(s):
        if s not in tokens:
            return False
        try:
            ok = fetch_one(api, s, tokens[s], start, end)
        except Exception as e:
            note(f"{s}: {type(e).__name__}: {e}", "warning")
            ok = False
        with _lock:
            _count[0] += 1
            if _count[0] % 25 == 0:
                print(f"{_count[0]}/{len(syms)} done", flush=True)
        return ok

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(work, syms))
    done = sum(results)
    note(f"Saved {done} stocks, skipped {len(syms) - done}")
    if done < 50:
        sys.exit("Too few stocks downloaded - something is wrong.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            note(e.code, "error")
        raise
    except Exception as e:
        import traceback
        note(f"{type(e).__name__}: {e} | " + traceback.format_exc()[-600:], "error")
        raise
