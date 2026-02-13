import requests, json
from datetime import datetime

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

THRESHOLD = 0.97   # <<< change here if you want
FEE = 0.02         # conservative all-in friction assumption
SLUG_PREFIX = "btc-updown-15m-"

def iso_to_ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())

def fetch_btc15m_markets(max_pages=50, page_size=200):
    """Fetch closed BTC 15m markets from Gamma, newest first.

    The Gamma API has 1.3M+ markets.  btc-updown-15m markets are among
    the newest, so we paginate in descending ID order and stop early once
    we've scanned enough pages without finding new matches.
    """
    found, offset, empty_pages = [], 0, 0
    print(f"[fetch] Scanning Gamma for closed '{SLUG_PREFIX}*' markets …")
    for page in range(max_pages):
        r = requests.get(
            f"{GAMMA}/markets",
            params={
                "limit": page_size,
                "offset": offset,
                "closed": True,
                "order": "id",
                "ascending": False,
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break

        hits = [m for m in batch if (m.get("slug") or "").startswith(SLUG_PREFIX)]
        found.extend(hits)

        # Stop early if we've had 3 consecutive pages with no matches
        # after already finding some (means we've moved past the date range)
        if hits:
            empty_pages = 0
        else:
            empty_pages += 1
            if found and empty_pages >= 3:
                break

        offset += page_size
        print(f"  page {page+1}: {len(batch)} mkts, {len(hits)} btc-15m hits "
              f"(total found: {len(found)})", flush=True)

    print(f"[fetch] Done — {len(found)} btc-updown-15m markets found\n")
    return found

def prices(token, start, end):
    r = requests.get(
        f"{CLOB}/prices-history",
        params={"market":token,"startTs":start,"endTs":end,"fidelity":1},
        timeout=30
    )
    r.raise_for_status()
    return r.json().get("history",[])

def resolved_outcome(m):
    try:
        outs = json.loads(m.get("outcomes","[]"))
        prices = json.loads(m.get("outcomePrices","[]"))
        if prices and outs:
            i = max(range(len(prices)), key=lambda i: float(prices[i]))
            if float(prices[i]) > 0.9:
                return outs[i]
    except:
        pass
    return None

def run():
    mkts = fetch_btc15m_markets()

    if not mkts:
        print("ERROR: No btc-updown-15m markets found. Check API / network.")
        return

    closed = [m for m in mkts if m.get("closed")]
    print(f"[info] {len(mkts)} btc-15m markets total, {len(closed)} closed\n")

    trades, skipped = [], 0

    for m in closed:
        slug = m.get("slug", "?")

        # --- parse token IDs & outcomes ---
        try:
            token_ids = json.loads(m["clobTokenIds"])
            outcomes  = json.loads(m["outcomes"])
        except Exception as e:
            print(f"  [{slug}] SKIP — bad clobTokenIds/outcomes: {e}")
            skipped += 1
            continue

        if len(token_ids) != 2 or len(outcomes) != 2:
            print(f"  [{slug}] SKIP — expected 2 sides, got {len(token_ids)}")
            skipped += 1
            continue

        # --- compute 15-min window timestamps ---
        # endDate = end of the 15-min window.  Window start = endDate - 900s.
        # (startDate is market *creation* time, often ~24 h earlier — wrong for
        #  price queries.)
        end_ts   = iso_to_ts(m["endDate"])
        start_ts = end_ts - 900          # 15 minutes

        # --- fetch price history for both sides ---
        hists = []
        for tid in token_ids:
            h = prices(tid, start_ts, end_ts)
            hists.append(h)

        print(f"  [{slug}] tokens={[t[:8]+'…' for t in token_ids]}  "
              f"pts=[{len(hists[0])},{len(hists[1])}]", end="")

        # --- find the EARLIEST trigger across both sides ---
        # We must compare timestamps, not just take the first side that has
        # any point >= threshold.
        trigger = None
        for side_idx, hist in enumerate(hists):
            for p in hist:
                if p["p"] >= THRESHOLD:
                    if trigger is None or p["t"] < trigger["time"]:
                        trigger = {
                            "side": outcomes[side_idx],
                            "side_idx": side_idx,
                            "price": p["p"],
                            "time": p["t"],
                        }
                    break   # only need the first hit per side

        if not trigger:
            print("  → no trigger")
            continue

        # --- determine resolved winner ---
        winner = resolved_outcome(m)

        # --- PnL ---
        won = trigger["side"] == winner
        pnl = (1 - trigger["price"] - FEE) if won else -(trigger["price"] + FEE)

        secs_to_expiry = end_ts - trigger["time"]
        print(f"  → {trigger['side']}@{trigger['price']:.3f}  "
              f"winner={winner}  pnl={pnl:+.4f}  "
              f"tte={secs_to_expiry}s")

        trades.append({
            "slug": slug,
            "side": trigger["side"],
            "price": round(trigger["price"], 4),
            "winner": winner,
            "won": won,
            "pnl": round(pnl, 4),
            "tte_secs": secs_to_expiry,
        })

    # --- summary ---
    print("\n" + "=" * 60)
    wins = [t for t in trades if t["pnl"] > 0]
    n = len(trades)
    print(f"Markets scanned : {len(closed)}")
    print(f"Skipped (bad data): {skipped}")
    print(f"Trades           : {n}")
    print(f"Wins             : {len(wins)} ({len(wins)/max(1,n)*100:.1f}%)")
    print(f"Avg PnL / trade  : {sum(t['pnl'] for t in trades)/max(1,n):.4f}")
    if trades:
        print(f"Total PnL        : {sum(t['pnl'] for t in trades):.4f}")
        ttes = [t["tte_secs"] for t in trades]
        print(f"Avg TTE (secs)   : {sum(ttes)/len(ttes):.0f}")
    print("=" * 60)
    print("\nAssumptions:")
    print(f"  - Entry at first price tick >= {THRESHOLD}")
    print(f"  - Fee/friction = {FEE} per contract")
    print("  - Fill assumed at observed price (no slippage model)")
    print("  - Read-only backtest; no live orders placed")

if __name__ == "__main__":
    run()
