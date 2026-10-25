# Gate 3 – Digital Twin: first-failure prediction for ASM-01/02/03

Scoring and event rules we must follow: [`RULES.md`](RULES.md).

## Run
```bash
python3 model.py                       # fit + freeze the white-box baseline on HISTORICAL data -> model.json
python3 validate.py                    # OQ: both commit rules on E/L, false alarms, data-integrity tests
python3 monitor.py --mode SIM          # offline demo with injected de-calibration (synthetic, flagged SIM)
python3 monitor.py --mode REALTIME --team 01 --code <passcode> --limit 1
python3 dashboard.py --mode REALTIME   # http://localhost:8050 (read-only)
python3 analysis.py --mode HISTORICAL  # full-data analysis -> analysis_HISTORICAL.json
python3 models.py                      # train + compare 14 models -> models_report.json (~70 s)
python3 model_figures.py               # presentation charts -> figures/model_accuracy.png, figures/model_twin.png
```
The website (`web/index.html`, `web/app.css`, `web/app.js`, served by `dashboard.py`) has five tabs:
- **How it works:** the six steps of the system, what insertion depth is, real vs synthetic data,
  a glossary, and how ALCOA+ is met.
- **Live monitor:** one card per line. It shows a plain-language status (in control / drift detected /
  prediction locked / failed), key numbers, the depth chart with forecast, the drift alarm meter and the locked
  prediction with its fingerprint. You can show the last 300 units or the whole run.
- **Data explorer:** every record from the API, where the line came closest to failing, timestamps per source,
  data quality checks, materials, and the synthetic failure runs.
- **Model lab:** see below.
- **Model comparison:** all 14 models side by side, with the verdict.

Every technical word (dotted underline) and every model name shows a short explanation when you hover over it.
Chart.js is served locally from `web/`, so the site works without internet.

The dashboard only processes ledger entries that are new since the last refresh, compresses every response,
and skips refreshes when nothing has changed or the browser tab is hidden. A live update takes about 1 ms and
50 KB, down from 1.1 s and 390 KB.
Use `--limit 1` in REALTIME so each prediction is committed before the next unit is requested.

## Model (no black box)
1. Baseline from HISTORICAL data (DOE: 3 bag-port levels × 9 tube reels × 3 pressures, R² 0.74):
   `depth_mm = c0 + c_port·portID_thou + c_od·OD_thou + c_p·pressure_bar`, coefficients in `model.json`.
2. Residual = measured − baseline. A calibrated line gives pure noise (σ ≈ 0.17 mm).
3. De-calibration alarm: CUSUM ≥ 5σ, plus a straight line through the residuals since the drift onset
   with |t| ≥ 3 in the same direction, held for 3 units.
4. Forecast: extend the line, add normal noise → P(fail) per future unit → first-failure distribution
   (shown on the dashboard).
5. Commit rule (`detector.estimator` in `model.json`, one prediction per line, immutable):
   - **`holt_next` (default):** Holt smoothing of the residual (level + trend) gives P(next unit fails).
     When it reaches 0.1, the prediction is X = current unit + 3, so the lead time is L = 3.
   - **`quantile` (previous default):** commits the 5 % quantile of the forecast first failure once it is
     ≤ 3 units ahead, after the alarm has held for 3 units.

Validation (`validate.py` → `validation_report.json`):
- **Scenarios:** 95 synthetic de-calibrations: 80 drifts (actuator or vision, both directions) and 15 steps.
  The stable line has 1,800 units.
- **Scoring:** the jury metrics in `RULES.md`, E = |Z − X| (error) and L = X − Y (lead).

| Commit rule | E ≤ 2 | E ≤ 5 | Missed | Median E | L | False predictions on stable line |
|---|---|---|---|---|---|---|
| `holt_next` p ≥ 0.1, L = 3 | **26 %** | 33 % | 35 % | 6 | 3 | 0 |
| `quantile` h = 3 | 13 % | 19 % | 35 % | 9 | 2.6 | 0 |

- **Variants tried and rejected:** a drift-rate-based lead (E ≤ 2 about 2 %), a 2-unit persistence (missed 54 %),
  and p ≥ 0.2 with L = 2 (missed 52 %).
- **Limits:**
  - Exact unit hits stay rare, because the first failure is a random tail event when part-to-part noise is about
    0.17 mm.
  - Slow drifts are locked early.
  - A sudden step cannot be foreseen before the first shifted unit.

  These limits are stated openly, not hidden.

## Model lab (Data analysis tab, `lab.py`)
Pick a **training dataset**, a **test dataset** and up to 8 models, then click *Train & compare*.

| Dataset | What it is |
|---|---|
| `HISTORICAL · ASM-01` | The real REST data (1,800 units, no failures), read from the GxP ledger. |
| `SIM · ASM-01/02/03` / `SIM · all lines` | The stored synthetic de-calibration runs. Each stream ends at its first failure. |
| `REALTIME · …` | Appears automatically once a REALTIME ledger exists. |
| `FAKE` | Generated on request: historical units plus a drift you choose (none / actuator / camera / sudden step), with its size in mm per unit, start unit, number of units, extra noise and random seed. |

Lab settings can be put in a link, e.g.
`http://localhost:8050/?train=HISTORICAL:ASM-01&test=SIM:ASM-02&models=M03F,M09,M13&run=1#lab`. That opens a ready-made
comparison, which is handy in a presentation.

For every model the lab shows a **timeline of where each model predicts the first failure** (one row per model:
▲ drift alarm, ○ prediction locked, ◆ predicted failure, red line = actual first failure), plus:
- prediction error on the test data (RMSE, MAE, R², bias, max error),
- a chart of measured vs predicted depth,
- a rolling residual chart, where drift shows up as a trend,
- a **twin replay**: the model plugged in as the twin's baseline and run over the test units with the
  production alarm and commit rules. It reports the first drift alarm, the locked prediction, the actual first
  failure, and HIT / EARLY / LATE / MISSED / FALSE ALARM.

If training and test data are the same, the lab uses 5-fold cross-validation, so no unit is predicted by a model
that saw it. `M03F` is the frozen production model from `model.json`. It ignores the training choice and shows
what production would do.

Things to try:
- Train on historical data, test on a SIM line. Every model catches the drift.
- Train on FAKE data with strong drift, test on historical data. The models learn the drift, are off by about
  4 mm, and some raise false alarms. Only the frozen model stays correct, which shows why the training data
  must come from a calibrated line.
- FAKE with a sudden step. Nobody can predict a jump in advance: MISSED.
- FAKE with 0.1 mm extra noise. Look at how much of the advantage of each model survives.

Lab runs happen **in memory only**. Nothing is written to any ledger and `model.json` is never changed, so
experiments cannot touch GxP records or the production model. Results show a *SYNTHETIC* badge whenever fake or
SIM data is involved.

## Model comparison (`models.py`)
Every model gets the same 1,800 ASM-01 units and predicts insertion depth only from information known
**before** the unit is assembled: the bag lot's port ID, the reel's tube OD (mean and spread of their BAG-01 /
PVC-01 measurements) and the assembly pressure. Each model is tested four ways:

| Test | What it asks |
|---|---|
| random | 5-fold random split. Optimistic: the same lots appear in training and test. |
| new lots | Leave one bag lot and one reel out. Does it work on the next production lot? |
| unseen setting | Leave out one whole pressure, bag-port level or OD level. Can it handle a setting it has never seen? |
| future | Train on the first 70 % of the run, test on the last 30 %. |

### The models
| # | Model | Type | How it works |
|---|---|---|---|
| M00 | Average only | reference | Predicts the overall mean for every unit. Any useful model must beat it. |
| M01 | Gap only (Gate 2 idea) | linear, white box | depth = a + b·(port ID − tube OD). The Gate 2 root cause as one straight line. |
| M02 | Gap + pressure | linear, white box | Adds the technician-set pressure. |
| **M03** | **Port + OD + pressure (production)** | **linear, white box** | **4 coefficients with physical meaning; the frozen model in `model.json`.** |
| M04 | Linear + material variation | linear, white box | Adds lot/reel spread (standard deviation) as inputs. |
| M05 | Quadratic + interactions (ridge) | linear, white box | Squares and pairwise products of port, OD and pressure, with ridge shrinkage. |
| M06 | Lookup table | table, white box | Average depth of each bag-level × OD-level × pressure cell. Cannot answer for a new level. |
| M07 | Decision tree (depth 4) | tree, white box | At most 16 readable if-then rules. Gives steps, not slopes. |
| M08 | Random forest | ML, grey box | Average of 300 randomised trees. Explained only through feature importance. |
| M09 | Gradient boosting | ML, grey box | 300 small trees, each correcting the previous ones' errors. |
| M10 | k-nearest neighbours | ML, grey box | Average of the 25 most similar past units. |
| M11 | Support vector regression | ML, black box | Smooth non-linear surface (RBF kernel). |
| M12 | Gaussian process | ML, grey box | Bayesian smooth surface; also returns its own uncertainty. |
| M13 | Neural network (MLP 32-16) | ML, black box | Two hidden layers, about 740 weights with no physical meaning. |

### Results (RMSE in mm, lower is better)
| # | Model | random | new lots | unseen setting | future | parameters | twin: failures caught early |
|---|---|---|---|---|---|---|---|
| M00 | Average only | 0.327 | 0.333 | 0.375 | 0.338 | 1 | 44 % + false alarm |
| M01 | Gap only | 0.251 | 0.263 | 0.285 | 0.252 | 2 | |
| M02 | Gap + pressure | 0.182 | 0.199 | 0.200 | 0.183 | 3 | |
| **M03** | **Production linear** | **0.166** | **0.185** | **0.181** | **0.167** | **4** | **78 %** |
| M04 | Linear + variation | 0.164 | 0.183 | 6.369 | 0.164 | 6 | |
| M05 | Quadratic (ridge) | 0.160 | 0.179 | 4.475 | 0.162 | 11 | 78 % |
| M06 | Lookup table | 0.160 | 0.180 | 0.375 | 0.163 | 27 | 78 % |
| M07 | Decision tree | 0.182 | 0.200 | 0.311 | 0.189 | 31 nodes | |
| M08 | Random forest | 0.160 | 0.188 | 0.289 | 0.164 | ~101k nodes | 78 % |
| M09 | Gradient boosting | 0.154 | 0.190 | 0.287 | 0.159 | ~4.5k nodes | 78 % |
| M10 | k-NN | 0.163 | 0.188 | 0.241 | 0.164 | 9,000 stored values | |
| M11 | SVR | 0.162 | 0.189 | 0.298 | 0.164 | ~6.8k | |
| M12 | Gaussian process | 0.160 | 0.179 | 0.176 | 0.162 | 5,400 stored values | |
| M13 | Neural network | 0.176 | 0.218 | 5.290 | 0.178 | 737 weights | 83 % |

"Twin" = the model plugged in as the digital twin's baseline, run on 18 synthetic de-calibration scenarios
and on the stable 1,800-unit line.

### What the comparison shows
1. **Machine learning barely helps on familiar settings.** The best ML model (gradient boosting, 0.154 mm)
   beats the production linear model (0.166 mm) by only 0.012 mm. About 0.16 mm is part-to-part noise that
   no model can predict from material and pressure data.
2. **On a setting never seen in training, most models fail.** Trees and forests get about 60 % worse, and the
   lookup table falls back to the average. The neural network (5.3 mm) and the extended linear models
   (4.5–6.4 mm) break down, because they rely on lot spread. In this experiment, spread is tied to the
   material level, so it stops making sense outside the training range (see permutation importance:
   for M13, shuffling the port spread costs 2.8 mm). Only M03 and the Gaussian process stay accurate.
3. **Inside the twin, the model choice barely matters.** Every real model catches 78–83 % of failures early.
   The average-only model shows that a material/pressure baseline *is* needed: without it the twin catches
   only 44 % and raises a false alarm.
4. **Why ML is a hard sell under GxP.** The 4 coefficients can be checked by hand and have a physical meaning.
   ML models are GAMP 5 category 5 software, need ongoing monitoring for model drift, and cannot be explained
   to an inspector. Actuator position would predict depth almost perfectly (R² 0.98), but it is recorded
   *with* the depth of the same unit, so using it would be target leakage.

**Decision:** keep M03 in production. The Gaussian process (M12) is the only credible alternative, because it
is equally accurate, extrapolates, and also gives its own uncertainty.

## Data integrity (ALCOA+ / GxP)
| URS | How it is met |
|---|---|
| DI-001 | `record_uid = MODE/LINE/event_no` (UNIQUE), plus `line_id` and `unit_id` columns |
| DI-002 | every REST body is stored verbatim (`RAW_RESPONSE`) before parsing; source timestamps are never changed |
| DI-003 | `PREDICTION` entry: line, predicted unit, current unit, UTC timestamp, model version, evidence |
| DI-004 | each unit links to bag lot/reel → BAG/PVC records, the prediction and the `EVALUATION` |
| DI-005 | SQLite ledger + JSONL export, read-only dashboard |

- Append-only: DB triggers block UPDATE/DELETE. A SHA-256 hash chain detects any edit made outside the app.
- One prediction per line (UNIQUE key). It can never be changed after it is committed.
- The access code is never stored. The model is versioned by hash and linked to the hashes of its training files.
- SIM (synthetic) data is kept separate by mode and never mixed with REST data.
