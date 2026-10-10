"""Removes stocks from the 20/50 Crossover list. Used by the "Remove crossover: A, B" issue from the dashboard.
Standard library only - no price download needed."""
import json, os, re

LIST = "site/data/ema_cross_list.json"
title = os.environ.get("ISSUE_TITLE", "")
names = [x.strip().upper() for x in re.split(r"[,\n]", re.sub(r"(?i)^remove\s+crossovers?\s*:?", "", title)) if x.strip()]
d = json.load(open(LIST))
gone, missing = [], []
for n in names:
    hit = [s for s in d["stocks"] if s["symbol"].upper() == n]
    if hit:
        d["removed"][hit[0]["symbol"]] = hit[0]["signal_date"]       # remembered so a rerun on the same day does not add it back
        d["stocks"] = [s for s in d["stocks"] if s is not hit[0]]
        gone.append(n)
    else:
        missing.append(n)
json.dump(d, open(LIST, "w"))
lines = ([f"- Removed: {', '.join(gone)}"] if gone else []) + ([f"- Not in the list (already removed?): {', '.join(missing)}"] if missing else [])
open("/tmp/ema_remove_summary.md", "w").write("\n".join(lines) or "Nothing to remove.")
print("\n".join(lines))
