"""Gate 3 digital twin -- real-time monitor and first-failure predictor.

    python3 monitor.py --mode HISTORICAL --team 01 --code <historical access code>
    python3 monitor.py --mode REALTIME   --team 01 --code <passcode from mentor>
    python3 monitor.py --mode SIM        (synthetic de-calibration demo, no network)

The access code is used only in the request URL; it is never written to the ledger.
"""
import argparse
import json
import threading
import time

from api import ASM_LINES, DEFAULT_BASE, FatalAPIError, SimulatorClient
from model import LineMonitor, MaterialBook, next_unit_id
from store import Ledger, MemoryLedger, claim_writer, reset_ledger, utc_now

REQUIRED = ("line_id", "unit_id", "time", "bag_lot_id", "tubing_lot_id", "reel_id", "pressure_bar",
            "insertion_speed_mm_s", "insertion_target_cm", "actuator_position_mm",
            "axis_following_error_mm", "visual_inspection_mm")
NUMERIC = ("pressure_bar", "insertion_speed_mm_s", "insertion_target_cm", "actuator_position_mm",
           "axis_following_error_mm", "visual_inspection_mm")


class Twin:
    def __init__(self, ledger, model, mode, lines, log=print):
        self.ledger, self.model, self.mode, self.lines, self.log = ledger, model, mode, lines, log
        self.book = MaterialBook()
        self.mon = {l: LineMonitor(l, model) for l in lines}
        self.done = set()
        self.alerted = set()
        ledger.append("MODEL", f"MODEL/{model['version']}", model)

    # ---------------- ingestion
    def add_component(self, kind, rec):
        (self.book.add_bag if kind == "BAG" else self.book.add_pvc)(rec)

    def add_unit(self, line, event_no, rec, allow_commit=True):
        """Process one completed unit. allow_commit=False inside a batch: a prediction may only be locked
        after the last unit of a batch, so the predicted unit can never be one we have already received."""
        mon = self.mon[line]
        if not self._data_checks(line, event_no, rec, mon):
            return None                    # stored as received, flagged, but not used for prediction
        u = mon.process(rec, event_no, self.book)
        if not u["material_known"]:
            self._alert(line, event_no, "MATERIAL_UNKNOWN",
                        f"No BAG/PVC data for lot {rec['bag_lot_id']} / reel {rec['reel_id']}; population mean used")
        f = mon.last_forecast
        if f and f["drift_alarm"] and ("drift", line) not in self.alerted:
            self.alerted.add(("drift", line))
            self._alert(line, event_no, "DECALIBRATION_SUSPECTED",
                        f"slope {f['slope_mm_per_unit']:+.4f} mm/unit (t={f['t_slope']}), "
                        f"CUSUM+ {u['cusum_pos']} / CUSUM- {u['cusum_neg']}")
        self._evaluate(line, u)
        if line not in self.done and allow_commit:
            self._maybe_commit(line, u)
        return u

    def _data_checks(self, line, event_no, rec, mon):
        missing = [k for k in REQUIRED if rec.get(k) is None]
        if missing:
            self._alert(line, event_no, "DATA_INCOMPLETE", f"missing {missing}; unit kept in the ledger but not used")
            return False
        bad = [k for k in NUMERIC if not isinstance(rec.get(k), (int, float)) or isinstance(rec.get(k), bool)]
        if bad:
            self._alert(line, event_no, "DATA_INVALID", f"not a number: {bad}; unit kept in the ledger but not used")
            return False
        if rec.get("line_id") != line:
            self._alert(line, event_no, "LINE_MISMATCH", f"record says {rec.get('line_id')}")
        if mon.units:
            prev = mon.units[-1]
            if rec["time"] < prev["time"]:
                self._alert(line, event_no, "TIME_NOT_MONOTONIC", f"{rec['time']} < {prev['time']}")
            if next_unit_id(prev["unit_id"], 1) != rec["unit_id"]:
                self._alert(line, event_no, "UNIT_SEQUENCE_GAP",
                            f"expected {next_unit_id(prev['unit_id'], 1)}, got {rec['unit_id']}")
        return True

    def _alert(self, line, event_no, code, text):
        self.ledger.append("ALERT", f"ALERT/{self.mode}/{line}/{event_no:06d}/{code}",
                           {"code": code, "text": text}, mode=self.mode, line_id=line, event_no=event_no)
        self.log(f"  ! {line} #{event_no} {code}: {text}")

    # ---------------- prediction (immutable, one per line)
    def prediction(self, line):
        rows = self.ledger.rows(kind="PREDICTION", line_id=line, mode=self.mode)
        return json.loads(rows[0]["payload"]) if rows else None

    def _maybe_commit(self, line, u):
        mon = self.mon[line]
        if self.prediction(line) or mon.first_failure or not mon.should_commit():
            return
        f = mon.last_forecast
        k = f["k_first_fail"]
        pred_id = next_unit_id(u["unit_id"], k)
        payload = {
            "line_id": line, "predicted_unit_id": pred_id, "units_ahead": k,
            "current_unit_id": u["unit_id"], "current_event_no": u["event_no"],
            "current_unit_time": u["time"], "prediction_time_utc": utc_now(),
            "model_version": self.model["version"], "predicted_failure_mode": f["direction"],
            "evidence": {key: f[key] for key in ("slope_mm_per_unit", "t_slope", "level_mm", "noise_sd",
                                                  "cusum_alarm", "p_at_k", "p_next")}
                        | {"last_depth_mm": u["depth"], "last_baseline_mm": u["baseline"],
                           "cusum_pos": u["cusum_pos"], "cusum_neg": u["cusum_neg"]},
            "forecast_path": f["path"][:max(k, 10)],
        }
        e = self.ledger.append("PREDICTION", f"PRED/{self.mode}/{line}", payload, mode=self.mode,
                               line_id=line, event_no=u["event_no"], unit_id=pred_id)
        if e:
            self.log(f"\n  >>> PREDICTION LOCKED {line}: first failure = {pred_id} "
                     f"({k} units after {u['unit_id']}, {f['direction']}) hash {e['entry_hash'][:16]}\n")

    def _evaluate(self, line, u):
        mon = self.mon[line]
        if not u["nok"] or mon.first_failure is not u:
            return
        pred = self.prediction(line)
        ev = {"line_id": line, "actual_first_failure_unit_id": u["unit_id"], "actual_event_no": u["event_no"],
              "actual_depth_mm": u["depth"], "limits_mm": [u["lsl"], u["usl"]]}
        if pred:
            off = u["event_no"] - (pred["current_event_no"] + pred["units_ahead"])
            # Jury metrics (RULES.md): X = predicted, Y = current at lock, Z = actual first failure
            ev |= {"predicted_unit_id": pred["predicted_unit_id"], "exact_hit": pred["predicted_unit_id"] == u["unit_id"],
                   "error_units": off, "lead_units": u["event_no"] - pred["current_event_no"],
                   "E_abs_error_units": abs(off), "L_lead_units": pred["units_ahead"],
                   "status": "HIT" if off == 0 else ("EARLY" if off > 0 else "LATE")}
        else:
            ev |= {"predicted_unit_id": None, "status": "MISSED (failure before any prediction)"}
        self.ledger.append("EVALUATION", f"EVAL/{self.mode}/{line}", ev, mode=self.mode, line_id=line,
                           event_no=u["event_no"], unit_id=u["unit_id"])
        self.done.add(line)
        self.log(f"\n  === {line} first actual failure {u['unit_id']} ({u['depth']} mm): {ev['status']}\n")


def run_rest(args, ledger, model):
    """Pull every assembly line from the REST API until its first failure (or the end of the data).

    Lines run in parallel threads; network calls happen outside the shared lock, all processing and
    ledger writes inside it. Batch size adapts per line: up to --limit records while the line is healthy,
    1 record as soon as the risk of a failure rises (--slow-at) or a drift alarm is active, so that a
    prediction can be locked one unit at a time close to the failure. Predictions are only allowed after
    the last record of a batch, so the predicted unit is always one that has not been received yet."""
    lock = threading.RLock()
    client = SimulatorClient(ledger, args.mode, args.team, args.code, base=args.base_url, lock=lock)
    twin = Twin(ledger, model, args.mode, args.lines)

    def refresh_materials():
        n = 0
        for comp, kind in (("BAG-01", "BAG"), ("PVC-01", "PVC")):
            with lock:
                start = 1 + ledger.max_event_no(kind, args.mode)
            new = client.fetch_component(comp, start)
            with lock:
                for _, rec, _ in new:
                    twin.add_component(kind, rec)
            n += len(new)
        return n

    # resume: rebuild state from what is already in the ledger, then fetch new material data
    for kind in ("BAG", "PVC"):
        for r in ledger.rows(kind=kind, mode=args.mode):
            twin.add_component(kind, json.loads(r["payload"]))
    print(f"materials: {refresh_materials()} new records -> {len(twin.book.port)} bag lots, {len(twin.book.od)} reels")
    next_row = {}
    for line in args.lines:
        old = ledger.rows(kind="ASM", line_id=line, mode=args.mode)
        for r in old:
            rec = json.loads(r["payload"])
            if twin._data_checks(line, r["event_no"], rec, twin.mon[line]):
                twin.mon[line].process(rec, r["event_no"], twin.book)
        next_row[line] = 1 + max([r["event_no"] for r in old], default=0)
        if ledger.rows(kind="EVALUATION", line_id=line, mode=args.mode):
            twin.done.add(line)
        if old:
            print(f"{line}: resumed after {len(old)} stored units")

    stats = {l: {"requests": 0, "units": 0} for l in args.lines}
    errors = []

    def batch_size(line):
        f = twin.mon[line].last_forecast
        risky = f and (f["drift_alarm"] or (f.get("p_next") or 0) >= args.slow_at)
        return 1 if risky or len(twin.mon[line].units) < twin.mon[line].cfg["min_points"] else args.limit

    def run_line(line):
        idle = 0
        try:
            while line not in twin.done or args.continue_after_fail:
                if stats[line]["requests"] >= args.max_requests:
                    break
                got = client.fetch_assembly(line, next_row[line], batch_size(line))
                stats[line]["requests"] += 1
                if not got:
                    if args.follow and idle < args.follow:
                        idle += 1
                        time.sleep(2)
                        continue
                    print(f"{line}: end of data after {stats[line]['units']} new units")
                    return
                idle = 0
                if any(rec.get("reel_id") not in twin.book.od or rec.get("bag_lot_id") not in twin.book.port for _, rec, _ in got):
                    refresh_materials()           # a new lot / reel appeared: pull the new BAG / PVC records first
                with lock:
                    for i, (event_no, rec, _) in enumerate(got):
                        u = twin.add_unit(line, event_no, rec, allow_commit=(i == len(got) - 1))
                        next_row[line] = event_no + 1
                        stats[line]["units"] += 1
                        if u and args.verbose:
                            f = twin.mon[line].last_forecast or {}
                            print(f"{line} #{event_no:4d} {u['unit_id']:>12} depth {u['depth']:6.3f}  resid {u['resid']:+.3f}  "
                                  f"P(next fails) {f.get('p_next') or 0:.3f}")
                    if stats[line]["units"] % 100 < len(got):
                        print(f"{line}: {stats[line]['units']} units, {stats[line]['requests']} requests, batch {len(got)}")
                if args.delay:
                    time.sleep(args.delay)
        except FatalAPIError as e:
            errors.append(str(e))
            print(f"\nSTOPPED {line}: {e}")
        except Exception as e:                     # keep the other lines running; everything so far is stored
            errors.append(f"{line}: {type(e).__name__}: {e}")
            print(f"\nSTOPPED {line}: {type(e).__name__}: {e}")

    t0 = time.time()
    threads = [threading.Thread(target=run_line, args=(l,), daemon=True) for l in args.lines if l not in twin.done]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = time.time() - t0
    print("\n=== summary " + "=" * 50)
    for l in args.lines:
        ev = ledger.rows(kind="EVALUATION", line_id=l, mode=args.mode)
        pr = ledger.rows(kind="PREDICTION", line_id=l, mode=args.mode)
        res = json.loads(ev[0]["payload"]) if ev else {}
        p = json.loads(pr[0]["payload"]) if pr else {}
        print(f"{l}: {stats[l]['units']} new units in {stats[l]['requests']} requests | prediction "
              f"{p.get('predicted_unit_id', '-')} (locked at {p.get('current_unit_id', '-')}) | first failure "
              f"{res.get('actual_first_failure_unit_id', '-')} | {res.get('status', 'no failure yet')}"
              + (f" | E={res['E_abs_error_units']} L={res['L_lead_units']}" if res.get("E_abs_error_units") is not None else ""))
    print(f"took {took:.1f} s" + (f" | ERRORS: {errors}" if errors else ""))
    return twin


def preflight(args):
    """Connectivity check that only reads BAG-01 / PVC-01 (never consumes assembly data)."""
    led = MemoryLedger()   # the check stores nothing in the GxP ledger
    c = SimulatorClient(led, args.mode, args.team, args.code, base=args.base_url, retries=3, wait_unloaded=0)
    print(f"checking {args.base_url} mode {args.mode} team {args.team} (the first call can take up to a minute)")
    try:
        for line, r in c.check().items():
            print(f"  {line}: {'OK' if r['ok'] else 'EMPTY'} in {r['seconds']} s  first record: {json.dumps(r['first'])[:120]}")
        print("ready: run the same command without --check")
    except Exception as e:
        print(f"  NOT READY: {e}")


def run_sim(args, ledger, model):
    from sim import decalibrate
    hist = json.load(open("data_hist/ASM-01.json"))
    twin = Twin(ledger, model, "SIM", list(ASM_LINES))
    for kind, f in (("BAG", "BAG-01"), ("PVC", "PVC-01")):
        recs = json.load(open(f"data_hist/{f}.json"))
        ledger.append_many([dict(kind=kind, record_uid=f"SIM/{f}/{i:06d}", payload=rec, mode="SIM", line_id=f,
                                 event_no=i, source_time=rec["time"]) for i, rec in enumerate(recs, 1)])
        for rec in recs:
            twin.add_component(kind, rec)
    streams = {"ASM-01": decalibrate(hist, "ASM-01", "SIM_A", 150, 0.010, "axis", 400, seed=1),
               "ASM-02": decalibrate(hist, "ASM-02", "SIM_B", 100, -0.015, "vision", 400, seed=2),
               "ASM-03": decalibrate(hist, "ASM-03", "SIM_C", 250, 0.006, "axis", 400, seed=3)}
    for i in range(400):
        for line, recs in streams.items():
            if line in twin.done:
                continue
            ledger.append("ASM", f"SIM/{line}/{i + 1:06d}", recs[i], mode="SIM", line_id=line, event_no=i + 1,
                          unit_id=recs[i]["unit_id"], source_time=recs[i]["time"])
            twin.add_unit(line, i + 1, recs[i])
        if len(twin.done) == 3:
            break
        time.sleep(args.delay)
    return twin


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["HISTORICAL", "REALTIME", "SIM"], required=True)
    ap.add_argument("--team", default="01")
    ap.add_argument("--code", help="team access code (required for HISTORICAL / REALTIME; never stored)")
    ap.add_argument("--db", default=None, help="ledger name (default: the mode); PostgreSQL schema ledger_<name>")
    ap.add_argument("--fresh", action="store_true", help="SIM only: drop and recreate the SIM ledger first")
    ap.add_argument("--lines", nargs="+", default=list(ASM_LINES))
    ap.add_argument("--limit", type=int, default=5, help="max records per assembly request while a line is healthy "
                                                          "(REALTIME 1-5, HISTORICAL 1-100); drops to 1 near a failure")
    ap.add_argument("--slow-at", type=float, default=0.01, help="switch to 1 record per request once P(next fails) reaches this")
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="simulator base URL (use http://localhost:8090/simulator for mock_server.py)")
    ap.add_argument("--check", action="store_true", help="only test the connection (reads BAG/PVC, never assembly data)")
    ap.add_argument("--follow", type=int, default=0, help="when a line returns no data, ask again this many times (2 s apart)")
    ap.add_argument("--verbose", action="store_true", help="print every unit")
    ap.add_argument("--max-requests", type=int, default=100000)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    ap.add_argument("--continue-after-fail", action="store_true")
    ap.add_argument("--operator", default="team-operator")
    args = ap.parse_args()
    if args.mode != "SIM" and not args.code:
        ap.error("--code is required for HISTORICAL and REALTIME")
    if args.mode == "REALTIME" and not 1 <= args.limit <= 5:
        ap.error("REALTIME allows 1-5 records per request (--limit)")
    if args.check:
        preflight(args)
        raise SystemExit

    model = json.load(open("model.json"))
    name = args.db or args.mode
    # only one monitor may write a ledger at a time (a second terminal would otherwise race it);
    # PostgreSQL advisory lock, so it works on Windows too and is taken before --fresh can drop anything
    writer = claim_writer(name)
    if writer is None:
        raise SystemExit(f"Another monitor is already writing the {name} ledger. Stop it first (Ctrl+C in its terminal).")
    if args.fresh:
        if args.mode != "SIM":
            ap.error("--fresh is only allowed for SIM (real ledgers are never dropped)")
        reset_ledger(name)
    ledger = Ledger(name, actor=args.operator)
    ledger.append("SESSION", f"SESSION/{utc_now()}", {"mode": args.mode, "team": args.team, "lines": args.lines,
                                                      "model_version": model["version"], "limit": args.limit,
                                                      "slow_at": args.slow_at, "base_url": args.base_url},
                  mode=args.mode)
    twin = (run_sim if args.mode == "SIM" else run_rest)(args, ledger, model)
    ok, n, problem = ledger.verify()
    print(f"\nLedger integrity: {'OK' if ok else 'FAILED - ' + problem} ({n} entries)")
    ledger.export(f"export_{args.mode}.jsonl")
