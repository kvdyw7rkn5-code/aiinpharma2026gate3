"""Read-only jury dashboard over the ledger.   python3 dashboard.py --mode REALTIME  ->  http://localhost:8050

Serves the web app in web/ (index.html, app.css, app.js) and these JSON endpoints:
  /api/state          live monitor (incremental, only changes are processed)
  /api/analysis       full-data analysis of the current ledger
  /api/models         model comparison report (models_report.json)
  /api/lab/datasets   datasets and model descriptions for the model lab
  /api/lab?...        train / test / twin replay for the selected models (in memory only)
  /api/urs            URS-DI-001..005 assertions, recomputed from the ledger on every request
  /api/unit?id=...    end-to-end trace of one unit (record, raw response, bag lot, reel, prediction, result)
  /api/ledger         ledger browser · /api/verify re-checks the hash chain · /api/export downloads it all

The dashboard opens the ledger with SQLite mode=ro, so it can never change records (URS-DI-005).
All derived values shown are recomputed from the stored original records with the frozen model.
"""
import argparse
import gzip
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import audit
import lab
from analysis import analyze
from model import LineMonitor, MaterialBook
from store import Ledger

LINES = ("ASM-01", "ASM-02", "ASM-03")
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css"),
       "/app.js": ("app.js", "application/javascript"),
       "/chart.umd.min.js": ("chart.umd.min.js", "application/javascript")}   # Chart.js 4.4.1, served locally (works offline)


def _np(o):
    return o.item() if hasattr(o, "item") else str(o)


def _gz(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, default=_np).encode()
    return gzip.compress(raw, 5)


class LiveState:
    """Incremental view of the ledger: only entries newer than the last request are processed."""

    def __init__(self, db, mode):
        self.db, self.mode, self.lock = db, mode, threading.Lock()
        self.led = Ledger(db, readonly=True)
        self.seq, self.tip, self.body = 0, None, None
        self.model, self.book, self.mons = None, MaterialBook(), {}
        self.pred, self.eval, self.alerts = {}, {}, []

    def current_tip(self):
        return self.led.db.execute("SELECT coalesce(max(seq), 0) FROM ledger").fetchone()[0]

    def get(self):
        with self.lock:
            tip = self.current_tip()
            if tip == self.tip:
                return tip, self.body
            for r in self.led.db.execute("SELECT * FROM ledger WHERE seq > ? ORDER BY seq", (self.seq,)):
                self._apply(dict(r))
                self.seq = r["seq"]
            ok, n, problem = self.led.verify()
            self.tip, self.body = tip, _gz(self._build(ok, n, problem, tip))
            return tip, self.body

    def _apply(self, r):
        kind = r["kind"]
        if kind == "MODEL":
            self.model = json.loads(r["payload"])
            return
        if r["mode"] != self.mode:
            return
        if kind == "BAG":
            self.book.add_bag(json.loads(r["payload"]))
        elif kind == "PVC":
            self.book.add_pvc(json.loads(r["payload"]))
        elif kind == "ASM" and self.model:
            mon = self.mons.setdefault(r["line_id"], LineMonitor(r["line_id"], self.model))
            mon.process(json.loads(r["payload"]), r["event_no"], self.book)
        elif kind == "PREDICTION":
            self.pred[r["line_id"]] = dict(json.loads(r["payload"]), entry_hash=r["entry_hash"], received_utc=r["received_utc"])
        elif kind == "EVALUATION":
            self.eval[r["line_id"]] = json.loads(r["payload"])
        elif kind == "ALERT":
            self.alerts.append(dict(json.loads(r["payload"]), line=r["line_id"], event_no=r["event_no"], utc=r["received_utc"]))

    def _build(self, ok, n, problem, tip):
        keys = ("event_no", "unit_id", "time", "depth", "baseline", "resid", "cusum_pos", "cusum_neg", "nok", "follow_err",
                "vision_offset", "pressure", "bag_lot", "reel")
        lines = {}
        for line in LINES:
            mon = self.mons.get(line)
            lines[line] = {"units": [{k: u[k] for k in keys} for u in mon.units] if mon else [],
                           "forecast": mon.last_forecast if mon else None,
                           "prediction": self.pred.get(line), "evaluation": self.eval.get(line)}
        mat = self.book.summary()
        m = self.model or {}
        return {"tip": tip, "mode": self.mode, "integrity": {"ok": ok, "entries": n, "problem": problem},
                "model": {k: m.get(k) for k in ("version", "formula", "coef", "sigma_mm", "r2", "detector")},
                "lines": lines, "alerts": self.alerts[-40:],
                "materials": {"bag": {k: round(v["mean"], 5) for k, v in mat["bag_lots"].items()},
                              "pvc": {k: round(v["mean"], 5) for k, v in mat["reels"].items()}}}


def state(db_path, mode):
    """Uncached full state (kept for tests)."""
    return json.loads(gzip.decompress(LiveState(db_path, mode).get()[1]))


_ANA_CACHE = {}


def analysis_cached(db, mode):
    with Ledger(db, readonly=True).db as c:
        tip = c.execute("SELECT max(seq) FROM ledger").fetchone()[0]
    if _ANA_CACHE.get("key") != (db, mode, tip):
        _ANA_CACHE.update(key=(db, mode, tip), value=_gz(analyze(db, mode)))
    return _ANA_CACHE["value"]


_STATIC = {}
_LAB = {}


def static(name, loader):
    if name not in _STATIC:
        _STATIC[name] = loader()
    return _STATIC[name]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        gz, ctype, cache = True, "application/json", "no-store"
        if url.path == "/api/state":
            tip, body = self.server.live.get()
            if parse_qs(url.query).get("tip", [""])[0] == str(tip):
                body = _gz({"unchanged": True, "tip": tip})
        elif url.path == "/api/analysis":
            body = analysis_cached(self.server.db, self.server.mode)
        elif url.path == "/api/urs":
            body = _gz(audit.urs_checks(self.server.db, self.server.mode))
        elif url.path == "/api/unit":
            q = parse_qs(url.query)
            body = _gz(audit.unit_trace(self.server.db, self.server.mode, q.get("id", [""])[0].strip()))
        elif url.path == "/api/quickunits":
            body = _gz(audit.predictions_and_failures(self.server.db, self.server.mode))
        elif url.path == "/api/ledger":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = _gz(audit.ledger_page(self.server.db, q.get("kind") or None, q.get("limit", 25), q.get("before")))
        elif url.path == "/api/verify":
            ok, n, problem = Ledger(self.server.db, readonly=True).verify()
            body = _gz({"ok": ok, "entries": n, "problem": problem, "checked_utc": audit.utc_now()})
        elif url.path == "/api/export":
            body, ctype, cache = gzip.compress(audit.export_bytes(self.server.db), 5), "application/x-ndjson", "no-store"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Disposition", f'attachment; filename="ledger_{self.server.mode}_export.jsonl"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif url.path == "/api/lab/datasets":
            body = _gz({"datasets": lab.datasets(), "models": lab.model_info()})
        elif url.path == "/api/lab":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            key = url.query
            if key not in _LAB:
                try:
                    keys = [k for k in q.get("models", "").split(",") if k in lab.NAMES][:8]
                    if not keys:
                        raise ValueError("choose at least one model")
                    fake = {"kind": q.get("fk", "axis"), "slope": float(q.get("fs", 0.01)), "start": int(q.get("fst", 150)),
                            "n": int(q.get("fn", 600)), "noise": float(q.get("fnoise", 0)), "seed": int(q.get("fseed", 1))}
                    _LAB[key] = _gz(lab.run(q["train"], q["test"], keys, fake))
                except Exception as e:  # report the reason to the page instead of a broken response
                    _LAB[key] = _gz({"error": f"{type(e).__name__}: {e}"})
                if len(_LAB) > 50:
                    _LAB.pop(next(iter(_LAB)))
            body = _LAB[key]
        elif url.path == "/api/models":
            if not os.path.exists("models_report.json"):
                return self.send_error(404, "run: python3 models.py")
            body = static("models", lambda: _gz(open("models_report.json", "rb").read()))
        elif url.path.startswith("/figures/") and re.fullmatch(r"/figures/[\w\-]+\.png", url.path) \
                and os.path.exists(url.path[1:]):
            body, ctype, gz, cache = open(url.path[1:], "rb").read(), "image/png", False, "max-age=3600"
        elif url.path in WEB:
            name, ctype = WEB[url.path]
            body, cache = _gz(open(os.path.join(HERE, "web", name), "rb").read()), "no-cache"
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="REALTIME", choices=["HISTORICAL", "REALTIME", "SIM"])
    ap.add_argument("--db")
    ap.add_argument("--port", type=int, default=8050)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.db, srv.mode = a.db or f"ledger_{a.mode}.db", a.mode
    srv.live = LiveState(srv.db, a.mode)
    # warm the caches in the background so the first click on every tab is instant
    threading.Thread(target=lambda: (srv.live.get(), analysis_cached(srv.db, a.mode)), daemon=True).start()
    print(f"Dashboard: http://localhost:{a.port}  (ledger {srv.db}, read-only)")
    srv.serve_forever()
