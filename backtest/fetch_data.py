"""Downloads about 7 years of daily prices for midcap + smallcap NSE stocks from Angel One into ./data/
Needs 4 GitHub secrets: ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET.
Stock list: put CSV files (with a 'Symbol' column) in ./universe/ ; if none, it tries niftyindices.com."""
import glob, io, os, sys, time
from datetime import datetime, timedelta
import pandas as pd, pyotp, requests
from SmartApi import SmartConnect

YEARS_BACK = 7.2              # 6 years + extra history so indicators are ready
OUT = "data"
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INDEX_URLS = ["https://niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
              "https://niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv"]


def stock_list():
    files = glob.glob("universe/*.csv")
    texts = [open(f, encoding="utf-8").read() for f in files]
    if not texts:
        for u in INDEX_URLS:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            r.raise_for_status()
            texts.append(r.text)
    syms = set()
    for t in texts:
        syms |= set(pd.read_csv(io.StringIO(t))["Symbol"].dropna().astype(str).str.strip())
    return sorted(syms)


def get_candles(api, token, start, end):
    rows, s = [], start
    while s < end:
        e = min(s + timedelta(days=1700), end)      # Angel allows ~2000 days per request
        for attempt in range(5):
            try:
                r = api.getCandleData({"exchange": "NSE", "symboltoken": token, "interval": "ONE_DAY",
                                       "fromdate": s.strftime("%Y-%m-%d 09:15"),
                                       "todate": e.strftime("%Y-%m-%d 15:30")})
                if r and r.get("status"):
                    rows += r.get("data") or []
                    break
                time.sleep(2 * (attempt + 1))          # probably "too many requests" - wait and retry
            except Exception:
                time.sleep(2 * (attempt + 1))
        time.sleep(0.4)
        s = e + timedelta(days=1)
    return rows


def main():
    key, cid, pin, totp = (os.environ.get(k) for k in
                           ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET"))
    if not all([key, cid, pin, totp]):
        sys.exit("Missing one of the 4 secrets. Add them in GitHub > Settings > Secrets and variables > Actions.")
    api = SmartConnect(api_key=key)
    login = api.generateSession(cid, pin, pyotp.TOTP(totp).now())
    if not login or not login.get("status"):
        sys.exit(f"Angel One login failed: {login.get('message') if login else 'no reply'}")
    print("Angel One login OK")

    master = requests.get(SCRIP_URL, timeout=120).json()
    tokens = {m["name"]: m["token"] for m in master
              if m.get("exch_seg") == "NSE" and str(m.get("symbol", "")).endswith("-EQ")}
    syms = stock_list()
    print(f"{len(syms)} stocks in list, {sum(s in tokens for s in syms)} found at Angel One")

    os.makedirs(OUT, exist_ok=True)
    end = datetime.now()
    start = end - timedelta(days=int(365 * YEARS_BACK))
    done = skipped = 0
    for i, s in enumerate(syms, 1):
        if s not in tokens:
            skipped += 1
            continue
        rows = get_candles(api, tokens[s], start, end)
        if len(rows) < 300:
            skipped += 1
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df.drop_duplicates("date").sort_values("date").to_csv(f"{OUT}/{s.replace('&', '_')}.csv", index=False)
        done += 1
        if i % 25 == 0:
            print(f"{i}/{len(syms)} done")
    print(f"Saved {done} stocks, skipped {skipped}")
    if done < 50:
        sys.exit("Too few stocks downloaded - something is wrong.")


if __name__ == "__main__":
    main()
