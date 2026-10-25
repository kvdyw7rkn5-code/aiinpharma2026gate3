"""One command for a live run: check the connection, start the dashboard, open the browser, stream the data.

    python3 live.py --team 03 --code <passcode>                    # real REALTIME run (jury)
    python3 live.py --mode HISTORICAL --team 03 --code team03      # live-style replay of the historical data
    python3 live.py --mock                                         # rehearsal against mock_server.py

The dashboard keeps running after the stream ends, so you can keep presenting. Ctrl+C stops everything.
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def wait_for(url, seconds=20):
    for _ in range(seconds * 5):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="REALTIME", choices=["REALTIME", "HISTORICAL"])
    ap.add_argument("--team", default="01")
    ap.add_argument("--code", help="passcode (REALTIME) or team0X (HISTORICAL)")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between requests per line (0 = full speed)")
    ap.add_argument("--db", help="ledger file (default: ledger_<MODE>.db, or a fresh test file for HISTORICAL / mock)")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--mock", action="store_true", help="rehearse against a local mock_server.py")
    ap.add_argument("--skip-check", action="store_true")
    a = ap.parse_args()

    procs, base = [], []
    os.chdir(HERE)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))   # clean stop on kill too
    if a.mock:
        a.mode, a.code = "REALTIME", "LIVE"
        a.db = a.db or "rehearsal.db"
        for f in (a.db, a.db + ".lock"):          # a rehearsal always starts from scratch
            if os.path.exists(f):
                os.remove(f)
        procs.append(subprocess.Popen([PY, "mock_server.py", "--port", "8090"], stdout=subprocess.DEVNULL))
        base = ["--base-url", "http://localhost:8090/simulator"]
        wait_for("http://localhost:8090/mock/status")
    if not a.code:
        ap.error("--code is required (the REALTIME passcode, or team0X for HISTORICAL)")
    if a.mode == "HISTORICAL" and not a.db:
        a.db = f"live_historical_team{a.team}.db"     # never touch the main historical ledger
        for f in (a.db, a.db + ".lock"):
            if os.path.exists(f):
                os.remove(f)
    db = a.db or f"ledger_{a.mode}.db"
    mon = [PY, "monitor.py", "--mode", a.mode, "--team", a.team, "--code", a.code, "--db", db, "--limit", "5"] + base

    try:
        if shutil.which("caffeinate"):               # keep the Mac awake while this runs
            procs.append(subprocess.Popen(["caffeinate", "-dis", "-w", str(os.getpid())]))
        if not a.skip_check:
            print("1/3  checking the connection…")
            out = subprocess.run(mon + ["--check"], capture_output=True, text=True).stdout
            print("     " + out.strip().replace("\n", "\n     "))
            if "ready" not in out:
                raise SystemExit("Connection check failed – fix the team / passcode / Wi-Fi and try again.")
        print(f"2/3  starting the dashboard on http://localhost:{a.port}")
        procs.append(subprocess.Popen([PY, "dashboard.py", "--mode", a.mode, "--db", db, "--port", str(a.port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not wait_for(f"http://localhost:{a.port}/"):
            raise SystemExit(f"Dashboard did not start (is port {a.port} already in use? try --port 8060)")
        webbrowser.open(f"http://localhost:{a.port}/#live")
        print(f"3/3  streaming {a.mode} data into {db}  (Ctrl+C stops everything)\n")
        subprocess.run(mon + (["--delay", str(a.delay)] if a.delay else []))
        print(f"\nStream finished. The dashboard stays open at http://localhost:{a.port} – press Ctrl+C when you are done.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
