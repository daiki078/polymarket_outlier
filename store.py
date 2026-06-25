"""SQLite storage for the Polymarket edge dashboard.

Two tables:
  trades  -- one row per fill we've ever seen (deduped). `edge`/`won` are filled
             in once the fill's market resolves; NULL means not-yet-scorable.
  markets -- resolution cache: win_index is the winning outcome (NULL = open/ambiguous)

WAL mode + per-thread connections so the collector thread can write while the web
threads read.
"""
import sqlite3, os, math
from datetime import datetime, timezone


def parse_ts(s):
    """Parse Gamma time strings ('2026-06-25 20:12:23+00', '2024-11-06T15:17:41Z')
    into a unix int, or None."""
    if not s:
        return None
    s = str(s).strip().replace("T", " ").replace("Z", "+00:00")
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pmedge.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            TEXT PRIMARY KEY,
    wallet        TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    asset         TEXT,
    side          TEXT,
    price         REAL,
    size          REAL,
    ts            INTEGER,
    outcome_index INTEGER,
    title         TEXT,
    edge          REAL,   -- won - price (sell-normalized); NULL until scored
    won           REAL     -- 1/0 (sell-normalized);        NULL until scored
);
CREATE INDEX IF NOT EXISTS ix_trades_ts     ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_cond   ON trades(condition_id);
CREATE INDEX IF NOT EXISTS ix_trades_wallet ON trades(wallet);
CREATE INDEX IF NOT EXISTS ix_trades_scored ON trades(ts) WHERE edge IS NOT NULL;

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    question     TEXT,
    closed       INTEGER,
    win_index    INTEGER,   -- NULL = unresolved / not clean-binary
    resolved_ts  INTEGER,   -- when the market actually settled (Gamma closedTime)
    sectors      TEXT,      -- "|politics|crypto|" style membership (NULL = unclassified)
    checked_ts   INTEGER
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    # migrate older DBs that predate resolved_ts
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(markets)").fetchall()]
    if "resolved_ts" not in cols:
        conn.execute("ALTER TABLE markets ADD COLUMN resolved_ts INTEGER")
    if "sectors" not in cols:
        conn.execute("ALTER TABLE markets ADD COLUMN sectors TEXT")
    conn.commit()
    conn.close()


def trade_id(t):
    return (f"{t.get('transactionHash','')}|{t.get('asset','')}|{t.get('proxyWallet','')}"
            f"|{t.get('timestamp','')}|{t.get('price','')}|{t.get('size','')}|{t.get('side','')}")


def insert_trades(conn, trades):
    """Insert raw API trade dicts. Returns count of *new* rows."""
    rows = []
    for t in trades:
        try:
            rows.append((
                trade_id(t), t["proxyWallet"], t["conditionId"], t.get("asset"),
                t.get("side"), float(t["price"]), float(t.get("size") or 0),
                int(t["timestamp"]), int(t["outcomeIndex"]), t.get("title"),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO trades "
        "(id,wallet,condition_id,asset,side,price,size,ts,outcome_index,title) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


def upsert_market(conn, condition_id, question, closed, win_index, checked_ts,
                  resolved_ts=None, sectors=None):
    conn.execute(
        "INSERT INTO markets (condition_id,question,closed,win_index,resolved_ts,sectors,checked_ts) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(condition_id) DO UPDATE SET "
        "question=excluded.question, closed=excluded.closed, "
        "win_index=excluded.win_index, "
        "resolved_ts=COALESCE(excluded.resolved_ts, markets.resolved_ts), "
        "sectors=COALESCE(excluded.sectors, markets.sectors), "
        "checked_ts=excluded.checked_ts",
        (condition_id, question, int(bool(closed)), win_index, resolved_ts, sectors, checked_ts))
    conn.commit()


def set_sectors(conn, condition_id, sectors):
    conn.execute("UPDATE markets SET sectors=? WHERE condition_id=?",
                 (sectors, condition_id))


def known_condition_ids(conn):
    return {r["condition_id"] for r in
            conn.execute("SELECT DISTINCT condition_id FROM trades").fetchall()}


def score_pending(conn):
    """Score any unscored trades whose market is now resolved. Returns rows scored."""
    pend = conn.execute(
        "SELECT DISTINCT t.condition_id AS cid, m.win_index AS win "
        "FROM trades t JOIN markets m ON t.condition_id=m.condition_id "
        "WHERE t.edge IS NULL AND m.win_index IS NOT NULL").fetchall()
    total = 0
    for r in pend:
        cid, win = r["cid"], r["win"]
        # sell-normalized: SELL of outcome i == long the complement at (1-price)
        cur = conn.execute(
            "UPDATE trades SET "
            " won = CASE WHEN side='SELL' "
            "         THEN (CASE WHEN outcome_index=:win THEN 0.0 ELSE 1.0 END) "
            "         ELSE (CASE WHEN outcome_index=:win THEN 1.0 ELSE 0.0 END) END, "
            " edge = (CASE WHEN side='SELL' "
            "         THEN (CASE WHEN outcome_index=:win THEN 0.0 ELSE 1.0 END) "
            "         ELSE (CASE WHEN outcome_index=:win THEN 1.0 ELSE 0.0 END) END) "
            "        - (CASE WHEN side='SELL' THEN 1.0-price ELSE price END) "
            "WHERE condition_id=:cid AND edge IS NULL",
            {"win": win, "cid": cid})
        total += cur.rowcount
    conn.commit()
    return total


def unresolved_condition_ids(conn, limit):
    """condition_ids that have trades but no known resolution yet (oldest-checked first)."""
    rows = conn.execute(
        "SELECT DISTINCT t.condition_id AS cid FROM trades t "
        "LEFT JOIN markets m ON t.condition_id=m.condition_id "
        "WHERE m.condition_id IS NULL OR (m.win_index IS NULL AND m.closed=0) "
        "LIMIT ?", (limit,)).fetchall()
    return [r["cid"] for r in rows]


def get_meta(conn, k, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_meta(conn, k, v):
    conn.execute("INSERT INTO meta (k,v) VALUES (?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()


def stats(conn):
    row = conn.execute(
        "SELECT COUNT(*) n, "
        "       SUM(CASE WHEN edge IS NOT NULL THEN 1 ELSE 0 END) scored, "
        "       MAX(ts) newest, MIN(ts) oldest, "
        "       COUNT(DISTINCT wallet) wallets FROM trades").fetchone()
    mk = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN win_index IS NOT NULL THEN 1 ELSE 0 END) resolved "
        "FROM markets").fetchone()
    return {
        "trades": row["n"] or 0,
        "scored": row["scored"] or 0,
        "wallets": row["wallets"] or 0,
        "newest_ts": row["newest"],
        "oldest_ts": row["oldest"],
        "markets": mk["n"] or 0,
        "markets_resolved": mk["resolved"] or 0,
        "last_ingest": get_meta(conn, "last_ingest"),
        "last_resolve": get_meta(conn, "last_resolve"),
    }


SECTORS = ["politics", "sports", "crypto", "finance", "economy",
           "geopolitics", "tech", "culture", "weather"]


def sector_counts(conn, since_ts):
    """Scored-fill count per sector within the resolution window (for UI labels)."""
    out = {"all": 0}
    base = ("SELECT COUNT(*) FROM trades t JOIN markets m ON t.condition_id=m.condition_id "
            "WHERE t.edge IS NOT NULL AND COALESCE(m.resolved_ts,t.ts) >= ?")
    out["all"] = conn.execute(base, (since_ts,)).fetchone()[0]
    for s in SECTORS:
        out[s] = conn.execute(base + " AND m.sectors LIKE ?",
                              (since_ts, f"%|{s}|%")).fetchone()[0]
    return out


def leaderboard(conn, since_ts, min_trades, limit, std_floor=0.05, sector="all"):
    """Per-wallet edge t-statistic over scored trades with ts >= since_ts.

    t = mean(edge) * sqrt(n) / std(edge)   -- sample-size-aware edge.
    std is floored at `std_floor` so degenerate zero-variance wallets stay finite.
    `sector` filters to markets tagged with that sector ('all' = no filter).
    """
    params = [since_ts]
    sector_sql = ""
    if sector and sector != "all":
        sector_sql = "AND m.sectors LIKE ? "
        params.append(f"%|{sector}|%")
    params.append(min_trades)
    # Window by when the market RESOLVED (when the bet paid off), not when the
    # trade was placed -- "recent performance" means recently-settled bets. Fall
    # back to trade time for markets whose resolution time we never recorded.
    rows = conn.execute(
        "SELECT t.wallet wallet, COUNT(*) n, SUM(t.edge) s1, SUM(t.edge*t.edge) s2, "
        "       SUM(t.won) hits, SUM(t.size*t.edge) pnl, SUM(t.size*t.price) vol "
        "FROM trades t JOIN markets m ON t.condition_id = m.condition_id "
        "WHERE t.edge IS NOT NULL AND COALESCE(m.resolved_ts, t.ts) >= ? "
        + sector_sql +
        "GROUP BY t.wallet HAVING n >= ?", params).fetchall()
    out = []
    for r in rows:
        n = r["n"]
        mean = r["s1"] / n
        var = (r["s2"] - r["s1"] * r["s1"] / n) / (n - 1) if n > 1 else 0.0
        std = math.sqrt(var) if var > 0 else 0.0
        t = mean * math.sqrt(n) / max(std, std_floor)
        out.append({
            "wallet": r["wallet"],
            "n": n,
            "tstat": round(t, 3),
            "edge": round(mean, 4),
            "edge_std": round(std, 4),
            "hit_rate": round(r["hits"] / n, 4),
            "pnl": round(r["pnl"], 2),
            "volume": round(r["vol"], 2),
        })
    out.sort(key=lambda x: x["tstat"], reverse=True)
    return out[:limit]
