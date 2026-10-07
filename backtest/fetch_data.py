"""Downloads daily prices for midcap + smallcap NSE stocks (and Nifty 50) from Angel One into ./data/
Incremental: if a stock file already exists (restored from the GitHub cache), only the missing recent days are fetched.
Needs 4 GitHub secrets: ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET.
Stock list: your Google Sheet (see SHEET_CSV), else ./universe/*.csv, else niftyindices.com."""
import glob, io, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pandas as pd, requests

YEARS_BACK = 7.2              # 6 years + extra history so indicators are ready
OUT = "data"
WORKERS = 3
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INDEX_URLS = ["https://niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
              "https://niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv"]
SHEET_CSV = os.environ.get("SHEET_CSV_URL") or (
    "https://docs.google.com/spreadsheets/d/1hu1z9l4Ghj8Ji5U9xztZsX_HC3Py9eYElP5UErA-yOM/export?format=csv&gid=66309173")
COLS = ["date", "open", "high", "low", "close", "volume"]
_lock, _last, _count = threading.Lock(), [0.0], [0]


def note(msg, level="notice"):
    """Prints a line that also shows up as a GitHub annotation."""
    print(f"::{level}::" + str(msg).replace("\n", " | ")[:900], flush=True)


def pace():
    """Keeps all threads together under ~3 requests per second (Angel's limit)."""
    with _lock:
        wait = _last[0] + 0.35 - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def stock_list():
    frames = []
    try:
        r = requests.get(SHEET_CSV, timeout=60)
        r.raise_for_status()
        frames.append(pd.read_csv(io.StringIO(r.text)))
        note(f"Stock list read from Google Sheet: {len(frames[0])} rows")
    except Exception as ex:
        note(f"Could not read Google Sheet ({ex}); trying universe/ folder", "warning")
        for f in glob.glob("universe/*.csv"):
            frames.append(pd.read_csv(f))
        if not frames:
            for u in INDEX_URLS:
                r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
                r.raise_for_status()
                frames.append(pd.read_csv(io.StringIO(r.text)))
    syms = set()
    for df in frames:
        if "Series" in df.columns:
            df = df[df["Series"].astype(str).str.strip() == "EQ"]
        syms |= set(df["Symbol"].dropna().astype(str).str.strip())
    return sorted(syms)


def get_candles(api, token, start, end):
    rows, s = [], start
    while s < end:
        e = min(s + timedelta(days=1700), end)
        for attempt in range(5):
            try:
                pace()
                r = api.getCandleData({"exchange": "NSE", "symboltoken": token, "interval": "ONE_DAY",
                                       "fromdate": s.strftime("%Y-%m-%d 09:15"),
                                       "todate": e.strftime("%Y-%m-%d 15:30")})
                if r and r.get("status"):
                    rows += r.get("data") or []
                    break
                if attempt == 4:
                    note(f"Candle request failed for token {token}: {str(r)[:200]}", "warning")
                time.sleep(2 * (attempt + 1))
            except Exception:
                time.sleep(2 * (attempt + 1))
        s = e + timedelta(days=1)
    return rows


def fetch_one(api, name, token, start_all, end):
    path = f"{OUT}/{name.replace('&', '_')}.csv"
    old, start = None, start_all
    if os.path.exists(path):
        old = pd.read_csv(path, parse_dates=["date"])
        if len(old):
            start = max(start_all, old["date"].max().to_pydatetime() - timedelta(days=7))
    rows = get_candles(api, token, start, end)
    new = pd.DataFrame(rows, columns=COLS)
    new["date"] = pd.to_datetime(new["date"]).dt.tz_localize(None).dt.normalize()
    df = pd.concat([old, new]) if old is not None else new
    df = df.drop_duplicates("date", keep="last").sort_values("date")
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
    key, cid, pin, totp = (os.environ.get(k) for k in
                           ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET"))
    if not all([key, cid, pin, totp]):
        sys.exit("Missing one of the 4 secrets. Add them in GitHub > Settings > Secrets and variables > Actions.")
    api = SmartConnect(api_key=key)
    login = api.generateSession(cid, pin, pyotp.TOTP(totp).now())
    if not login or not login.get("status"):
        sys.exit(f"Angel One login failed: {login.get('message') if login else 'no reply'}")
    note("Angel One login OK")

    master = requests.get(SCRIP_URL, timeout=120).json()
    tokens = {m["name"]: m["token"] for m in master
              if m.get("exch_seg") == "NSE" and str(m.get("symbol", "")).endswith("-EQ")}
    nifty = next((m["token"] for m in master if m.get("exch_seg") == "NSE" and m.get("symbol") == "Nifty 50"), "99926000")
    syms = stock_list()
    note(f"{len(syms)} stocks in list, {sum(s in tokens for s in syms)} found at Angel One")

    os.makedirs(OUT, exist_ok=True)
    end = datetime.now()
    start = end - timedelta(days=int(365 * YEARS_BACK))
    try:
        fetch_one(api, "_NIFTY50", nifty, start, end)
        note("Nifty 50 index data saved")
    except Exception as e:
        note(f"Could not get Nifty 50 data: {e}", "warning")

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
