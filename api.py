"""REST client for the Gate 3 manufacturing simulator (real server or mock_server.py).

Every HTTP response body is written to the ledger verbatim (RAW_RESPONSE) before it is parsed,
then each record is stored individually with a unique record_uid = MODE/LINE/event_no.

Live-run robustness
  - connection errors, timeouts and 5xx are retried with backoff (the hosted simulator can take
    30-60 s to wake up); 503 "dataset not loaded" is waited out
  - 400/401/403/404 stop immediately with a readable message (wrong team, passcode or line)
  - the HTTP call runs outside the ledger lock, so several lines can be fetched in parallel
"""
import json
import threading
import time

import requests

from store import canonical

DEFAULT_BASE = "https://manufacturingsimulator.onrender.com/simulator"
ASM_LINES = ("ASM-01", "ASM-02", "ASM-03")
LIMITS = {"HISTORICAL": 100, "REALTIME": 5}


class FatalAPIError(RuntimeError):
    """Not worth retrying: wrong team / passcode / line / parameters."""


class SimulatorClient:
    def __init__(self, ledger, mode, team, code, base=DEFAULT_BASE, timeout=120, retries=8, wait_unloaded=600,
                 log=print, lock=None):
        self.ledger, self.mode, self.team, self.code = ledger, mode, team, code
        self.base, self.timeout, self.retries, self.wait_unloaded, self.log = base.rstrip("/"), timeout, retries, wait_unloaded, log
        self.lock = lock or threading.RLock()
        self.local = threading.local()          # one HTTP session per thread
        self._req_no = len(ledger.rows(kind="RAW_RESPONSE", mode=mode))

    def _session(self):
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
        return self.local.s

    def _alert(self, line, code, text):
        with self.lock:
            self.ledger.append("ALERT", f"ALERT/{self.mode}/{line}/API/{time.time_ns()}/{code}", {"code": code, "text": text},
                               mode=self.mode, line_id=line)
        self.log(f"  ! {line} {code}: {text}")

    def _get(self, line, start, limit):
        url = f"{self.base}/lines/{line}/modes/{self.mode}/teams/{self.team}/codes/{self.code}"
        params = {"startWith": start}
        if limit:
            params["limit"] = limit
        attempt, waited = 0, 0.0
        while True:
            try:
                r = self._session().get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                attempt += 1
                if attempt > self.retries:
                    raise RuntimeError(f"{line}: no answer from the simulator after {self.retries} retries ({e})")
                pause = min(2 ** (attempt - 1), 30)
                self._alert(line, "API_RETRY", f"{type(e).__name__} on startWith={start}; retry {attempt} in {pause} s")
                time.sleep(pause)
                continue
            with self.lock:
                self._req_no += 1
                # Original response, preserved before any parsing (URS-DI-002). The access code is not stored.
                self.ledger.append("RAW_RESPONSE", f"RAW/{self.mode}/{self._req_no:06d}", None, mode=self.mode,
                                   line_id=line, event_no=start,
                                   payload_text=canonical({"status": r.status_code, "path": f"{line}?startWith={start}&limit={limit}",
                                                           "body": r.text}))
            if r.status_code == 200:
                return r.json()
            if r.status_code == 503:            # dataset not loaded yet by the organisers
                if waited >= self.wait_unloaded:
                    raise RuntimeError(f"{line}: dataset still not loaded (503) after {self.wait_unloaded:.0f} s")
                if waited == 0:
                    self._alert(line, "API_NOT_LOADED", "503: dataset not loaded yet, waiting")
                time.sleep(5)
                waited += 5
                continue
            if r.status_code in (400, 401, 403, 404):
                hint = {404: "wrong team ID, passcode or line", 400: "bad parameters (limit/startWith/mode)"}.get(r.status_code, "access denied")
                raise FatalAPIError(f"{line}: HTTP {r.status_code} – {hint}. Server said: {r.text[:200]}")
            attempt += 1                        # other 5xx / 429: retry with backoff
            if attempt > self.retries:
                raise RuntimeError(f"{line}: HTTP {r.status_code} after {self.retries} retries")
            pause = min(2 ** (attempt - 1), 30)
            self._alert(line, "API_RETRY", f"HTTP {r.status_code} on startWith={start}; retry {attempt} in {pause} s")
            time.sleep(pause)

    def _store(self, kind, line, start, records):
        stored = []
        with self.lock:
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
        limit = max(1, min(int(limit), LIMITS.get(self.mode, 100)))
        recs = self._get(line, start, limit)
        return self._store("ASM", line, start, recs)

    def check(self):
        """Pre-flight: read-only BAG/PVC requests (they never consume assembly data)."""
        out = {}
        for line in ("BAG-01", "PVC-01"):
            t = time.perf_counter()
            recs = self._get(line, 1, 1)
            out[line] = {"ok": bool(recs), "seconds": round(time.perf_counter() - t, 2), "first": recs[0] if recs else None}
        return out


def load_json_records(path):
    with open(path) as f:
        return json.load(f)
