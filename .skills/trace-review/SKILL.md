---
name: trace-review
description: Review a defined candidate against named criteria and source versions, through the kit's agentic reviewer on whichever headless CLI the project has, retaining grounded findings and limits without treating model review as acceptance.
---

# Review against sources

Read AGENTS.md and `ai/SKILL-RUN-CONTRACT.md`.
Name the exact candidate, input versions and completion criterion before reviewing.

The review is agentic: run `python3 scripts/agentic_review.py <sha> --backend <codex|claude> --checks --criteria "<the criterion>" --checks-jsonl <the record's checks.jsonl>`.
The reviewer is whichever headless CLI the project has, `--backend codex` or `claude`, and each of the two opens the files and runs the checks for itself inside an export of the candidate outside the checkout.
`--timeout` is in seconds, 1800 by default, and expiry kills the reviewer together with the tools it started.
The host labels each quote against the reviewed source and each check against the transcript, so a finding the reviewer only asserted stays visible as one.
The report names the reviewer as its own transcript states it, or says the identity was only requested where the CLI states none.
The run appends the check row described below to the record named; read `report.md` and reproduce a finding before acting on it.
The run writes what a reader needs to judge the check to the tracked `ai/reviews/<run>/`: the report, which prints what the reviewer says it examined and the criteria requested beside the ones it applied, the reply, the prompt and the rows, which point at that report.
Commit that directory with the record, so the report resolves on any clone; the reviewer's transcript stays in the ignored `.agent-reviews/agentic-review/<run>/`, and the report names it with its sha256.
When the packet carried people's own words or ignored files, the reviewer's text stays in `.agent-reviews/` as well, and the tracked report says so.
Only the claude CLI declares the calls it was denied, which the report lists in a section of their own, and codex declares none, which is not the same as none having happened.
Only codex runs under an operating-system sandbox; a claude run says so in its `limits`, because there a write outside the packet is not prevented.
One review run is the default, by a model other than the one that executed the work where another is available (`--model` picks it on either backend); a second run, from another model family, is optional, and each run records its own row.
Where the only reviewer available is the producer's own model, the run is still recorded, as self-review: `accept` compares the reviewer with the covered commits' `Execution-agent:` trailers and flags the row.
A draft, a table or a passage goes through `--path` instead of a commit, without `--checks`, which the tool refuses there because the packet holds no repository to check; the run writes the check row and no acceptance row, because a path covers no commit.
A `--path` file may lie in the project's shared folder, even where that folder is a link to a sync app's folder elsewhere on the machine.
Give the reviewer what the candidate is checked against with `--source PATH`, once for each file or folder: data, a cited paper or a table, in the repository, in the shared folder, or a snapshot under `workspace/snapshots/`, which is verified against its manifest first.
Each source goes into the packet under `.review/sources/`, its sha256 goes into the prompt, the report and the check row's `sources`, and a finding can quote the source as well as the candidate; the host labels both quotes.
A source kept out of Git, as everything in the shared folder is, keeps the reviewer's own text out of the tracked report too, which then says why.
The reviewer applies the project's own prose rules when `ai/review-prose-rules.md` exists, and none otherwise; the report and the row name the file and its sha256.
Where neither CLI is installed, or the candidate is not something a reviewer reads as text, examine it yourself against the cited sources and the criterion, write the check row yourself, and say so in `limits`.
A review that arrives as text rather than through the agentic reviewer (a review panel, a coauthor's or an outside reviewer's notes written as `Issue N (severity): claim` with a `Quote:` line) has its quotes checked with `python3 scripts/review_grounding.py --input <reviewed file> --dir <folder of reviews>`, which labels each grounded, stitched or invented; an issue's `Source quote:` line is labelled the same way against the `--source` file its `Source:` line names, reads not compared when that file was not given, and is labelled against each `--source` file when the issue names none.
When a person says they checked something, record it with `python3 scripts/trace_record.py check --person --by "Full Name" --said "<their words>"`, adding the candidate, criterion, what they examined (`--examined`), the result, the limits and each `--source` as they stated them (a source in a snapshot is verified against its manifest and named by the snapshot, as for the reviewer; one that does not verify is recorded with the reason); the draft is printed back field by field with `not stated` for what they did not say, and `--confirm --checks-jsonl <the record's checks.jsonl>` appends it once they confirm. An agent's own check uses `--agent` and is advisory.

For a textual issue, retain an exact source quotation and the representation checked.
Do not create an issue merely to fill a review; mark unsupported, duplicate or stale proposals as such.
Separate reproduced defects from documented limits and future design proposals.

Record the check with `procedure` set to `trace-review`, `procedure_version` set to `sha256:` and the digest that `trace_record.py procedure-version trace-review` reads off the skill file in force, and the candidate, criterion, method, result, artifact and limits required by the common contract; the agentic run writes this row itself.
Give every supported finding a ledger row before it is fixed, as the trace-evaluate skill says: `python3 scripts/defect_ledger.py ingest --run ai/reviews/<run> --finding <id>` for each finding of the run you reproduced, with `--matches` naming any already in the ledger; a finding marked unsupported, duplicate or stale gets none.
Retain the row ids in `defect_ids` of the check row that acts on them.
A model may record its advisory review with `trace_record.py accept --agent` over the commits actually reviewed; the agentic run does so for the commit it reviewed.
That row is model review, not acceptance.
A delegated return, refusal or acceptance by the reviewer needs `--authorized-by` and the authorizing text in `--authority`; a reviewer that executed or selected the covered work stays advisory.
