# Gate 3: scoring and event rules

Transcribed from the printed event rules (photos, 2026-10-25). Where a line was cut off in the photo, it is marked *(cut off)*.

## Gate III scoring (max 70 points)

"Gate 3 – Solution: the Team delivers its solution (the Project)."

| Area | Max points | What the jury looks at |
|---|---|---|
| Solution Quality & Control | 15 | Software reliability, data integrity, testing, requirements traceability and validation evidence. The system should assess **all** production lines. |
| Solution Architecture | 10 | Architecture design, component responsibilities, technology selection, scalability, and justification of technical decisions. How modules and parts have been split by team members. |
| Real-Time Monitoring & Visualization | 20 | Independent visualization of ASM-01, ASM-02 and ASM-03. Clear summary identification of BAGS and PVC tube reels used in production. Identification of each event and of missed points. |
| Prediction Accuracy | 15 | See the formulas below. |
| Team collaboration | 10 | How tasks have been split, and whether everybody has a clear role and assigned tasks. |
| **Total** | **70** | |

### Prediction accuracy

- **X** = the unit ID the team's system predicted as the first failing unit.
- **Y** = the last unit already inspected when the prediction was made.
- **Z** = the actual first failing unit ID found by vision inspection.

**Prediction error:** E = |Z − X|

**Prediction lead time (in units):** L = X − Y

How E and L are turned into the 15 points is not stated. Small E is clearly better. Larger L is presumably better, because it means an earlier warning.

## Event rules (excerpt)

1. *(cut off: point 1 is the scoring table above)*
2. Work on the Project may begin only after the kick-off and presentation of the case at **10:00**.
3. Teams may use open-source libraries, public APIs, frameworks and publicly available datasets, provided their licences allow it. **Pre-existing code, designs or models not created during the Event must be declared in the submission.**
4. Projects may not be copied from other sources, and may not use code or materials that infringe third-party rights.
5. Participants bring their own laptops and equipment. The Organiser provides Wi-Fi, power sockets, tables and refreshments; it does not provide computers.
6. The use of AI-assisted tools is permitted, **provided it is declared in the submission**.

## §8 Project submission and presentation

1. Each Team submits one Project by **16:00** through *(cut off: channel not shown)*. Late submissions are not evaluated.
2. Between 16:00 and 18:00, each Team presents its Project to the Jury for **5 minutes**, followed by at most **5 minutes of questions**. Each Team brings a laptop on which the Solution can be launched and shown at the Jury's desk.
3. Teams must demonstrate the Project **as submitted**; changes after the deadline are not allowed.

## §9 Jury, evaluation criteria and awards

1. The Jury is appointed by the Organiser and has at least 3 members. Jury members may not evaluate a Team in which *(cut off)*.

## What this means for our build

| Rule | Consequence |
|---|---|
| E = \|Z − X\| and L = X − Y | The lock rule trades E against L. `validate.py` scores both commit rules with E and L. The default `holt_next` rule (P(next fails) ≥ 0.1, L = 3) doubled E ≤ 2 compared with `quantile` (26 % vs 13 %). |
| Prediction on each line, and the system assesses all lines | The monitor, lock and evaluation must run on ASM-01, ASM-02 and ASM-03, each with its own X, Y and Z. |
| Independent visualization per line, BAG and PVC summary, "each event, missed points" | The dashboard needs one panel per line, a table of the bag lots and reels used, a log of every event (alarm, lock, NOK), and the NOKs that were missed (not predicted). |
| Testing, requirements traceability, validation evidence | `validate.py` → `validation_report.json` (prediction scenarios, false alarms, integrity tests) is the evidence. Add a URS → implementation → test matrix to the deck. |
| Architecture: who built which module | The deck names an owner for every module (ingest/store, model, predictions, dashboard, deck). |
| Pre-existing code must be declared | Code written **before** the 10:00 kick-off must be declared in the submission. Rule 2 says work may begin only after the kick-off. **Ask the organisers whether preparation before the event is allowed.** |
| AI tools must be declared | Declare Claude Code (used for analysis and code) in the submission. |
| Submit by 16:00, demo as submitted | Freeze the code before 16:00, tag the submitted commit, and rehearse a 5-minute demo on our own laptop. |
