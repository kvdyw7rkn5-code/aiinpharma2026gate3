"""GxP data store: append-only, hash-chained ledger in PostgreSQL 16 (docker compose up -d).

Every ledger (HISTORICAL, SIM, REALTIME, test runs) is its own schema `ledger_<name>` with one table `ledger`.

Append-only is enforced three times:
  1. Privileges: the monitor connects as twin_app (INSERT + SELECT only, no UPDATE/DELETE/TRUNCATE/DDL);
     dashboard, analysis and lab connect as twin_reader in read-only transactions.
  2. Triggers: UPDATE, DELETE and TRUNCATE raise an error even for the schema owner.
  3. SHA-256 hash chain: any change made by bypassing 1 and 2 is detected by verify().
Concurrent writers (e.g. one per line) are serialised per ledger with a transaction-level advisory lock,
so the chain never forks; seq is the primary key as a second guard.

ALCOA+ mapping
  Attributable    every entry carries record_uid (mode/line/event no.), line_id, unit_id, actor
  Legible         plain JSON payloads (also queryable as JSONB), human-readable kinds
  Contemporaneous received_utc is stamped at the moment of receipt / commit
  Original        raw REST response bodies are stored verbatim with SHA-256
  Accurate        source values are never transformed in place; derived values live in separate entries
  Complete        nothing can be deleted (privileges + triggers), gaps in event numbers are detectable
  Consistent      one UTC clock, one canonical JSON encoding, one hash chain per ledger
  Enduring        PostgreSQL with a persistent volume, plus JSONL export
  Available       read-only role for the dashboard and the jury

    python3 store.py list                 # ledgers and their entry counts
    python3 store.py verify SIM           # recompute the hash chain
    python3 store.py reset SIM            # owner only: drop and recreate a ledger (refused for REALTIME)
"""
import hashlib
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

GENESIS = "0" * 64
PG = dict(host=os.environ.get("GATE3_PG_HOST", "127.0.0.1"), port=int(os.environ.get("GATE3_PG_PORT", 55432)),
          dbname=os.environ.get("GATE3_PG_DB", "gate3"))
# Local demo credentials (docker-compose.yml); override with environment variables
ROLES = {"owner": ("gate3_owner", os.environ.get("GATE3_OWNER_PASSWORD", "owner-demo")),
         "app": ("twin_app", os.environ.get("GATE3_APP_PASSWORD", "app-demo")),
         "reader": ("twin_reader", os.environ.get("GATE3_READER_PASSWORD", "reader-demo"))}
PROTECTED = {"REALTIME"}   # never dropped by reset()

COLS = ("seq", "kind", "record_uid", "mode", "line_id", "event_no", "unit_id", "source_time", "received_utc",
        "actor", "payload", "payload_sha256", "prev_hash", "entry_hash")
HASHED_FIELDS = ("seq", "kind", "record_uid", "mode", "line_id", "event_no", "unit_id",
                 "source_time", "received_utc", "actor", "payload_sha256", "prev_hash")

DDL = """
CREATE SCHEMA {s} AUTHORIZATION gate3_owner;
CREATE TABLE {s}.ledger (
    seq            BIGINT PRIMARY KEY,
    kind           TEXT NOT NULL,          -- RAW_RESPONSE | BAG | PVC | ASM | MODEL | PREDICTION | EVALUATION | ALERT | SESSION
    record_uid     TEXT NOT NULL UNIQUE,   -- URS-DI-001 unique identity
    mode           TEXT,
    line_id        TEXT,
    event_no       INTEGER,                -- 1-based row number at the source
    unit_id        TEXT,
    source_time    TEXT,                   -- timestamp as given by the source, unmodified
    received_utc   TEXT NOT NULL,          -- our clock, ISO 8601 UTC (kept as text: it is part of the hash)
    actor          TEXT NOT NULL,
    payload        TEXT NOT NULL,          -- canonical JSON, exactly as hashed
    payload_sha256 TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL,
    payload_json   JSONB GENERATED ALWAYS AS (payload::jsonb) STORED   -- for SQL queries only, not hashed
);
CREATE INDEX ON {s}.ledger (kind, line_id, event_no);
CREATE INDEX ON {s}.ledger (kind, mode);
CREATE FUNCTION {s}.forbid() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RAISE EXCEPTION 'GxP: ledger is append-only (% forbidden)', TG_OP; END$$;
CREATE TRIGGER no_update_delete BEFORE UPDATE OR DELETE ON {s}.ledger FOR EACH ROW EXECUTE FUNCTION {s}.forbid();
CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {s}.ledger FOR EACH STATEMENT EXECUTE FUNCTION {s}.forbid();
GRANT USAGE ON SCHEMA {s} TO twin_app, twin_reader;
GRANT SELECT, INSERT ON {s}.ledger TO twin_app;
GRANT SELECT ON {s}.ledger TO twin_reader;
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_hash(row):
    return sha256(canonical({k: row[k] for k in HASHED_FIELDS}))


def _new_row(tip, kind, record_uid, text, mode, line_id, event_no, unit_id, source_time, actor):
    row = dict(seq=(tip["seq"] + 1) if tip else 1, kind=kind, record_uid=record_uid, mode=mode,
               line_id=line_id, event_no=event_no, unit_id=unit_id, source_time=source_time,
               received_utc=utc_now(), actor=actor, payload=text,
               payload_sha256=sha256(text), prev_hash=tip["entry_hash"] if tip else GENESIS)
    row["entry_hash"] = _entry_hash(row)
    return row


def _check_chain(rows):
    """Recompute every payload hash and the whole chain. Returns (ok, n_entries, first_problem)."""
    prev, n = GENESIS, 0
    for r in rows:
        n += 1
        if sha256(r["payload"]) != r["payload_sha256"]:
            return False, n, f"seq {r['seq']}: payload altered"
        if r["prev_hash"] != prev:
            return False, n, f"seq {r['seq']}: chain broken (entry missing or reordered)"
        if _entry_hash(r) != r["entry_hash"]:
            return False, n, f"seq {r['seq']}: entry metadata altered"
        prev = r["entry_hash"]
    return True, n, None


def ledger_name(name):
    """'ledger_SIM.db', 'SIM' and 'sim' all mean the ledger SIM."""
    return re.sub(r"^ledger_|\.db$", "", os.path.basename(str(name))).upper()


def schema(name):
    return "ledger_" + re.sub(r"[^a-z0-9_]", "_", ledger_name(name).lower())


def connect(role, readonly=False):
    user, pw = ROLES[role]
    opts = "-c default_transaction_read_only=on" if readonly else None
    return psycopg.connect(**PG, user=user, password=pw, autocommit=True, row_factory=dict_row, options=opts)


def ledger_exists(name):
    with connect("reader", readonly=True) as c:
        return c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
                         (schema(name),)).fetchone() is not None


def create_ledger(name):
    with connect("owner") as c:
        if c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", (schema(name),)).fetchone():
            return False
        with c.transaction():
            c.execute(DDL.format(s=schema(name)))
        return True


def reset_ledger(name, force=False):
    """Owner-only: drop a ledger (SIM rehearsals, test runs). The jury ledger REALTIME is protected."""
    if ledger_name(name) in PROTECTED and not force:
        raise PermissionError(f"ledger {ledger_name(name)} is protected and cannot be reset")
    with connect("owner") as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema(name)} CASCADE")
    create_ledger(name)


def claim_writer(name):
    """Only one monitor process may write a ledger at a time. Returns the connection that holds a session-level
    advisory lock (keep a reference: the lock is released when it closes or the process exits), or None if
    another process already holds it. Works on every OS and also guards `reset_ledger` from dropping a ledger
    that another monitor is writing."""
    conn = connect("app")
    if conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", ("writer:" + schema(name),)).fetchone()["ok"]:
        return conn
    conn.close()
    return None


def list_ledgers():
    with connect("reader", readonly=True) as c:
        names = [r["schema_name"] for r in c.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'ledger\\_%' ORDER BY 1")]
        return {n: c.execute(f"SELECT count(*) AS n FROM {n}.ledger").fetchone()["n"] for n in names}


class Ledger:
    """One named ledger. Writers use twin_app (auto-creating the schema via the owner if missing);
    readonly=True uses twin_reader in read-only transactions."""

    def __init__(self, name, actor="gate3-twin", readonly=False):
        self.name, self.actor, self.readonly = ledger_name(name), actor, readonly
        self.schema = schema(name)
        if not readonly:
            create_ledger(name)
        self.db = connect("reader" if readonly else "app", readonly)
        self._lock = threading.RLock()     # one connection shared by the dashboard's threads
        self._t = f"{self.schema}.ledger"

    def _q(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def exists(self, record_uid):
        return bool(self._q(f"SELECT 1 FROM {self._t} WHERE record_uid=%s", (record_uid,)))

    def append(self, kind, record_uid, payload, mode=None, line_id=None, event_no=None,
               unit_id=None, source_time=None, payload_text=None):
        """Append one entry. Returns the entry dict, or None if record_uid already exists
        (duplicates are never overwritten; the first received version stays the original)."""
        return self.append_many([dict(kind=kind, record_uid=record_uid, payload=payload, mode=mode, line_id=line_id,
                                      event_no=event_no, unit_id=unit_id, source_time=source_time,
                                      payload_text=payload_text)])[0]

    def append_many(self, entries):
        """Append several entries in one transaction (e.g. all records of one API response).
        Returns one item per entry: the stored row, or None for a record_uid that already existed."""
        out, new = [], []
        with self._lock, self.db.transaction():
            # Serialise writers of this ledger (other threads or processes) until commit. The lock must be its
            # own statement: a statement's snapshot is taken before it waits, so reading the tip in the same
            # statement would see a stale tip after another writer committed.
            self.db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema,))
            uids = [e["record_uid"] for e in entries]
            r = self.db.execute(
                f"SELECT (SELECT json_build_object('seq', seq, 'entry_hash', entry_hash) FROM {self._t}"
                f"  ORDER BY seq DESC LIMIT 1) AS tip,"
                f" (SELECT coalesce(array_agg(record_uid), '{{}}') FROM {self._t} WHERE record_uid = ANY(%s)) AS seen",
                (uids,)).fetchone()
            tip, seen = r["tip"], set(r["seen"])
            for e in entries:
                if e["record_uid"] in seen:
                    out.append(None)
                    continue
                text = e.get("payload_text") if e.get("payload_text") is not None else canonical(e.get("payload"))
                tip = _new_row(tip, e["kind"], e["record_uid"], text, e.get("mode"), e.get("line_id"),
                               e.get("event_no"), e.get("unit_id"), e.get("source_time"), self.actor)
                seen.add(e["record_uid"])
                new.append([tip[c] for c in COLS])
                out.append(tip)
            if new:
                with self.db.cursor() as cur:
                    cur.executemany(f"INSERT INTO {self._t} ({','.join(COLS)})"
                                    f" VALUES ({','.join(['%s'] * len(COLS))})", new)
        return out

    def rows(self, kind=None, line_id=None, mode=None):
        q, args = f"SELECT {','.join(COLS)} FROM {self._t} WHERE true", []
        for col, val in (("kind", kind), ("line_id", line_id), ("mode", mode)):
            if val is not None:
                q += f" AND {col}=%s"
                args.append(val)
        return self._q(q + " ORDER BY seq", args)

    def rows_after(self, seq):
        return self._q(f"SELECT {','.join(COLS)} FROM {self._t} WHERE seq > %s ORDER BY seq", (seq,))

    def tip(self):
        return self._q(f"SELECT coalesce(max(seq), 0) AS tip FROM {self._t}")[0]["tip"]

    def max_event_no(self, kind, mode):
        return self._q(f"SELECT coalesce(max(event_no), 0) AS n FROM {self._t} WHERE kind=%s AND mode=%s",
                       (kind, mode))[0]["n"]

    def verify(self):
        """Recompute every payload hash and the whole chain (streamed). Returns (ok, n_entries, first_problem)."""
        with self._lock, self.db.transaction():
            cur = self.db.cursor(name="verify")
            cur.itersize = 5000
            cur.execute(f"SELECT {','.join(COLS)} FROM {self._t} ORDER BY seq")
            return _check_chain(cur)

    def export(self, path):
        with open(path, "w") as f:
            for r in self.rows():
                f.write(canonical(r) + "\n")


class MemoryLedger:
    """Same interface, kept in memory, for scratch replays (validation scenarios, model comparison).
    Nothing here is a manufacturing record, so it never touches the database."""

    def __init__(self, actor="scratch"):
        self.actor, self._rows, self._uids = actor, [], set()

    def exists(self, record_uid):
        return record_uid in self._uids

    def append(self, kind, record_uid, payload, mode=None, line_id=None, event_no=None,
               unit_id=None, source_time=None, payload_text=None):
        if record_uid in self._uids:
            return None
        text = payload_text if payload_text is not None else canonical(payload)
        row = _new_row(self._rows[-1] if self._rows else None, kind, record_uid, text, mode, line_id, event_no,
                       unit_id, source_time, self.actor)
        self._rows.append(row)
        self._uids.add(record_uid)
        return row

    def append_many(self, entries):
        return [self.append(**e) for e in entries]

    def rows(self, kind=None, line_id=None, mode=None):
        return [dict(r) for r in self._rows if (kind is None or r["kind"] == kind)
                and (line_id is None or r["line_id"] == line_id) and (mode is None or r["mode"] == mode)]

    def verify(self):
        return _check_chain(self._rows)


if __name__ == "__main__":
    cmd, arg = (sys.argv[1:] + ["list", None])[:2]
    if cmd == "list":
        for n, k in list_ledgers().items():
            print(f"{n:24s} {k:8d} entries")
    elif cmd == "verify":
        ok, n, problem = Ledger(arg, readonly=True).verify()
        print(f"{ledger_name(arg)}: {'OK' if ok else 'FAILED - ' + problem} ({n} entries)")
    elif cmd == "reset":
        reset_ledger(arg)
        print(f"{ledger_name(arg)}: reset")
    else:
        sys.exit(__doc__)
