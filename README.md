# Polymarket Edge — live wallet leaderboard

A local dashboard that ranks Polymarket wallets by **edge t-statistic** — a
sample-size-aware measure of trading skill — and keeps accumulating trade history
in the background so the picture sharpens over time.

```bash
python3 app.py                 # starts web server + background collector
# open http://127.0.0.1:8000
```

First launch seeds history (a one-time backfill of resolved markets), then the
collector polls Polymarket continuously. The dashboard auto-refreshes every 15 min
and lets you slice the leaderboard by:

- **lookback window** (1h / 6h / 24h / 7d / 30d / 1y / all) — who has edge *recently*
  vs *over the long run*;
- **sector** (politics / sports / crypto / finance / economy / geopolitics / tech /
  culture / weather) — to surface specialists.

## The metric: edge t-statistic

Per trade, sell-normalized so a SELL of outcome *i* at price *p* = a long of the
complement at *(1−p)*:

```
edge_i = outcome_i − price_i          # realized return per $1 share
t      = mean(edge) · √n / std(edge)  # how reliable that edge is
```

- **`edge`** (mean) — alpha per trade. Positive = systematically took the winning
  side below fair value.
- **`t-stat`** — ranks wallets by *confidence* in their edge. A `+0.30` edge over
  150 trades beats `+0.50` over 4, because the t-stat scales with `√n` and divides
  by the trade-to-trade variance. This is what removes the dependence on raw trade
  count. (std is floored at 0.05 so zero-variance wallets stay finite.)

Why not plain Brier? A Brier score on execution price measures *the market's*
accuracy, not the trader's — everyone trading the same market is scored against the
same consensus price, so it barely distinguishes them, and it actually penalizes the
sharp who buys an underpriced winner. Edge isolates the individual: it rewards taking
the correct side *below* fair value, which is exactly what alpha is.

## Architecture

| file | role |
|---|---|
| `store.py` | SQLite (WAL). `trades` (deduped fills, `edge`/`won` filled in on resolution) + `markets` (resolution cache). |
| `collector.py` | background thread: **ingest** global firehose every 60s → **resolve** markets from Gamma + score every 15 min → **backfill** seed on first run. Classifies markets into sectors from event tags. |
| `app.py` | stdlib HTTP server + dashboard. Starts the collector thread. |
| `dashboard.html` | Polymarket-style dark UI: ranked table, window + sector selectors, live status, auto-refresh. |

### Why a continuous collector

The public APIs only expose a shallow window of recent trades (the global feed is
capped at offset ~3000). The collector beats that by **polling and persisting
forever** — every poll captures whatever's new, so local history grows without
bound. The one-time backfill seeds older history from resolved markets (whose
per-market history pages deeper than the global cap, as long as it fits under
`--cap`).

## API

- `GET /api/leaderboard?window=24h&sector=crypto&min_trades=10&limit=200` → wallets ranked by t-stat
- `GET /api/sectors?window=24h` → scored-fill count per sector (for the chip labels)
- `GET /api/stats` → collector status (fills, scored, wallets, markets, last update)

## CLI

```bash
python3 app.py --port 8000 [--no-collector] [--no-backfill]
python3 collector.py --backfill-only --markets 200   # seed more history, then exit
python3 collector.py --balance --markets 360          # even out sector coverage
```

## Requirements

Python 3.8+ standard library only — no third-party packages.

## Caveats

- Only fills in **fully-resolved** markets are scored; short windows (1h/6h) only
  populate as short-lived markets (sports, hourly crypto) resolve.
- Markets can belong to **multiple sectors**, so sector fill-counts overlap and sum
  to more than the `all` total.
- Wallets are **not deduplicated** — one actor can hold many addresses.
- Resolved markets are backward-looking: this finds who *has been* right.
