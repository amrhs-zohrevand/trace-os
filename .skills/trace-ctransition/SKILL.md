---
name: trace-ctransition
description: Check and record the authorized intake, manuscript promotion or standing-default adoption of a defined object.
---

# Check and record a transition

Read AGENTS.md, `ai/RECORD-CONVENTION.md` and `ai/SKILL-RUN-CONTRACT.md`.
Identify the exact object, source state, destination, checks and existing authority for the boundary crossing.
The registry row records a decision; it does not supply authority.
For outside material entering the project, run `trace_record.py intake` with origin, prior check, intended use, authority, the path when available and `--version` when the material has one (a release, a dataset version, a commit or a snapshot identifier); the digest is recorded beside it.
The row names who decided: `--by` defaults to `git config user.name`, and `--person`, `--agent` or `--authorized-by` declares the actor's kind, which a name alone never does.
Material turned away at entry is recorded too, with `--decision refuse` (or `return`, sent back for correction); the row records the decision and removes nothing already in the tree.
Outside material waits in the holding folder until the check and the decision: incoming/ inside the shared folder, or the folder `incoming_root` in ai/project.json names; `shared_folder.py status` names it and counts what is there.
Only after an admitting row does the material move into the project's data (its digest on the row names it wherever it goes); a returned or refused version stays in the holding folder or is removed, and is never copied into the data.
For manuscript promotion or standing-default adoption, run `trace_record.py transition` with the applicable kind, object, checks, actor, authority, decision, destination and covered commits.
A default adoption names the later trace record checked under the new default when one already exists; otherwise the observation is added later with `trace_record.py later-run <TR-id> --record <later record> --observed "..." --by NAME` and the observer's kind declared (`--person`, `--agent` or `--authorized-by`); `later-run <TR-id>` alone lists the candidate records, found by the instruction digest and not judged. No observation, no link.
Record the boundary check with `procedure` set to `trace-ctransition`, `procedure_version` set to `sha256:` and the digest that `trace_record.py procedure-version trace-ctransition` reads off the skill file in force, and all fields required by the common contract.
Run `trace_record.py registries` and retain any return or refusal rather than rewriting it as success.
The governing-actions report (`trace_report.py`) says which decision each transition row records, flags a returned or refused object that is in `paper/` anyway, counts the data files, shared-inventory files and snapshots no admitting intake row names (a row with no decision, written before intake took one, admits), names a file there that only a return or refuse row names as returned or refused yet present, and counts what is held in the holding folder by decision; all of it is information, and an unnamed file is a row to write, not a fault.
Moving an object does not establish acceptance or scientific validity.
This skill was `trace-transition` until 0.1.0-rc15, when it was renamed to `trace-ctransition` so the id carries its TRACE letter, C for controlled boundary transitions, the principle now called control transitions; records written under the old id stay as they were.
