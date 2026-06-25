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

For fill $i$, let $o_i$ be the outcome index it traded, $w$ the market's winning outcome index, and $\pi_i \in [0,1]$ the executed price. We put BUYs and SELLs on a common long axis (a SELL of an outcome at price $\pi$ is a long of the complement at $1-\pi$), giving a normalized price $p_i$ and win indicator $y_i$:

$$
p_i = \begin{cases} \pi_i & \text{if BUY} \\[2pt] 1-\pi_i & \text{if SELL} \end{cases}
\qquad\qquad
y_i = \begin{cases} \mathbf{1}[\,o_i = w\,] & \text{if BUY} \\[2pt] \mathbf{1}[\,o_i \neq w\,] & \text{if SELL} \end{cases}
$$

where $\mathbf{1}[\cdot]\in\{0,1\}$ indicates whether the held side won. The **edge** is
the realized return per \$1 share,

$$e_i = y_i - p_i \in [-1, 1].$$

A wallet with $n \ge 2$ scored fills is ranked by the **t-statistic** of its mean edge
against the null hypothesis of no skill, $\mathbb{E}[e] = 0$

$$t = \frac{\bar{e}\,\sqrt{n}}{\max(s_e,\,\varepsilon)}, \qquad
\bar{e} = \frac{1}{n}\sum_{i=1}^{n} e_i, \qquad
s_e = \sqrt{\frac{1}{n-1}\sum_{i=1}^{n}\left(e_i-\bar{e}\right)^2},$$

where we avoid zero-variance wallets by setting $\varepsilon = 0.05$ in practice. 

- **$\bar{e}$ (mean edge)** — alpha per trade. Positive = systematically took the
  winning side below fair value.
- **$t$ (the score)** — ranks wallets by *confidence* in their edge: a $+0.30$ edge
  over $150$ fills beats $+0.50$ over $4$, because $t$ scales with $\sqrt{n}$ and divides
  by the trade-to-trade dispersion $s_e$. This is what removes the dependence on raw
  trade count.

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
