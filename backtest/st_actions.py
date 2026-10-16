"""Applies Monthly Supertrend dashboard actions sent as GitHub issues (standard library only):
  "Remove Supertrend: SYM|side@signal_date, ..."   -> deletes stocks from the list (remembered so they are not added back)
  "Buy Supertrend: SYM|side@signal_date@price@qty" -> moves the stock from the list into open positions
  "Sell Supertrend: SYM@buy_date@price"            -> closes the position, P/L is locked in
  "Remove Supertrend trade: SYM@buy_date"          -> deletes a position / closed trade from the P/L table"""
import json, os, re
OUT = os.environ.get("OUT_DIR", "site/data")
LIST, POS = f"{OUT}/supertrend_list.json", f"{OUT}/st_positions.json"
title = os.environ.get("ISSUE_TITLE", "").strip()
today = os.environ.get("TODAY", "")
lst = json.load(open(LIST)) if os.path.exists(LIST) else {"stocks": [], "removed": {}}
pos = json.load(open(POS)) if os.path.exists(POS) else {"open": [], "closed": []}
msg = []
low = title.lower()
if low.startswith("remove supertrend trade"):
    for n in [x.strip() for x in title.split(":", 1)[1].split(",") if x.strip()]:
        sym, _, bd = n.partition("@")
        b = len(pos["open"]) + len(pos["closed"])
        pos["open"] = [p for p in pos["open"] if not (p["symbol"] == sym and p["buy_date"] == bd)]
        pos["closed"] = [p for p in pos["closed"] if not (p["symbol"] == sym and p["buy_date"] == bd)]
        msg.append(f"- Removed trade {n}" if b != len(pos["open"]) + len(pos["closed"]) else f"- Trade not found: {n}")
elif low.startswith("remove supertrend"):
    for n in [x.strip() for x in title.split(":", 1)[1].split(",") if x.strip()]:
        ks, _, sd = n.partition("@")
        sym, _, side = ks.partition("|")
        hit = [s for s in lst["stocks"] if s["symbol"] == sym and (not side or s["side"] == side) and (not sd or s["signal_date"] == sd)]
        for s in hit:
            lst["removed"][f"{s['symbol']}|{s['side']}"] = s["signal_date"]
        lst["stocks"] = [s for s in lst["stocks"] if s not in hit]
        msg.append(f"- Removed: {n}" if hit else f"- Not in the list (already removed?): {n}")
elif low.startswith("buy supertrend"):
    ks, sd, price, qty = (title.split(":", 1)[1].strip().split("@") + ["", "", "", "1"])[:4]
    sym, _, side = ks.partition("|")
    hit = [s for s in lst["stocks"] if s["symbol"] == sym and (not side or s["side"] == side) and (not sd or s["signal_date"] == sd)]
    price, qty = float(price), int(float(qty or 1))
    if hit and price > 0 and qty > 0 and not any(p["symbol"] == sym and p["signal_date"] == hit[0]["signal_date"] for p in pos["open"] + pos["closed"]):
        s = hit[0]
        pos["open"].append(dict(symbol=sym, tv=s["tv"], company=s.get("company", ""), side=s["side"], signal_date=s["signal_date"], buy_date=today,
                                buy_price=price, qty=qty, last_close=s.get("last_close", price)))
        lst["removed"][f"{sym}|{s['side']}"] = s["signal_date"]
        lst["stocks"] = [x for x in lst["stocks"] if x is not s]
        msg.append(f"- Bought {sym}: {qty} @ {price} - moved to the P/L table")
    else:
        msg.append(f"- Could not buy {sym} (not in the list, already bought, or bad price/quantity)")
elif low.startswith("sell supertrend"):
    sym, bd, price = (title.split(":", 1)[1].strip().split("@") + ["", "", ""])[:3]
    hit = [p for p in pos["open"] if p["symbol"] == sym and (not bd or p["buy_date"] == bd)]
    if hit and float(price) > 0:
        p = hit[0]
        pos["open"].remove(p)
        pos["closed"].insert(0, dict(p, sell_price=float(price), sell_date=today))
        msg.append(f"- Closed {sym} at {price}")
    else:
        msg.append(f"- No open position found for {sym}")
json.dump(lst, open(LIST, "w"))
json.dump(pos, open(POS, "w"))
open("/tmp/st_summary.md", "w").write("\n".join(msg) or "Nothing to do.")
print("\n".join(msg))
