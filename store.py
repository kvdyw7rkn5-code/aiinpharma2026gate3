"""GxP data store: append-only, hash-chained SQLite ledger.

ALCOA+ mapping
  Attributable    every entry carries record_uid (mode/line/event no.), line_id, unit_id, actor
  Legible         plain JSON payloads, human-readable kinds
  Contemporaneous received_utc is stamped at the moment of receipt / commit
  Original        raw REST response bodies are stored verbatim with SHA-256
  Accurate        source values are never transformed in place; derived values live in separate entries
  Complete        nothing can be deleted (DB triggers), gaps in event numbers are detectable
  Consistent      one UTC clock, one canonical JSON encoding, one hash chain
  Enduring        single SQLite file + JSON export, survives restarts
  Available       read-only dashboard and export for the jury
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,          -- RAW_RESPONSE | BAG | PVC | ASM | MODEL | PREDICTION | EVALUATION | ALERT | SESSION
    record_uid     TEXT NOT NULL UNIQUE,   -- URS-DI-001 unique identity
    mode           TEXT,
    line_id        TEXT,
    event_no       INTEGER,                -- 1-based row number at the source
    unit_id        TEXT,
    source_time    TEXT,                   -- timestamp as given by the source, unmodified
    received_utc   TEXT NOT NULL,          -- our clock, ISO 8601 UTC
    actor          TEXT NOT NULL,
    payload        TEXT NOT NULL,          -- canonical JSON
    payload_sha256 TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kind_line ON ledger(kind, line_id, event_no);
CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'GxP: ledger is append-only (UPDATE forbidden)'); END;
CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'GxP: ledger is append-only (DELETE forbidden)'); END;
"""

HASHED_FIELDS = ("seq", "kind", "record_uid", "mode", "line_id", "event_no", "unit_id",
                 "source_time", "received_utc", "actor", "payload_sha256", "prev_hash")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_hash(row):
    return sha256(canonical({k: row[k] for k in HASHED_FIELDS}))


class Ledger:
    def __init__(self, path, actor="gate3-twin", readonly=False):
        self.path = path
        self.actor = actor
        if readonly:
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row

    def exists(self, record_uid):
        return self.db.execute("SELECT 1 FROM ledger WHERE record_uid=?", (record_uid,)).fetchone() is not None

    def append(self, kind, record_uid, payload, mode=None, line_id=None, event_no=None,
               unit_id=None, source_time=None, payload_text=None):
        """Append one entry. Returns the entry dict, or None if record_uid already exists
        (duplicates are never overwritten; the first received version stays the original)."""
        text = payload_text if payload_text is not None else canonical(payload)
        with self.db:  # one transaction: read tip + insert
            if self.exists(record_uid):
                return None
            tip = self.db.execute("SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            row = dict(seq=(tip["seq"] + 1) if tip else 1, kind=kind, record_uid=record_uid, mode=mode,
                       line_id=line_id, event_no=event_no, unit_id=unit_id, source_time=source_time,
                       received_utc=utc_now(), actor=self.actor, payload=text,
                       payload_sha256=sha256(text), prev_hash=tip["entry_hash"] if tip else GENESIS)
            row["entry_hash"] = _entry_hash(row)
            cols = ",".join(row)
            self.db.execute(f"INSERT INTO ledger({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return row

    def rows(self, kind=None, line_id=None, mode=None):
        q, args = "SELECT * FROM ledger WHERE 1=1", []
        for col, val in (("kind", kind), ("line_id", line_id), ("mode", mode)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        return [dict(r) for r in self.db.execute(q + " ORDER BY seq", args)]

    def verify(self):
        """Recompute every payload hash and the whole chain. Returns (ok, n_entries, first_problem)."""
        prev = GENESIS
        n = 0
        for r in self.db.execute("SELECT * FROM ledger ORDER BY seq"):
            r = dict(r)
            n += 1
            if sha256(r["payload"]) != r["payload_sha256"]:
                return False, n, f"seq {r['seq']}: payload altered"
            if r["prev_hash"] != prev:
                return False, n, f"seq {r['seq']}: chain broken (entry missing or reordered)"
            if _entry_hash(r) != r["entry_hash"]:
                return False, n, f"seq {r['seq']}: entry metadata altered"
            prev = r["entry_hash"]
        return True, n, None

    def export(self, path):
        with open(path, "w") as f:
            for r in self.rows():
                f.write(canonical(r) + "\n")
