# Gate 3 – Digital Twin: first-failure prediction for ASM-01/02/03

## Run
```bash
python3 model.py                       # fit + freeze the white-box baseline on HISTORICAL data -> model.json
python3 validate.py 3                  # OQ: prediction scenarios, false alarms, data-integrity tests
python3 monitor.py --mode SIM          # offline demo with injected de-calibration (synthetic, flagged SIM)
python3 monitor.py --mode REALTIME --team 01 --code <passcode> --limit 1
python3 dashboard.py --mode REALTIME   # http://localhost:8050 (read-only)
python3 analysis.py --mode HISTORICAL  # full-data analysis -> analysis_HISTORICAL.json
```
The dashboard has two tabs:
- **Live monitor:** the whole run per line, the forecast and the locked predictions.
- **Data analysis:** every record from the API, timestamp counts per source, data checks, material charts,
  what drives depth, the point of failure (closest approach on real data) and the synthetic de-calibration runs.
Use `--limit 1` in REALTIME so each prediction is committed before the next unit is requested.

## Model (no black box)
1. Baseline from HISTORICAL data (DOE: 3 bag-port levels × 9 tube reels × 3 pressures, R² 0.74):
   `depth_mm = c0 + c_port·portID_thou + c_od·OD_thou + c_p·pressure_bar`, coefficients in `model.json`.
2. Residual = measured − baseline. A calibrated line gives pure noise (σ ≈ 0.17 mm).
3. De-calibration alarm: CUSUM ≥ 5σ, plus a straight line through the residuals since the drift onset
   with |t| ≥ 3 in the same direction, held for 3 units.
4. Forecast: extend the line, add normal noise → P(fail) per future unit → first-failure distribution.
   Commit the unit at the 5 % quantile once it is ≤ 3 units ahead. This errs early, which is the safe side.

Validation (`validation_report.json`, 80 synthetic drift scenarios + 1,800 stable units):
0 false predictions on the stable line. 75 % of failures predicted in advance. Most predictions are early
(median error 9 units). Exact unit hits are rare (5 %), because the first failure is a random tail event
when part-to-part noise is about 0.17 mm. This limit is stated openly, not hidden.

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
