"""Removes stocks from a dashboard list (20/50 Crossover or VTD 4H Buy). Used by the "Remove crossover: A, B" / "Remove VTD: A@2026-10-09 13:15" issues.
Standard library only - no price download needed."""
import json, os, re

LIST = os.environ.get("LIST_FILE", "site/data/ema_cross_list.json")
title = os.environ.get("ISSUE_TITLE", "")
names = [x.strip() for x in re.split(r"[,\n]", re.sub(r"(?i)^remove\s+(crossovers?|vtd)\s*:?", "", title)) if x.strip()]
d = json.load(open(LIST))
stamp = lambda s: s.get("signal_date") or s.get("signal_time")
gone, missing = [], []
for n in names:
    sym, _, when = n.partition("@")                       # "SYMBOL" removes every row of that stock, "SYMBOL@time" just one signal
    sym, when = sym.strip().upper(), when.strip()
    hit = [s for s in d["stocks"] if s["symbol"].upper() == sym and (not when or stamp(s) == when)]
    if hit:
        d["removed"][hit[0]["symbol"]] = max(stamp(s) for s in hit)     # remembered so a rerun does not add it back
        d["stocks"] = [s for s in d["stocks"] if s not in hit]
        gone.append(n)
    else:
        missing.append(n)
json.dump(d, open(LIST, "w"))
lines = ([f"- Removed: {', '.join(gone)}"] if gone else []) + ([f"- Not in the list (already removed?): {', '.join(missing)}"] if missing else [])
open("/tmp/ema_remove_summary.md", "w").write("\n".join(lines) or "Nothing to remove.")
print("\n".join(lines))
