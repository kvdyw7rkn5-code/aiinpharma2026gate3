"""SYNTHETIC test data for validation (OQ) and offline demo only.

HISTORICAL ASM-01 contains no failures, so de-calibration is injected on a copy of it.
Synthetic records are always stored under mode="SIM" and are never mixed with REST data.
"""
import copy
import random


def decalibrate(hist_asm, line_id, prefix, start, slope, kind="axis", n=None, seed=0):
    """Copy historical units; from unit `start` on, add a drift of `slope` mm per unit.
    kind="axis"   -> actuator over-travels (actuator, following error and depth all move)
    kind="vision" -> camera calibration drifts (only the measured depth moves)
    kind="step"   -> sudden offset of `slope` mm from `start` on"""
    rng = random.Random(seed)
    n = n or len(hist_asm)
    offset = rng.randrange(0, len(hist_asm) - n + 1)
    out = []
    for i in range(n):
        r = copy.deepcopy(hist_asm[(offset + i) % len(hist_asm)])
        d = 0.0 if i < start else (slope if kind == "step" else slope * (i - start + 1))
        if kind in ("axis", "step"):
            r["actuator_position_mm"] = round(r["actuator_position_mm"] + d, 3)
            r["axis_following_error_mm"] = round(r["axis_following_error_mm"] - d, 3)
        r["visual_inspection_mm"] = round(r["visual_inspection_mm"] + d, 3)
        r["line_id"] = line_id
        r["unit_id"] = f"{prefix}{i + 1:03d}"
        out.append(r)
    return out


def first_failure(records):
    for i, r in enumerate(records):
        t = r["insertion_target_cm"] * 10
        if not (t - 2.0 <= r["visual_inspection_mm"] <= t + 2.0):
            return i, r["unit_id"]
    return None, None
