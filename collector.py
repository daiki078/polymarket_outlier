"""Continuous data collector for the Polymarket edge dashboard.

The public APIs only expose a shallow window of recent trades (the global feed is
capped at offset ~3000). The collector defeats that by *polling continuously* and
persisting everything new, so history accumulates locally over time:

  ingest()   -- page the global trades firehose, INSERT OR IGNORE new fills
  resolve()  -- look up resolutions for markets we hold trades in, then score
  backfill() -- one-time seed: pull full history of resolved markets in a volume band

run_forever() loops ingest every POLL_SECS and resolves periodically.
"""
import json, time, re, urllib.request, urllib.error
import store

DATA = "https://data-api.polymarket.com/trades"
GAMMA = "https://gamma-api.polymarket.com/markets"
EVENTS = "https://gamma-api.polymarket.com/events"

# Broad sectors for the dashboard. SECTOR_TAGS = authoritative Gamma tag slugs;
# SECTOR_WORDS = word-boundary fallback for markets we only have title text for.
SECTOR_TAGS = {
    "politics": {"politics", "elections", "us-election", "us-elections", "trump",
        "biden", "kamala-harris", "congress", "senate", "house-races", "gop",
        "democratic-party", "republican-party", "potus", "governor", "2024-election"},
    "sports": {"sports", "nba", "nfl", "mlb", "nhl", "soccer", "football",
        "basketball", "baseball", "tennis", "ufc", "mma", "boxing", "golf", "f1",
        "formula-1", "cricket", "esports", "csgo", "cs2", "dota", "dota2",
        "valorant", "lol", "league-of-legends", "olympics", "epl", "premier-league",
        "la-liga", "champions-league", "nascar", "wnba", "games"},
    "crypto": {"crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "altcoins",
        "memecoins", "defi", "nft", "dogecoin", "xrp", "ripple", "bnb", "hype",
        "cardano", "litecoin"},
    "finance": {"finance", "stocks", "stock-market", "etf", "sp500", "nasdaq",
        "earnings", "ipo", "dividends", "bonds"},
    "economy": {"economy", "inflation", "gdp", "recession", "jobs", "unemployment",
        "cpi", "interest-rates", "fed", "federal-reserve", "macro", "rate-cut"},
    "geopolitics": {"geopolitics", "war", "israel", "ukraine", "russia", "china",
        "iran", "gaza", "middle-east", "nato", "international-affairs", "nuclear",
        "taiwan", "north-korea", "venezuela"},
    "tech": {"tech", "technology", "ai", "artificial-intelligence", "openai",
        "claude", "anthropic", "google", "apple", "tesla", "spacex", "meta",
        "microsoft", "nvidia"},
    "culture": {"culture", "entertainment", "movies", "music", "awards", "oscars",
        "celebrity", "tv", "pop-culture", "grammys"},
    "weather": {"weather", "hurricane", "temperature", "climate", "wildfire",
        "storm", "snow"},
}
SECTOR_WORDS = {
    "politics": ["election", "president", "senate", "congress", "trump", "biden",
        "harris", "governor", "primary", "gop", "democrat", "republican"],
    "crypto": ["bitcoin", "ethereum", "solana", "crypto", "dogecoin", "xrp", "btc", "eth"],
    "finance": ["stock", "nasdaq", "earnings", "ipo"],
    "economy": ["inflation", "gdp", "recession", "unemployment", "cpi"],
    "geopolitics": ["war", "israel", "ukraine", "russia", "china", "iran", "gaza",
        "nato", "nuclear", "taiwan"],
    "tech": ["openai", "google", "apple", "tesla", "spacex", "nvidia", "chatgpt"],
    "culture": ["movie", "album", "oscar", "grammy", "celebrity"],
    "weather": ["hurricane", "temperature", "weather", "wildfire", "snow", "storm"],
}
SECTORS = ["politics", "sports", "crypto", "finance", "economy",
           "geopolitics", "tech", "culture", "weather"]


def classify(tags, text):
    """Return a '|sec|sec|' membership string (or '' if none) from event tag slugs
    plus a word-boundary scan of the market title."""
    tagset = {str(t).lower() for t in (tags or [])}
    text = (text or "").lower()
    out = set()
    for sec, slugs in SECTOR_TAGS.items():
        if tagset & slugs:
            out.add(sec)
    for sec, words in SECTOR_WORDS.items():
        if sec in out:
            continue
        if any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words):
            out.add(sec)
    return "|" + "|".join(sorted(out)) + "|" if out else ""

POLL_SECS = 60          # firehose ingest cadence
RESOLVE_EVERY = 15 * 60  # resolution/scoring cadence (seconds)
RESOLVE_BATCH = 120      # max market resolutions to look up per resolve pass
FEED_MAX_OFFSET = 3000   # API hard cap on the global feed
PAGE = 500


def _get(url, retries=4):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pmedge/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError):
            if i == retries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


def ingest(conn, log=print):
    """Page the global feed newest-first; stop when a page adds nothing new."""
    total_new, offset = 0, 0
    while offset < FEED_MAX_OFFSET:
        batch = _get(f"{DATA}?limit={PAGE}&offset={offset}")
        if not isinstance(batch, list) or not batch:
            break
        new = store.insert_trades(conn, batch)
        total_new += new
        # firehose is time-ordered; once a full page is all-dupes we've caught up
        if new == 0 and offset > 0:
            break
        if len(batch) < PAGE:
            break
        offset += PAGE
    store.set_meta(conn, "last_ingest", int(time.time()))
    if total_new:
        log(f"[ingest] +{total_new} new fills")
    return total_new


def _win_index(market):
    try:
        prices = json.loads(market["outcomePrices"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None, None
    if len(prices) != 2:
        return None, prices
    ones = [i for i, p in enumerate(prices) if abs(float(p) - 1.0) < 1e-9]
    return (ones[0] if len(ones) == 1 else None), prices


def resolve(conn, log=print, batch=RESOLVE_BATCH):
    """Look up resolutions for unresolved markets we hold trades in, then score."""
    cids = store.unresolved_condition_ids(conn, batch)
    checked = 0
    for cid in cids:
        m = _get(f"{GAMMA}?condition_ids={cid}")
        now = int(time.time())
        if not isinstance(m, list) or not m:
            store.upsert_market(conn, cid, "", 0, None, now)
            continue
        mk = m[0]
        closed = mk.get("closed")
        win, _ = _win_index(mk) if closed else (None, None)
        rts = store.parse_ts(mk.get("closedTime")) if closed else None
        # markets endpoint omits tags -> classify from question text
        secs = classify([], mk.get("question", "")) if closed else None
        store.upsert_market(conn, cid, mk.get("question", ""), closed, win, now, rts, secs)
        checked += 1
    scored = store.score_pending(conn)
    store.set_meta(conn, "last_resolve", int(time.time()))
    if checked or scored:
        log(f"[resolve] checked {checked} markets, scored {scored} fills")
    return checked, scored


def backfill(conn, min_vol=5000, max_vol=600000, n_markets=200, cap=2500,
             order="volumeNum", tag=None, log=print):
    """Seed history via the EVENTS endpoint (which carries sector tags). Pulls full
    trade history of resolved binary markets small enough not to be truncated.
      order='volumeNum'  -> biggest events (long-run history; all/30d/1y windows)
      order='closedTime' -> most recently resolved (populates 1h/6h/24h windows)
      tag='politics'     -> restrict to one sector (balances coverage)"""
    tagq = f"&tag_slug={tag}" if tag else ""
    log(f"[backfill:{order}{'/'+tag if tag else ''}] seeding up to {n_markets} "
        f"resolved markets (event vol ${min_vol:,.0f}-${max_vol:,.0f})...")
    scored_mkts, offset = 0, 0
    while scored_mkts < n_markets and offset < 4000:
        url = (f"{EVENTS}?closed=true&limit=100&offset={offset}"
               f"&volume_num_min={min_vol}&volume_num_max={max_vol}"
               f"&order={order}{tagq}&ascending=false")
        page = _get(url)
        if not isinstance(page, list) or not page:
            break
        for ev in page:
            tags = [t.get("slug") for t in (ev.get("tags") or [])]
            for mk in ev.get("markets", []):
                if scored_mkts >= n_markets:
                    break
                if not mk.get("closed"):
                    continue
                cid = mk.get("conditionId")
                win, _ = _win_index(mk)
                if not cid or win is None:
                    continue
                if float(mk.get("volumeNum") or 0) < 300:
                    continue
                trades, truncated = _market_trades(cid, cap)
                if truncated:
                    continue
                secs = classify(tags, mk.get("question") or ev.get("title"))
                store.upsert_market(conn, cid, mk.get("question", ""), True, win,
                                    int(time.time()),
                                    store.parse_ts(mk.get("closedTime")), secs)
                store.insert_trades(conn, trades)
                scored_mkts += 1
                if scored_mkts % 20 == 0:
                    store.score_pending(conn)
                    log(f"[backfill:{order}] {scored_mkts} markets seeded")
        offset += 100
    n = store.score_pending(conn)
    log(f"[backfill:{order}] done: {scored_mkts} markets, {n} fills scored this pass")
    return scored_mkts


def reclassify(conn, max_pages=40, log=print):
    """Fast pass: walk events and assign sectors to markets we already hold trades
    for, without re-fetching trades. Covers history ingested before sectors existed."""
    have = store.known_condition_ids(conn)
    updated, offset = 0, 0
    for order in ("volumeNum", "closedTime"):
        offset = 0
        while offset < max_pages * 100:
            page = _get(f"{EVENTS}?closed=true&limit=100&offset={offset}&order={order}&ascending=false")
            if not isinstance(page, list) or not page:
                break
            for ev in page:
                tags = [t.get("slug") for t in (ev.get("tags") or [])]
                for mk in ev.get("markets", []):
                    cid = mk.get("conditionId")
                    if cid in have:
                        secs = classify(tags, mk.get("question") or ev.get("title"))
                        store.set_sectors(conn, cid, secs)
                        updated += 1
            conn.commit()
            offset += 100
            if updated >= len(have):
                break
    conn.commit()
    log(f"[reclassify] tagged {updated} markets with sectors")
    return updated


def _market_trades(cid, cap):
    out, offset = [], 0
    while offset < cap:
        batch = _get(f"{DATA}?market={cid}&limit={PAGE}&offset={offset}")
        if not isinstance(batch, list) or not batch:
            return out, False
        out.extend(batch)
        if len(batch) < PAGE:
            return out, False
        offset += PAGE
    return out, True


def run_forever(do_backfill=True, log=print):
    store.init()
    conn = store.connect()
    if do_backfill:
        if (store.stats(conn)["scored"] or 0) < 1000:
            backfill(conn, order="volumeNum", log=log)   # long-run history
        # recently-resolved short-lived markets -> populates 1h/6h/24h windows
        backfill(conn, order="closedTime", min_vol=2000, n_markets=200, log=log)
        reclassify(conn, log=log)                        # tag any unsectored history
    last_resolve = 0
    while True:
        try:
            ingest(conn, log=log)
            if time.time() - last_resolve >= RESOLVE_EVERY:
                resolve(conn, log=log)
                last_resolve = time.time()
        except Exception as e:  # keep the loop alive no matter what
            log(f"[collector] error: {e!r}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-only", action="store_true")
    ap.add_argument("--balance", action="store_true",
                    help="sector-targeted backfill to even out coverage, then exit")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--markets", type=int, default=200)
    a = ap.parse_args()
    store.init()
    c = store.connect()
    if a.balance:
        for s in ["politics", "sports", "finance", "economy", "geopolitics",
                  "tech", "culture", "weather", "crypto"]:
            backfill(c, tag=s, order="closedTime", min_vol=2000, max_vol=300000,
                     n_markets=max(40, a.markets // 9), log=lambda m: print(m, flush=True))
        print(store.stats(c))
    elif a.backfill_only:
        backfill(c, n_markets=a.markets)
        print(store.stats(c))
    else:
        def _log(m):
            print(m, flush=True)
        run_forever(do_backfill=not a.no_backfill, log=_log)
