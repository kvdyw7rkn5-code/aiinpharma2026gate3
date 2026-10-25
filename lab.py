"""Model lab: train any model on any dataset, test it on any other, and replay it inside the twin.

Datasets
  <MODE>:<LINE>   assembly records from a ledger (HISTORICAL, SIM, REALTIME), e.g. HISTORICAL:ASM-01
  <MODE>:ALL      all assembly lines of that ledger together
  FAKE            synthetic units generated on request (historical units + injected de-calibration)

Everything here runs in memory. Nothing is written to any ledger and model.json is never changed,
so lab experiments cannot affect GxP records or the frozen production model.
"""
import copy
import json
import os
import threading

import numpy as np
from sklearn.model_selection import KFold

import models as M
from model import LineMonitor, MaterialBook, baseline
from sim import decalibrate
from store import Ledger

LINES = ("ASM-01", "ASM-02", "ASM-03")
FROZEN = "M03F"
NAMES = {k: n for k, n, *_ in M.MODELS} | {FROZEN: "Production model (frozen model.json)"}

# Plain-language descriptions shown as tooltips everywhere a model appears.
# kind: what sort of model it is; box: how easy it is to explain to an inspector.
MODEL_INFO = {
    FROZEN: ("Production model (frozen)", "Simple formula", "white",
             "The exact formula the live system uses (model.json). It is never retrained, so it ignores the "
             "training data you pick and shows what production would do."),
    "M00": ("Average only", "Reference", "white",
            "Always predicts the average depth. A 'do nothing' yardstick that every real model must beat."),
    "M01": ("Gap only (Gate 2 idea)", "Simple formula", "white",
            "A straight line on one number: bag port size minus tube size, the root cause found in Gate 2."),
    "M02": ("Gap + pressure", "Simple formula", "white",
            "The gap plus the pressure the technician sets. Still a straight-line formula."),
    "M03": ("Linear: port + tube + pressure", "Simple formula", "white",
            "Same formula as production (4 numbers you can check by hand), but retrained on the data you pick."),
    "M04": ("Linear + material unevenness", "Simple formula", "white",
            "Also uses how uneven each bag lot and tube reel is. Looks better on known data, misleads on new materials."),
    "M05": ("Curved formula (quadratic)", "Simple formula", "white",
            "Lets the effects bend and interact, e.g. pressure matters more when the fit is loose. 10 numbers."),
    "M06": ("Lookup table", "Table", "white",
            "Looks up the average depth of past units with the same bag level, tube level and pressure. "
            "Has no answer for a setting it has never seen."),
    "M07": ("Decision tree", "Machine learning", "white",
            "Up to 16 readable if-then rules, e.g. IF pressure > 4.2 AND port is large THEN 16.3 mm."),
    "M08": ("Random forest", "Machine learning", "grey",
            "Averages 300 randomly varied decision trees. Accurate on familiar data, cannot extrapolate."),
    "M09": ("Gradient boosting", "Machine learning", "grey",
            "300 small trees, each fixing the errors of the previous ones. Usually the most accurate ML model on familiar data."),
    "M10": ("Nearest neighbours", "Machine learning", "grey",
            "Finds the 25 most similar past units and averages their depth."),
    "M11": ("Support vector regression", "Machine learning", "black",
            "Fits a smooth curved surface through the data. Hard to explain why it predicts what it does."),
    "M12": ("Gaussian process", "Machine learning", "grey",
            "A smooth curve that also reports how unsure it is. Accurate on new settings, slowest to train."),
    "M13": ("Neural network", "Machine learning", "black",
            "Two layers of artificial neurons, about 740 weights with no physical meaning. Fails badly on new settings."),
}


def model_info():
    return [{"key": k, "name": n, "kind": kind, "box": box, "desc": d} for k, (n, kind, box, d) in MODEL_INFO.items()]
_cache, _lock = {}, threading.Lock()


def _ledger_records(mode):
    path = f"ledger_{mode}.db"
    if not os.path.exists(path):
        return None
    led = Ledger(path, readonly=True)
    rec = {k: [json.loads(r["payload"]) for r in led.rows(kind=k, mode=mode)] for k in ("BAG", "PVC")}
    rec["ASM"] = {l: [json.loads(r["payload"]) for r in led.rows(kind="ASM", mode=mode, line_id=l)] for l in LINES}
    return rec


def _records(mode):
    with _lock:
        if mode not in _cache:
            _cache[mode] = _ledger_records(mode)
        return _cache[mode]


TITLE = {"HISTORICAL": "Historical (real)", "SIM": "Synthetic drift", "REALTIME": "Realtime (jury)"}


def datasets():
    out = []
    for mode in ("HISTORICAL", "SIM", "REALTIME"):
        rec = _records(mode)
        if not rec:
            continue
        lines = [l for l in LINES if rec["ASM"][l]]
        for l in lines:
            n = len(rec["ASM"][l])
            nok = sum(not (13 <= r["visual_inspection_mm"] <= 17) for r in rec["ASM"][l])
            desc = {"HISTORICAL": "Real data from the simulator API. Calibrated line.",
                    "SIM": "Synthetic: real units with an injected drift. Stops at the first failure.",
                    "REALTIME": "Live data received during the jury run."}[mode]
            out.append({"id": f"{mode}:{l}", "label": f"{TITLE[mode]} · {l}", "n": n, "nok": nok,
                        "desc": f"{desc} {n} units, {nok} out of spec.", "synthetic": mode == "SIM"})
        if len(lines) > 1:
            out.append({"id": f"{mode}:ALL", "label": f"{TITLE[mode]} · all lines", "n": None, "nok": None,
                        "desc": f"All {mode.lower()} assembly lines together.", "synthetic": mode == "SIM"})
    out.append({"id": "FAKE", "label": "Fake · generate your own", "n": None, "nok": None,
                "desc": "Real units plus a drift you choose (type, speed, start). Made up on the spot.", "synthetic": True})
    return out


def load(spec, fake):
    """Return (feature frame, raw unit records, BAG records, PVC records) for a dataset id."""
    if spec == "FAKE":
        hist = _records("HISTORICAL") or {"ASM": {"ASM-01": json.load(open("data_hist/ASM-01.json"))},
                                          "BAG": json.load(open("data_hist/BAG-01.json")),
                                          "PVC": json.load(open("data_hist/PVC-01.json"))}
        src = hist["ASM"]["ASM-01"]
        n = int(min(max(fake.get("n", 600), 50), len(src)))
        kind = fake.get("kind", "axis")
        slope = 0.0 if kind == "none" else float(fake.get("slope", 0.01))
        recs = decalibrate(src, "FAKE", "FAKE_", int(fake.get("start", 150)), slope,
                           "axis" if kind == "none" else kind, n=n, seed=int(fake.get("seed", 1)))
        noise = float(fake.get("noise", 0.0))
        if noise:
            rng = np.random.RandomState(int(fake.get("seed", 1)))
            for r in recs:
                r["visual_inspection_mm"] = round(r["visual_inspection_mm"] + rng.normal(0, noise), 3)
        bag, pvc = hist["BAG"], hist["PVC"]
    else:
        mode, line = spec.split(":")
        rec = _records(mode)
        if not rec:
            raise ValueError(f"no ledger for {mode}")
        recs = [r for l in (LINES if line == "ALL" else (line,)) for r in rec["ASM"][l]]
        bag, pvc = rec["BAG"], rec["PVC"]
    if not recs:
        raise ValueError(f"{spec} has no assembly records")
    return M.features(recs, pvc, bag), recs, bag, pvc


class Frozen:
    """The frozen production model from model.json, exposed like the lab models."""

    def __init__(self):
        self.m = json.load(open("model.json"))

    def fit(self, X, y):
        return self

    def predict(self, X):
        return np.array([baseline(self.m, p / 1000, o / 1000, pr)
                         for p, o, pr in zip(X.port_mean_thou, X.od_mean_thou, X.pressure_bar)])


def make(key):
    if key == FROZEN:
        return Frozen()
    return next(m[4] for m in M.MODELS if m[0] == key)()


def twin_replay(model, sigma, recs, bag, pvc, d_test):
    """Run the digital twin over the test stream with `model` as its baseline (stops at first failure)."""
    book = MaterialBook()
    for r in bag:
        book.add_bag(r)
    for r in pvc:
        book.add_pvc(r)
    lot_sd = d_test.groupby("bag_lot_id").port_sd_thou.first().to_dict()
    reel_sd = d_test.groupby("reel_id").od_sd_thou.first().to_dict()
    cache = {}

    def base_fn(rec, port, od):
        k = (rec["bag_lot_id"], rec["reel_id"], rec["pressure_bar"])
        if k not in cache:
            row = M.pd.DataFrame([{"port_mean_thou": port * 1000, "od_mean_thou": od * 1000,
                                   "pressure_bar": rec["pressure_bar"], "gap_thou": (port - od) * 1000,
                                   "port_sd_thou": lot_sd.get(rec["bag_lot_id"], 0.3),
                                   "od_sd_thou": reel_sd.get(rec["reel_id"], 0.3),
                                   "bag_level": rec["bag_lot_id"][-1], "od_level": rec["reel_id"][-2]}])
            cache[k] = float(model.predict(row)[0])
        return cache[k]

    cfg = copy.deepcopy(json.load(open("model.json")))
    cfg["sigma_mm"] = sigma
    out = {"first_alarm": None, "predicted_unit": None, "predicted_at": None, "actual_failure": None,
           "status": None, "error_units": None,
           "first_alarm_idx": None, "predicted_at_idx": None, "predicted_idx": None, "actual_idx": None}
    streams = {}
    for i, r in enumerate(recs):                     # one monitor per line if several lines are mixed
        streams.setdefault(r["line_id"], []).append((i, r))
    first_line = next(iter(streams))
    mon = LineMonitor(first_line, cfg, baseline_fn=base_fn)
    pred_idx = None
    for j, (i, r) in enumerate(streams[first_line]):
        u = mon.process(r, j + 1, book)
        f = mon.last_forecast
        if out["first_alarm"] is None and f and f["drift_alarm"]:
            out["first_alarm"], out["first_alarm_idx"] = r["unit_id"], i + 1
        if u["nok"]:
            out["actual_failure"], out["actual_idx"] = r["unit_id"], i + 1
            if pred_idx is None:
                out["status"] = "MISSED"
            else:
                out["error_units"] = j - pred_idx
                out["status"] = "HIT" if j == pred_idx else ("EARLY" if j > pred_idx else "LATE")
            break
        if pred_idx is None and mon.should_commit():
            k = mon.last_forecast["k_first_fail"]
            pred_idx = j + k
            out["predicted_at"], out["predicted_at_idx"] = r["unit_id"], i + 1
            inside = pred_idx < len(streams[first_line])
            out["predicted_unit"] = streams[first_line][pred_idx][1]["unit_id"] if inside else f"+{k} after {r['unit_id']}"
            out["predicted_idx"] = streams[first_line][pred_idx][0] + 1 if inside else i + 1 + k
    if out["status"] is None:
        out["status"] = "FALSE ALARM (no failure followed)" if pred_idx is not None else "no failure, no alarm"
    out["line"] = first_line
    return out


def run(train, test, keys, fake, max_points=1800):
    d_tr, _, _, _ = load(train, fake)
    d_te, recs, bag, pvc = (load(test, fake) if test != train else (d_tr, *load(train, fake)[1:]))
    same = train == test
    res = {"train": train, "test": test, "same_data": same, "n_train": len(d_tr), "n_test": len(d_te),
           "method": "5-fold cross-validation (train = test dataset)" if same else "fit on training data, predict test data",
           "actual": d_te.y.round(3).tolist()[:max_points], "unit_id": d_te.unit_id.tolist()[:max_points],
           "limits": [13.0, 17.0], "models": []}
    for key in keys:
        if same and key != FROZEN:
            pred = np.zeros(len(d_te))
            for tr, te in KFold(5, shuffle=True, random_state=0).split(d_te):
                pred[te] = make(key).fit(d_te.iloc[tr], d_te.y.iloc[tr]).predict(d_te.iloc[te])
        else:
            pred = make(key).fit(d_tr, d_tr.y).predict(d_te)
        full = make(key).fit(d_tr, d_tr.y)
        sigma = float(np.std(d_tr.y.values - full.predict(d_tr)))
        e = d_te.y.values - pred
        ok = (d_te.y >= 13) & (d_te.y <= 17)
        res["models"].append({
            "key": key, "name": NAMES[key],
            "rmse": float(np.sqrt(np.mean(e ** 2))), "mae": float(np.mean(np.abs(e))),
            "r2": float(1 - np.sum(e ** 2) / np.sum((d_te.y - d_te.y.mean()) ** 2)),
            "bias": float(np.mean(e)), "max_err": float(np.max(np.abs(e))),
            "rmse_in_spec_units": float(np.sqrt(np.mean(e[ok.values] ** 2))) if ok.any() else None,
            "sigma_train": sigma,
            "pred": np.round(pred, 3).tolist()[:max_points],
            "twin": twin_replay(full, sigma, recs, bag, pvc, d_te),
        })
    return res
