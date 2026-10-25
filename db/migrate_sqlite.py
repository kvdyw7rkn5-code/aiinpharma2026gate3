"""One-off migration of the old SQLite ledgers (ledger_<NAME>.db) into PostgreSQL.

Entries are copied verbatim (same seq, timestamps, payload text and hashes) through the twin_app role,
then the chain is verified in PostgreSQL. A ledger that already has entries is skipped.

    python3 db/migrate_sqlite.py ledger_HISTORICAL.db ledger_SIM.db
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from store import COLS, Ledger, ledger_name  # noqa: E402

for path in sys.argv[1:]:
    name = ledger_name(path)
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    led = Ledger(name, actor="migration")
    if led.tip():
        print(f"{name}: already has {led.tip()} entries, skipped")
        continue
    rows = [[r[c] for c in COLS] for r in src.execute(f"SELECT {','.join(COLS)} FROM ledger ORDER BY seq")]
    with led.db.transaction():
        with led.db.cursor() as cur:
            cur.executemany(f"INSERT INTO {led._t} ({','.join(COLS)}) VALUES ({','.join(['%s'] * len(COLS))})", rows)
    ok, n, problem = led.verify()
    print(f"{name}: {len(rows)} entries copied from {path}; chain {'OK' if ok else 'FAILED - ' + problem} ({n} entries)")
