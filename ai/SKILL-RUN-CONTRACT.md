# Skill run contract

Use this contract when a project-local skill or external procedure produces a review, decision, transition or correction check.
Keep it in the session trace record rather than creating a parallel run log.

Every new check row records:

- `kind`, `runner`, `method`, `result` and `artifact`;
- the exact `candidate`, the `criterion` applied and the `limits` of the result;
- `sources`, when the check compared the candidate with anything: a list of objects, each with the `path` and, for a file, its `sha256`, plus `snapshot` when it came from a verified snapshot;
- `procedure` and `procedure_version` when a named skill or external procedure was used;
- `defect_ids` for stable findings selected into correction work;
- `recheck_evidence` for later targeted checks of those defects.

`defect_ids` and `recheck_evidence` are lists of strings.
Stable defect ids use the project's ledger ids, such as `DEF-031`; the prefix is `ledger_prefix` in ai/project.json, else the prefix of the ledger's existing rows, else `DEF` for a new ledger.
`verify` resolves each one against the project's defect ledger and reports an id the ledger does not hold, because a procedure's own internal numbering, such as an audit's `CLM-001`, has the same shape and would otherwise pass unnoticed.
Where the project has no ledger, the links are reported as not resolvable and the row stays readable.
A source that is not a file here (a printed book, a page read once) is named by `path` alone; `verify` reports a `sources` entry without a path, or with a digest that is not a sha256, and counts the rows that name their sources.
A check a person says they made is recorded with `trace_record.py check --person`, in their words (`--said`), printed back field by field with `not stated` for what they did not say, and appended only with `--confirm`.
A recheck reference names its durable evidence row or file and check id.
Do not invent a procedure version, defect or recheck link when none exists.
Historical rows remain readable without these optional handoff fields.

## The procedure version is read off the procedure text

A version number somebody typed records only that they typed it.
When the procedure is a skill this project holds, record `procedure_version` as the version derived from that skill file:

```bash
python3 scripts/trace_record.py procedure-version trace-review
sha256:0b1c2d3e4f56...
```

That value is `sha256:` followed by the SHA-256 of `.skills/<procedure>/SKILL.md`, or of `.skills/<procedure>.md` where a project keeps its skills as single files.
At least the first twelve hex characters are required and the whole digest is the convention.

`verify` checks that value against the digest the same record kept for that file.
`extract` writes the digest of every `.skills/**/*.md` file as it stood when the turns were captured, so the comparison is with the text in force during the run, not with the working tree.
A skill edited afterwards therefore never retroactively contradicts a record made under the earlier text, while a row whose digest the record's own provenance contradicts is reported as a problem, and promotion refuses it.

A value in any other form is read as a label the procedure itself declares, counted as declared rather than contradicted, which keeps older rows readable.
When the record digested no such file, the row is reported as not resolvable rather than failed: a globally installed or client-generated copy is outside `.skills/`, so its text was never captured here.
A matching digest establishes which file was present, not that the agent read it, followed it, or applied it competently.

The trace record also retains the task authority and input turns, instruction file digests, actor and execution model, decision account and checked output artifacts.
The check row does not repeat those fields unless a different input or actor matters to that check.
A passing schema check establishes that the fields are present and well formed.
It does not establish source adequacy, actor independence, authorization or acceptance.
