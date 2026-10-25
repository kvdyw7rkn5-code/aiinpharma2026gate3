"""REST client for the Gate 3 manufacturing simulator.

Every HTTP response body is written to the ledger verbatim (RAW_RESPONSE) before it is parsed,
then each record is stored individually with a unique record_uid = MODE/LINE/event_no.
"""
import json

import requests

from store import canonical

BASE = "https://manufacturingsimulator.onrender.com/simulator/lines"
ASM_LINES = ("ASM-01", "ASM-02", "ASM-03")


class SimulatorClient:
    def __init__(self, ledger, mode, team, code, timeout=120):
        self.ledger, self.mode, self.team, self.code, self.timeout = ledger, mode, team, code, timeout
        self.http = requests.Session()
        self._req_no = len(ledger.rows(kind="RAW_RESPONSE", mode=mode))

    def _get(self, line, start, limit):
        url = f"{BASE}/{line}/modes/{self.mode}/teams/{self.team}/codes/{self.code}"
        params = {"startWith": start}
        if limit:
            params["limit"] = limit
        r = self.http.get(url, params=params, timeout=self.timeout)
        self._req_no += 1
        # Original response, preserved before any parsing (URS-DI-002). Access code is not stored.
        self.ledger.append("RAW_RESPONSE", f"RAW/{self.mode}/{self._req_no:06d}",
                           None, mode=self.mode, line_id=line, event_no=start,
                           payload_text=canonical({"status": r.status_code, "path": f"{line}?startWith={start}&limit={limit}",
                                                   "body": r.text}))
        r.raise_for_status()
        return r.json()

    def _store(self, kind, line, start, records):
        stored = []
        for i, rec in enumerate(records):
            n = start + i
            e = self.ledger.append(kind, f"{self.mode}/{line}/{n:06d}", rec, mode=self.mode, line_id=line,
                                   event_no=n, unit_id=rec.get("unit_id"), source_time=rec.get("time"))
            stored.append((n, rec, e is not None))
        return stored

    def fetch_component(self, line, start=1):
        """Fetch all remaining BAG-01 / PVC-01 records from row `start` (stateless, paged by 100)."""
        kind = "BAG" if line == "BAG-01" else "PVC"
        out = []
        while True:
            recs = self._get(line, start, 100)
            if not recs:
                return out
            out += self._store(kind, line, start, recs)
            start += len(recs)

    def fetch_assembly(self, line, start, limit=1):
        recs = self._get(line, start, limit)
        return self._store("ASM", line, start, recs)


def load_json_records(path):
    with open(path) as f:
        return json.load(f)
