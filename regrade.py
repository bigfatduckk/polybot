import sys, sqlite3, time
sys.path.insert(0, "/root/polybot/bot")
import markets
from markets import DB_PATH  # falls back; we use explicit path
DB = "/root/polybot/bot/polymarket_bot.db"

def fill_pnl(side, price, size, yes_won):
    if side == "buy":
        return (1.0 - price) * size if yes_won else (-price) * size
    return (price - 1.0) * size if yes_won else price * size

conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
fills = conn.execute("SELECT id, market_id, side, price, size FROM fills ORDER BY id").fetchall()
# paper open-meteo settled pnl per fill (from settlements) for comparison
paper_sett = {r["market_id"]: r["pnl"] for r in conn.execute(
    "SELECT market_id, pnl FROM settlements WHERE pnl != 0")}

cache = {}
def mkt_res(mid):
    if mid in cache: return cache[mid]
    closed, outcome, prices = markets.fetch_resolution(mid)
    # only treat as resolved if prices are binary [1,0]/[0,1]
    resolved = closed and len(prices) >= 2 and prices[0] in (0.0,1.0) and prices[1] in (0.0,1.0) and prices[0]!=prices[1]
    cache[mid] = (resolved, outcome, prices)
    time.sleep(0.08)
    return cache[mid]

mkt_total = 0.0; mkt_wins = 0; mkt_losses = 0; resolved_n = 0; unresolved = 0
paper_on_resolved = 0.0
flips = []
for f in fills:
    resolved, outcome, prices = mkt_res(f["market_id"])
    if not resolved:
        unresolved += 1
        continue
    resolved_n += 1
    yes_won = (outcome == "yes")
    pnl = fill_pnl(f["side"], f["price"], f["size"], yes_won)
    mkt_total += pnl
    if pnl > 0: mkt_wins += 1
    elif pnl < 0: mkt_losses += 1
    p = paper_sett.get(f["market_id"])
    if p is not None:
        paper_on_resolved += p
        # flip = sign differs between market-grade and paper open-meteo grade
        if (p > 0) != (pnl > 0):
            flips.append((f["market_id"], round(p,2), round(pnl,2), outcome))

print("=== RE-GRADE: paper A fills graded against Polymarket market resolution ===")
print(f"total fills:            {len(fills)}")
print(f"resolved (market):      {resolved_n}")
print(f"unresolved (open/not-archived): {unresolved}")
print(f"market wins/losses:    {mkt_wins}W / {mkt_losses}L  (win rate {100*mkt_wins/(mkt_wins+mkt_losses):.1f}%)" if (mkt_wins+mkt_losses) else "no resolved")
print(f"market-graded pnl:     ${mkt_total:+.2f}")
print(f"paper open-meteo pnl on SAME resolved set: ${paper_on_resolved:+.2f}")
print(f"paper all-time settled (210): $+8906.51 (from /pnl)")
print(f"flips (sign change vs paper): {len(flips)}")
print("--- flips (market_id, paper_om_pnl, market_pnl, mkt_outcome) ---")
for x in flips[:40]:
    print(f"  {x[0]}  paper_OM={x[1]:+8.2f}  market={x[2]:+8.2f}  {x[3]}")
