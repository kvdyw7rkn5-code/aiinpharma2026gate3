"""Operational qualification (OQ) of the twin. Run: python3 validate.py [horizon ...]

Part 1  prediction performance on SYNTHETIC de-calibration scenarios (historical units + injected drift or step),
        scored with the jury metrics (RULES.md): E = |Z - X| (error), L = X - Y (lead time), missed = failure
        before any prediction. Both commit rules are run on the same scenarios:
        "quantile" (5 % quantile of the forecast first failure, within `horizon` units) and
        "holt_next" (P(next unit fails) from Holt smoothing >= p_next_commit, X = Y + lead_units).
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


DRIFTS = list(itertools.product(("axis", "vision"), (0.003, 0.005, 0.01, 0.02, 0.04, -0.005, -0.01, -0.02)))
STEPS = [("step", d) for d in (0.6, 0.9, 1.2, -0.9)]


def config(estimator, **detector):
    model = copy.deepcopy(MODEL)
    model["detector"].update(estimator=estimator, **detector)
    return model


CONFIGS = {
    "quantile h=3": config("quantile", horizon_units=3, p_next_commit=0.5),
    "holt_next p>=0.1 L=3": config("holt_next", p_next_commit=0.1, lead_units=3),
    "holt_next p>=0.2 L=2": config("holt_next", p_next_commit=0.2, lead_units=2),
}


def part1(name, model):
    rows = []
    for (kind, slope), seed in itertools.product(DRIFTS + STEPS, range(5)):
        start = 60 + 37 * seed
        recs = decalibrate(HIST["ASM-01"], "ASM-01", "T", start, slope, kind, n=900, seed=seed)
        idx, uid = first_failure(recs)
        if idx is None:
            continue
        ev, pr = run_stream(recs, model)
        err = ev.get("error_units")
        rows.append(dict(kind=kind, slope=slope, seed=seed, truth=uid, status=ev["status"],
                         E=abs(err) if err is not None else None, L=pr["units_ahead"] if pr else None))
    n = len(rows)

    def stats(rs):
        es = sorted(r["E"] for r in rs if r["E"] is not None)
        ls = [r["L"] for r in rs if r["L"] is not None]
        k = len(rs)
        return {
            "scenarios": k,
            "exact_hit_%": round(100 * sum(e == 0 for e in es) / k, 1),
            "E<=2_%": round(100 * sum(e <= 2 for e in es) / k, 1),
            "E<=5_%": round(100 * sum(e <= 5 for e in es) / k, 1),
            "missed_%": round(100 * sum(r["E"] is None for r in rs) / k, 1),
            "median_E": es[len(es) // 2] if es else None,
            "mean_L": round(sum(ls) / len(ls), 1) if ls else None,
        }

    summary = {"config": name, **stats(rows),
               "drift": stats([r for r in rows if r["kind"] != "step"]),
               "step": stats([r for r in rows if r["kind"] == "step"])}
    return summary, rows


def part2(model):
    ev, pr = run_stream(HIST["ASM-01"], model)
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
    # Optional args: extra quantile horizons to compare, e.g. python3 validate.py 1 5
    for h in sys.argv[1:]:
        CONFIGS[f"quantile h={h}"] = config("quantile", horizon_units=int(h))
    report = {"model_version": MODEL["version"], "active_estimator": MODEL["detector"].get("estimator"),
              "part1": [], "part2": {}, "part3": part3()}
    for name, model in CONFIGS.items():
        s, rows = part1(name, model)
        report["part1"].append(s)
        report["part2"][name] = part2(model)
        print(s)
    print("false alarms on stable historical line:", report["part2"])
    print("integrity tests:", report["part3"])
    json.dump(report, open("validation_report.json", "w"), indent=2)
