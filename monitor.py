"""Gate 3 digital twin -- real-time monitor and first-failure predictor.

    python3 monitor.py --mode HISTORICAL --team 01 --code <historical access code>
    python3 monitor.py --mode REALTIME   --team 01 --code <passcode from mentor>
    python3 monitor.py --mode SIM        (synthetic de-calibration demo, no network)

The access code is used only in the request URL; it is never written to the ledger.
"""
import argparse
import json
import time

from api import ASM_LINES, SimulatorClient
from model import LineMonitor, MaterialBook, next_unit_id
from store import Ledger, utc_now

REQUIRED = ("line_id", "unit_id", "time", "bag_lot_id", "tubing_lot_id", "reel_id", "pressure_bar",
            "insertion_speed_mm_s", "insertion_target_cm", "actuator_position_mm",
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

    def add_unit(self, line, event_no, rec):
        mon = self.mon[line]
        self._data_checks(line, event_no, rec, mon)
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
        if line not in self.done:
            self._maybe_commit(line, u)
        return u

    def _data_checks(self, line, event_no, rec, mon):
        missing = [k for k in REQUIRED if rec.get(k) is None]
        if missing:
            self._alert(line, event_no, "DATA_INCOMPLETE", f"missing {missing}")
        if rec.get("line_id") != line:
            self._alert(line, event_no, "LINE_MISMATCH", f"record says {rec.get('line_id')}")
        if mon.units:
            prev = mon.units[-1]
            if rec["time"] < prev["time"]:
                self._alert(line, event_no, "TIME_NOT_MONOTONIC", f"{rec['time']} < {prev['time']}")
            if next_unit_id(prev["unit_id"], 1) != rec["unit_id"]:
                self._alert(line, event_no, "UNIT_SEQUENCE_GAP",
                            f"expected {next_unit_id(prev['unit_id'], 1)}, got {rec['unit_id']}")

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
    client = SimulatorClient(ledger, args.mode, args.team, args.code)
    twin = Twin(ledger, model, args.mode, args.lines)
    # resume: rebuild state from what is already in the ledger (no replay in REALTIME)
    for kind in ("BAG", "PVC"):
        for r in ledger.rows(kind=kind, mode=args.mode):
            twin.add_component(kind, json.loads(r["payload"]))
    next_row = {}
    for comp, kind in (("BAG-01", "BAG"), ("PVC-01", "PVC")):
        start = 1 + max([r["event_no"] for r in ledger.rows(kind=kind, mode=args.mode)], default=0)
        new = client.fetch_component(comp, start)
        for _, rec, _ in new:
            twin.add_component(kind, rec)
        print(f"{comp}: {len(new)} new records")
    print(f"materials: {len(twin.book.port)} bag lots, {len(twin.book.od)} reels")
    for line in args.lines:
        old = ledger.rows(kind="ASM", line_id=line, mode=args.mode)
        for r in old:
            twin.mon[line].process(json.loads(r["payload"]), r["event_no"], twin.book)
        next_row[line] = 1 + max([r["event_no"] for r in old], default=0)
        if ledger.rows(kind="EVALUATION", line_id=line, mode=args.mode):
            twin.done.add(line)

    active = [l for l in args.lines if l not in twin.done]
    n = 0
    while active and n < args.max_requests:
        for line in list(active):
            got = client.fetch_assembly(line, next_row[line], args.limit)
            n += 1
            if not got:
                print(f"{line}: no more records")
                active.remove(line)
                continue
            for event_no, rec, _ in got:
                if rec.get("reel_id") not in twin.book.od or rec.get("bag_lot_id") not in twin.book.port:
                    for comp, kind in (("BAG-01", "BAG"), ("PVC-01", "PVC")):
                        start = 1 + max([r["event_no"] for r in ledger.rows(kind=kind, mode=args.mode)], default=0)
                        for _, c, _ in client.fetch_component(comp, start):
                            twin.add_component(kind, c)
                u = twin.add_unit(line, event_no, rec)
                f = twin.mon[line].last_forecast or {}
                print(f"{line} #{event_no:4d} {u['unit_id']:>10} depth {u['depth']:6.3f}  resid {u['resid']:+.3f}  "
                      f"slope {f.get('slope_mm_per_unit', 0):+.4f} t {f.get('t_slope', 0):+5.1f}  "
                      f"k* {f.get('k_first_fail')}")
                next_row[line] = event_no + 1
            if line in twin.done and not args.continue_after_fail:
                active.remove(line)
            time.sleep(args.delay)
    return twin


def run_sim(args, ledger, model):
    from sim import decalibrate
    hist = json.load(open("data_hist/ASM-01.json"))
    twin = Twin(ledger, model, "SIM", list(ASM_LINES))
    for kind, f in (("BAG", "BAG-01"), ("PVC", "PVC-01")):
        for i, rec in enumerate(json.load(open(f"data_hist/{f}.json")), 1):
            ledger.append(kind, f"SIM/{f}/{i:06d}", rec, mode="SIM", line_id=f, event_no=i, source_time=rec["time"])
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
    ap.add_argument("--db", default=None, help="ledger file (default: ledger_<MODE>.db)")
    ap.add_argument("--lines", nargs="+", default=list(ASM_LINES))
    ap.add_argument("--limit", type=int, default=1, help="records per assembly request (REALTIME 1-5)")
    ap.add_argument("--max-requests", type=int, default=100000)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    ap.add_argument("--continue-after-fail", action="store_true")
    ap.add_argument("--operator", default="team-operator")
    args = ap.parse_args()
    if args.mode != "SIM" and not args.code:
        ap.error("--code is required for HISTORICAL and REALTIME")

    model = json.load(open("model.json"))
    ledger = Ledger(args.db or f"ledger_{args.mode}.db", actor=args.operator)
    ledger.append("SESSION", f"SESSION/{utc_now()}", {"mode": args.mode, "team": args.team, "lines": args.lines,
                                                      "model_version": model["version"], "limit": args.limit},
                  mode=args.mode)
    twin = (run_sim if args.mode == "SIM" else run_rest)(args, ledger, model)
    ok, n, problem = ledger.verify()
    print(f"\nLedger integrity: {'OK' if ok else 'FAILED - ' + problem} ({n} entries)")
    ledger.export(f"export_{args.mode}.jsonl")
