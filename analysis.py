"""Full-data analysis for the dashboard "Data analysis" tab.   python3 analysis.py [--mode HISTORICAL]

Works only on records stored in the ledger (the GxP copy of what the REST API returned), read-only.
"""
import argparse
import json
import os
from statistics import NormalDist

import numpy as np
import pandas as pd

from model import LineMonitor, MaterialBook, baseline
from store import Ledger

SPEC = {"port_id_in": (0.173, 0.176), "outer_diameter_in": (0.1675, 0.1725), "inner_diameter_in": (0.110, 0.122),
        "wall_thickness_in": (0.025, 0.028), "wall_difference_in": (None, 0.003)}


def _frame(led, kind, mode, line=None):
    rows = led.rows(kind=kind, mode=mode, line_id=line)
    df = pd.DataFrame([json.loads(r["payload"]) for r in rows])
    if len(df):
        df["event_no"] = [r["event_no"] for r in rows]
        df["received_utc"] = [r["received_utc"] for r in rows]
    return df


def _cap(x, lo, hi):
    m, s = float(np.mean(x)), float(np.std(x, ddof=1))
    cpu = (hi - m) / (3 * s) if hi is not None else None
    cpl = (m - lo) / (3 * s) if lo is not None else None
    cpk = min(v for v in (cpu, cpl) if v is not None)
    ppm = 1e6 * ((NormalDist(m, s).cdf(lo) if lo is not None else 0) + (1 - NormalDist(m, s).cdf(hi) if hi is not None else 0))
    return {"mean": round(m, 5), "sd": round(s, 5), "min": round(float(np.min(x)), 5), "max": round(float(np.max(x)), 5),
            "cpk": round(cpk, 2), "ppm_expected": round(ppm, 1),
            "n_out_of_spec": int((x < lo).sum() if lo is not None else 0) + int((x > hi).sum() if hi is not None else 0)}


def _timing(df, name):
    t = pd.to_datetime(df["time"])
    dt = t.diff().dt.total_seconds().dropna()
    ts = t.sort_values()
    sdt = ts.diff().dt.total_seconds().dropna()
    return {"source": name, "records": len(df), "timestamps_unique": int(t.nunique()),
            "duplicate_timestamps": int(len(df) - t.nunique()),
            "first": ts.iloc[0].isoformat(), "last": ts.iloc[-1].isoformat(),
            "span_h": round((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 3600, 2),
            "typical_interval_s": float(sdt.mode().iloc[0]) if len(sdt) else None,
            "max_gap_s": float(sdt.max()) if len(sdt) else None,
            "backward_steps_in_source_order": int((dt < 0).sum())}


def analyze(db, mode, sim_db="ledger_SIM.db"):
    led = Ledger(db, readonly=True)
    ok, n_entries, problem = led.verify()
    model = json.loads(led.rows(kind="MODEL")[-1]["payload"])
    bag, pvc = _frame(led, "BAG", mode), _frame(led, "PVC", mode)
    out = {"mode": mode, "integrity": {"ok": ok, "entries": n_entries, "problem": problem},
           "raw_responses": len(led.rows(kind="RAW_RESPONSE", mode=mode)), "timing": [], "checks": [], "lines": {}}

    book = MaterialBook()
    for r in bag.to_dict("records"):
        book.add_bag(r)
    for r in pvc.to_dict("records"):
        book.add_pvc(r)

    # ---------------- materials
    if len(bag):
        out["timing"].append(_timing(bag, "BAG-01"))
        out["bag"] = {"capability": _cap(bag.port_id_in, *SPEC["port_id_in"]),
                      "series": {"i": list(range(1, len(bag) + 1)), "port": bag.port_id_in.round(5).tolist(),
                                 "lot": bag.lot_id.tolist()},
                      "lots": bag.groupby("lot_id").port_id_in.agg(["count", "mean", "std", "min", "max"]).round(5)
                      .reset_index().to_dict("records")}
    if len(pvc):
        out["timing"].append(_timing(pvc, "PVC-01"))
        pv = pvc.sort_values("time")
        out["pvc"] = {"capability": {k: _cap(pvc[k], *SPEC[k]) for k in SPEC if k in pvc},
                      "series": {"time": pv.time.tolist(), "od": pv.outer_diameter_in.tolist(), "reel": pv.reel_id.tolist()},
                      "reels": pvc.groupby(["lot_id", "reel_id"]).agg(n=("outer_diameter_in", "size"),
                                                                     od_mean=("outer_diameter_in", "mean"),
                                                                     od_sd=("outer_diameter_in", "std"),
                                                                     id_mean=("inner_diameter_in", "mean"),
                                                                     first=("time", "min"), last=("time", "max"))
                      .round(5).reset_index().to_dict("records"),
                      "status": pvc.status.value_counts().to_dict(),
                      "extruder_speed": [float(pvc.extruder_speed.min()), float(pvc.extruder_speed.max())]}
        oos = {k: v["n_out_of_spec"] for k, v in out["pvc"]["capability"].items()}
        out["checks"].append(("PVC-01 dimensional spec", "WARN" if any(oos.values()) else "PASS",
                              f"out-of-spec readings: {oos}"))
        out["checks"].append(("PVC-01 sampling vs spec", "INFO",
                              f"1 record/s at {out['pvc']['extruder_speed'][0]}-{out['pvc']['extruder_speed'][1]} ft/min "
                              f"(spec: 1 record per metre; 100 m/min would be 1.67 records/s)"))
    if len(bag):
        out["checks"].append(("BAG-01 port ID spec 0.173-0.176 in",
                              "PASS" if out["bag"]["capability"]["n_out_of_spec"] == 0 else "WARN",
                              f"{out['bag']['capability']['n_out_of_spec']} of {len(bag)} out of spec"))

    # ---------------- assembly lines
    for line in ("ASM-01", "ASM-02", "ASM-03"):
        a = _frame(led, "ASM", mode, line)
        if not len(a):
            out["lines"][line] = None
            continue
        out["timing"].append(_timing(a, line))
        mon = LineMonitor(line, model)
        units = [mon.process(r, r["event_no"], book) for r in a.to_dict("records")]
        u = pd.DataFrame(units)
        t = pd.to_datetime(a.time)
        ctx = a.bag_lot_id + "|" + a.reel_id + "|" + a.pressure_bar.astype(str)
        change = ctx != ctx.shift()
        dt = t.diff().dt.total_seconds()
        depth = a.visual_inspection_mm
        lsl, usl = float(u.lsl.iloc[0]), float(u.usl.iloc[0])
        nok = u[u.nok]
        alarms = u.index[(u.cusum_pos >= model["detector"]["cusum_h"]) | (u.cusum_neg >= model["detector"]["cusum_h"])]

        # model-predicted mean depth for every material level x pressure seen in this run
        lvl = lambda s: s.split("_")[-1]
        grid = []
        for bl in sorted(set(a.bag_lot_id)):
            for reel in sorted(set(a.reel_id)):
                for p in sorted(set(a.pressure_bar)):
                    grid.append({"bag_lot": bl, "reel": reel, "pressure": p,
                                 "pred_mm": round(baseline(model, book.port_mean(bl, model)[0], book.od_mean(reel, model)[0], p), 3)})
        g = pd.DataFrame(grid)

        pred = led.rows(kind="PREDICTION", line_id=line, mode=mode)
        ev = led.rows(kind="EVALUATION", line_id=line, mode=mode)
        out["lines"][line] = {
            "units": len(a), "first_unit": a.unit_id.iloc[0], "last_unit": a.unit_id.iloc[-1],
            "capability": _cap(depth, lsl, usl), "limits": [lsl, usl],
            "n_nok": int(len(nok)),
            "first_failure": nok.iloc[0][["event_no", "unit_id", "time", "depth", "baseline", "resid", "cusum_pos",
                                          "cusum_neg", "pressure"]].to_dict() if len(nok) else None,
            "closest_high": u.nlargest(5, "depth")[["event_no", "unit_id", "depth", "pressure"]].assign(
                margin_mm=lambda d: (usl - d.depth).round(3), bag_lot=a.bag_lot_id[u.nlargest(5, "depth").index].values,
                reel=a.reel_id[u.nlargest(5, "depth").index].values).to_dict("records"),
            "closest_low": u.nsmallest(5, "depth")[["event_no", "unit_id", "depth", "pressure"]].assign(
                margin_mm=lambda d: (d.depth - lsl).round(3), bag_lot=a.bag_lot_id[u.nsmallest(5, "depth").index].values,
                reel=a.reel_id[u.nsmallest(5, "depth").index].values).to_dict("records"),
            "cusum_alarm_units": int(len(alarms)),
            "prediction": json.loads(pred[0]["payload"]) | {"entry_hash": pred[0]["entry_hash"]} if pred else None,
            "evaluation": json.loads(ev[0]["payload"]) if ev else None,
            "timing": {"intervals_s": dt.dropna().round().value_counts().sort_index().astype(int).to_dict(),
                       "setup_changes": int(change.sum() - 1),
                       "pauses_gt_10s": int((dt > 10).sum()),
                       "pauses_at_setup_change": int(((dt > 10) & change).sum()),
                       "units_per_hour": round(len(a) / max((t.iloc[-1] - t.iloc[0]).total_seconds() / 3600, 1e-9), 1)},
            "effects": {"bag_level": u.groupby(a.bag_lot_id.map(lvl)).depth.mean().round(3).to_dict(),
                        "reel_level": u.groupby(a.reel_id.map(lambda s: s.split("_")[-1])).depth.mean().round(3).to_dict(),
                        "pressure": {str(k): v for k, v in u.groupby("pressure").depth.mean().round(3).to_dict().items()}},
            "worst_case": g.nlargest(3, "pred_mm").to_dict("records") + g.nsmallest(3, "pred_mm").to_dict("records"),
            "drift_to_fail_mm": {"to_USL_from_worst": round(usl - g.pred_mm.max(), 3),
                                 "to_LSL_from_worst": round(g.pred_mm.min() - lsl, 3)},
            "series": {"event_no": u.event_no.tolist(), "time": a.time.tolist(), "unit_id": u.unit_id.tolist(),
                       "depth": u.depth.tolist(), "baseline": u.baseline.round(3).tolist(), "resid": u.resid.tolist(),
                       "cusum_pos": u.cusum_pos.tolist(), "cusum_neg": u.cusum_neg.tolist(),
                       "pressure": u.pressure.tolist(), "nok": u.nok.tolist(),
                       "follow_err": u.follow_err.tolist(), "speed": a.insertion_speed_mm_s.tolist()},
        }
        missing = [k for k in ("bag_lot_id", "reel_id") for v in a[k] if v not in (book.port if k == "bag_lot_id" else book.od)]
        out["checks"].append((f"{line} traceability unit -> BAG lot + PVC reel", "PASS" if not missing else "FAIL",
                              f"{len(a)} units, {len(set(a.bag_lot_id))} bag lots, {len(set(a.reel_id))} reels, "
                              f"{len(missing)} unlinked"))
        out["checks"].append((f"{line} unit_id sequence / timestamps", "PASS" if a.unit_id.is_unique and t.is_monotonic_increasing else "FAIL",
                              f"unique unit_ids: {a.unit_id.is_unique}, time increasing: {t.is_monotonic_increasing}"))
        out["checks"].append((f"{line} insertion depth {lsl}-{usl} mm", "PASS" if not len(nok) else "FAIL",
                              f"{len(nok)} nonconforming of {len(a)}; Cpk {out['lines'][line]['capability']['cpk']}"))

    out["checks"].append(("Ledger hash chain", "PASS" if ok else "FAIL", f"{n_entries} entries" + (f" - {problem}" if problem else "")))
    out["total_timestamps"] = sum(x["records"] for x in out["timing"])
    out["sim"] = _sim_failures(sim_db) if os.path.exists(sim_db) and mode != "SIM" else None
    return out


def _sim_failures(sim_db):
    """Point-of-failure view from the SYNTHETIC de-calibration run (clearly labelled)."""
    led = Ledger(sim_db, readonly=True)
    res = {}
    for line in ("ASM-01", "ASM-02", "ASM-03"):
        ev = led.rows(kind="EVALUATION", line_id=line, mode="SIM")
        if not ev:
            continue
        ev = json.loads(ev[0]["payload"])
        pr = led.rows(kind="PREDICTION", line_id=line, mode="SIM")
        al = [r for r in led.rows(kind="ALERT", line_id=line, mode="SIM") if "DECALIBRATION" in r["record_uid"]]
        recs = [json.loads(r["payload"]) for r in led.rows(kind="ASM", line_id=line, mode="SIM")]
        res[line] = {"evaluation": ev, "prediction_unit": json.loads(pr[0]["payload"])["predicted_unit_id"] if pr else None,
                     "prediction_made_at": json.loads(pr[0]["payload"])["current_unit_id"] if pr else None,
                     "first_alarm_event": al[0]["event_no"] if al else None,
                     "depth": [r["visual_inspection_mm"] for r in recs],
                     "follow_err": [r["axis_following_error_mm"] for r in recs]}
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="HISTORICAL")
    ap.add_argument("--db")
    a = ap.parse_args()
    res = analyze(a.db or f"ledger_{a.mode}.db", a.mode)
    json.dump(res, open(f"analysis_{a.mode}.json", "w"), default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print("total timestamps:", res["total_timestamps"])
    for t in res["timing"]:
        print(t)
    for c in res["checks"]:
        print(c)
    for l, v in res["lines"].items():
        if v:
            print(l, v["capability"], "first failure:", v["first_failure"], v["timing"]["setup_changes"], v["drift_to_fail_mm"])
