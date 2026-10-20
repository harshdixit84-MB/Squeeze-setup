"""Cross-check: runs the dashboard engine (api/_engine.js, via node) and the Python reference (backtest_ema.py) on the same
synthetic price history and compares every trade. Offline - no Angel login needed.   Usage: python3 backtest/parity_check.py"""
import datetime as dt, json, os, random, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import backtest_ema as py


def synth(seed, start="2013-01-07", end="2026-10-16", split_on="2019-03-11"):
    rnd, d, price, out = random.Random(seed), dt.date.fromisoformat(start), 100.0, []
    stop = dt.date.fromisoformat(end)
    drift, left, factor = 0.0, 0, 1.0
    while d <= stop:
        if d.weekday() < 5:
            if left <= 0:
                drift, left = rnd.choice([0.0016, 0.0009, -0.0012, 0.0002, -0.0005]), rnd.randint(40, 160)
            left -= 1
            o = price * (1 + rnd.gauss(0, 0.004))
            c = o * (1 + drift + rnd.gauss(0, 0.018))
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.008)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.008)))
            if split_on and d.isoformat() == split_on:
                factor = 0.5                                  # a 2:1 split that Angel did not adjust
            out.append(dict(date=d.isoformat(), open=o * factor, high=h * factor, low=l * factor, close=c * factor))
            price = c
        d += dt.timedelta(days=1)
    return out


JS = """
const fs=require('fs'),E=require(process.argv[1]+'/api/_engine.js');
const a=JSON.parse(fs.readFileSync(process.argv[2]));
const f=E.fixSplits(a.daily);
const r=E.runBacktest(f.rows,a.tf,{today:a.today,from:a.from});
console.log(JSON.stringify({trades:r.trades,notes:f.notes}));
"""


def compare(name, daily, tf, today, from_date=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(dict(daily=daily, tf=tf, today=today, **({"from": from_date} if from_date else {})), fh)
    js = json.loads(subprocess.run(["node", "-e", JS, ROOT, fh.name], capture_output=True, text=True, check=True).stdout)
    os.unlink(fh.name)
    rows, notes = py.fix_splits(daily)
    mine = py.run_backtest(rows, tf, today, from_date)
    ok = notes == js["notes"] and len(mine) == len(js["trades"])
    for a, b in zip(mine, js["trades"]):
        for key, v in a.items():
            w = b.get(key)
            same = (abs(v - w) <= 0.011) if isinstance(v, float) and isinstance(w, (int, float)) else v == w
            ok = ok and same
            if not same:
                print(f"   MISMATCH trade {a['trade']} {key}: python={v} js={w}")
    kinds = {}
    for t in mine:
        kinds[t["reason"]] = kinds.get(t["reason"], 0) + 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {len(mine)} trades (js {len(js['trades'])}), split fixes {len(notes)}, {kinds}")
    return ok


if __name__ == "__main__":
    allok = True
    sunday = dict(date="2026-10-18", dow=0, minutes=0)
    for seed in (1, 2, 3, 4, 5, 6):
        allok &= compare(f"weekly seed {seed}", synth(seed), "W", sunday, "2016-10-18")
    allok &= compare("monthly seed 7", synth(7), "M", sunday)
    # mid-week: the running week must not give a signal, but an entry on its Monday open must still be tracked
    d = synth(8)
    wed = [r for r in d if r["date"] <= "2026-10-14"]
    allok &= compare("weekly, today = Wednesday (running week)", wed, "W", dict(date="2026-10-14", dow=3, minutes=700))
    print("\nALL MATCH" if allok else "\nDIFFERENCES FOUND")
    sys.exit(0 if allok else 1)
