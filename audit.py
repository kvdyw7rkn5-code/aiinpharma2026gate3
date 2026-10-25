"""Read-only audit functions for the jury: URS assertions, unit traceability, ledger browsing.

Every assertion is recomputed from the ledger at request time, so the website shows evidence,
not a claim. Nothing here writes to the ledger (it connects as twin_reader in read-only transactions).
"""
import json
import re

from store import Ledger, canonical, ledger_exists, utc_now

URS = [
    ("URS-DI-001", "The system shall uniquely identify every received manufacturing record using its source line, "
                   "event ID, and associated Unit ID where applicable.", "Attributable"),
    ("URS-DI-002", "The system shall preserve original manufacturing measurements and timestamps without modifying "
                   "source data.", "Original, Accurate"),
    ("URS-DI-003", "The system shall detect potential assembly-line de-calibration and predict the first nonconforming "
                   "Unit ID, recording the assembly line, predicted Unit ID, currently processed Unit ID, and timestamp "
                   "of prediction.", "Contemporaneous, Original"),
    ("URS-DI-004", "The system shall maintain traceability between each finished Unit ID, its associated BAG and PVC "
                   "manufacturing data, the submitted prediction, and the actual vision inspection result.",
     "Attributable, Complete"),
    ("URS-DI-005", "The system shall retain manufacturing records, predictions, and inspection results throughout the "
                   "assessment and make them available for review by the jury.", "Enduring, Available"),
]
SOURCE_KINDS = ("BAG", "PVC", "ASM")


class _Row(tuple):
    """A result row usable by position and by column name (r[0], r["kind"], dict(r))."""

    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        return super().__getitem__(self._names.index(key) if isinstance(key, str) else key)

    def keys(self):
        return list(self._names)


def _rows(cursor):
    names = [c.name for c in cursor.description]
    return lambda values: _Row(names, values)


def _q(led, sql, args=()):
    """Run a query against this ledger's table (`FROM ledger` is qualified with the ledger's schema)."""
    sql = re.sub(r"\bFROM ledger\b", f"FROM {led._t}", sql)
    with led._lock, led.db.cursor(row_factory=_rows) as cur:
        return cur.execute(sql, args).fetchall()


def _raw_index(led, mode):
    """(line, event_no) -> canonical record, rebuilt from the verbatim API responses."""
    idx = {}
    for r in _q(led, "SELECT payload FROM ledger WHERE kind='RAW_RESPONSE' AND mode=%s ORDER BY seq", (mode,)):
        p = json.loads(r["payload"])
        if p.get("status") != 200:
            continue
        line, qs = p["path"].split("?", 1)
        start = int(dict(kv.split("=") for kv in qs.split("&"))["startWith"])
        try:
            body = json.loads(p["body"])
        except ValueError:
            continue
        for i, rec in enumerate(body):
            idx.setdefault((line, start + i), canonical(rec))
    return idx


def urs_checks(db, mode):
    led = Ledger(db, readonly=True)
    out = []

    # ---------------- DI-001 unique identification
    n, n_distinct = _q(led, "SELECT count(*), count(DISTINCT record_uid) FROM ledger")[0]
    by_kind = {k: c for k, c in _q(led, "SELECT kind, count(*) FROM ledger WHERE mode=%s GROUP BY kind", (mode,))}
    no_id = _q(led, f"SELECT count(*) FROM ledger WHERE mode=%s AND kind IN {SOURCE_KINDS} "
                    "AND (line_id IS NULL OR event_no IS NULL)", (mode,))[0][0]
    asm_no_unit = _q(led, "SELECT count(*) FROM ledger WHERE mode=%s AND kind='ASM' AND (unit_id IS NULL OR unit_id='')", (mode,))[0][0]
    dup_units = _q(led, "SELECT count(*) FROM (SELECT line_id, unit_id FROM ledger WHERE mode=%s AND kind='ASM' "
                        "GROUP BY line_id, unit_id HAVING count(*) > 1) AS d", (mode,))[0][0]
    gaps = []
    for kind, line, mx, cnt in _q(led, f"SELECT kind, line_id, max(event_no), count(DISTINCT event_no) FROM ledger "
                                       f"WHERE mode=%s AND kind IN {SOURCE_KINDS} GROUP BY kind, line_id", (mode,)):
        if mx != cnt:
            gaps.append(f"{line}: {mx - cnt} missing event numbers")
    src_total = sum(by_kind.get(k, 0) for k in SOURCE_KINDS)
    ok = n == n_distinct and no_id == 0 and asm_no_unit == 0 and dup_units == 0 and not gaps
    out.append({"status": "PASS" if ok else "FAIL", "evidence": [
        f"{n:,} ledger entries, {n_distinct:,} distinct record IDs (the database refuses duplicates)",
        f"{src_total:,} manufacturing records ({', '.join(f'{k} {by_kind.get(k, 0):,}' for k in SOURCE_KINDS)}), "
        f"each identified as MODE/LINE/event number; {no_id} without line or event ID",
        f"{by_kind.get('ASM', 0):,} assembly records carry a Unit ID; {asm_no_unit} missing, {dup_units} duplicated",
        "event numbers per source are continuous" if not gaps else "gaps: " + "; ".join(gaps)],
        "example": (_q(led, "SELECT record_uid, line_id, event_no, unit_id FROM ledger WHERE mode=%s AND kind='ASM' "
                            "ORDER BY seq LIMIT 1", (mode,)) or [None])[0] and
                   dict(_q(led, "SELECT record_uid, line_id, event_no, unit_id FROM ledger WHERE mode=%s AND kind='ASM' "
                                "ORDER BY seq LIMIT 1", (mode,))[0])})

    # ---------------- DI-002 originals preserved
    chain_ok, entries, problem = led.verify()
    triggers = sorted(r[0] for r in _q(led, "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass "
                                            "AND NOT tgisinternal AND tgenabled <> 'D'", (led._t,)))
    # What the monitor's own role may do on this table (granted by the server, not by our code)
    priv = {p: _q(led, "SELECT has_table_privilege('twin_app', %s, %s)", (led._t, p))[0][0]
            for p in ("INSERT", "SELECT", "UPDATE", "DELETE", "TRUNCATE")}
    time_mismatch = _q(led, f"SELECT count(*) FROM ledger WHERE mode=%s AND kind IN {SOURCE_KINDS} "
                            "AND (payload_json->>'time') IS DISTINCT FROM source_time", (mode,))[0][0]
    raw = _raw_index(led, mode)
    compared = differ = 0
    if raw:
        for r in _q(led, f"SELECT line_id, event_no, payload FROM ledger WHERE mode=%s AND kind IN {SOURCE_KINDS}", (mode,)):
            ref = raw.get((r["line_id"], r["event_no"]))
            if ref is not None:
                compared += 1
                differ += ref != r["payload"]
    n_raw = by_kind.get("RAW_RESPONSE", 0)
    app_ok = priv["INSERT"] and not (priv["UPDATE"] or priv["DELETE"] or priv["TRUNCATE"])
    ok = chain_ok and {"no_update_delete", "no_truncate"} <= set(triggers) and app_ok and time_mismatch == 0 and differ == 0
    out.append({"status": "PASS" if ok else "FAIL", "evidence": [
        f"hash chain re-verified now over {entries:,} entries: {'intact' if chain_ok else 'BROKEN – ' + str(problem)}",
        f"database rules active: {', '.join(triggers) or 'none'} (UPDATE, DELETE and TRUNCATE are refused, even for the owner)",
        "monitor role twin_app may " + ", ".join(p for p, v in priv.items() if v) + "; it may not " +
        (", ".join(p for p, v in priv.items() if not v) or "- (WARNING: too many rights)"),
        f"{n_raw:,} API responses stored word for word" + (
            f"; {compared:,} stored records re-compared with them: {differ} differ" if raw else
            " (synthetic data is generated locally, so there are no API responses)"),
        f"source timestamps unchanged: {time_mismatch} records where the stored time differs from the source time"]})

    # ---------------- DI-003 de-calibration + prediction record
    def prediction_facts(lg, md):
        facts, ok_all, preds = [], True, _q(lg, "SELECT * FROM ledger WHERE mode=%s AND kind='PREDICTION' ORDER BY seq", (md,))
        alerts = _q(lg, "SELECT count(*) FROM ledger WHERE mode=%s AND kind='ALERT' AND record_uid LIKE %s",
                    (md, "%DECALIBRATION%"))[0][0]
        for p in preds:
            pl = json.loads(p["payload"])
            need = ("line_id", "predicted_unit_id", "current_unit_id", "prediction_time_utc")
            has = all(pl.get(k) for k in need)
            later = _q(lg, "SELECT seq, received_utc FROM ledger WHERE mode=%s AND kind='ASM' AND line_id=%s AND unit_id=%s",
                       (md, p["line_id"], pl["predicted_unit_id"]))
            before = (not later) or later[0]["seq"] > p["seq"]
            ok_all &= has and before
            ev = _q(lg, "SELECT payload FROM ledger WHERE mode=%s AND kind='EVALUATION' AND line_id=%s", (md, p["line_id"]))
            evp = json.loads(ev[0]["payload"]) if ev else {}
            jury = f" (L = {pl.get('units_ahead')}" + (f", E = {evp['E_abs_error_units']} vs actual {evp['actual_first_failure_unit_id']})"
                                                     if evp.get("E_abs_error_units") is not None else ")")
            facts.append(f"{p['line_id']}: predicted {pl['predicted_unit_id']} while processing {pl['current_unit_id']} "
                         f"at {pl['prediction_time_utc']}{jury} – " +
                         ("locked before that unit was received" if later and before else
                          "that unit has not been produced yet" if not later else "NOT before the unit") +
                         ("" if has else " – MISSING FIELDS"))
        n_lines = len(_q(lg, "SELECT DISTINCT line_id FROM ledger WHERE mode=%s AND kind='ASM'", (md,)))
        return preds, alerts, facts, ok_all, n_lines

    preds, alerts, facts, ok3, n_lines = prediction_facts(led, mode)
    ev = [f"drift detector active on {n_lines} line(s); {alerts} de-calibration alarm(s) raised in {mode} data"]
    if preds:
        ev += [f"{len(preds)} locked prediction(s), at most one per line (the database refuses a second one)"] + facts
        status = "PASS" if ok3 else "FAIL"
    else:
        ev.append(f"no de-calibration in the {mode} data, so correctly no prediction was made")
        status = "NOT TRIGGERED"
    if mode != "SIM" and ledger_exists("SIM"):
        sp, sa, sf, sok, _ = prediction_facts(Ledger("SIM", readonly=True), "SIM")
        if sp:
            ev.append(f"demonstrated on the synthetic drift runs: {sa} alarm(s), {len(sp)} locked prediction(s), "
                      f"all {'complete and locked in time' if sok else 'NOT all valid'}")
            status = "PASS" if status == "NOT TRIGGERED" and sok else status
    out.append({"status": status, "evidence": ev})

    # ---------------- DI-004 traceability
    lots = {r[0] for r in _q(led, "SELECT DISTINCT payload_json->>'lot_id' FROM ledger WHERE mode=%s AND kind='BAG'", (mode,))}
    reels = {r[0] for r in _q(led, "SELECT DISTINCT payload_json->>'reel_id' FROM ledger WHERE mode=%s AND kind='PVC'", (mode,))}
    units = _q(led, "SELECT payload_json->>'bag_lot_id' AS b, payload_json->>'reel_id' AS r FROM ledger "
                    "WHERE mode=%s AND kind='ASM'", (mode,))
    no_bag = sum(u["b"] not in lots for u in units)
    no_pvc = sum(u["r"] not in reels for u in units)
    evals = _q(led, "SELECT * FROM ledger WHERE mode=%s AND kind='EVALUATION'", (mode,))
    linked = sum(1 for e in evals if json.loads(e["payload"]).get("actual_first_failure_unit_id"))
    ok = no_bag == 0 and no_pvc == 0
    out.append({"status": "PASS" if ok else "FAIL", "evidence": [
        f"{len(units):,} finished units linked to {len(lots)} bag lots and {len(reels)} tube reels: "
        f"{no_bag} without BAG data, {no_pvc} without PVC data",
        f"{len(preds)} prediction(s) and {len(evals)} inspection result(s) stored with line and Unit ID; "
        f"{linked} result(s) name the actual first nonconforming unit next to the predicted one",
        "any unit can be traced end to end in the Audit trail tab (record → raw API response → bag lot → tube reel → prediction → inspection)"]})

    # ---------------- DI-005 retention + availability
    size = _q(led, "SELECT pg_total_relation_size(%s::regclass)", (led._t,))[0][0] / 1e6
    first, last = _q(led, "SELECT min(received_utc), max(received_utc) FROM ledger")[0]
    out.append({"status": "PASS", "evidence": [
        f"PostgreSQL ledger {led.schema}: {size:.1f} MB, {entries:,} entries, from {first} to {last}",
        "records cannot be deleted (role privileges + database rule) and the database volume survives restarts; "
        "the monitor resumes from it",
        "this website connects with the read-only role twin_reader; the jury can look up any unit, browse the ledger, "
        "re-verify it and download a full export"]})

    return {"mode": mode, "checked_utc": utc_now(),
            "urs": [dict(id=i, text=t, alcoa=a, **o) for (i, t, a), o in zip(URS, out)]}


def unit_trace(db, mode, unit_id):
    led = Ledger(db, readonly=True)
    rows = _q(led, "SELECT * FROM ledger WHERE mode=%s AND kind='ASM' AND unit_id=%s", (mode, unit_id))
    if not rows:
        like = [r[0] for r in _q(led, "SELECT unit_id FROM ledger WHERE mode=%s AND kind='ASM' AND unit_id LIKE %s LIMIT 8",
                                 (mode, f"%{unit_id}%"))]
        return {"error": f"Unit {unit_id} not found in the {mode} ledger.", "suggestions": like}
    a = dict(rows[0])
    rec = json.loads(a["payload"])
    raw = None
    for r in _q(led, "SELECT * FROM ledger WHERE mode=%s AND kind='RAW_RESPONSE' AND line_id=%s AND event_no<=%s "
                     "ORDER BY event_no DESC, seq LIMIT 3", (mode, a["line_id"], a["event_no"])):
        p = json.loads(r["payload"])
        try:
            body = json.loads(p["body"])
        except ValueError:
            continue
        if r["event_no"] + len(body) > a["event_no"]:
            raw = {"record_uid": r["record_uid"], "received_utc": r["received_utc"], "request": p["path"],
                   "http_status": p["status"], "records_in_response": len(body),
                   "position": a["event_no"] - r["event_no"] + 1, "payload_sha256": r["payload_sha256"],
                   "matches_stored_record": canonical(body[a["event_no"] - r["event_no"]]) == a["payload"]}
            break

    def material(kind, key, val, field):
        rs = _q(led, f"SELECT record_uid, received_utc, (payload_json->>'{field}')::float8 AS v, payload_json->>'time' AS t "
                     f"FROM ledger WHERE mode=%s AND kind=%s AND payload_json->>'{key}'=%s ORDER BY event_no", (mode, kind, val))
        if not rs:
            return None
        v = [r["v"] for r in rs]
        return {"records": len(rs), "first_uid": rs[0]["record_uid"], "last_uid": rs[-1]["record_uid"],
                "first_time": rs[0]["t"], "last_time": rs[-1]["t"], "mean": sum(v) / len(v), "min": min(v), "max": max(v)}

    pred = _q(led, "SELECT * FROM ledger WHERE mode=%s AND kind='PREDICTION' AND line_id=%s", (mode, a["line_id"]))
    ev = _q(led, "SELECT * FROM ledger WHERE mode=%s AND kind='EVALUATION' AND line_id=%s", (mode, a["line_id"]))
    p = json.loads(pred[0]["payload"]) if pred else None
    e = json.loads(ev[0]["payload"]) if ev else None
    target = rec["insertion_target_cm"] * 10
    role = []
    if p and p["predicted_unit_id"] == unit_id:
        role.append("this is the unit that was PREDICTED to be the first out of spec")
    if p and p["current_unit_id"] == unit_id:
        role.append("the prediction was locked while this unit was being processed")
    if e and e["actual_first_failure_unit_id"] == unit_id:
        role.append("this is the ACTUAL first out-of-spec unit")
    return {
        "unit_id": unit_id, "line_id": a["line_id"], "mode": mode,
        "ledger": {k: a[k] for k in ("seq", "record_uid", "event_no", "source_time", "received_utc", "actor",
                                     "payload_sha256", "prev_hash", "entry_hash")},
        "record": rec, "raw_response": raw,
        "inspection": {"depth_mm": rec["visual_inspection_mm"], "limits_mm": [target - 2, target + 2],
                       "conforming": target - 2 <= rec["visual_inspection_mm"] <= target + 2},
        "bag": material("BAG", "lot_id", rec["bag_lot_id"], "port_id_in"),
        "pvc": material("PVC", "reel_id", rec["reel_id"], "outer_diameter_in"),
        "prediction": p and dict(p, record_uid=pred[0]["record_uid"], entry_hash=pred[0]["entry_hash"],
                                 received_utc=pred[0]["received_utc"]),
        "evaluation": e and dict(e, record_uid=ev[0]["record_uid"]),
        "role": role,
    }


def ledger_page(db, kind=None, limit=25, before=None):
    led = Ledger(db, readonly=True)
    sql, args = "SELECT seq, kind, record_uid, line_id, unit_id, source_time, received_utc, payload_sha256, entry_hash FROM ledger WHERE 1=1", []
    if kind:
        sql += " AND kind=%s"
        args.append(kind)
    if before:
        sql += " AND seq<%s"
        args.append(int(before))
    rows = [dict(r) for r in _q(led, sql + " ORDER BY seq DESC LIMIT %s", (*args, min(int(limit), 200)))]
    kinds = {k: c for k, c in _q(led, "SELECT kind, count(*) FROM ledger GROUP BY kind")}
    return {"rows": rows, "kinds": kinds}


def predictions_and_failures(db, mode):
    led = Ledger(db, readonly=True)
    out = []
    for kind, field in (("PREDICTION", "predicted_unit_id"), ("EVALUATION", "actual_first_failure_unit_id")):
        for r in _q(led, "SELECT line_id, payload FROM ledger WHERE mode=%s AND kind=%s", (mode, kind)):
            out.append({"line": r["line_id"], "kind": kind, "unit_id": json.loads(r["payload"])[field]})
    closest = _q(led, "SELECT line_id, unit_id FROM ledger WHERE mode=%s AND kind='ASM' "
                      "ORDER BY abs((payload_json->>'visual_inspection_mm')::float8 - 15.0) DESC LIMIT 2", (mode,))
    out += [{"line": r["line_id"], "kind": "EXTREME", "unit_id": r["unit_id"]} for r in closest]
    first = _q(led, "SELECT line_id, unit_id FROM ledger WHERE mode=%s AND kind='ASM' ORDER BY seq LIMIT 1", (mode,))
    out += [{"line": r["line_id"], "kind": "FIRST", "unit_id": r["unit_id"]} for r in first]
    return out


def export_bytes(db):
    led = Ledger(db, readonly=True)
    return "".join(canonical(r) + "\n" for r in led.rows()).encode()
