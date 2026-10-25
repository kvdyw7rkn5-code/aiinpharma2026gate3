"""Operational qualification (OQ) of the twin. Run: python3 validate.py [horizon ...]

Part 1  prediction performance on SYNTHETIC de-calibration scenarios (historical units + injected drift)
Part 2  false alarms on the unmodified HISTORICAL line (no failures -> no prediction expected)
Part 3  data-integrity tests: append-only, tamper detection, duplicate handling, immutable prediction
"""
import copy
import itertools
import json
import os
import sqlite3
import sys
import tempfile

from model import MaterialBook
from monitor import Twin
from sim import decalibrate, first_failure
from store import Ledger

MODEL = json.load(open("model.json"))
HIST = {n: json.load(open(f"data_hist/{n}.json")) for n in ("ASM-01", "PVC-01", "BAG-01")}


def run_stream(recs, model, line="ASM-01"):
    led = Ledger(":memory:")
    twin = Twin(led, model, "TEST", [line], log=lambda *_: None)
    for r in HIST["BAG-01"]:
        twin.add_component("BAG", r)
    for r in HIST["PVC-01"]:
        twin.add_component("PVC", r)
    for i, r in enumerate(recs, 1):
        twin.add_unit(line, i, r)
        if line in twin.done:
            break
    ev = led.rows(kind="EVALUATION")
    pr = led.rows(kind="PREDICTION")
    return (json.loads(ev[0]["payload"]) if ev else None), (json.loads(pr[0]["payload"]) if pr else None)


def part1(horizon):
    model = copy.deepcopy(MODEL)
    model["detector"]["horizon_units"] = horizon
    rows = []
    for kind, slope, seed in itertools.product(("axis", "vision"),
                                               (0.003, 0.005, 0.01, 0.02, 0.04, -0.005, -0.01, -0.02), range(5)):
        start = 60 + 37 * seed
        recs = decalibrate(HIST["ASM-01"], "ASM-01", "T", start, slope, kind, n=900, seed=seed)
        idx, uid = first_failure(recs)
        if idx is None:
            continue
        ev, pr = run_stream(recs, model)
        rows.append(dict(kind=kind, slope=slope, seed=seed, truth=uid, status=ev["status"],
                         err=ev.get("error_units"), lead=ev.get("lead_units")))
    n = len(rows)
    errs = [abs(r["err"]) for r in rows if r["err"] is not None]
    summary = {
        "horizon": horizon, "scenarios": n,
        "exact_hit_%": round(100 * sum(r["status"] == "HIT" for r in rows) / n, 1),
        "within_2_units_%": round(100 * sum(e <= 2 for e in errs) / n, 1),
        "within_5_units_%": round(100 * sum(e <= 5 for e in errs) / n, 1),
        "missed_%": round(100 * sum(r["err"] is None for r in rows) / n, 1),
        "median_abs_err": sorted(errs)[len(errs) // 2] if errs else None,
    }
    return summary, rows


def part2():
    ev, pr = run_stream(HIST["ASM-01"], MODEL)
    return {"units": len(HIST["ASM-01"]), "false_predictions": int(pr is not None), "failures": int(ev is not None)}


def part3():
    tests = {}
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    led = Ledger(path)
    led.append("ASM", "TEST/ASM-01/000001", HIST["ASM-01"][0], line_id="ASM-01", event_no=1)
    led.append("PREDICTION", "PRED/TEST/ASM-01", {"predicted_unit_id": "X1"}, line_id="ASM-01")
    for sql, name in (("UPDATE ledger SET payload='{}' WHERE seq=1", "update_blocked"),
                      ("DELETE FROM ledger WHERE seq=1", "delete_blocked")):
        try:
            led.db.execute(sql)
            tests[name] = False
        except sqlite3.DatabaseError:
            tests[name] = True
    tests["duplicate_not_overwritten"] = led.append("ASM", "TEST/ASM-01/000001", {"tampered": 1}) is None
    tests["prediction_immutable"] = led.append("PREDICTION", "PRED/TEST/ASM-01", {"predicted_unit_id": "X2"}) is None
    tests["chain_verifies"] = led.verify()[0]
    # attacker bypasses the triggers on the file directly -> hash chain must catch it
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER no_update")
    raw.execute("UPDATE ledger SET payload=replace(payload,'16.418','15.000') WHERE seq=1")
    raw.commit()
    ok, _, problem = Ledger(path).verify()
    tests["tamper_detected"] = (not ok) and "payload altered" in problem
    return tests


if __name__ == "__main__":
    horizons = [int(h) for h in sys.argv[1:]] or [1, 2, 3, 5]
    report = {"model_version": MODEL["version"], "part1": [], "part2": part2(), "part3": part3()}
    for h in horizons:
        s, rows = part1(h)
        report["part1"].append(s)
        print(s)
    print("false alarms on stable historical line:", report["part2"])
    print("integrity tests:", report["part3"])
    json.dump(report, open("validation_report.json", "w"), indent=2)
