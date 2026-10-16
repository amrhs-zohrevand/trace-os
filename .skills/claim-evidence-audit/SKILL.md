---
name: claim-evidence-audit
description: Audit whether claims in a draft, memo or argument sketch are supported by the project's evidence base, keeping evidential support separate from acceptance.
compatibility: Requires no script, credential or external service. `paper/`, `paper/bib/` and `workspace/shared/papers/` exist only in the academic profile; the audit degrades to the paths that are present.
---

# Audit claims against the evidence

## Purpose

Audit whether claims in a manuscript draft, memo, section outline, or
argument sketch are actually supported by the project's evidence base.

This is a lightweight "living paper" audit: claims are treated as discrete
objects, evidence can support, challenge, qualify, or merely illustrate them,
and weak or overextended claims are surfaced before they harden into prose.

## When to use

Use this skill when the user asks to:

- verify claims against the literature or coding records
- check whether a section is supported by evidence
- prepare an evidence audit before revising or submitting
- identify unsupported, thinly supported, or overclaimed statements
- create a claim-evidence map for a section, memo, or draft

Do not use this skill for simple copyediting or broad literature search. The
core audit works from evidence the user supplies and evidence already present
in the project. It does not require a literature search or source-processing
method.

New-source discovery and source extraction are separate, optional tasks. Run
either when it falls within the existing task authority. If it would materially
extend the task, identify it as a possible next step and ask before executing
it. Resolve an optional method through the current project or global skill
registry, not through an assumed `.skills/` path. If the needed method or source
is unavailable, complete only the supportable part of the audit and name the
missing source or capability. Do not imply that a search, extraction, or source
check occurred when it did not.

## Procedure

### 1. Set the audit scope

Identify exactly what is being audited:

- a manuscript section in `paper/sections/`, in a project that has one
- a draft in `workspace/sandbox/`
- a memo in `sources/memos/`
- a user-provided claim list

If the source is in `paper/`, treat it as read-only unless the user explicitly
asked for manuscript edits.

Define the output location before starting:

- exploratory audit: `workspace/sandbox/claim-evidence-audit-<topic>-YYYYMMDD.md`
- durable analytical audit: `sources/memos/claim-evidence-audit-<topic>-YYYYMMDD.md`
- structured claim-evidence layer, if needed:
  `sources/coding/claim-evidence/`

Inventory the evidence supplied for the task and the relevant evidence paths
that actually exist. Record material limits, including cited sources that are
missing, unprocessed, or unavailable for inspection. A missing evidence path is
an audit limitation, not a reason to create a replacement workflow.

### 2. Extract auditable claims

List substantive claims, not every sentence. Prioritize:

- causal claims
- mechanism claims
- scope or boundary claims
- definitional claims central to the argument
- empirical generalizations across sources
- claims that reviewers might challenge

Assign stable IDs:

```text
CLM-001
CLM-002
CLM-003
```

A `CLM-` id identifies a claim inside this audit. It is not a defect id, and it
must never be written into a check row's `defect_ids`; see step 8.

For each claim, record:

- exact claim text
- source location
- claim type: `empirical`, `mechanism`, `theoretical`, `scope`, `definition`,
  or `methodological`
- what evidence would count as support
- what evidence would count as a challenge

### 3. Search the evidence base

Search the evidence the task supplied first, then whichever of these exist:
`sources/coding/`, `sources/memos/`, `sources/excerpts/`,
`workspace/shared/papers/notes/`, `workspace/shared/papers/text/` for spot
checks only, and `paper/bib/` for citation-key confirmation.

When the claim is about an artifact this project controls, such as a count, a
file list, a command interface or a manifest, read that artifact before any memo
about it. A project memo records what was true when it was written, and a claim
that has gone stale will usually be confirmed by the memo that first reported
it. Cite the artifact, and cite the memo only as the report it is.

For a claim about the literature rather than about this project, use processed
source notes and coding records first. Do not build major interpretive
conclusions directly from a raw extraction unless the relevant source has not
yet been processed; when that is the only available basis, say so in the audit's
limits and keep the rating provisional.

For each claim, search for:

- direct support
- partial support
- challenging evidence
- qualifying evidence
- missing evidence
- sources the draft cites but the coding layer does not actually support

When the available record cannot answer part of a claim, classify only the
relationship that can be inspected. Mark the remainder as unassessed, identify
the missing source or needed capability, and keep any overall support rating
provisional. Never infer support from an unavailable citation or treat an
unfinished optional search as completed evidence collection.

### 4. Classify the evidence relationship

Use these relationship types:

| Relationship | Meaning |
|---|---|
| `supports` | Evidence directly backs the claim |
| `challenges` | Evidence cuts against the claim |
| `qualifies` | Evidence supports only under narrower conditions |
| `illustrates` | Evidence makes the claim vivid but does not prove it |
| `backgrounds` | Source provides context but not direct support |

Use these support ratings:

| Rating | Meaning |
|---|---|
| `strong` | Multiple relevant sources or a highly diagnostic source support the claim, with challenges handled |
| `moderate` | Plausibly supported, but thinner, narrower, or less direct than ideal |
| `thin` | Some support exists, but the text should hedge or add evidence |
| `unsupported` | No adequate evidence found in the project record |
| `contested` | Support and challenge are both substantial |

### 5. Keep evidence assessment separate from acceptance

An evidence audit may find strong support or recommend retaining a claim without
revision. Neither finding establishes accountable acceptance. The same rule
applies to silence, the absence of objections, and a recommendation to take no
action. These are audit findings, not acceptance decisions.

Every audit must report an **Acceptance status** separately. Set it to `not
assessed` unless the supplied material contains direct evidence of an explicit
acceptance decision tied to the audited object and a named actor. When such
evidence is supplied, report the status and cite its source without inferring
authority, independence, or scope beyond what the record states.

Performing this analysis does not accept, return, or refuse the work and does
not create or modify an acceptance record. Act on or record an acceptance
decision only when separately authorized through the project's governing
process.

### 6. Flag concern levels

Flag each claim:

- `low`: supported or only needs small wording precision
- `medium`: needs hedging, a citation fix, or a boundary condition
- `high`: likely overclaim, unsupported claim, or contradiction with evidence

For high-concern claims, recommend one of:

- revise claim
- add boundary condition
- add or process source
- downgrade to speculation
- remove claim

### 7. Optionally create structured claim-evidence files

If the user wants a reusable claim-evidence layer, create or update:

```text
sources/coding/claim-evidence/claims.jsonl
sources/coding/claim-evidence/evidence.jsonl
sources/coding/claim-evidence/links.csv
```

Use this minimal schema:

`claims.jsonl`

```json
{"claim_id":"CLM-001","claim_type":"mechanism","text":"...","source_location":"...","status":"draft","support_rating":"thin","concern_level":"medium"}
```

`evidence.jsonl`

```json
{"evidence_id":"EVD-001","source_kind":"source_note","locator":"...","summary":"..."}
```

`source_kind` names what the evidence is: `source_note`, `coding_record`,
`memo`, `bib_entry`, `repository_artifact`, `command_output`, or another value
the project defines. `locator` is whatever identifies it, a cite key, a path, a
path plus a line range, or a command.

`links.csv`

```csv
claim_id,evidence_id,relation,weight,note
CLM-001,EVD-001,supports,central,Direct support for mechanism
```

The structured layer must be able to reproduce the summary table above it, which
is why `support_rating` and `concern_level` belong in `claims.jsonl`.

Do not copy large copyrighted passages into these files. Use short summaries,
locators, and source locations.

### 8. Hand the audit to the record

Read `ai/SKILL-RUN-CONTRACT.md` and record the audit in the session trace
record rather than in a parallel log.

Write one check row with `procedure` set to `claim-evidence-audit` and
`procedure_version` set to the value
`python3 scripts/trace_record.py procedure-version claim-evidence-audit` reads
off this file, the audited object and its source version as the `candidate`, the
support criterion actually applied as the `criterion`, the audit file as the
`artifact`, and the evidence that was unavailable as the `limits`. The audit
already computes that last item under "Evidence Available and Limits"; carry it
across rather than discarding it.

When a finding enters correction work, open it with
`python3 scripts/defect_ledger.py open --label ... --severity ... --quote ... --source <file>`
and carry the returned ledger id in `defect_ids`. An internal `CLM-` id
identifies a claim in this audit and is not a ledger id; a `defect_ids` list of
`CLM-` ids points at nothing.

Carry the acceptance status into the row's `result` exactly as the audit reports
it. An audit is advisory model review, recorded with
`python3 scripts/trace_record.py accept --agent` over the commits actually
examined, and a favourable finding never becomes an acceptance in the row.

## Output format

Write a markdown audit with this structure:

```markdown
# Claim-Evidence Audit: [Topic]

## Scope

## Evidence Available and Limits

## Executive Summary

| Claim ID | Short Claim | Support Rating | Concern | Recommendation |
|---|---|---|---|---|

## Acceptance Status

`not assessed`, unless direct acceptance evidence was supplied. If it was,
state the recorded status, actor, audited object, scope, and source.

## Claims Audited

### CLM-001: [Short label]

**Claim text**:

**Source location**:

**Claim type**:

**Evidence found**:

| Evidence | Source | Relation | Weight | Note |
|---|---|---|---|---|

**Missing or unassessed evidence**:

**Support rating**:

**Concern level**:

**Recommended action**:

## Cross-Cutting Issues

## Next Steps
```

## Notes

- The goal is not to make every claim maximally defensive. The goal is to make
  the relationship between claim strength and evidence strength honest.
- Do not treat a citation as support just because it appears near the claim.
  Read the relevant source note, coding record, or artifact.
- Evidence that merely illustrates a claim should not be counted as direct
  support.
- If an audit changes the project's interpretation of a source or construct,
  carry that judgment into `sources/coding/` or `sources/memos/`, not only into
  chat history.
