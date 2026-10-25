"""Fake manufacturing simulator for rehearsing the REALTIME jury run.   python3 mock_server.py

Same URLs and rules as the organisers' simulator:
  GET /simulator/lines/{BAG-01|PVC-01|ASM-01|ASM-02|ASM-03}/modes/{HISTORICAL|REALTIME}/teams/{01-04}/codes/{code}
      ?startWith=1&limit=...      REALTIME assembly limit 1-5, HISTORICAL 1-100, BAG/PVC 1-100 or omitted
  503 while "not loaded", [] past the end, 404 for a wrong team / code / line, 400 for bad parameters.

Data is semi-random but physically consistent: bag lots and tube reels with their own sizes, each line holding
a lot / reel for a realistic run, depth = the same physics as the real data + noise + a scenario per line:
  actuator:<mm per unit>@<start>   actuator drifts (depth and actuator position move)
  camera:<mm per unit>@<start>     camera drifts (only the measured depth moves)
  step:<mm>@<start>                sudden jump
  stable                           healthy line, no failure
  random                           one of the above, chosen by the seed
<start> is a unit number or a fraction of the run (0.4 = 40 %).

Test switches: --latency (s), --flaky (share of requests answered 500/503 or slowly), --unloaded (s of 503 at
start), --cursor (REALTIME assembly ignores startWith and advances a per-team cursor, as in the task text).

Answer key (for grading after a run, never used by the monitor): /mock/truth · counters: /mock/status ·
reset cursors and counters: /mock/reset
"""
import argparse
import json
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODEL = json.load(open("model.json"))
LINES = ("ASM-01", "ASM-02", "ASM-03")
T0 = datetime(2026, 10, 26, 7, 0, 0, tzinfo=timezone.utc)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_scenario(text, n, rng):
    if text == "random":
        text = rng.choice([f"step:{rng.choice([1.3, -2.4])}@{rng.uniform(0.3, 0.7):.2f}",
                           f"actuator:{rng.choice([0.004, 0.006, -0.006])}@{rng.uniform(0.2, 0.5):.2f}", "stable"])
    m = re.fullmatch(r"(actuator|camera|step|stable)(?::([-+]?[\d.]+))?(?:@([\d.]+))?", text)
    if not m:
        raise SystemExit(f"bad scenario {text!r}")
    kind, size, start = m.group(1), float(m.group(2) or 0), m.group(3)
    start = 0 if start is None else (int(float(start) * n) if float(start) < 1 else int(float(start)))
    return {"text": text, "kind": kind, "size": size, "start_unit": start + 1}


def generate(seed, n_units, scenarios):
    rng = random.Random(seed)
    g = lambda s: rng.gauss(0, s)
    # ---- BAG-01: lots with their own port size and spread, 100 measurements each
    bags, bag_lots, t = [], {}, T0
    for k in range(10):
        lot = f"RT-BAG-{101 + k}"
        mean, sd = rng.choice([0.1735, 0.1740, 0.1745, 0.1750]) + g(0.0001), rng.uniform(0.00012, 0.00045)
        bag_lots[lot] = mean
        for _ in range(100):
            bags.append({"lot_id": lot, "time": iso(t), "port_id_in": round(mean + g(sd), 5)})
            t += timedelta(seconds=1)
        t += timedelta(seconds=rng.randint(30, 120))
    # ---- PVC-01: reels with their own OD, 648 readings each
    pvcs, reels, t = [], {}, T0 + timedelta(minutes=5)
    for k in range(12):
        lot, reel = f"RT-PVC-{301 + k // 4}", f"RT-REEL-{k + 1:02d}"
        od, sd = rng.choice([0.1685, 0.1700, 0.1715]) + g(0.0001), rng.uniform(0.00015, 0.0006)
        reels[reel] = (lot, od)
        for _ in range(648):
            o = round(od + g(sd), 4)
            wall = round(0.026 + g(0.0003), 4)
            pvcs.append({"lot_id": lot, "reel_id": reel, "time": iso(t), "outer_diameter_in": o,
                         "inner_diameter_in": round(o - 2 * wall, 4), "wall_difference_in": round(abs(0.0013 + g(0.0003)), 4),
                         "wall_thickness_in": wall, "status": "RUNNING", "extruder_speed": round(329.5 + g(1.5), 1)})
            t += timedelta(seconds=1)
        t += timedelta(seconds=rng.randint(60, 200))
    # ---- assembly lines
    c = MODEL["coef"]
    ctx_off = {}
    asm, truth = {}, {}
    for li, line in enumerate(LINES):
        n = n_units.get(line, n_units["*"])
        sc = parse_scenario(scenarios.get(line, "stable"), n, rng)
        t = T0 + timedelta(days=1, minutes=37 * li)
        lot, reel, p = rng.choice(list(bag_lots)), rng.choice(list(reels)), rng.choice([3.5, 4.0, 4.5])
        lot_left, reel_left, p_left = rng.randint(60, 140), rng.randint(40, 100), rng.randint(100, 220)
        letter = "ABC"[li]
        recs, first_fail = [], None
        for i in range(1, n + 1):
            changed = False
            lot_left -= 1; reel_left -= 1; p_left -= 1
            if lot_left <= 0:
                lot, lot_left, changed = rng.choice(list(bag_lots)), rng.randint(60, 140), True
            if reel_left <= 0:
                reel, reel_left, changed = rng.choice(list(reels)), rng.randint(40, 100), True
            if p_left <= 0:
                p, p_left, changed = rng.choice([3.5, 4.0, 4.5]), rng.randint(100, 220), True
            t += timedelta(seconds=rng.randint(60, 130) if changed else 3)
            base = c["c0"] + c["c_port"] * bag_lots[lot] * 1000 + c["c_od"] * reels[reel][1] * 1000 + c["c_p"] * p
            off = ctx_off.setdefault((lot, reel, p), g(0.07))
            d = 0.0
            if sc["kind"] in ("actuator", "camera") and i >= sc["start_unit"]:
                d = sc["size"] * (i - sc["start_unit"] + 1)
            elif sc["kind"] == "step" and i >= sc["start_unit"]:
                d = sc["size"]
            true_depth = base + off + g(0.11) + (d if sc["kind"] in ("actuator", "step") else 0)
            measured = true_depth + (d if sc["kind"] == "camera" else 0)
            actuator = true_depth - 0.41 + g(0.04)
            gap = (bag_lots[lot] - reels[reel][1]) * 1000
            rec = {"line_id": line, "unit_id": f"RT-{letter}{i:04d}", "time": iso(t), "bag_lot_id": lot,
                   "tubing_lot_id": reels[reel][0], "reel_id": reel, "pressure_bar": p,
                   "insertion_speed_mm_s": round(-4.5 + 0.78 * gap + 2.8 * p + g(0.6), 3), "insertion_target_cm": 1.5,
                   "actuator_position_mm": round(actuator, 3), "axis_following_error_mm": round(15 - actuator, 3),
                   "visual_inspection_mm": round(measured, 3)}
            recs.append(rec)
            if first_fail is None and not 13.0 <= rec["visual_inspection_mm"] <= 17.0:
                first_fail = {"unit_id": rec["unit_id"], "event_no": i, "depth_mm": rec["visual_inspection_mm"]}
        asm[line] = recs
        truth[line] = {"scenario": sc, "units": n, "first_failure": first_fail}
    return {"BAG-01": bags, "PVC-01": pvcs, **asm}, truth


class State:
    def __init__(self, args):
        self.args = args
        per_line = {k: int(v) for k, v in (x.split("=") for x in args.units_per_line or [])}
        scen = dict(zip(LINES, args.scenarios)) | dict(x.split("=", 1) for x in args.scenario or [])
        self.data, self.truth = generate(args.seed, {"*": args.units, **per_line}, scen)
        self.hist = {}
        try:
            self.hist = {n: json.load(open(f"data_hist/{n}.json")) for n in ("ASM-01", "BAG-01", "PVC-01")}
        except OSError:
            pass
        self.started = time.time()
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.cursor, self.requests = {}, {}


def _int(v, name):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        S, a = self.server.state, self.server.state.args
        url = urlparse(self.path)
        if url.path == "/mock/truth":
            return self._send(200, S.truth)
        if url.path == "/mock/status":
            return self._send(200, {"requests": S.requests, "cursors": {"/".join(k): v for k, v in S.cursor.items()},
                                    "uptime_s": round(time.time() - S.started, 1)})
        if url.path == "/mock/reset":
            with S.lock:
                S.reset()
            S.started = time.time()
            return self._send(200, {"reset": True})
        m = re.fullmatch(r"/simulator/lines/([^/]+)/modes/([^/]+)/teams/([^/]+)/codes/([^/]+)", url.path)
        if not m:
            return self._send(404, {"message": "Resource not found", "error": "NOT_FOUND"})
        line, mode, team, code = m.groups()
        if mode not in ("HISTORICAL", "REALTIME"):
            return self._send(400, {"message": "Unsupported mode", "error": "BAD_REQUEST"})
        ok_code = f"team{team}" if mode == "HISTORICAL" else a.code
        if team not in ("01", "02", "03", "04") or code != ok_code or line not in ("BAG-01", "PVC-01") + LINES:
            return self._send(404, {"message": "Resource not found", "error": "NOT_FOUND"})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        with S.lock:
            S.requests[f"{mode}/{line}"] = S.requests.get(f"{mode}/{line}", 0) + 1
        if a.latency:
            time.sleep(random.expovariate(1 / a.latency))
        if mode == "REALTIME" and time.time() - S.started < a.unloaded:
            return self._send(503, {"message": "Dataset not loaded", "error": "SERVICE_UNAVAILABLE"})
        if a.flaky and random.random() < a.flaky:
            r = random.random()
            if r < 0.4:
                return self._send(500, {"message": "Internal error (mock)", "error": "INTERNAL"})
            if r < 0.7:
                return self._send(503, {"message": "Dataset not loaded (mock)", "error": "SERVICE_UNAVAILABLE"})
            time.sleep(3)                         # slow answer
        try:
            start = _int(q.get("startWith", 1), "startWith")
            limit = q.get("limit")
            if start < 1:
                raise ValueError("startWith must be >= 1")
            src = S.data if mode == "REALTIME" else S.hist
            rows = src.get(line, [])
            if line in ("BAG-01", "PVC-01"):
                limit = None if limit is None else _int(limit, "limit")
                if limit is not None and not 1 <= limit <= 100:
                    raise ValueError("limit must be 1-100")
                out = rows[start - 1:] if limit is None else rows[start - 1:start - 1 + limit]
            else:
                limit = _int(limit or 1, "limit")
                mx = 5 if mode == "REALTIME" else 100
                if not 1 <= limit <= mx:
                    raise ValueError(f"limit must be 1-{mx} in {mode}")
                if mode == "REALTIME" and a.cursor:          # cursor semantics: startWith is ignored
                    with S.lock:
                        cur = S.cursor.get((team, line), 0)
                        out = rows[cur:cur + limit]
                        S.cursor[(team, line)] = cur + len(out)
                else:
                    out = rows[start - 1:start - 1 + limit]
        except ValueError as e:
            return self._send(400, {"message": str(e), "error": "BAD_REQUEST"})
        return self._send(200, out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--seed", type=int, default=7, help="change for a different random plant")
    ap.add_argument("--units", type=int, default=600, help="assembly units per line")
    ap.add_argument("--units-per-line", nargs="*", help="e.g. ASM-03=900")
    ap.add_argument("--scenarios", nargs=3, default=["actuator:0.012@0.45", "camera:-0.009@0.35", "random"],
                    metavar=("ASM01", "ASM02", "ASM03"))
    ap.add_argument("--scenario", nargs="*", help="override one line, e.g. ASM-03=step:1.5@300")
    ap.add_argument("--code", default="LIVE", help="REALTIME passcode the mock accepts")
    ap.add_argument("--latency", type=float, default=0.05, help="mean extra delay per request (s)")
    ap.add_argument("--flaky", type=float, default=0.0, help="share of requests that fail (500/503) or answer slowly")
    ap.add_argument("--unloaded", type=float, default=0.0, help="seconds of 503 'not loaded' after start (REALTIME)")
    ap.add_argument("--cursor", action="store_true", help="REALTIME assembly ignores startWith (per-team cursor)")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.state = State(args)
    for l, t in srv.state.truth.items():
        ff = t["first_failure"]
        print(f"{l}: {t['units']} units · scenario {t['scenario']['text']} · first failure "
              f"{ff['unit_id'] + ' at unit ' + str(ff['event_no']) if ff else 'none'}")
    print(f"mock simulator on http://localhost:{args.port}/simulator  (REALTIME passcode {args.code!r}, "
          f"HISTORICAL serves the real downloaded data)")
    srv.serve_forever()
