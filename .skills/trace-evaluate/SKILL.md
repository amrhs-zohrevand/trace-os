---
name: trace-evaluate
description: Take what review or use finds back into the work, a ledger row for every supported finding before it is fixed, and record a targeted recheck that the correction worked, preserving the distinction between observed closure, verified correction and human acceptance.
compatibility: Requires this starter's defect_ledger.py, verification-evidence.example.json and the diagnosed input object.
---

# Take findings into the work and recheck that the correction worked

Read AGENTS.md and the existing defect entry.
Every supported finding gets a ledger row before anyone fixes it, so the correction has something to be rechecked against.
An agentic review's findings enter from its tracked directory: `scripts/defect_ledger.py ingest --run ai/reviews/RUN --finding ID` once for each finding you reproduced, with `--matches FILE` saying which are already rows (an empty array when none is); a finding you did not reproduce gets no row, and the review closes nothing by absence.
Where the reviewer's text was withheld from that directory, enter each supported finding with `scripts/defect_ledger.py open` after reading what its words disclose.
A REVIEW marker that diagnoses a defect (an error in the work, not a request for a change) is captured in the trace record first, then entered with `scripts/defect_ledger.py open`, `--by` naming its writer, `--source` its file and `--quote` the text it marks; the commit that resolves the marker claims the fix as the next sentence says.
A commit that fixes a row says so in its message, `Fixes ID` (or Fix, Fixed, Closes, Corrects, Resolves before the id), which is how the governing-actions report joins the correction to its recheck; an id the message only mentions is not joined, and neither is a row a later sweep closed.
Any other finding that did not come from a review run enters the ledger with `scripts/defect_ledger.py open --label TEXT --severity high|medium|low`, with `--quote` and `--source` when a quoted span can be checked against its source, `--by` for the person or agent who raised it, `--note` for context, `--affected` for the trace records it touched, `--object` once for each output the defect reaches (a table, a file, a section) and `--faulty-default` for the instruction file or default that produced it.
The same `--affected`, `--object` and `--faulty-default` fields can be set later with `scripts/defect_ledger.py mark`; a person's return takes `--object` too, for the outputs the person names.
When the same code or text produced several outputs, list every one the error reaches, not only the one where it was noticed.
Identify the original diagnosis and the exact corrected object before assessing the change.
When the row lists objects, recheck each in its own event, naming it with `draft-recheck ID --object PATH`, and, for a table or a section that is not a file in the project, its hash with `--object-hash` (the commit of the correction, or the sha256 of the file that builds it); the row verifies only when every listed object has a complete passing recheck, a passing recheck of some is recorded and leaves the state alone, and `status`, `check` and the queue's E line say how many are rechecked.
A failed recheck on any one object moves the row as any failed recheck does, and each object is rechecked again after the next correction.
Run a recheck capable of detecting the diagnosed failure.
Fill the evidence template with the object hash, corrective action, criterion, method, outcome, evidence reference, checker, implementer, timestamp and limits.
Write the evidence with `scripts/defect_ledger.py draft-recheck ID --checker NAME --person` (or `--agent` for a model's advisory recheck): it fills the diagnosis, the corrected object and its hash, the fixer from `mark --fixed --by` and whether the checker is the fixer, shows the row's label as the proposed criterion, and takes what the checker compared, found and did not look at as `--method`, `--outcome`, `--limitations` and `--said` verbatim.
Print the draft back to the checker field by field; record only what they confirm, with `scripts/defect_ledger.py verify ID --evidence FILE`.
A field the checker did not state reads `not stated`; an event with no stated outcome is recorded and leaves the state alone; never ask the checker for more than they said, and never fill a judgment field for them.
Keep failed and inadequate checks, including contrary evidence; do not turn an absent complaint into verification.
Closing a row by hand (`mark --state closed`) is recorded as a repair claim, not as the sweep that `closed` names, and the row stays in what E is waiting on until a recheck reaches it.
A finding answered without a fix stays open: record who answered it, why, and the raiser's response with `scripts/defect_ledger.py mark --id ID --note TEXT`, which keeps the row's state and replaces its note, so carry any earlier note over; an answer is not a correction and never verifies a row.
A failed recheck moves a verified row to `recurred` and an open, fixed or closed row to `unresolved`; both states fail `scripts/defect_ledger.py check` until a later recheck passes and verifies, and no later sweep closes them by absence.
When the row names a `faulty_default`, correct the instruction as well as the output, record the trace records it touched in `affected`, and examine each of them.
A corrected default verifies only with a later-run recheck: the evidence's `recheck.later_run` names a later trace record produced under the revised instruction and the commit or digest of that instruction, and the recheck tests that later record against the original diagnosis.
The later run has to resolve in the project: the record is `ai/records/<id>` with its evidence.json, and the version names a commit (or the sha256 of the instruction's content at one) that changes the faulty default.
A passing recheck without that later run, or with one that does not resolve, is recorded with the reason and leaves the state alone.
A checker who implemented the fix is a self-checker.
`verified` means a recorded passing targeted recheck under declared evidence and adequacy. It does not authenticate the evidence reference or independently establish the adequacy judgment.
Verification is a technical record and does not authorize publication or record human acceptance.
Add the actual test and evidence artifact to the trace record, following `ai/SKILL-RUN-CONTRACT.md` and linking the defect id and recheck evidence.
Record the recheck with `procedure` set to `trace-evaluate`, `procedure_version` set to `sha256:` and the digest that `trace_record.py procedure-version trace-evaluate` reads off the skill file in force, and all fields required by the common contract.
This skill was `trace-recheck` until 0.1.0-rc15, when it was renamed to `trace-evaluate` so the id carries its TRACE letter, E for evaluated corrective action, the principle now called evaluate corrections; records written under the old id stay as they were.
