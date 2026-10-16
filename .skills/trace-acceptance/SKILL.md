---
name: trace-acceptance
description: Examine evidence and record an authorized independent decision to accept, return or refuse a defined candidate.
---

# Record an independent acceptance decision

Read AGENTS.md, `ai/RECORD-CONVENTION.md` and `ai/SKILL-RUN-CONTRACT.md`.
Use this procedure only for a defined candidate and authority already present in the task or standing instructions.
When a person asks what is outstanding, show them the overview `trace_record.py queue` prints: a numbered table, one row per record: the work to accept or return, what hangs on it, what the agent chose as the record states it, where it comes from, and its owners; add to a cell only what reading the brief tells, and recommend nothing. Show the commits it lists as naming no trace record too, as it lists them, so the person sees every commit nobody has accepted.
A record that logs no choice is labelled `needs you` for that reason; say so rather than reading it as routine.
Identify the actor kind, scope, covered commits, criteria, evidence, unresolved objections and production roles considered.
An agent that produced, proposed or selected the candidate records advisory model review with `--agent`; it does not accept its own work. `accept` compares the execution trailers and the selectors the record's decisions name, and records an executing or selecting actor's attempt as self-review.
A person deciding about a commit they typed stays the person's decision; the row and `review-status` say they accepted their own work.
A person decides in their own words, naming the candidate; the decision is recorded with `--person`, by the person, or by the agent they said it to with `--recorded-by <its model id>` (the model `trace_record.py session` prints) and the words in `--words`, verbatim. The person is the one `git config user.name` names unless they named themselves otherwise; when another name for the speaker is in view and differs, ask before recording.
A decision covers the version the person saw: read back the `version:` line of `trace_record.py brief <record-id>` (the commit count and the commit it runs through) when putting the decision, and pass `--through` with that commit, so a commit that lands later is not covered and stays waiting in `queue`.
An independent agent or process authorized by a named researcher uses `--authorized-by` with the authorization's words, verbatim, in `--authority`, names its independence basis and records accept, return or refuse; without those words the row reads authority not stated and is advisory.
Never decide for a person, never infer a decision nobody stated, never record one under another name, and never infer authority from a name.
Run `trace_record.py accept` with the applicable actor flag and `--decision`, then run `trace_record.py registries`.
A return also opens a ledger row from the objection, with the person as raiser and the record as affected; the fix and the raiser's recheck are recorded on that row (`trace-evaluate`), and the acceptance of the corrected work is a later, separate decision.
Record the evidence examination with `procedure` set to `trace-acceptance`, `procedure_version` set to `sha256:` and the digest that `trace_record.py procedure-version trace-acceptance` reads off the skill file in force, and all fields required by the common contract.
If authority or independence is absent, retain the review evidence and leave the candidate unaccepted.
