# Architecture and scalability

Status note, 2026-10-25. The numbers below were measured on the team laptop: PostgreSQL 16 in Docker Desktop (WSL2) and Python 3.10.

## How the system is built

```
 Simulator REST ──► api.py ──► store.py ──► PostgreSQL 16 (Docker, volume gate3_pgdata)
 (BAG, PVC, ASM)   ingest      Ledger        schema per ledger: ledger_historical / ledger_sim / ledger_realtime
                                 ▲           append-only table, SHA-256 hash chain, JSONB copy for queries
                                 │ twin_app (INSERT + SELECT)
                     monitor.py: per-line twin ── model.py (model.json, frozen, versioned by hash)
                     assess → lock prediction → evaluate
                                 │ twin_reader (SELECT, read-only transactions)
                                 ▼
                     dashboard.py ──► web/ (index.html, app.js, local Chart.js)
 Offline: validate.py (OQ), models.py (14-model comparison), lab.py (model lab), analysis.py
```

The design is **event sourcing**:
- Every fact goes into an append-only ledger as an event: a raw response, a record, an alert, a prediction or an evaluation.
- Everything else (line state, forecasts, dashboard) is rebuilt from those events.
- The writer (monitor) and the reader (dashboard) are separate processes with separate database roles.

### Decisions and why

| Decision | Why | Trade-off |
|---|---|---|
| One append-only ledger per mode, event sourcing | ALCOA+: Original, Complete, Enduring. The audit trail *is* the database. The dashboard recomputes from the originals, so it cannot drift from the data. | Every derived view has to be rebuilt or cached. |
| **PostgreSQL 16 in Docker** (replaced SQLite) | Many concurrent writers, and append-only enforced by **role privileges** on the server. Network access for several readers and services. Same engine at plant scale. One `docker compose up -d`, image available offline. | Docker Desktop must run on the demo laptop. |
| Three roles | `gate3_owner` creates schemas. `twin_app` (monitor) has INSERT + SELECT only. `twin_reader` (dashboard, analysis, lab) has SELECT only, in read-only transactions. Least privilege: even our own code cannot change a record. | Credentials to manage (demo defaults, overridable by env). |
| Triggers on UPDATE / DELETE / TRUNCATE | Second barrier: they stop the schema owner too. | An administrator can disable them, which is why there is the third barrier. |
| SHA-256 hash chain + verbatim raw responses | Third barrier, tamper evidence. It proves the data is exactly what the API sent. Tested: the owner disables the trigger and edits a value, and `verify()` reports it. | Someone with full access could rebuild the chain (see limit 5). |
| Advisory lock per ledger | Writers from several threads or processes cannot fork the chain. The primary key on `seq` is a second guard. | Writes to one ledger are serialised (about 230 entries/s). |
| One transaction per API response (`append_many`) | 20–30× faster ingest. A response is stored completely or not at all. | |
| REST polling with `limit=1` per line | The prediction is committed before the next unit is requested, as the rules require. | Speed is bound by the network. |
| White-box linear model + SPC, frozen `model.json` | 4 coefficients, checkable by hand (GAMP 5, change control). ML scored the same inside the twin (`models.py`). The model extrapolates, which trees cannot. | It cannot learn non-linear effects. None were found. |
| Read-only dashboard in a separate process (CQRS) | It cannot change records, and a crash does not stop data capture. | Two processes to start. |
| Scratch replays in memory (`MemoryLedger`) | The 95 validation scenarios and the model comparison are calculations, not manufacturing records, so they never touch the GxP database. | |

## Measured capacity

| | Measured | Needed for 3 lines (1 unit every 3 s per line) | Headroom |
|---|---|---|---|
| Ledger write, one entry per transaction | 144–196 entries/s (5–7 ms, round trip to the container) | about 2 entries/s (unit + raw response) | **~70–100×** |
| Ledger write, one transaction per response (100 records) | ~4,000 entries/s | (component downloads) | |
| 10 concurrent writers on one ledger | 230 entries/s total, **0 errors, chain intact** | | |
| Full chain verify | 39,000 entries/s (25 s at 1 M entries) | | |
| Model per unit | 430 units/s per line (2.3 ms) | 1 unit/s | **~400×** |
| Simulator REST round trip | about 0.6 s per request | 3 requests every 3 s | **1.7× (bottleneck)** |

- **Full SIM rehearsal** (14,342 entries): 89 s with per-entry transactions, **13 s** with per-response transactions.
- **Migration:** the SQLite ledgers (29,766 entries) were copied verbatim in 5 s, and the chains still verify.
- **Bottleneck:** the network, not the database or compute. `monitor.py` polls the lines one after another. A full round of 3 lines takes about 1.8 s, and a line makes one unit every 3 s, so the loop would fall behind from about **5 lines**.

### Found by testing

The first PostgreSQL version took the advisory lock and read the chain tip in the same SQL statement. In PostgreSQL a statement's snapshot is taken before it waits for the lock, so after the wait it read a stale tip. The concurrency test (5 threads) caught it:
- the `seq` primary key rejected the forked entries, so the chain stayed intact;
- 4 of 5 writers failed.

The fix takes the lock in its own statement. The re-test with 10 writers had 0 errors and an intact chain. This is a concrete example for the deck of testing that found a real defect.

## Remaining limits

1. **Sequential polling** (`monitor.run_rest`): one slow line delays all the others.
2. **Full chain verify on every dashboard change** (`LiveState.get`): it costs O(N).
3. **Hard-coded line list:** `ASM_LINES` in `api.py`, `LINES` in `dashboard.py`, and `web/app.js`.
4. **Dashboard sends every unit of every line** on each change. The payload grows linearly.
5. **No external anchor of the chain tip.**
6. **One chain per ledger:** all lines share it, so writes are serialised at about 230/s.
7. **One model for all lines:** no per-line calibration offset.

## What to improve

### Before the demo (each under 1 hour)

- **A. Lines from configuration:** adding a line becomes a config change, with no code change (limit 3).
- **B. One worker thread per line.** The ledger is now safe for concurrent writers, so this removes the network bottleneck (limit 1). Measure N lines × throughput.
- **C. Incremental verify:** check only the entries after the last verified hash (limit 2).
- **D. Show the chain tip hash** on screen and write it to the export at every lock (limit 5).

### Next step (roadmap slide)

- **E.** Windowed or delta payloads for the dashboard; `LISTEN/NOTIFY` to push new entries instead of polling (limit 4).
- **G.** One hash chain per line (partition by `line_id`): writers stop contending and throughput scales with lines (limit 6).
- **H.** Per-line calibration offset in a model registry (limit 7).

### Plant scale (same design, other components)

| Today | Plant |
|---|---|
| REST polling | Event streaming from the PLC (OPC UA, MQTT), Kafka topic per line, one consumer per line, horizontal scaling |
| PostgreSQL in Docker | Managed PostgreSQL / TimescaleDB with replication and point-in-time recovery. Raw payloads also in WORM storage (S3 Object Lock). |
| Roles + triggers + hash chain | Plus digital signatures (21 CFR Part 11) and a periodic external anchor of the chain tip |
| `model.json` | Model registry with validated, versioned models and GAMP 5 change control |
| Local dashboard | Containerised services (ingest, model, dashboard) with SSO / RBAC and an audit trail of user actions |

**One-line message for the jury:**
- The system is an event-sourced pipeline on PostgreSQL.
- Append-only is enforced by role privileges, triggers and a hash chain, and each layer is tested.
- On one laptop it has about 100× headroom in storage and 400× in compute for 3 lines, and stays correct with 10 concurrent writers.
- The only limit, network polling at about 5 lines, is removed by per-line workers.
- The same design maps onto Kafka + managed PostgreSQL for a whole plant.
