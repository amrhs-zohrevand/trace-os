---
name: trace-check
description: Run the project's mechanical checks and summarize evidence, unavailable checks, review coverage and defect states.
compatibility: Requires this starter's scripts, Python 3.11 or later and Git. No cloud or model account is required.
---

# Check the workflow mechanically

Read AGENTS.md and `ai/SKILL-RUN-CONTRACT.md`.
From the project root run python3 scripts/check_workflow.py --out workflow-check.json, then python3 scripts/trace_stats.py --report workflow-check.json --out TRACE-STATS.md even if a check failed.
The composed check runs installed-skill byte parity when `.trace-os/install.json` exists (or the same file in `.traceware/` or `.trace-kit/`, an earlier release's folder, still read) and reports it as not applicable otherwise.
In an installed project its test suite is the kit's own test modules that `.trace-os/install.json` lists: the project's other test files under `tests/` are listed as not run, and a listed module that is absent is named, both as information that never fails the check.
If every listed module is absent, or `.trace-os/install.json` lists none, nothing runs and the suite reports an execution error, which fails the run; the project's test files are still listed as not run.
Read the report. Keep failure, execution error, skip and informational states distinct. Report the evaluated commit and local changes; do not describe a stale result as current.
The registries check fails on a malformed acceptance, intake or transition row. The governing-actions check reports, per commit since the install baseline and per principle, recorded, missing, not applicable or unknown, and for a recorded R whether its check rows name their sources, only a criterion, or neither; run python3 scripts/trace_report.py to list the commits with a missing record. Missing is a state to report, not a reason to refuse anything.
Record the run with `procedure` set to `trace-check`, `procedure_version` set to `sha256:` and the digest that `trace_record.py procedure-version trace-check` reads off the skill file in force, and all fields required by the common contract.
A passing mechanical run does not establish research validity, PDF build success, storage synchronization, or human acceptance; recorded means a joined record exists, not that the check was adequate or the accepter independent. Add the actual check and artifact reference to the session trace record.
