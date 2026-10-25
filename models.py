"""Model comparison for the presentation.   python3 models.py   ->  models_report.json + figures/*.png

Every model predicts the insertion depth (visual_inspection_mm) of a unit from information known BEFORE
it is assembled: the bag lot's port ID (mean/sd of its BAG-01 measurements), the reel's tube OD
(mean/sd of its PVC-01 readings) and the assembly pressure. Same data, same features, same tests.

Four validation schemes, from optimistic to strict:
  random     5-fold random split (units from the same lot/reel appear in train and test)
  new_lots   leave-one-production-lot-out (BAG_00k / Reel0k): new lots of known material levels
  unseen     leave-one-LEVEL-out (a pressure, a bag-port level or an OD level never seen in training):
             can the model extrapolate to a setting it has not seen?
  future     train on the first 70 % of the run, test on the last 30 % (time order)
"""
import json
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, WhiteKernel, ConstantKernel
from sklearn.linear_model import LinearRegression, RidgeCV
from sklearn.neighbors import KNeighborsRegressor
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler
from sklearn.svm import SVR
from sklearn.tree import DecisionTreeRegressor, export_text

warnings.filterwarnings("ignore")
RNG = 0
SPEC = (13.0, 17.0)


# ------------------------------------------------------------------ data
def load(src="data_hist"):
    return features(*(json.load(open(f"{src}/{n}.json")) for n in ("ASM-01", "PVC-01", "BAG-01")))


def features(asm, pvc, bag):
    """One row per assembled unit, with the material statistics of its bag lot and tube reel."""
    a, p, b = pd.DataFrame(asm), pd.DataFrame(pvc), pd.DataFrame(bag)
    bs = b.groupby("lot_id").port_id_in.agg(port_mean="mean", port_sd="std")
    ps = p.groupby("reel_id").outer_diameter_in.agg(od_mean="mean", od_sd="std")
    d = a.join(bs, on="bag_lot_id").join(ps, on="reel_id").reset_index(drop=True)
    for c in ("port_mean", "port_sd", "od_mean", "od_sd"):     # unknown lot/reel -> population value
        d[c] = d[c].fillna((bs if c.startswith("port") else ps)[c].mean())
        d[c + "_thou"] = d[c] * 1000
    d["gap_thou"] = d.port_mean_thou - d.od_mean_thou
    d["bag_level"] = d.bag_lot_id.str[-1]
    d["bag_lotno"] = d.bag_lot_id.str.split("_").str[1]
    d["od_level"] = d.reel_id.str[-2]
    d["od_var"] = d.reel_id.str[-1]
    d["reel_no"] = d.reel_id.str[4:6]
    d["y"] = d.visual_inspection_mm
    return d


# ------------------------------------------------------------------ models
class CellMeans:
    """Lookup table: average depth of each (bag level, OD level, pressure) cell of the experiment."""

    def fit(self, X, y):
        df = X.assign(y=y.values)
        self.cells = df.groupby(["bag_level", "od_level", "pressure_bar"]).y.mean()
        self.fallback = float(y.mean())
        return self

    def predict(self, X):
        keys = list(zip(X.bag_level, X.od_level, X.pressure_bar))
        return np.array([self.cells.get(k, self.fallback) for k in keys])

    def get_params(self, deep=False):
        return {}


class Cols:
    """Select feature columns, then run an sklearn estimator."""

    def __init__(self, cols, est):
        self.cols, self.est = cols, est

    def fit(self, X, y):
        self.est_ = clone(self.est).fit(X[self.cols].values, y.values)
        return self

    def predict(self, X):
        return self.est_.predict(X[self.cols].values)


BASE3 = ["port_mean_thou", "od_mean_thou", "pressure_bar"]
FULL5 = BASE3 + ["port_sd_thou", "od_sd_thou"]
sc = lambda est: make_pipeline(StandardScaler(), est)

MODELS = [
    # key, name, family, box, factory, explanation
    ("M00", "Average only", "reference", "white", lambda: Cols(BASE3, DummyRegressor()),
     "Predicts the overall mean depth for every unit. Any useful model must beat it."),
    ("M01", "Gap only (Gate 2 idea)", "linear", "white", lambda: Cols(["gap_thou"], LinearRegression()),
     "depth = a + b·(port ID − tube OD). The Gate 2 root cause as a single-variable straight line."),
    ("M02", "Gap + pressure", "linear", "white", lambda: Cols(["gap_thou", "pressure_bar"], LinearRegression()),
     "Adds the technician-set pressure to the gap."),
    ("M03", "Port + OD + pressure (PRODUCTION)", "linear", "white", lambda: Cols(BASE3, LinearRegression()),
     "Four coefficients, each with a physical meaning. This is the frozen model in model.json."),
    ("M04", "Linear + material variation", "linear", "white", lambda: Cols(FULL5, LinearRegression()),
     "Adds lot/reel standard deviations (how uneven the bag ports and tube are)."),
    ("M05", "Quadratic + interactions (ridge)", "linear", "white",
     lambda: Cols(BASE3, make_pipeline(StandardScaler(), PolynomialFeatures(2), RidgeCV(np.logspace(-3, 3, 13)))),
     "All squares and pairwise products of port, OD and pressure, with ridge shrinkage. 10 coefficients."),
    ("M06", "Lookup table (cell means)", "table", "white", lambda: CellMeans(),
     "Average of each bag-level × OD-level × pressure cell of the experiment. Cannot handle a new level."),
    ("M07", "Decision tree (depth 4)", "tree", "white",
     lambda: Cols(FULL5, DecisionTreeRegressor(max_depth=4, min_samples_leaf=20, random_state=RNG)),
     "At most 16 if-then rules; can be printed and read. Steps, not slopes."),
    ("M08", "Random forest", "ensemble ML", "grey",
     lambda: Cols(FULL5, RandomForestRegressor(300, min_samples_leaf=5, random_state=RNG, n_jobs=-1)),
     "Average of 300 randomised trees. Explained only through feature importance."),
    ("M09", "Gradient boosting", "ensemble ML", "grey",
     lambda: Cols(FULL5, GradientBoostingRegressor(n_estimators=300, max_depth=3, learning_rate=0.05,
                                                   subsample=0.8, random_state=RNG)),
     "300 small trees, each correcting the previous ones' errors."),
    ("M10", "k-nearest neighbours (k=25)", "instance ML", "grey", lambda: Cols(FULL5, sc(KNeighborsRegressor(25))),
     "Average depth of the 25 most similar past units."),
    ("M11", "Support vector regression (RBF)", "kernel ML", "black", lambda: Cols(FULL5, sc(SVR(C=3, epsilon=0.05))),
     "Smooth non-linear surface in scaled feature space."),
    ("M12", "Gaussian process", "kernel ML", "grey",
     lambda: Cols(BASE3, sc(GaussianProcessRegressor(ConstantKernel() * RBF([1, 1, 1]) + WhiteKernel(),
                                                     normalize_y=True, random_state=RNG))),
     "Bayesian smooth surface that also returns its own uncertainty."),
    ("M13", "Neural network (MLP 32-16)", "neural net", "black",
     lambda: Cols(FULL5, sc(MLPRegressor(hidden_layer_sizes=(32, 16), alpha=1e-3, max_iter=3000, early_stopping=True,
                                         random_state=RNG))),
     "Two hidden layers, 32 and 16 neurons: about 750 weights with no physical meaning."),
]


# ------------------------------------------------------------------ validation
def splits(d):
    n = len(d)
    rnd = np.random.RandomState(RNG).permutation(n) % 5
    out = {"random": [(rnd != k, rnd == k) for k in range(5)]}
    out["new_lots"] = [((d.bag_lotno != k) & (d.reel_no != r)).values for k, r in
                       zip(sorted(d.bag_lotno.unique()), ["01", "02"] * 3)]
    out["new_lots"] = [(m, ((d.bag_lotno == k) & (d.reel_no == r)).values) for m, k, r in
                       zip(out["new_lots"], sorted(d.bag_lotno.unique()), ["01", "02"] * 3)]
    unseen = []
    for col in ("pressure_bar", "bag_level", "od_level"):
        for v in sorted(d[col].unique()):
            unseen.append(((d[col] != v).values, (d[col] == v).values))
    out["unseen"] = unseen
    cut = int(0.7 * n)
    out["future"] = [(np.arange(n) < cut, np.arange(n) >= cut)]
    return out


def metrics(y, p):
    e = y - p
    return {"rmse": float(np.sqrt(np.mean(e ** 2))), "mae": float(np.mean(np.abs(e))),
            "r2": float(1 - np.sum(e ** 2) / np.sum((y - y.mean()) ** 2)), "max_err": float(np.max(np.abs(e)))}


def evaluate(d):
    sp = splits(d)
    res = []
    for key, name, fam, box, make, expl in MODELS:
        row = {"key": key, "name": name, "family": fam, "box": box, "explanation": expl, "tests": {}}
        for scheme, folds in sp.items():
            ys, ps = [], []
            for tr, te in folds:
                m = make().fit(d[tr], d.y[tr])
                ys.append(d.y.values[te])
                ps.append(m.predict(d[te]))
            row["tests"][scheme] = metrics(np.concatenate(ys), np.concatenate(ps))
        t = time.perf_counter()
        full = make().fit(d, d.y)
        row["fit_s"] = time.perf_counter() - t
        t = time.perf_counter()
        for _ in range(50):
            full.predict(d.iloc[[0]])
        row["predict_ms_per_unit"] = (time.perf_counter() - t) / 50 * 1000
        row["train_r2"] = metrics(d.y.values, full.predict(d))["r2"]
        row["n_parameters"] = n_params(full)
        row["detail"] = detail(key, full)
        res.append(row)
        print(f"{key} {name:38s} " + "  ".join(f"{s}:{row['tests'][s]['rmse']:.3f}" for s in sp) +
              f"  fit {row['fit_s']:.2f}s")
    return res


def n_params(m):
    if isinstance(m, CellMeans):
        return len(m.cells)
    est = m.est_
    last = est.steps[-1][1] if hasattr(est, "steps") else est
    if hasattr(last, "coef_") and hasattr(last, "intercept_") and np.ndim(last.coef_) == 1:
        return int(np.size(last.coef_) + 1)
    if hasattr(last, "coefs_"):
        return int(sum(w.size for w in last.coefs_) + sum(b.size for b in last.intercepts_))
    if hasattr(last, "tree_"):
        return int(last.tree_.node_count)
    if hasattr(last, "estimators_"):
        ests = np.ravel(last.estimators_)
        return int(sum(e.tree_.node_count for e in ests))
    if hasattr(last, "support_vectors_"):
        return int(last.support_vectors_.size)
    if hasattr(last, "X_train_"):
        return int(last.X_train_.size)
    if hasattr(last, "_fit_X"):
        return int(last._fit_X.size)
    return None


def detail(key, m):
    """Human-readable inside of the model (coefficients / rules / importances)."""
    if isinstance(m, CellMeans):
        return {"cells": {f"{a}|{b}|{c}": round(v, 3) for (a, b, c), v in m.cells.items()}}
    est = m.est_
    last = est.steps[-1][1] if hasattr(est, "steps") else est
    if isinstance(last, LinearRegression):
        return {"intercept": round(float(last.intercept_), 4), "coef": dict(zip(m.cols, np.round(last.coef_, 4).tolist()))}
    if isinstance(last, DecisionTreeRegressor):
        return {"rules": export_text(last, feature_names=m.cols, decimals=3)}
    if hasattr(last, "feature_importances_"):
        return {"importance": dict(zip(m.cols, np.round(last.feature_importances_, 3).tolist()))}
    return {}


def permutation_importance_all(d, keys=("M03", "M08", "M09", "M13")):
    """Model-agnostic importance: RMSE increase when one input is shuffled (same method for every model)."""
    out = {}
    rng = np.random.RandomState(RNG)
    for key, name, *_ , make, _e in [(m[0], m[1], m[2], m[3], m[4], m[5]) for m in MODELS if m[0] in keys]:
        m = make().fit(d, d.y)
        base = metrics(d.y.values, m.predict(d))["rmse"]
        imp = {}
        for c in ("port_mean_thou", "od_mean_thou", "pressure_bar", "port_sd_thou", "od_sd_thou"):
            dd = d.copy()
            dd[c] = rng.permutation(dd[c].values)
            if c in ("port_mean_thou", "od_mean_thou"):
                dd["gap_thou"] = dd.port_mean_thou - dd.od_mean_thou
            imp[c] = round(metrics(d.y.values, m.predict(dd))["rmse"] - base, 4)
        out[key] = imp
    return out


def leakage_check(d):
    """Why actuator position is NOT used: it is only known after the unit is assembled."""
    m = LinearRegression().fit(d[["actuator_position_mm"]], d.y)
    return {"r2_with_actuator": round(float(m.score(d[["actuator_position_mm"]], d.y)), 4),
            "note": "Actuator endpoint explains 98 % of depth but is recorded together with the depth of the same "
                    "unit, so it cannot predict a unit before it is made (target leakage)."}


def twin_check(d, keys=("M00", "M03", "M05", "M06", "M08", "M09", "M13")):
    """Plug each model in as the twin's baseline and replay the drift scenarios + the stable line."""
    import copy
    from model import LineMonitor, MaterialBook
    from sim import decalibrate, first_failure
    hist = json.load(open("data_hist/ASM-01.json"))
    bag = json.load(open("data_hist/BAG-01.json"))
    pvc = json.load(open("data_hist/PVC-01.json"))
    book = MaterialBook()
    for r in bag:
        book.add_bag(r)
    for r in pvc:
        book.add_pvc(r)
    lot_sd = d.groupby("bag_lot_id").port_sd_thou.first().to_dict()
    reel_sd = d.groupby("reel_id").od_sd_thou.first().to_dict()
    frozen = json.load(open("model.json"))
    scenarios = [(s, k) for s in (0.005, 0.01, 0.02, 0.04, -0.01, -0.02) for k in range(3)]
    out = {}
    for key, name, fam, box, make, _ in MODELS:
        if key not in keys:
            continue
        m = make().fit(d, d.y)
        sigma = None
        cache = {}

        def base_fn(rec, port, od):
            k = (rec["bag_lot_id"], rec["reel_id"], rec["pressure_bar"])
            if k not in cache:
                row = pd.DataFrame([{"port_mean_thou": port * 1000, "od_mean_thou": od * 1000,
                                     "pressure_bar": rec["pressure_bar"], "gap_thou": (port - od) * 1000,
                                     "port_sd_thou": lot_sd.get(rec["bag_lot_id"], 0.3),
                                     "od_sd_thou": reel_sd.get(rec["reel_id"], 0.3),
                                     "bag_level": rec["bag_lot_id"][-1], "od_level": rec["reel_id"][-2]}])
                cache[k] = float(m.predict(row)[0])
            return cache[k]

        res = d.y.values - m.predict(d)
        cfg = copy.deepcopy(frozen)
        cfg["sigma_mm"] = float(res.std())
        caught, errs, leads, false_pred = 0, [], [], 0
        for slope, seed in scenarios:
            recs = decalibrate(hist, "ASM-01", "T", 60 + 37 * seed, slope, "axis", n=900, seed=seed)
            idx, _ = first_failure(recs)
            mon = LineMonitor("ASM-01", cfg, baseline_fn=base_fn)
            pred_at = None
            for i, r in enumerate(recs):
                u = mon.process(r, i + 1, book)
                if u["nok"]:
                    break
                if pred_at is None and mon.should_commit():
                    leads.append(mon.last_forecast["k_first_fail"])
                    pred_at = i + 1 + leads[-1]
            if pred_at is not None:
                caught += 1
                errs.append(idx + 1 - pred_at)
        mon = LineMonitor("ASM-01", cfg, baseline_fn=base_fn)
        for i, r in enumerate(hist):
            mon.process(r, i + 1, book)
            if mon.should_commit():
                false_pred = 1
                break
        # Jury metrics (RULES.md): E = |Z - X| over all scenarios (a miss counts as E > 2), L = X - Y
        es = np.abs(errs)
        out[key] = {"scenarios": len(scenarios), "caught_in_advance_%": round(100 * caught / len(scenarios), 1),
                    "median_units_early": float(np.median(errs)) if errs else None,
                    "E<=2_%": round(100 * float((es <= 2).sum()) / len(scenarios), 1),
                    "median_E": float(np.median(es)) if errs else None,
                    "mean_L": round(float(np.mean(leads)), 1) if leads else None,
                    "estimator": cfg["detector"].get("estimator"),
                    "false_prediction_on_stable_line": bool(false_pred), "sigma_mm": round(cfg["sigma_mm"], 4)}
        print("twin", key, out[key])
    return out


if __name__ == "__main__":
    d = load()
    report = {"n_units": len(d), "target": "visual_inspection_mm", "features_base": BASE3, "features_full": FULL5,
              "schemes": __doc__.split("Four validation schemes, from optimistic to strict:")[1].strip(),
              "models": evaluate(d)}
    report["permutation_importance"] = permutation_importance_all(d)
    report["leakage"] = leakage_check(d)
    report["twin"] = twin_check(d)
    json.dump(report, open("models_report.json", "w"), indent=1)
    print("written models_report.json")
