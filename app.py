"""Local Polymarket-edge dashboard.

Run:  python3 app.py            (starts web server + background collector)
      python3 app.py --no-collector --port 8000

Serves:
  GET /                       the dashboard (Polymarket-style dark UI)
  GET /api/leaderboard?...    wallets ranked by edge t-statistic
  GET /api/stats              collector status
"""
import json, os, threading, time, argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import store
import collector

HERE = os.path.dirname(os.path.abspath(__file__))

WINDOWS = {  # label -> seconds (None = all time)
    "1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 86400,
    "30d": 30 * 86400, "1y": 365 * 86400, "all": None,
}

# one sqlite connection per thread
_local = threading.local()


def _conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = store.connect()
    return c


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read().decode(), "text/html; charset=utf-8")
        if u.path == "/api/stats":
            return self._send(200, store.stats(_conn()))
        if u.path == "/api/sectors":
            q = parse_qs(u.query)
            secs = WINDOWS.get(q.get("window", ["24h"])[0], WINDOWS["24h"])
            since = 0 if secs is None else int(time.time()) - secs
            return self._send(200, store.sector_counts(_conn(), since))
        if u.path == "/api/leaderboard":
            q = parse_qs(u.query)
            win = q.get("window", ["24h"])[0]
            secs = WINDOWS.get(win, WINDOWS["24h"])
            since = 0 if secs is None else int(time.time()) - secs
            min_trades = max(1, int(q.get("min_trades", ["10"])[0]))
            limit = min(1000, int(q.get("limit", ["200"])[0]))
            sector = q.get("sector", ["all"])[0]
            data = store.leaderboard(_conn(), since, min_trades, limit, sector=sector)
            return self._send(200, {"window": win, "sector": sector,
                                    "min_trades": min_trades,
                                    "count": len(data), "rows": data})
        return self._send(404, {"error": "not found"})


def _collector_thread(do_backfill):
    def log(m):
        print(m, flush=True)
    try:
        collector.run_forever(do_backfill=do_backfill, log=log)
    except Exception as e:
        print(f"[collector] fatal: {e!r}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-collector", action="store_true")
    ap.add_argument("--no-backfill", action="store_true")
    a = ap.parse_args()

    store.init()
    if not a.no_collector:
        threading.Thread(target=_collector_thread, args=(not a.no_backfill,),
                         daemon=True).start()
        print("[collector] started in background", flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Dashboard:  http://127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
