"""Fetches the LATEST monthly portfolio (every holding + allocation %) of the mutual funds listed in mf/funds.json
and writes site/data/mf_portfolios.json for the "Fund Portfolios" tab.
Source: the fund pages on dezerv.in, which show the AMC's monthly portfolio disclosure.
Adding a fund: put its name in mf/funds.json (or open an issue titled "Add fund: <name>" from the dashboard).
A fund page URL can be given too; otherwise the page is found by matching the name against the dezerv.in sitemap."""
import json, os, re, sys, time
from datetime import datetime
from io import StringIO
import pandas as pd, requests
from bs4 import BeautifulSoup

FUNDS_FILE, OUT = "mf/funds.json", "site/data/mf_portfolios.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/124.0"}
BASE = "https://www.dezerv.in"
SITEMAPS = [BASE + "/mutual-funds/sitemap/", BASE + "/sitemap.xml", BASE + "/mutual-funds/sitemap.xml"]
STOP = {"fund", "plan", "growth", "regular", "direct", "option", "scheme", "the", "of", "and", "mutual"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                     "september", "october", "november", "december"], 1)}
LOG = []


def say(msg):
    print(msg, flush=True)
    LOG.append(str(msg))


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"could not open {url} ({last})")


def tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", s.lower().replace("&", " and ")) if t not in STOP}


_pool = {}
FUND_LINK = re.compile(r"(?:https://www\.dezerv\.in)?/mutual-funds/([a-z0-9\-]+-inf[0-9a-z]{9})/?")


def harvest(url, label):
    """Collects every fund page link found on one dezerv.in page."""
    try:
        txt = get(url, tries=1)
    except Exception as e:
        say(f"[discover] {label}: {e}")
        return 0
    n0 = len(_pool)
    for u in FUND_LINK.findall(txt):
        _pool[u] = f"{BASE}/mutual-funds/{u}/"
    if len(txt) < 1500:
        say(f"[discover] body of {label}: {txt[:900]!r}")
    say(f"[discover] {label}: {len(txt)} chars, {txt.count('/mutual-funds/')} links, {len(_pool) - n0} new fund pages")
    return len(_pool) - n0


def candidates(name):
    if not _pool:
        for sm in SITEMAPS:
            harvest(sm, sm)
    # the AMC's own listing page (e.g. /mutual-funds/amc/icici-prudential/) - try the first 1-3 words of the name
    words = re.findall(r"[a-z0-9]+", name.lower())
    for k in (1, 2, 3):
        if len(words) >= k:
            harvest(f"{BASE}/mutual-funds/amc/{'-'.join(words[:k])}/", f"amc {'-'.join(words[:k])}")
    if len(_pool) < 30:
        for u in list(_pool.values())[:3]:
            harvest(u, u)
    return sorted(_pool.values())


def resolve_url(name):
    want = tokens(name)
    best = None
    for u in candidates(name):
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"-inf[0-9a-z]{9}$", "", slug)
        have = tokens(slug.replace("-", " "))
        if not want <= have:
            continue
        score = (len(have - want), 0 if slug.endswith("regular-growth") else 1 if slug.endswith("direct-growth") else 2, len(slug))
        if best is None or score < best[0]:
            best = (score, u, slug)
    if not best:
        raise RuntimeError("no fund page matched this name - add the dezerv.in page link to mf/funds.json as \"url\"")
    say(f"'{name}' matched {best[2]}")
    return best[1]


def num(x):
    m = re.search(r"-?[\d,]*\.?\d+", str(x).replace("₹", "").replace("−", "-"))
    return float(m.group(0).replace(",", "")) if m else None


def parse_page(html):
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    title = (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    m = re.search(r"Portfolio Summary\s*as of\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text)
    as_of = f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}" if m and m.group(2).lower() in MONTHS else None
    size = re.search(r"Fund Size\s*₹\s*([\d,]+(?:\.\d+)?)\s*Cr", text)
    best = None
    for t in pd.read_html(StringIO(html)):
        cols = [str(c).strip().lower() for c in t.columns]
        if not (cols and cols[0].startswith("name") and any(c.startswith("holding") for c in cols) and any(c.startswith("type") for c in cols)):
            continue
        if best is None or len(t) > len(best):
            best = t
    if best is None or best.empty:
        raise RuntimeError("holdings table not found on the page (page layout may have changed)")
    cols = {str(c).strip().lower().split()[0]: c for c in best.columns}
    rows = []
    for _, r in best.iterrows():
        name = str(r[cols["name"]]).strip()
        cr, pct = num(r[cols["amount"]]), num(r[cols["holdings"]])
        if not name or name.lower() == "nan" or pct is None:
            continue
        if cr is not None and cr < 0 and pct > 0:
            pct = -pct                                  # page prints Net Current Assets % without its minus sign
        rows.append(dict(s=name, t=str(r[cols["type"]]).strip(), cr=cr, p=round(pct, 2)))
    if not rows:
        raise RuntimeError("holdings table was empty")
    return dict(title=title, as_of=as_of, fund_size_cr=num(size.group(1)) if size else None, holdings=rows)


def main():
    funds = json.load(open(FUNDS_FILE)) if os.path.exists(FUNDS_FILE) else []
    funds = [dict(name=f) if isinstance(f, str) else f for f in funds]
    # fund requested from the dashboard issue / manual run
    add, url = os.environ.get("ADD_FUND", "").strip(), os.environ.get("ADD_URL", "").strip()
    title, body = os.environ.get("ISSUE_TITLE", ""), os.environ.get("ISSUE_BODY", "")
    if title.lower().startswith("add fund:"):
        add = title.split(":", 1)[1].strip()
        m = re.search(r"https://www\.dezerv\.in/mutual-funds/\S+", body or "")
        url = m.group(0).rstrip(").,") if m else ""
    if add:
        add = re.sub(r"\s+", " ", add)[:120]
        hit = next((f for f in funds if f["name"].lower() == add.lower()), None)
        if hit is None:
            funds.append(dict(name=add, **({"url": url} if url else {})))
            say(f"Added fund: {add}")
        elif url:
            hit["url"] = url
    data = json.load(open(OUT)) if os.path.exists(OUT) else {"funds": {}}
    ok = fail = 0
    for f in funds:
        name = f["name"]
        try:
            if not f.get("url"):
                f["url"] = resolve_url(name)
            res = parse_page(get(f["url"]))
            old = data["funds"].get(name, {})
            e = dict(name=name, matched=res["title"] or name, url=f["url"], as_of=res["as_of"], fund_size_cr=res["fund_size_cr"],
                     fetched=datetime.now().strftime("%d %b %Y %H:%M"), holdings=res["holdings"])
            if old.get("holdings") and old.get("as_of") and res["as_of"] and old["as_of"] != res["as_of"]:
                e["prev_as_of"], e["prev"] = old["as_of"], old["holdings"]          # keep last month for change tracking
            elif old.get("prev"):
                e["prev_as_of"], e["prev"] = old.get("prev_as_of"), old["prev"]
            data["funds"][name] = e
            tot = sum(h["p"] for h in res["holdings"])
            say(f"OK  {name}: {len(res['holdings'])} holdings, total {tot:.2f}%, as of {res['as_of']}"
                + (" (NEW MONTH)" if e.get("prev_as_of") and e["prev_as_of"] != old.get("prev_as_of") else ""))
            ok += 1
        except Exception as ex:
            fail += 1
            say(f"FAIL {name}: {ex}")
            if name in data["funds"]:
                data["funds"][name]["error"] = str(ex)[:200]
            else:
                data["funds"][name] = dict(name=name, matched=name, holdings=[], error=str(ex)[:200])
    data["funds"] = {k: v for k, v in data["funds"].items() if k in {f["name"] for f in funds}}
    data["log"] = LOG[-40:]
    data["updated"] = datetime.now().strftime("%d %b %Y %H:%M")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w"), separators=(",", ":"))
    json.dump(funds, open(FUNDS_FILE, "w"), indent=1)
    open("/tmp/mf_summary.md", "w").write("\n".join(f"- {l}" for l in LOG if l.startswith(("OK", "FAIL", "Added", "'"))) or "Nothing to do.")
    if ok == 0 and fail:
        sys.exit("No fund could be fetched - see the messages above.")


if __name__ == "__main__":
    main()
