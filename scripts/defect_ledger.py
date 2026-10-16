#!/usr/bin/env python3
"""Track review findings across runs, so closure is checked rather than assumed.

Principle five asks whether a detected failure was actually corrected.
A review that only ever finds things cannot answer that.
This keeps every finding under a stable id and sorts them into six states:

  open        raised, nobody has claimed it is fixed
  fixed       someone claimed it is fixed, no run has re-checked yet
  closed      claimed fixed, and a later sweep did not raise it again
  verified    a targeted recheck against the original diagnosis passed, and the evidence for that recheck is on the row
  recurred    claimed fixed or verified, and a later check raised it anyway
  unresolved  a targeted recheck failed before the defect was ever verified

Two states fail the check, because both mean a correction did not hold or was checked and found not to hold.
`recurred` says a correction that was claimed or verified came undone.
`unresolved` says a recheck ran and failed while the row stood open, fixed or closed.
A failed recheck on an open, fixed or closed row sets `unresolved`; only a failed recheck on a verified row sets `recurred`, and a failed recheck on a recurred row keeps it `recurred`.
The row stays `unresolved` until a later recheck passes and verifies, under the same conditions as any other verification.
A later sweep never closes an unresolved row by absence, because a sweep that did not look is not a recheck that passed.
The check exits 1 for a recurred or an unresolved row, and 2 for an unreadable or malformed ledger; a ledger file that does not exist is unreadable, because the starter seeds one and a missing file means the wrong path was named.
It also reports, without failing, a verified row that names a faulty default its verifying event does not cover with a later run, as when the default was declared after the verification.
A ledger is malformed when a row has no string id or state, when a state is outside the six, or when a verified row carries no recheck event whose decision is verified.
Every row is validated before any finding is printed, so a bad row cannot leave a partial result that reads as complete.
`check --json` emits a completed result for a caller to interpret without parsing progress messages: a status of ok, recurred, unresolved or error, with the recurred and unresolved rows listed, and recurred taking precedence when both occur.
That is the regression test for an argument.

`closed` and `verified` are different claims, and keeping them apart is the point of this file.
`closed` is an observation: a later sweep happened not to raise the finding again.
`verified` is a recorded recheck of the original diagnosis on the corrected object, with a criterion, a method, an outcome, a named checker and a stated limit.
Absence of a complaint is not evidence of a correction, so no historical `closed` row is ever promoted to `verified` automatically.
Somebody has to run the recheck and record it.
A row closed by hand (`mark --state closed`) is not that observation, so the closure is recorded as a repair claim, with who said it and when, and a `manual:<name>:closed` marker in the row's runs.
A closed row with no recheck whose latest closure was not a sweep's, closed by hand or closed before the kit said how, stays visible: `check` lists it as information, never as a failure, and so does the line in `trace_record.py queue` that says what E is waiting on.

A verification is a technical record and nothing more.
It is not acceptance, it does not authorize publication or reliance, and it does not settle whether the underlying judgment was right.
A model may perform and record one, and that is labelled.
A checker who also implemented the correction is recorded as a self-check and labelled in the output, because a self-check is not independent review.
Only a named person accepts, through `scripts/trace_record.py`.

A corrected default is checked on later work, not only on the output where the failure was noticed.
A row may carry `faulty_default`, the instruction file or default that produced the failure, and `affected`, the trace records it touched, so that correction of the instruction and of downstream work is recorded and rechecked.
When `faulty_default` is set, a passing recheck verifies only if the evidence names a later run: `recheck.later_run` with `record`, a later trace record produced under the revised instruction, and `instruction_version`, a commit or digest of that revised instruction.
Evidence written before 2026-09-20 spells that field `contribution`, and stays readable under the old spelling.
Correcting the one output a reviewer happened to read leaves the instruction free to shape the next one, which is why the recheck has to reach a later run.
Without a later run the event is recorded and the state is left alone.
A stated later run is also resolved in the working tree before it counts: its record has to be a trace record, `ai/records/<id>` with an `evidence.json`, and its instruction version a commit (or the sha256 of the instruction's content at one) that changes the faulty default.
A later run that does not resolve is recorded on the event with what did not resolve, `verify` and the draft say so, and the row does not verify on it.
This holds for events recorded from 0.1.0-rc22 on; a row verified before keeps its state, and `check` reports it, without failing, when its later run does not resolve.

A correction is also checked on every output the error reaches, not only on the one where it was noticed.
A row may carry `objects`, the outputs the defect reaches (tables, files, sections), set with `open --object` or `mark --object`, once per object; `affected` keeps its meaning, the trace records the defect touched.
A row that lists objects verifies only when each of them has its own complete, passing, adequate recheck, one event per object, whose `target.corrected_object` names it.
A passing recheck of some of them is recorded, keeps the object it covers in the event's `covers`, and leaves the state where it is; `verify`, `status`, `check` and the line in `trace_record.py queue` that says what E is waiting on all say how many of the objects are rechecked and which still wait.
A failed recheck on any one object moves the row as any failed recheck does, and starts the round again, as a recurrence does, because the next correction may change every object the defect reaches.
A recurrence is kept on the row in `recurrences`, with how many recheck events came before it, when a sweep raises the finding again or `mark` moves the row into or out of `recurred`, so the round restarts there however the row then leaves `recurred`, a `mark --fixed` included.
Object names are compared as written, with slashes unified and a leading `./` dropped: an object may be a table or a section rather than a file, so no path is resolved.
`draft-recheck` hashes an object that is a file in the tree from its content; for a table or a section the checker states the hash with `--object-hash` (the commit of the correction, or the sha256 of the file that builds it), and without one the hash reads `not stated` and the event does not verify.
A row without `objects` verifies exactly as before, and no historical row changes state.

Two independent signals decide recurrence in a sweep.
Whether a later panel raises the finding again, matched by the aggregator, and whether the exact span the finding quoted is still in the source.
The second is cheap and hard to fool: if the sentence a reviewer objected to is gone, the objection no longer has a target.

Usage:
  python3 scripts/defect_ledger.py ingest --run .agent-reviews/deep-20260827
  python3 scripts/defect_ledger.py ingest --run ai/reviews/<run> --finding F1 --finding F3 --matches matches.json
  python3 scripts/defect_ledger.py open --label "..." --severity high --quote "..." --source paper/sections/03.tex --by "Full Name"
  python3 scripts/defect_ledger.py open --label "..." --severity high --object tables/t1.tex --object tables/t2.tex --by "Full Name"
  python3 scripts/defect_ledger.py mark --id AH-003 --fixed --note "..."
  python3 scripts/defect_ledger.py mark --id AH-003 --state fixed --affected <ref> --faulty-default AGENTS.md
  python3 scripts/defect_ledger.py draft-recheck AH-003 --checker "Full Name" --person --said "what the checker said"
  python3 scripts/defect_ledger.py draft-recheck AH-003 --object tables/t2.tex --checker "Full Name" --person
  python3 scripts/defect_ledger.py draft-recheck AH-003 --object "Table 2" --object-hash <commit> --checker "Full Name" --person
  python3 scripts/defect_ledger.py verify AH-003 --evidence recheck.json
  python3 scripts/defect_ledger.py status --source paper/sections/*.tex
  python3 scripts/defect_ledger.py check

`open` records a finding that did not come from a review run: an agent's observation while editing, a reader's objection, a coauthor's comment.
It writes a row in the same shape as an ingested one, with the next id, state open and a `manual:<name>` run marker, plus `affected`, `objects` and `faulty_default` when given.
`ingest` sets no objects, because a review run names where a finding was quoted, not every output it reaches; `mark --object` adds them to an ingested row.

`ingest` reads two kinds of run directory.
A deep-review run holds `defects.json`, its pooled findings, and is ingested whole; a fixed row the run did not raise is closed by absence.
An agentic review's tracked directory, `ai/reviews/<run>/` as `agentic_review.py` writes it, holds `findings.json`, the reviewer's reply, and each finding named with `--finding <id>` (every one when none is named) becomes a row with the reviewer's claim as its label, its file in `sources`, the reviewer as `opened_by` and the run and finding id in `review` and `review_finding`.
Name only the findings you reproduced: an unsupported, duplicate or stale one gets no row and stays in the report.
An agentic review examines one candidate, so it closes nothing by absence, and its findings are matched by the agent rather than a model: when the ledger has rows, `--matches` says which findings are already rows, by their position in the reply's findings, and an empty array says none is.
When the reviewer's text was withheld from the tracked directory, there is no `findings.json` to read, and each supported finding enters with `open` after its words are read for what they disclose.
When `--source` and a quote are both given, the grounding verdict is stored as `anchor_at_open`, which says whether the quoted span is in the source today and nothing about whether the finding is right.

Every subcommand takes `--ledger PATH`, and the environment variable TRACE_OS_DEFECT_LEDGER sets the same thing for a whole session (its earlier names, TRACEWARE_DEFECT_LEDGER in 0.1.0-rc19 and TRACE_KIT_DEFECT_LEDGER before, still work when it is unset).
An explicit `--ledger` wins over the variable, which wins over the default `ai/defects.jsonl` resolved against the working directory.
Tests and demonstrations point at a temporary file, never at the real ledger.
A new row's id is a prefix, a hyphen and the next number over every row: the prefix is `ledger_prefix` in the project.json beside the ledger (ai/project.json for the default), else the prefix of the existing rows (AH in every ledger the kit wrote before 0.1.0-rc22), else DEF for a ledger with no rows yet.

The evidence for `verify` is a JSON file rather than a row of flags, because a recheck record has five field groups and a flag list that long is neither readable nor reviewable.
`scripts/verification-evidence.example.json` is a filled-in template.
Required fields:

  target          defect_id, original_diagnosis, corrected_object, object_hash (a commit sha or a content hash)
  response        corrective_action, implementer
  recheck         criterion, method, outcome (pass, fail, or not stated), evidence_ref, timestamp
  accountability  checker, checker_kind, recorder, recorder_kind (human or model each)
  limits          adequacy (adequate, inadequate or unknown), limitations, unresolved_contrary_evidence

Three optional fields change the decision when present.
`accountability.checker_implemented` declares the self-check flag that is otherwise derived from the names, and `limits.essential_context_missing` says the recheck could not obtain context the original diagnosis needs.
`recheck.later_run` is an object with a non-empty `record` (or the historical `contribution`) and `instruction_version`, and a row with a `faulty_default` cannot verify without it, nor on one that does not resolve in the working tree.
Any other key is kept verbatim, which is where a consultation request, a reporter's reply, or a note about an unavailable reporter belongs.

Only a complete passing event with adequacy `adequate`, no unresolved contrary evidence, no missing essential context, and a later run where the row names a faulty default sets `verified`, and where the row lists objects, only the event that completes a passing recheck of each.
Everything else is still recorded, and says why it did not verify.
An outcome of `not stated` records the event and leaves the state alone: a checker who said less than a method and an outcome is recorded as having said that much, never asked for more, and never read as having verified.
`draft-recheck` writes the evidence file from what the row and the tree already know (the diagnosis, the corrected object and its hash, and the fixer and the fix of the current correction, which every `mark --fixed --by --note` records, a failed recheck outstanding or not) and shows the row's label as the proposed criterion; the checker's own words go in as method, outcome and limits, and the file is printed back field by field before `verify` records it.
On a row that lists more than one object, the corrected object is the one given with `--object`, and reads `not stated` when none is given, so that draft does not verify; a row that lists one object supplies it.
A field the checker did not state reads `not stated`, and the draft says what recording it would do to the row.
A verification names the corrected object and the correction, so a placeholder for the object, its hash, the action or the implementer is recorded and does not verify.
It also carries a criterion, a method, an evidence reference, a named checker and a stated limit, so a placeholder in any of those is recorded and does not verify, and so is a later run whose record or instruction version reads `not stated`.
An event with outcome `not stated` is kept and is not a recheck that happened, so the row stays fixed without recheck.
A failed event moves a verified or recurred row to `recurred` and any other row to `unresolved`.
`verify` exits 0 whenever the event was recorded, a failed recheck included, because `check` is the gate that fails.

Validation checks that the fields are present and well formed.
It cannot check that the diagnosis was right, that the evidence was sufficient, or that the checker was competent.
Those stay recorded judgments, open to review.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_grounding import verdict                           # noqa: E402
from review_grounding import source_text as grounding_text     # noqa: E402


def key() -> str:
    """Network matching is optional; offline ledger commands do not load it."""
    try:
        from review_panel import key as review_key
    except ImportError:
        raise ValueError("automatic matching is not installed; supply ingest --matches JSON") from None
    return review_key()

DEFAULT_LEDGER = Path("ai/defects.jsonl")
LEDGER_ENV = "TRACE_OS_DEFECT_LEDGER"
# The variable's earlier names, newest first (0.1.0-rc19, then before), honoured when LEDGER_ENV is unset.
EARLIER_LEDGER_ENVS = ("TRACEWARE_DEFECT_LEDGER", "TRACE_KIT_DEFECT_LEDGER")

STATES = ("open", "fixed", "closed", "verified", "recurred", "unresolved")
SEVERITIES = ("high", "medium", "low")

# Sorted by how much attention a row still needs, worst first. `unresolved`
# follows `recurred` because both are failed checks, and precedes `open`
# because a recheck that failed is more than a finding nobody has handled.
# `verified` sits below `closed` because it is the stronger claim, not because
# it is stronger evidence of anything else.
ORDER = {"recurred": 0, "unresolved": 1, "open": 2, "fixed": 3, "closed": 4, "verified": 5}

REQUIRED = {
    "target": ("defect_id", "original_diagnosis", "corrected_object", "object_hash"),
    "response": ("corrective_action", "implementer"),
    "recheck": ("criterion", "method", "outcome", "evidence_ref", "timestamp"),
    "accountability": ("checker", "checker_kind", "recorder", "recorder_kind"),
    "limits": ("adequacy", "limitations", "unresolved_contrary_evidence"),
}
# The later run names the trace record produced under the revised instruction.
# `record` is what the kit writes; `contribution` is the spelling used before
# 2026-09-20 and is still read, because ledger history is not rewritten.
LATER_RUN_RECORD = ("record", "contribution")
LATER_RUN = ("instruction_version",)


def later_run_record(later: dict) -> str:
    """The later run's record, under either spelling. Empty when absent."""
    for key in LATER_RUN_RECORD:
        value = later.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""
OUTCOMES = ("pass", "fail")
# A checker who stated no outcome is recorded as such. The event is kept, the
# state is left alone, and nobody is asked to say more than they said.
NOT_STATED = "not stated"
KINDS = ("human", "model")
ADEQUACY = ("adequate", "inadequate", "unknown")
# Only an explicit "none" counts as no unresolved contrary evidence. "unknown"
# does not, because not having looked is not the same as having looked.
NO_CONTRARY = ("none",)


def placeholder(value) -> bool:
    """Whether a field holds nothing, or the placeholder a draft writes for what nobody stated."""
    return str(value or "").strip().lower() in ("", NOT_STATED)


def later_run_covers(event: dict) -> bool:
    """Whether a recorded event's evidence names a later run, record and instruction version both stated."""
    evidence = event.get("evidence") if isinstance(event, dict) else None
    recheck = evidence.get("recheck") if isinstance(evidence, dict) else None
    later = recheck.get("later_run") if isinstance(recheck, dict) else None
    return (isinstance(later, dict) and not placeholder(later_run_record(later))
            and not placeholder(later.get("instruction_version")))


# Where a later run's trace record has to be, relative to the working tree.
RECORDS_DIR = Path("ai/records")
# How far back a content digest of the instruction is looked for.
DIGEST_HISTORY = 500


def _git(root: Path, *args: str) -> str | None:
    """A Git command's output in `root`, or None when it fails or Git is absent."""
    try:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def record_reference(text: str, root: Path) -> str | None:
    """The trace record a later run names, as `ai/records/<id>`, or None when it names none.

    The first word is the reference, so a record followed by a description still reads; a bare id is read under `ai/records/`, as a `Record:` line is, and an absolute path inside the tree as its relative one.
    """
    words = re.split(r"[\s,;]+", str(text or "").strip())
    token = words[0].replace("\\", "/").rstrip("/.,;:)") if words else ""
    if not token:
        return None
    path = Path(token)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return None
    if len(path.parts) == 1:
        path = RECORDS_DIR / path
    if path.parts[:2] != RECORDS_DIR.parts or len(path.parts) != 3:
        return None
    return path.as_posix()


def default_path(faulty_default: str, root: Path) -> str | None:
    """The file or directory a faulty default names, when Git has ever tracked it; None otherwise.

    A trailing line number (`AGENTS.md:119`) is dropped, and so is anything after the first word.
    """
    words = str(faulty_default or "").strip().split()
    token = re.sub(r":\d+(?:-\d+)?$", "", words[0].replace("\\", "/")) if words else ""
    while token.startswith("./"):
        token = token[2:]
    token = token.rstrip("/")
    if not token or token.startswith("-"):
        return None
    return token if (_git(root, "log", "-1", "--format=%H", "--", token) or "").strip() else None


def changes_path(root: Path, sha: str, target: str) -> bool:
    """Whether a commit changes `target` against its first parent (or adds it, for a root commit)."""
    parents = (_git(root, "rev-list", "--parents", "-n", "1", sha) or "").split()[1:]
    if parents:
        out = _git(root, "diff", "--name-only", parents[0], sha, "--", target)
    else:
        out = _git(root, "show", "--format=", "--name-only", sha, "--", target)
    return bool((out or "").strip())


def digest_commit(root: Path, target: str, digest: str) -> str | None:
    """The newest commit changing `target` whose content of it has this sha256, or None."""
    log = _git(root, "log", f"-n{DIGEST_HISTORY}", "--format=%H", "--", target) or ""
    for sha in log.split():
        try:
            blob = subprocess.run(["git", "show", f"{sha}:{target}"], cwd=root, capture_output=True)
        except OSError:
            return None
        if blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() == digest:
            return sha
    return None


def instruction_commit(version: str, faulty_default: str, root: Path) -> tuple[str | None, str]:
    """The commit an instruction version names that changes the faulty default with an empty reason, or None and why not.

    The version may name the commit (a sha, or a name Git resolves, alone or inside a phrase such as `AGENTS.md at 4f3924b`) or the sha256 of the instruction's content at a commit that changed it.
    """
    if _git(root, "rev-parse", "--git-dir") is None:
        return None, ("the working tree is not a Git repository, so recheck.later_run.instruction_version "
                      "cannot be resolved to a commit that changes the faulty default")
    target = default_path(faulty_default, root)
    if target is None:
        return None, (f"the faulty default {faulty_default!r} names no file Git has tracked here, so no "
                      "commit can be shown to revise it; mark --faulty-default can name the instruction file")
    text = str(version or "").strip()
    candidates = re.findall(r"(?<![0-9A-Za-z])(?:sha256:)?[0-9a-fA-F]{7,64}(?![0-9A-Za-z])", text)
    if text and not re.search(r"\s", text) and not text.startswith("-"):
        candidates.append(text)
    elsewhere = None
    for candidate in dict.fromkeys(candidates):
        hexpart = candidate.split(":")[-1].lower()
        if len(hexpart) == 64 and re.fullmatch(r"[0-9a-f]{64}", hexpart):
            sha = digest_commit(root, target, hexpart)
            if sha:
                return sha, ""
        if candidate.lower().startswith("sha256:"):
            continue
        sha = (_git(root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}") or "").strip()
        if sha and changes_path(root, sha, target):
            return sha, ""
        elsewhere = elsewhere or sha or None
    if elsewhere:
        return None, (f"recheck.later_run.instruction_version {text!r} names commit {elsewhere[:12]}, "
                      f"which does not change the faulty default {target}")
    return None, (f"recheck.later_run.instruction_version {text!r} names no commit, and no content "
                  f"digest, of the faulty default {target} in this repository")


def resolve_later_run(ev: dict, faulty_default: str, root: Path | str | None = None) -> dict | None:
    """Whether a stated later run exists here: its trace record, and a commit of the revised instruction.

    None when there is nothing to resolve: no faulty default, no later run, or a field that reads `not stated`, which `blocking_reasons` reports on its own.
    Otherwise `record` and `commit`, each resolved or None, and `problems`, empty when both resolve.
    Only that the record exists and that the commit changes the faulty default is checked; whether the record was produced under that revision stays the checker's stated judgment.
    """
    recheck = ev.get("recheck") if isinstance(ev, dict) else None
    later = recheck.get("later_run") if isinstance(recheck, dict) else None
    if not faulty_default or not isinstance(later, dict):
        return None
    named, version = later_run_record(later), later.get("instruction_version")
    if placeholder(named) or placeholder(version):
        return None
    root = Path(root) if root else Path.cwd()
    out: dict = {"record": None, "commit": None, "problems": []}
    record = record_reference(named, root)
    if record and (root / record / "evidence.json").is_file():
        out["record"] = record
    else:
        out["problems"].append(
            f"recheck.later_run.record {named.strip()!r} names no trace record in this repository "
            f"({RECORDS_DIR.as_posix()}/<id> with an evidence.json), so no later run is shown")
    commit, why = instruction_commit(str(version), faulty_default, root)
    if commit:
        out["commit"] = commit
    else:
        out["problems"].append(why)
    return out


def object_name(value) -> str:
    """An object's name as it is compared: slashes unified, a leading `./` and a trailing `/` dropped.

    Nothing is resolved against the tree, because an object may be a table or a section rather than a file, and a name that matches only after a guess is not a match.
    """
    name = str(value or "").strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/") or name


def row_objects(row: dict) -> list[str]:
    """The outputs the row says the defect reaches, in the row's order; empty for a row that lists none."""
    listed = row.get("objects") if isinstance(row, dict) else None
    if not isinstance(listed, list):
        return []
    return [o for o in listed if isinstance(o, str) and o.strip()]


def listed_object(objects: list[str], corrected) -> str | None:
    """The listed object a recheck's corrected object names, as the row spells it, or None."""
    if placeholder(corrected):
        return None
    name = object_name(corrected)
    return next((o for o in objects if object_name(o) == name), None)


def note_recurrence(row: dict, **source) -> None:
    """Keep a recurrence on the row, with how many recheck events came before it, in `recurrences`.

    The object round restarts there however the row then leaves `recurred`, a `mark --fixed` included (rc22 release review, bb872f3 F1).
    A second note at the same position is not added.
    """
    events = row.get("verifications")
    position = len(events) if isinstance(events, list) else 0
    marks = row.get("recurrences")
    if not isinstance(marks, list):
        marks = row["recurrences"] = []
    if marks and isinstance(marks[-1], dict) and marks[-1].get("after_events") == position:
        return
    marks.append({"after_events": position, **source})


def coverage_rounds(row: dict) -> tuple[set[str], set[str] | None]:
    """The object names covered in the round in progress, and in the round that last verified the row.

    An event covers the object named in its `covers`, which `verify` writes only for a complete, passing, adequate recheck of a listed object.
    A failed recheck on any object starts the round again, and so does a recurrence, because the correction that follows may change every object the defect reaches.
    A recurrence is read from the row's `recurrences` (a later sweep raised it again, or `mark` moved the row into or out of `recurred`), and, on a row written before those notes, from the row becoming recurred between two events.
    A verifying event closes its round.
    """
    events = row.get("verifications")
    marks = row.get("recurrences")
    restarts = {m.get("after_events") for m in (marks if isinstance(marks, list) else [])
                if isinstance(m, dict) and isinstance(m.get("after_events"), int)
                and not isinstance(m.get("after_events"), bool)}
    current: set[str] = set()
    verified_round: set[str] | None = None
    after = None
    for i, e in enumerate(events if isinstance(events, list) else []):
        if i in restarts:
            current = set()
        if not isinstance(e, dict):
            continue
        if e.get("state_before") == "recurred" and after not in (None, "recurred"):
            current = set()
        if str(e.get("outcome", "")).strip().lower() == "fail":
            current = set()
        elif isinstance(e.get("covers"), str):
            current.add(object_name(e["covers"]))
        if e.get("decision") == "verified":
            verified_round, current = current, set()
        after = e.get("state_after")
    if row.get("state") == "recurred" and after not in (None, "recurred"):
        current = set()
    if isinstance(events, list) and len(events) in restarts:
        current = set()
    return current, verified_round


def object_coverage(row: dict) -> tuple[list[str], list[str]]:
    """The row's objects split into rechecked and still waiting, each in the row's order.

    A verified row reports the round that verified it; any other row reports the round in progress.
    """
    objects = row_objects(row)
    current, verified_round = coverage_rounds(row)
    done = verified_round if row.get("state") == "verified" and verified_round is not None else current
    return ([o for o in objects if object_name(o) in done],
            [o for o in objects if object_name(o) not in done])


def coverage_phrase(row: dict) -> str:
    """'1 of 3 objects rechecked; waiting on b, c', or '' for a row that lists no objects."""
    objects = row_objects(row)
    if not objects:
        return ""
    done, waiting = object_coverage(row)
    text = f"{len(done)} of {len(objects)} objects rechecked"
    return text + (f"; waiting on {', '.join(waiting)}" if waiting else "")


MATCH_SYS = """You are matching new review findings against a ledger of known ones.

A new finding matches a ledger entry when they are the same underlying defect,
so that fixing one would fix the other. Wording will differ. A finding that
points at the same passage for a DIFFERENT reason is not a match.

Return ONLY JSON: {"matches":[{"n":<finding number>,"id":"<ledger id>"}]}
Omit any finding that matches nothing. Never invent a ledger id."""


class EvidenceError(Exception):
    """Raised when an evidence file is missing, unreadable, or incomplete."""


def ledger_path(arg: str | None = None) -> Path:
    """Resolve which ledger file to use.

    An explicit `--ledger` wins, then TRACE_OS_DEFECT_LEDGER, then its earlier names TRACEWARE_DEFECT_LEDGER (0.1.0-rc19) and TRACE_KIT_DEFECT_LEDGER (before), then the default.
    The indirection exists so a test or a demonstration can run the real command against a temporary file, rather than reaching into the module to reassign a constant.
    """
    if arg:
        return Path(arg)
    env = next((os.environ[name] for name in (LEDGER_ENV, *EARLIER_LEDGER_ENVS) if os.environ.get(name)), None)
    return Path(env) if env else DEFAULT_LEDGER


def load(path: Path | str | None = None) -> list[dict]:
    p = Path(path) if path else ledger_path()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def save(rows: list[dict], path: Path | str | None = None) -> None:
    """Write the ledger through a temporary file and one rename.

    A half-written ledger would be worse than none, and the promise that a rejected evidence file leaves the ledger byte-identical is easier to keep if every write is a single replace.
    """
    p = Path(path) if path else ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    os.replace(tmp, p)


# A row's id is its prefix, a hyphen and a serial number. The prefix is a project
# setting since rc22: `ledger_prefix` in ai/project.json, read from the
# project.json beside the ledger, so a test's or a demonstration's ledger in a
# temporary folder never takes the project's. Without it a ledger that already has
# rows keeps the prefix of its newest one (AH in this repository, and in every
# project whose ledger the kit wrote before), and a ledger with no rows yet gets
# FRESH_PREFIX: until rc22 every project's rows read AH-001, this repository's
# own prefix. The serial number runs on over every row, whatever its prefix, so
# a changed prefix never reuses a number.
FRESH_PREFIX = "DEF"
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
ID_NUMBER_RE = re.compile(r"^([A-Z][A-Z0-9]*)-(\d+)$")


def configured_prefix(ledger: Path | str | None = None) -> str | None:
    """The `ledger_prefix` set in the project.json beside the ledger (ai/project.json
    for the default ai/defects.jsonl), upper-cased, or None when it sets none.

    A value that is not letters and digits starting with a letter is reported on
    stderr and not used, so a row id always reads as one (`AB-001`).
    """
    path = Path(ledger or ledger_path()).parent / "project.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("ledger_prefix")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    prefix = value.strip().upper()
    if not PREFIX_RE.fullmatch(prefix):
        print(f"ledger_prefix {value!r} in {path} is not letters and digits starting with a "
              "letter; it is not used", file=sys.stderr)
        return None
    return prefix


def ledger_prefix(rows: list[dict], ledger: Path | str | None = None) -> str:
    """The prefix a new row's id takes: the project's setting, else the prefix of the
    row with the highest number, else FRESH_PREFIX for a ledger with no rows yet."""
    prefix = configured_prefix(ledger)
    if prefix:
        return prefix
    numbered = [(int(m.group(2)), m.group(1)) for r in rows
                for m in [ID_NUMBER_RE.match(str(r.get("id", "")))] if m]
    return max(numbered)[1] if numbered else FRESH_PREFIX


def next_id(rows: list[dict], ledger: Path | str | None = None) -> str:
    """The id a new row takes in the ledger at `ledger` (the default ledger when None)."""
    n = max((int(m.group(2)) for r in rows for m in [ID_NUMBER_RE.match(str(r.get("id", "")))] if m),
            default=0)
    return f"{ledger_prefix(rows, ledger)}-{n + 1:03d}"


def source_text(paths: list[str]) -> str:
    """The reader-visible text of the sources, prepared the way review grounding prepares it.

    LaTeX comments are removed from `.tex` files only: in Markdown, Python or plain text a `%` is content, and stripping from it to the line end made a quote such as "95% of the cases" read as invented (AH-156).
    """
    return " ".join(grounding_text(Path(p)) for p in paths)


def load_evidence(path: str, defect_id: str) -> dict:
    """Read one evidence file and check it, reporting every problem at once.

    Nothing is written before this returns, so a malformed file leaves the ledger byte-identical rather than half updated.
    The check is structural: presence, type, and the three closed vocabularies.
    It says nothing about whether the recheck was any good.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise EvidenceError(f"cannot read evidence file {p}: {e}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EvidenceError(f"evidence file {p} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise EvidenceError(f"evidence file {p} must hold a JSON object")

    bad: list[str] = []
    for group, fields in REQUIRED.items():
        block = data.get(group)
        if not isinstance(block, dict):
            bad.append(f"{group}: missing, or not a JSON object")
            continue
        for f in fields:
            v = block.get(f)
            if not isinstance(v, str) or not v.strip():
                bad.append(f"{group}.{f}: missing, or not a non-empty string")

    def val(group, field):
        block = data.get(group)
        v = block.get(field) if isinstance(block, dict) else None
        return v.strip() if isinstance(v, str) else None

    outcome = val("recheck", "outcome")
    if outcome is not None and outcome.lower() not in OUTCOMES + (NOT_STATED,):
        bad.append(f"recheck.outcome: {outcome!r} is not one of {OUTCOMES + (NOT_STATED,)}")
    for field in ("checker_kind", "recorder_kind"):
        kind = val("accountability", field)
        if kind is not None and kind.lower() not in KINDS:
            bad.append(f"accountability.{field}: {kind!r} is not one of {KINDS}")
    adequacy = val("limits", "adequacy")
    if adequacy is not None and adequacy.lower() not in ADEQUACY:
        bad.append(f"limits.adequacy: {adequacy!r} is not one of {ADEQUACY}")
    got_id = val("target", "defect_id")
    if got_id is not None and got_id != defect_id:
        bad.append(f"target.defect_id: {got_id!r} does not match the defect being verified ({defect_id!r})")
    for group, field in (("accountability", "checker_implemented"),
                         ("limits", "essential_context_missing")):
        block = data.get(group)
        if isinstance(block, dict) and field in block and not isinstance(block[field], bool):
            bad.append(f"{group}.{field}: must be true or false when present")
    # A later run is optional in general and required for a corrected default,
    # so its shape is checked whenever it is present: a half-filled later run
    # would otherwise verify a default that no later work has exercised.
    recheck = data.get("recheck")
    if isinstance(recheck, dict) and "later_run" in recheck:
        later = recheck["later_run"]
        if not isinstance(later, dict):
            bad.append("recheck.later_run: must be a JSON object when present")
        else:
            if not later_run_record(later):
                bad.append("recheck.later_run.record: missing, or not a non-empty string")
            for f in LATER_RUN:
                v = later.get(f)
                if not isinstance(v, str) or not v.strip():
                    bad.append(f"recheck.later_run.{f}: missing, or not a non-empty string")

    if bad:
        raise EvidenceError(f"evidence file {p} is incomplete:\n  "
                            + "\n  ".join(bad))
    return data


def self_check(ev: dict) -> tuple[bool, str]:
    """Decide whether the checker also implemented the correction.

    A declaration is honoured, with one exception: matching names override a declared false, because the names are the harder evidence and the self-check label is the more cautious of the two readings.
    A self-check is permitted and recorded; it is simply not independent review.
    """
    declared = ev["accountability"].get("checker_implemented")
    implementer = ev["response"]["implementer"].strip().lower()
    derived = ev["accountability"]["checker"].strip().lower() == implementer
    if declared is None and implementer in ("", NOT_STATED):
        return False, "the implementer is not stated, so the flag could not be derived"
    if declared is None:
        return derived, "derived from the checker and implementer names"
    if derived and not declared:
        return True, "derived from matching names, overriding a declared false"
    return bool(declared), "declared in the evidence file"


def decide(state: str, ev: dict, faulty_default: str = "", objects: list[str] | None = None,
           covered: set[str] | None = None, root: Path | str | None = None) -> tuple[str, str, list[str]]:
    """Work out what a validated event does to a row, and say why.

    Returns the new state, a one-word decision for the output, and the reasons behind it.
    A failed recheck moves a verified row to `recurred`, keeps a recurred row `recurred`, and moves any other row to `unresolved`, so a defect that was checked and found uncorrected cannot sit in a state the check passes.
    A passing recheck verifies only when the recorder states adequate evidence, no unresolved contrary evidence, and no missing essential context, and when the criterion, method, evidence reference, limitations and checker are stated.
    When the row names a faulty default, a passing recheck additionally needs a later run with its record and instruction version stated, because the corrected instruction is what has to be shown to hold, and only later work produced under it can show that.
    That later run also has to resolve in the working tree at `root` (default: the working directory): a trace record under `ai/records/` and a commit that changes the faulty default.
    When the row lists `objects`, the outputs the defect reaches, a complete passing recheck covers the one its corrected object names, and verifies only when that leaves none of them waiting; `covered` holds the object names earlier events in the round already cover.
    Any other passing event is still recorded, because an inadequate recheck that happened is worth more in the record than one that quietly did not.
    """
    outcome = ev["recheck"]["outcome"].strip().lower()
    if outcome == NOT_STATED:
        return state, "recorded", [
            "the recheck states no outcome, so the event is recorded and the state is left alone"]
    if outcome == "fail":
        # A second failed recheck of a correction that was once verified is
        # still a correction that came undone, not a defect that was never
        # corrected (AH-158).
        if state in ("verified", "recurred"):
            return "recurred", "recurred", [
                "a targeted recheck of a verified correction failed"]
        return "unresolved", "unresolved", [
            f"the recheck failed while the row was {state}, so the defect stays "
            "unresolved until a later recheck passes and verifies"]

    reasons = blocking_reasons(ev, faulty_default, root)
    objects = list(objects or [])
    corrected = ev["target"]["corrected_object"]
    named = listed_object(objects, corrected) if objects else None
    if objects and placeholder(corrected):
        reasons.append(f"the row lists {len(objects)} objects the defect reaches, and the recheck "
                       "names none of them: each object is rechecked in its own event")
    elif objects and named is None:
        reasons.append(f"target.corrected_object {corrected.strip()!r} is not one of the objects the row "
                       f"lists ({', '.join(objects)}), so the recheck covers none of them")
    if reasons:
        return state, "recorded", reasons
    if objects:
        done = {object_name(c) for c in (covered or set())} | {object_name(named)}
        waiting = [o for o in objects if object_name(o) not in done]
        if waiting:
            return state, "recorded", [
                f"the recheck passed on {named}: {len(objects) - len(waiting)} of {len(objects)} "
                f"objects rechecked, waiting on {', '.join(waiting)}; the row verifies when each "
                "object the defect reaches has its own passing recheck"]
    reason = ("the recheck passed, the evidence is stated adequate, and no unresolved "
              "contrary evidence was reported")
    if faulty_default:
        later = ev["recheck"]["later_run"]
        reason += (f"; the later run {later_run_record(later)} under instruction version "
                   f"{later['instruction_version']} was checked against the original diagnosis")
        found = resolve_later_run(ev, faulty_default, root)
        if found and not found["problems"]:
            reason += (f" (trace record {found['record']}; commit {found['commit'][:12]} changes "
                       f"{default_path(faulty_default, Path(root) if root else Path.cwd())})")
    if objects:
        reason += (f"; with this recheck of {named}, each of the {len(objects)} objects "
                   "the row lists has its own passing recheck")
    return "verified", "verified", [reason]


def covers(ev: dict, faulty_default: str = "", objects: list[str] | None = None,
           root: Path | str | None = None) -> str | None:
    """The listed object a validated event covers: a complete, passing, adequate recheck naming it, else None."""
    if not objects or ev["recheck"]["outcome"].strip().lower() != "pass":
        return None
    if blocking_reasons(ev, faulty_default, root):
        return None
    return listed_object(list(objects), ev["target"]["corrected_object"])


def blocking_reasons(ev: dict, faulty_default: str = "", root: Path | str | None = None) -> list[str]:
    """Why a passing recheck does not verify on its own terms, objects aside; empty when nothing stands in the way."""
    adequacy = ev["limits"]["adequacy"].strip().lower()
    contrary = ev["limits"]["unresolved_contrary_evidence"].strip()
    later = ev["recheck"].get("later_run")
    reasons = []
    if adequacy != "adequate":
        reasons.append(f"evidence and context adequacy is stated as {adequacy}")
    if ev["limits"].get("essential_context_missing"):
        reasons.append("essential context for the original diagnosis is missing")
    if contrary.lower() not in NO_CONTRARY:
        reasons.append(f"unresolved contrary evidence is stated as {contrary!r}")
    if faulty_default and not later:
        reasons.append(
            f"the row names a faulty default ({faulty_default}), so a corrected default "
            "needs a later-run recheck: recheck.later_run naming a later trace record "
            "produced under the revised instruction, checked against the original diagnosis")
    elif faulty_default:
        # A later run whose record or instruction version reads `not stated`
        # names no later work under a known revision (AH-150).
        for field, value in (("record", later_run_record(later)),
                             ("instruction_version", later.get("instruction_version"))):
            if placeholder(value):
                reasons.append(
                    f"recheck.later_run.{field} is not stated, and the row names a faulty default "
                    f"({faulty_default}), whose correction verifies only on a named later trace "
                    "record under a named revision of the instruction")
        # A stated later run counts only when it resolves here: a record that
        # does not exist, or a version that changes nothing, shows no later
        # work under the corrected instruction (rc22; earlier events keep
        # their decisions, and `check` reports them).
        found = resolve_later_run(ev, faulty_default, root)
        if found:
            reasons.extend(found["problems"])
    # `verified` is a recheck of the original diagnosis on the corrected
    # object: a placeholder where the object, its hash, the action or the
    # implementer should be is recorded, and says why it did not verify.
    for group, field in (("target", "corrected_object"), ("target", "object_hash"),
                         ("response", "corrective_action"), ("response", "implementer")):
        if placeholder(ev.get(group, {}).get(field)):
            reasons.append(f"{group}.{field} is not stated, and a verification names the corrected object and the correction")
    # It also carries a criterion, a method, an evidence reference, a named
    # checker and a stated limit; a recheck that leaves one of them unstated is
    # recorded as thin, never refused, and does not verify (AH-151).
    for group, field in (("recheck", "criterion"), ("recheck", "method"),
                         ("recheck", "evidence_ref"), ("accountability", "checker"),
                         ("limits", "limitations")):
        if placeholder(ev.get(group, {}).get(field)):
            reasons.append(f"{group}.{field} is not stated, and a verification carries a criterion, "
                           "a method, an evidence reference, a named checker and a stated limit")
    return reasons


def match(known: list[dict], found: list[dict], tok: str) -> dict[int, str]:
    """Ask the aggregator which new findings are already in the ledger."""
    from deep_review import AGGREGATOR
    if not known or not found:
        return {}
    payload = ("LEDGER:\n" + "\n".join(f'{r["id"]}: {r["label"]}' for r in known)
               + "\n\nNEW FINDINGS:\n"
               + "\n".join(f'[{i}] {f["label"]}' for i, f in enumerate(found)))
    from record_privacy import text_findings
    if text_findings(payload):
        raise ValueError("possible sensitive content in external ledger-matching packet; inspect locally")
    # The aggregator is a reasoning model, so the completion budget has to cover
    # the reasoning as well as the answer. At 8000 it spent 7987 tokens thinking
    # and returned nothing, and the first version of this function read that
    # empty answer as "no matches". A recurrence would have been recorded as a
    # clean run. Failures are raised now: a matcher that cannot fail is worse
    # than no matcher, because it reports success either way.
    body = json.dumps({"model": AGGREGATOR, "temperature": 0.1, "max_tokens": 16000,
                       "messages": [{"role": "system", "content": MATCH_SYS},
                                    {"role": "user", "content": payload}]}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=600).read())
    choice = d["choices"][0]
    txt = choice["message"].get("content")
    if not txt:
        raise RuntimeError(
            f"matcher returned no content (finish_reason="
            f"{choice.get('finish_reason')}, usage={d.get('usage')}). "
            f"Raise --max-tokens; do not treat this as 'no matches'.")
    m = re.search(r'\{.*"matches".*\}', txt, re.S)
    if not m:
        raise RuntimeError(f"matcher returned unparseable output: {txt[:300]!r}")
    ids = {r["id"] for r in known}
    out = {}
    for e in json.loads(m.group(0)).get("matches", []):
        if e.get("id") in ids and isinstance(e.get("n"), int):
            out[e["n"]] = e["id"]
    return out


def supplied_matches(filename: str, known: list[dict], count: int) -> dict[int, str]:
    """Validate an explicit zero-based mapping, allowing ingestion without a model."""
    data = json.loads(Path(filename).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
        raise ValueError("matches file must contain a matches array")
    ids = {r["id"] for r in known}
    result = {}
    for item in data["matches"]:
        if not isinstance(item, dict):
            raise ValueError("each match must be an object")
        number, rid = item.get("n"), item.get("id")
        if type(number) is not int or not 0 <= number < count or number in result:
            raise ValueError("match n must be a unique zero-based finding index")
        if not isinstance(rid, str) or rid not in ids:
            raise ValueError("match id is not in the selected ledger")
        result[number] = rid
    return result


def reviewer_of(run: Path) -> str:
    """The reviewer an agentic review's check row names, or '' when it names none."""
    try:
        row = json.loads((run / "check-row.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    m = re.search(r"\breviewer (\S+) via\b", str(row.get("runner") or "") if isinstance(row, dict) else "")
    return m.group(1) if m else ""


def review_findings(run: Path, chosen: list[str] | None) -> tuple[list[dict], int]:
    """The findings of an agentic review's directory that `ingest` records, and how many the reply lists.

    Each keeps its position in the reply's findings as `n`, the index `--matches` uses, so naming a subset with `--finding` does not renumber the rest.
    """
    data = json.loads((run / "findings.json").read_text(encoding="utf-8"))
    listed = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(listed, list):
        raise ValueError(f"{run / 'findings.json'} holds no findings array")
    ids = [str(f.get("id") or "").strip() if isinstance(f, dict) else "" for f in listed]
    unknown = [c for c in dict.fromkeys(chosen or []) if c not in ids]
    if unknown:
        raise ValueError(f"--finding names {', '.join(unknown)}, which the review does not list "
                         f"(it lists {', '.join(i for i in ids if i) or 'none'})")
    reviewer = reviewer_of(run)
    found = []
    for n, f in enumerate(listed):
        if not isinstance(f, dict) or (chosen and ids[n] not in chosen):
            continue
        quote, source = str(f.get("quote") or "").strip(), str(f.get("file") or "").strip()
        label = (str(f.get("claim") or "").strip() or str(f.get("why") or "").strip()
                 or f"finding {ids[n] or n}")
        extra = {"review": run.as_posix(), "review_finding": ids[n] or f"#{n}"}
        if source:
            extra["sources"] = [source]
            if quote and Path(source).is_file():
                # Informational, as on `open`: whether the quoted span is in the
                # file in this tree today, not whether the finding is right.
                extra["anchor_at_open"] = verdict(quote, source_text([source]))
        if reviewer:
            extra["opened_by"] = reviewer
        severity = str(f.get("severity") or "").strip().lower()
        found.append({"n": n, "label": label, "quote": quote, "extra": extra,
                      "severity": severity if severity in SEVERITIES else "medium"})
    return found, len(listed)


def run_findings(run: Path, chosen: list[str] | None) -> tuple[list[dict], int, bool]:
    """The findings a run directory holds, how many it lists, and whether it is an agentic review."""
    if (run / "defects.json").is_file():
        if chosen:
            raise ValueError("--finding names an agentic review's findings; a deep-review run "
                             "(defects.json) is ingested whole")
        data = json.loads((run / "defects.json").read_text(encoding="utf-8"))
        issues = data.get("issues", [])
        found = [{"n": n, "label": d["label"], "severity": d.get("severity", "medium"),
                  "quote": next((i["quote"] for i in issues
                                 if i["n"] in d.get("members", [])), "")}
                 for n, d in enumerate(data["defects"])]
        return found, len(found), False
    if (run / "findings.json").is_file():
        found, count = review_findings(run, chosen)
        return found, count, True
    if (run / "report.md").is_file():
        raise ValueError(f"{run} has a report and no findings.json: the reviewer's text was withheld "
                         "from the tracked directory, so open each supported finding with "
                         "defect_ledger.py open after reading what its words disclose")
    raise ValueError(f"{run} holds neither defects.json (a deep-review run) nor findings.json "
                     "(an agentic review's directory under ai/reviews/)")


def cmd_ingest(a) -> int:
    path = ledger_path(a.ledger)
    run = Path(a.run)
    chosen = getattr(a, "findings", None)
    try:
        found, count, review = run_findings(run, chosen)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cannot read the run: {exc}; ledger unchanged", file=sys.stderr)
        return 2
    rows = load(path)
    try:
        if getattr(a, "matches", None):
            seen = supplied_matches(a.matches, rows, count)
        elif review and rows:
            # The model matcher is a paid call that an installed project does
            # not ship, and the agent ingesting a review it has just read can say
            # which findings are already rows.
            raise ValueError("an agentic review's findings are matched by whoever ingests them: "
                             "name the ones already in the ledger with --matches, by their position "
                             "in the reply's findings (an empty array states that none is)")
        else:
            seen = match(rows, found, key()) if rows else {}
        stray = sorted(set(seen) - {f["n"] for f in found})
        if stray:
            raise ValueError(f"--matches names finding position(s) {stray} that --finding did not select")
    except (OSError, ValueError, ImportError) as exc:
        print(f"cannot match findings: {exc}; ledger unchanged", file=sys.stderr)
        return 2
    by_id = {r["id"]: r for r in rows}
    today = f"{date.today():%Y-%m-%d}"
    recurred, closed_now, added, mapped = [], [], [], []

    for f in found:
        rid = seen.get(f["n"])
        if rid:
            r = by_id[rid]
            r.setdefault("runs", []).append(run.name)
            # A finding raised again after anyone claimed it was handled is a
            # recurrence, and that includes a verified row: the recheck said the
            # failure was gone and a later reviewer found it anyway. The
            # verification events stay on the row, because the record of a check
            # that turned out to be wrong is exactly what a reader needs.
            if r["state"] in ("fixed", "closed", "verified"):
                r.setdefault("previous_states", []).append(r["state"])
                r["state"] = "recurred"
                note_recurrence(r, run=run.name, date=today)
                recurred.append(rid)
        else:
            rid = next_id(rows, path)
            rows.append({"id": rid, "label": f["label"], "severity": f["severity"],
                         "quote": f["quote"], "state": "open", "first_seen": today,
                         "runs": [run.name], "note": ""})
            rows[-1].update(f.get("extra") or {})
            by_id[rid] = rows[-1]; added.append(rid)
        if review:
            mapped.append(f"{f['extra']['review_finding']} -> {rid}")

    # Anything claimed fixed that this run did NOT raise is closed. This is the
    # legacy observation, and it is deliberately weaker than verification: a
    # sweep that looked at other things and did not complain has not rechecked
    # anything. It touches `fixed` only, so an unrelated later sweep can neither
    # create a verification nor quietly take one away. An agentic review
    # examined one candidate, not the work the ledger covers, so it closes
    # nothing by absence.
    raised = set(seen.values()) | set(added)
    for r in rows if not review else []:
        if r["state"] == "fixed" and r["id"] not in raised:
            r["state"] = "closed"; r["runs"].append(f"{run.name}:absent")
            r.setdefault("previous_states", []).append("fixed")
            closed_now.append(r["id"])
    save(rows, path)
    print(f"ingested {run.name}: {len(found)} {'finding(s)' if review else 'defects'}, "
          f"{len(added)} new, {len(seen)} matched")
    if mapped:
        print(f"  rows    : {', '.join(mapped)}")
    if review and len(found) < count:
        print(f"  not ingested: {count - len(found)} finding(s) --finding did not name; they stay in "
              "the review's report")
    if review:
        print("  an agentic review examines one candidate, so it closed nothing by absence")
    if closed_now:
        print(f"  closed  : {', '.join(closed_now)}")
    if recurred:
        print(f"  RECURRED: {', '.join(recurred)}")
    return 0


def cmd_verify(a) -> int:
    """Record one targeted recheck of a defect against its original diagnosis.

    This writes a technical record and nothing else.
    It is not acceptance, it does not authorize publication or reliance, and a model may perform and record it as long as that is visible in the row.
    """
    path = ledger_path(a.ledger)
    try:
        ev = load_evidence(a.evidence, a.id)
    except EvidenceError as e:
        print(str(e), file=sys.stderr)
        print("nothing was written: the ledger is unchanged", file=sys.stderr)
        return 2
    rows = load(path)
    row = next((r for r in rows if r["id"] == a.id), None)
    if row is None:
        print(f"no ledger entry for {a.id}: the ledger is unchanged", file=sys.stderr)
        return 2

    before = row.get("state", "open")
    faulty = str(row.get("faulty_default") or "").strip()
    objects = row_objects(row)
    covered = coverage_rounds(row)[0] if objects else set()
    after, decision, reasons = decide(before, ev, faulty, objects, covered)
    flag, basis = self_check(ev)
    event = {
        "recorded": datetime.now().astimezone().isoformat(timespec="seconds"),
        "outcome": ev["recheck"]["outcome"].strip().lower(),
        "decision": decision,
        "state_before": before,
        "state_after": after,
        "self_check": flag,
        "self_check_basis": basis,
        "reasons": reasons,
        "evidence_file": str(a.evidence),
        "evidence": ev,
    }
    resolution = resolve_later_run(ev, faulty)
    if resolution:
        # What the stated later run resolved to in this tree, or why it did not,
        # kept on the event so a later reader sees what the decision rested on.
        event["later_run_resolution"] = resolution
    covered_now = covers(ev, faulty, objects)
    if covered_now:
        # The listed object this complete passing recheck covers, as the row
        # spells it; the row's coverage is counted from these.
        event["covers"] = covered_now
    row.setdefault("verifications", []).append(event)
    if after != before:
        row.setdefault("previous_states", []).append(before)
        row["state"] = after
    save(rows, path)

    acc, lim = ev["accountability"], ev["limits"]
    print(f"{a.id}: {decision} (state {before} -> {row['state']})")
    print(f"  recheck   : {event['outcome']} against {ev['recheck']['criterion']}")
    print(f"  checker   : {acc['checker']} ({acc['checker_kind']}), "
          f"recorded by {acc['recorder']} ({acc['recorder_kind']})")
    print(f"  self-check: {'YES' if flag else 'no'} ({basis})")
    print(f"  limits    : adequacy {lim['adequacy']}; unresolved contrary "
          f"evidence {lim['unresolved_contrary_evidence']}")
    later = ev["recheck"].get("later_run")
    if later:
        print(f"  later run : {later_run_record(later)} under instruction version "
              f"{later['instruction_version']}")
    if resolution:
        print("  resolves  : " + (f"trace record {resolution['record']}, commit {resolution['commit'][:12]}"
                                  if not resolution["problems"] else
                                  "no; " + "; ".join(resolution["problems"])))
    if objects:
        print(f"  objects   : {coverage_phrase(row)}")
    for r in reasons:
        print(f"  because   : {r}")
    print(f"  events on this row: {len(row['verifications'])}")
    print("  This is a technical record. It is not acceptance, and it does not "
          "authorize publication or reliance.")
    return 0


def stated_objects(values: list[str] | None) -> list[str]:
    """The objects as given, stripped, without blanks, each once under its compared name."""
    out, seen = [], set()
    for value in values or []:
        text = str(value or "").strip()
        if text and object_name(text) not in seen:
            seen.add(object_name(text))
            out.append(text)
    return out


def open_finding(rows: list[dict], label: str, severity: str, by: str = "", quote: str = "",
                 sources: list[str] | None = None, note: str = "", affected: list[str] | None = None,
                 faulty_default: str | None = None, anchor: str | None = None,
                 extra: dict | None = None, objects: list[str] | None = None,
                 ledger: Path | str | None = None) -> dict:
    """Append the row `open` writes and return it. The caller validates label and severity.
    `ledger` is the file the rows come from, whose project setting names the id's prefix.

    `trace_record.py accept --decision return` calls this too, so a person's return opens the
    finding in the same shape a reader's objection would, with the objection as the label.
    """
    by = (by or "").strip() or "not stated"
    new = {"id": next_id(rows, ledger), "label": label.strip(), "severity": severity, "quote": (quote or "").strip(),
           "state": "open", "first_seen": f"{date.today():%Y-%m-%d}",
           "runs": [f"manual:{by}"], "note": note or "", "opened_by": by}
    if sources:
        new["sources"] = list(sources)
    if affected:
        new["affected"] = list(affected)
    listed = stated_objects(objects)
    if listed:
        new["objects"] = listed
    if faulty_default:
        new["faulty_default"] = faulty_default
    if anchor is not None:
        # Informational only: whether the quoted span is in the named source
        # today. It says nothing about whether the finding is right.
        new["anchor_at_open"] = anchor
    if extra:
        new.update(extra)
    rows.append(new)
    return new


def cmd_open(a) -> int:
    """Record a finding that did not come from a review run.

    Findings arrive from outside the review loop too: an agent notices something while editing, a reader objects, a coauthor's comment names a defect.
    Without a way in, those findings live in chat and are never rechecked, and the ledger would only ever close the loop on what a panel happened to raise.
    The row has the same shape as an ingested one, with a `manual:<name>` run marker in place of a run directory, so every later command treats it alike.
    Nothing is written until every input has been checked, so a rejected call leaves the ledger byte-identical.
    """
    if a.severity not in SEVERITIES:
        print(f"severity must be one of {', '.join(SEVERITIES)}, not {a.severity!r}",
              file=sys.stderr)
        print("nothing was written: the ledger is unchanged", file=sys.stderr)
        return 2
    if not a.label.strip():
        print("label must not be empty", file=sys.stderr)
        print("nothing was written: the ledger is unchanged", file=sys.stderr)
        return 2
    path = ledger_path(a.ledger)
    quote = (a.quote or "").strip()
    anchor = None
    if a.source and quote:
        try:
            anchor = verdict(quote, source_text(a.source))
        except OSError as exc:
            print(f"cannot read source: {exc}", file=sys.stderr)
            print("nothing was written: the ledger is unchanged", file=sys.stderr)
            return 2
    rows = load(path)
    new = open_finding(rows, a.label, a.severity, by=a.by, quote=quote, sources=a.source,
                       note=a.note, affected=a.affected, faulty_default=a.faulty_default,
                       anchor=anchor, objects=getattr(a, "objects", None), ledger=path)
    rid, by = new["id"], new["opened_by"]
    save(rows, path)
    print(rid)
    detail = f"opened {rid} ({a.severity}) by {by}: {new['label']}"
    if anchor is not None:
        detail += f"; quote {anchor} in the given source"
    if new.get("objects"):
        detail += (f"; it reaches {len(new['objects'])} object(s), each verified by its own "
                   "passing recheck")
    print(detail, file=sys.stderr)
    return 0


def rechecked(row: dict) -> bool:
    """Whether any recorded event on the row states an outcome. An event with
    outcome `not stated` is kept, but it is not a recheck that happened, so it
    never clears a row from what E is waiting on."""
    return any(isinstance(e, dict) and str(e.get("outcome", "")).strip().lower() in OUTCOMES
               for e in (row.get("verifications") or []))


# The marker `mark --state closed` adds to a row's runs, after the
# `manual:<name>` marker `open` writes; a sweep's closure is `<run>:absent`.
HAND_CLOSED = ":closed"


def closure_basis(row: dict) -> str:
    """How a row's latest closure came about, read from its runs, newest first.

    `sweep` when it is a later sweep's absence (`<run>:absent`), `hand` when it is `mark --state closed` (`manual:<name>:closed`), and `not stated` when the runs record neither, as on a row closed before the kit said how.
    """
    runs = row.get("runs") if isinstance(row, dict) else None
    for run in reversed(runs if isinstance(runs, list) else []):
        if not isinstance(run, str):
            continue
        if run.endswith(":absent"):
            return "sweep"
        if run.startswith("manual:") and run.endswith(HAND_CLOSED):
            return "hand"
    return "not stated"


def closed_without_recheck(row: dict) -> bool:
    """A closed row that no sweep closed and no recheck reached: closed by hand, or closed before the kit said how.

    `closed` is what a later sweep observes, so a closure said by hand is a repair claim and stays in what E is waiting on, as a fixed row does, until a recheck reaches it (each listed object, on a row that lists objects).
    """
    if not isinstance(row, dict) or row.get("state") != "closed" or closure_basis(row) == "sweep":
        return False
    return not rechecked(row) or bool(row_objects(row) and object_coverage(row)[1])


def outstanding_recheck(row: dict) -> str | None:
    """A failed targeted recheck remains outstanding until a verifying event.

    Repair claims and later sweeps are not rechecks. Read the event history so
    a row marked fixed by an older version does not conceal an outstanding fail.
    """
    events = row.get("verifications", [])
    if not isinstance(events, list):
        raise ValueError(f"{row.get('id')}: verifications must be a list")
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if event.get("decision") == "verified":
            return None
        if event.get("outcome") == "fail":
            return "recurred" if event.get("decision") == "recurred" else "unresolved"
    return None


def cmd_mark(a) -> int:
    if a.state == "verified":
        print("mark cannot set 'verified': that state stands for a recorded "
              "recheck, so it needs evidence.", file=sys.stderr)
        print("Use: defect_ledger.py verify <ID> --evidence <file.json>",
              file=sys.stderr)
        return 2
    if a.state is not None and a.state not in STATES:
        print(f"mark cannot set {a.state!r}: the states are {', '.join(STATES)}",
              file=sys.stderr)
        return 2
    path = ledger_path(a.ledger)
    rows = load(path)
    hit = [r for r in rows if r["id"] in a.id]
    if not hit:
        sys.exit(f"no ledger entry for {a.id}")
    # The ids that match are marked; every one that matches nothing is named,
    # so a typo is seen rather than dropped (AH-161).
    known = {r["id"] for r in rows}
    unmatched = [i for i in dict.fromkeys(a.id) if i not in known]
    fixer = (getattr(a, "by", "") or "").strip()
    claims_fix = bool(a.fixed or a.state == "fixed")
    for r in hit:
        # Without --fixed or --state the row keeps its state: a note added to a
        # fixed, closed or verified row must not reopen it.
        new = "fixed" if a.fixed else a.state or r.get("state", "open")
        pending = outstanding_recheck(r)
        # A fixed claim names a new correction when a failed recheck is
        # outstanding or the row was not already fixed. Its implementer and
        # action are then recorded together, so neither is left over from the
        # correction before it (AH-149).
        new_correction = claims_fix and (bool(pending) or r.get("state") != "fixed")
        if pending and (a.fixed or a.state):
            r.setdefault("repair_claims", []).append({
                "state": new, "note": a.note or "", "by": fixer or "not stated",
                "recorded": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            print(f"{r['id']}: recorded repair claim {new}; {pending} until a passing recheck verifies it")
            new = pending
        elif new == "closed" and r.get("state") != "closed":
            # `closed` names a later sweep's observation. A closure said by hand
            # is a repair claim: who said it, when and why, with a marker in the
            # runs so the latest closure can be told from a sweep's, and the row
            # stays in what E is waiting on until a recheck reaches it.
            who = fixer or "not stated"
            r.setdefault("repair_claims", []).append({
                "state": "closed", "note": a.note or "", "by": who,
                "recorded": datetime.now().astimezone().isoformat(timespec="seconds"),
                "basis": "closed by hand, not by a sweep, with no recheck",
            })
            r.setdefault("runs", []).append(f"manual:{who}{HAND_CLOSED}")
            print(f"{r['id']}: recorded closure by hand as a repair claim; it stays in what E is "
                  "waiting on until a recheck reaches it")
        if (new == "recurred") != (r.get("state") == "recurred"):
            # A recurrence said by hand, or a row leaving `recurred` (one that
            # recurred before the notes existed has none), restarts the object
            # round here, so the next correction is rechecked on every object.
            note_recurrence(r, by=fixer or "not stated",
                            recorded=datetime.now().astimezone().isoformat(timespec="seconds"))
        if new != r.get("state"):
            r.setdefault("previous_states", []).append(r.get("state", "open"))
        r["state"] = new
        if a.note:
            r["note"] = a.note
        if a.affected:
            r["affected"] = list(a.affected)
        if stated_objects(getattr(a, "objects", None)):
            # The list is replaced, as `affected` is. A verified row keeps its
            # state; `check` says which of the objects its verification did
            # not reach.
            r["objects"] = stated_objects(a.objects)
        if a.faulty_default:
            r["faulty_default"] = a.faulty_default
        if claims_fix:
            # Who made the fix and what they said it was, kept apart from the
            # row's note, so a recheck can name the correction and its
            # implementer without reading bookkeeping as a claim of repair.
            # This holds for `--state fixed` and while a failed recheck is
            # outstanding, where the row keeps its failure state.
            if new_correction:
                r.pop("fixed_by", None)
                r.pop("fix_note", None)
            if fixer:
                r["fixed_by"] = fixer
            if a.note:
                r["fix_note"] = a.note
    save(rows, path)
    for r in hit:
        print(f"marked {r['id']} as {r['state']}")
    for i in unmatched:
        print(f"no ledger entry for {i}: not marked", file=sys.stderr)
    return 0


def _when(stamp) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


def current_correction(row: dict) -> tuple[str, str]:
    """The corrective action and the implementer of the correction a recheck now examines.

    `mark` records them as `fix_note` and `fixed_by` with every fixed claim.
    A fixed repair claim written before AH-149 left those fields as the earlier correction had them, so the latest such claim is read instead while no verifying event came after it.
    A claim for any other state is not a correction and is never read as one.
    """
    action = (row.get("fix_note") or "").strip()
    implementer = (row.get("fixed_by") or "").strip()
    fixed = [c for c in (row.get("repair_claims") or [])
             if isinstance(c, dict) and c.get("state") == "fixed"]
    if fixed:
        latest, made = fixed[-1], _when(fixed[-1].get("recorded"))
        after = [_when(e.get("recorded")) for e in (row.get("verifications") or [])
                 if isinstance(e, dict) and e.get("decision") == "verified"]
        try:
            superseded = made is not None and any(t is not None and t >= made for t in after)
        except TypeError:
            superseded = False
        if not superseded:
            action = (latest.get("note") or "").strip()
            by = (latest.get("by") or "").strip()
            implementer = "" if placeholder(by) else by
    return action or NOT_STATED, implementer or NOT_STATED


def draft_recheck(row: dict, a) -> dict:
    """The evidence file a checker confirms, built from what is already known.

    Mechanical fields come from the row and the tree; judgment fields come from
    the checker's words or read `not stated`. Nothing here decides anything:
    the draft is printed back, the checker confirms or restates it, and only
    then does `verify` record it.
    """
    quote = (row.get("quote") or "").strip()
    diagnosis = row.get("label", "").strip()
    if quote:
        diagnosis += f' Quote: "{quote}"'
    sources = row.get("sources") or []
    objects = row_objects(row)
    obj = (a.object or "").strip()
    if not obj and objects:
        # Each listed object is rechecked in its own event. With one listed
        # the draft names it; with several, the checker says which, and an
        # unnamed one reads `not stated`, which does not verify.
        obj = objects[0] if len(objects) == 1 else NOT_STATED
    elif not obj:
        obj = sources[0] if sources else NOT_STATED
    # A file in the tree is hashed from its content. An object that is not one
    # (a table or a section) takes the hash the checker states: the commit of
    # the correction, or the sha256 of the file that builds it.
    obj_hash, hash_basis = NOT_STATED, ""
    stated_hash = (getattr(a, "object_hash", "") or "").strip()
    if stated_hash:
        obj_hash, hash_basis = stated_hash, "stated by the checker"
    elif obj != NOT_STATED and Path(obj).is_file():
        import hashlib
        obj_hash = hashlib.sha256(Path(obj).read_bytes()).hexdigest()
        hash_basis = "sha256 of the file's content"
    fix_action, fix_by = current_correction(row)
    action = (a.action or "").strip() or fix_action
    implementer = (a.implementer or "").strip() or fix_by
    checker = a.checker.strip()
    stated = lambda v: (v or "").strip() or NOT_STATED  # noqa: E731
    method, outcome = stated(a.method), (a.outcome or NOT_STATED).strip().lower()
    evidence_ref = stated(a.evidence_ref)
    # Adequacy is the checker's judgment, so it is never inferred from the
    # other fields: unstated, it reads unknown, and the row cannot verify.
    adequacy = (a.adequacy or "").strip().lower() or "unknown"
    ev = {
        "target": {"defect_id": row["id"], "original_diagnosis": diagnosis or NOT_STATED,
                   "corrected_object": obj, "object_hash": obj_hash},
        "response": {"corrective_action": action, "implementer": implementer},
        "recheck": {"criterion": (a.criterion or "").strip() or row.get("label", "").strip() or NOT_STATED,
                    "criterion_basis": "restated by the checker" if (a.criterion or "").strip()
                    else "proposed from the row's label, shown to the checker",
                    "method": method, "outcome": outcome, "evidence_ref": evidence_ref,
                    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds")},
        "accountability": {"checker": checker, "checker_kind": "human" if a.person else "model",
                           "recorder": (a.recorder or "").strip() or NOT_STATED,
                           "recorder_kind": a.recorder_kind},
        "limits": {"adequacy": adequacy, "limitations": stated(a.limitations),
                   "unresolved_contrary_evidence": stated(a.contrary)},
    }
    if hash_basis:
        ev["target"]["object_hash_basis"] = hash_basis
    if a.authorized_by:
        ev["accountability"]["authorized_by"] = a.authorized_by.strip()
    if a.later_run:
        ev["recheck"]["later_run"] = {"record": a.later_run.strip(),
                                      "instruction_version": stated(a.instruction_version)}
    if a.said:
        ev["words"] = {"checker_said": a.said, "kept": "verbatim, as said to the recorder"}
    return ev


def cmd_draft_recheck(a) -> int:
    """Write a recheck's evidence file from the row and the checker's words, and print it back."""
    if bool(a.person) == bool(a.agent):
        print("declare the checker's kind: --person or --agent (one of them)", file=sys.stderr)
        return 2
    if not (a.checker or "").strip():
        print("name the checker with --checker", file=sys.stderr)
        return 2
    if a.outcome and a.outcome.strip().lower() not in OUTCOMES:
        print(f"--outcome must be one of {OUTCOMES}, or left out when the checker stated none",
              file=sys.stderr)
        return 2
    if a.adequacy and a.adequacy.strip().lower() not in ADEQUACY:
        print(f"--adequacy must be one of {ADEQUACY}", file=sys.stderr)
        return 2
    path = ledger_path(a.ledger)
    row = next((r for r in load(path) if r["id"] == a.id), None)
    if row is None:
        print(f"no ledger entry for {a.id}", file=sys.stderr)
        return 2
    ev = draft_recheck(row, a)
    out = Path(a.out) if a.out else Path("workspace/record-staging") / f"recheck-{a.id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    state = row.get("state", "open")
    objects = row_objects(row)
    covered = coverage_rounds(row)[0] if objects else set()
    after, decision, reasons = decide(state, ev, str(row.get("faulty_default") or "").strip(),
                                      objects, covered)
    flag, basis = self_check(ev)
    acc, lim, rc = ev["accountability"], ev["limits"], ev["recheck"]
    print(f"draft recheck of {a.id} ({state}), to confirm before it is recorded")
    print(f"  diagnosis : {' '.join(ev['target']['original_diagnosis'].split())}")
    print(f"  object    : {ev['target']['corrected_object']} ({ev['target']['object_hash'][:12]})")
    if not placeholder(ev["target"]["corrected_object"]) and placeholder(ev["target"]["object_hash"]):
        print("              the object is not a file in the tree: state its hash with --object-hash "
              "(the commit of the correction, or the sha256 of the file that builds it)")
    if objects:
        print(f"  objects   : the row lists {len(objects)} ({', '.join(objects)}); "
              f"before this recheck, {coverage_phrase(row)}")
        if placeholder(ev["target"]["corrected_object"]):
            print("              name the one rechecked with --object; each is rechecked in its own event")
    print(f"  fix       : {ev['response']['corrective_action']}, by {ev['response']['implementer']}")
    print(f"  criterion : {rc['criterion']} ({rc['criterion_basis']})")
    print(f"  compared  : {rc['method']}")
    print(f"  found     : {rc['outcome']}; evidence {rc['evidence_ref']}")
    print(f"  checker   : {acc['checker']} ({acc['checker_kind']}); self-check "
          f"{'YES' if flag else 'no'} ({basis})")
    print(f"  limits    : adequacy {lim['adequacy']}; not covered: {lim['limitations']}; "
          f"contrary evidence: {lim['unresolved_contrary_evidence']}")
    if "later_run" in rc:
        print(f"  later run : {rc['later_run']['record']} under {rc['later_run']['instruction_version']}")
    if a.said:
        print(f"  said      : {a.said}")
    would = f"{decision}, state {state} -> {after}"
    print(f"  recording it would read: {would}")
    for r in reasons:
        print(f"    because {r}")
    print(f"\nWritten to {out}. Read it back to the checker; when they confirm, record it with:")
    print(f"  python3 scripts/defect_ledger.py verify {a.id} --evidence {out}")
    print("A field that reads 'not stated' is recorded that way; nobody is asked for more than they said.")
    return 0


def cmd_status(a) -> int:
    rows = load(ledger_path(a.ledger))
    if not rows:
        print("ledger empty"); return 0
    src = source_text(a.source) if a.source else None
    print(f"{'id':<8} {'state':<11} {'sev':<7} {'anchor':<8} {'checks':<7} label")
    for r in sorted(rows, key=lambda x: (ORDER.get(x["state"], 9), x["id"])):
        anchor = ""
        if src is not None and r.get("quote"):
            anchor = "present" if verdict(r["quote"], src) != "invented" else "gone"
        events = r.get("verifications", [])
        checks = f"{len(events)}{'*' if events and events[-1].get('self_check') else ''}" if events else ""
        print(f"{r['id']:<8} {r['state']:<11} {r['severity']:<7} {anchor:<8} "
              f"{checks:<7} {r['label'][:58]}")
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print("\n" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    events = [e for r in rows for e in r.get("verifications", [])]
    selfs = sum(1 for e in events if e.get("self_check"))
    print(f"{len(events)} recheck event(s) recorded, {selfs} of them self-checks (*)")
    listing = [r for r in rows if row_objects(r)]
    if listing:
        print("\nrows that list the objects the defect reaches, each rechecked in its own event:")
        for r in sorted(listing, key=lambda x: (ORDER.get(x["state"], 9), x["id"])):
            print(f"  {r['id']:<8} {r['state']:<11} {coverage_phrase(r)}")
    return 0


def cmd_check(a) -> int:
    """Fail on a correction that did not hold or was checked and found not to hold; refuse a ledger that cannot be trusted.

    Every row is validated before any finding is emitted, so a later bad row cannot leave partial output that a caller mistakes for completion.
    Validation is structural: a string id and state, a state from the vocabulary, and a verified row that can point at the recheck event which verified it.
    A verified row with no such event is the one shape this file exists to prevent, a claim of correction with nothing behind it, so it is a malformed ledger rather than a finding.
    A ledger file that does not exist is an input error, exit 2, rather than an empty ledger that passes (AH-160); `open` and `ingest` still start a new one.
    A verified row whose faulty default its latest verifying event does not cover with a later run is reported and does not change the exit code (AH-159): the verification event stays as it was recorded, and the report says what it does not cover.
    Rows that list objects are reported the same way, without changing the exit code: a row partly rechecked says how many of its objects are rechecked and which still wait, and a verified row whose verification did not reach an object listed on it (one added afterwards) says which.
    Two more are reported as information and never change the exit code: a verified row whose later run, named on its verifying event, does not resolve in the working tree (verified before rc22, or the record or commit gone since), and a closed row no sweep closed and no recheck reached, closed by hand or before the kit said how.
    """
    uncovered, partly, unreached, later_gone, hand_closed = [], [], [], [], []
    try:
        recurred, unresolved = [], []
        path = ledger_path(a.ledger)
        if not path.exists():
            raise ValueError(f"no ledger file at {path}: name the ledger with --ledger or "
                             f"${LEDGER_ENV}, or start one with open or ingest")
        for number, row in enumerate(load(path), start=1):
            if not isinstance(row, dict):
                raise ValueError(f"row {number}: not a JSON object")
            missing = [f for f in ("id", "state") if not isinstance(row.get(f), str)]
            if missing:
                raise ValueError(f"row {number}: no string {', '.join(missing)}")
            rid, state = row["id"], row["state"]
            if state not in STATES:
                raise ValueError(f"row {number} ({rid}): state {state!r} is not one of "
                                 f"{', '.join(STATES)}")
            if state == "verified":
                events = row.get("verifications")
                verifying = [e for e in events if isinstance(e, dict)
                             and e.get("decision") == "verified"] if isinstance(events, list) else []
                if not verifying:
                    raise ValueError(f"row {number} ({rid}): state verified with no "
                                     "recheck event whose decision is verified")
                default = str(row.get("faulty_default") or "").strip()
                if default and not later_run_covers(verifying[-1]):
                    label = row.get("label")
                    uncovered.append({"id": rid, "faulty_default": default,
                                      "label": label if isinstance(label, str) else ""})
                elif default:
                    # The row keeps its state; what its later run does not
                    # resolve to here is reported.
                    found = resolve_later_run(verifying[-1].get("evidence") or {}, default)
                    if found and found["problems"]:
                        later_gone.append({"id": rid, "faulty_default": default,
                                           "problems": found["problems"]})
            if closed_without_recheck(row):
                claims = [c for c in row.get("repair_claims") or []
                          if isinstance(c, dict) and c.get("state") == "closed"]
                basis = closure_basis(row)
                hand_closed.append({"id": rid, "basis": basis,
                                    "by": str(claims[-1].get("by") or "not stated")
                                    if claims and basis == "hand" else "not stated"})
            objects = row_objects(row)
            if objects:
                done, waiting = object_coverage(row)
                if state == "verified" and waiting:
                    unreached.append({"id": rid, "objects": len(objects), "waiting": waiting})
                elif state != "verified" and done and waiting:
                    partly.append({"id": rid, "state": state, "rechecked": len(done),
                                   "objects": len(objects), "waiting": waiting})
            failure_state = outstanding_recheck(row) or state
            if failure_state in ("recurred", "unresolved"):
                missing = [f for f in ("severity", "label")
                           if not isinstance(row.get(f), str)]
                if missing:
                    raise ValueError(f"row {number} ({rid}): no string {', '.join(missing)}")
                (recurred if failure_state == "recurred" else unresolved).append(
                    {f: row[f] for f in ("id", "severity", "label")})
        status = "recurred" if recurred else "unresolved" if unresolved else "ok"
        result = {"status": status, "recurred": recurred, "unresolved": unresolved}
        if uncovered:
            result["verified_without_later_run"] = uncovered
        if partly:
            result["partly_rechecked"] = partly
        if unreached:
            result["verified_without_every_object"] = unreached
        if later_gone:
            result["verified_on_a_later_run_that_does_not_resolve"] = later_gone
        if hand_closed:
            result["closed_without_recheck"] = hand_closed
        code = 1 if recurred or unresolved else 0
    except (OSError, ValueError) as exc:
        result = {"status": "error", "reason": str(exc)}
        code = 2
    if getattr(a, "json", False):
        print(json.dumps(result))
        return code
    if code == 2:
        print(f"ERROR {result['reason']}", file=sys.stderr)
        return code
    for r in recurred:
        print(f"RECURRED {r['id']} ({r['severity']}): {r['label']}", file=sys.stderr)
    for r in unresolved:
        print(f"UNRESOLVED {r['id']} ({r['severity']}): {r['label']}", file=sys.stderr)
    for r in uncovered:
        print(f"NOT COVERED {r['id']}: verified, and its verifying event names no later run "
              f"for the faulty default {r['faulty_default']}; a later-run recheck would cover it",
              file=sys.stderr)
    for r in partly:
        print(f"PARTLY RECHECKED {r['id']} ({r['state']}): {r['rechecked']} of {r['objects']} objects "
              f"rechecked; waiting on {', '.join(r['waiting'])}", file=sys.stderr)
    for r in unreached:
        print(f"NOT COVERED {r['id']}: verified, and no passing recheck in its verification names "
              f"{', '.join(r['waiting'])} of the {r['objects']} objects it lists; a recheck of each "
              "would cover them", file=sys.stderr)
    for r in later_gone:
        print(f"LATER RUN UNRESOLVED {r['id']}: verified, and the later run its verification names "
              f"does not resolve here: {'; '.join(r['problems'])}; the row keeps its state",
              file=sys.stderr)
    for r in hand_closed:
        how = (f"closed by hand by {r['by']}" if r["basis"] == "hand"
               else "closed other than by a sweep (how is not recorded)")
        print(f"CLOSED WITHOUT RECHECK {r['id']}: {how}, with no recheck; a recheck would verify it",
              file=sys.stderr)
    if code:
        print(f"\n{len(recurred)} correction(s) did not hold, {len(unresolved)} "
              "defect(s) unresolved after a failed recheck", file=sys.stderr)
        return 1
    print("no recurrences, nothing unresolved")
    return 0


OBJECT_HELP = ("an output the defect reaches (a table, a file, a section); repeat the flag for each, "
               "and the row verifies only when each has its own passing recheck")


def add_ledger_flag(p) -> None:
    p.add_argument("--ledger", default=None,
                   help=f"ledger file (default: ${LEDGER_ENV}, else {DEFAULT_LEDGER})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("--run", required=True,
                                                 help="a deep-review run directory (defects.json), or an "
                                                      "agentic review's directory, ai/reviews/<run> (findings.json)")
    p.add_argument("--finding", dest="findings", action="append", default=None, metavar="ID",
                   help="for an agentic review: a finding to record, by the reviewer's id; repeat for each "
                        "supported finding (every one when none is named)")
    p.add_argument("--matches", help='a JSON file {"matches": [{"n": 0, "id": "AH-001"}]}, where n is the '
                   "zero-based position in the run's defects array (for an agentic review, its findings "
                   "array) and id an existing ledger id; "
                   "an empty array states that none matches an existing defect. No model call: "
                   "the tool checks ids and bounds, not whether a match is right")
    p.set_defaults(fn=cmd_ingest); add_ledger_flag(p)
    p = sub.add_parser("open", help="record a finding that did not come from a review run")
    p.add_argument("--label", required=True)
    p.add_argument("--severity", required=True, help=f"one of {', '.join(SEVERITIES)}")
    p.add_argument("--quote", default="", help="the span the finding objects to, if any")
    p.add_argument("--source", nargs="*", default=[],
                   help="files to ground the quote against; stored as anchor_at_open")
    p.add_argument("--by", default="", help="who raised the finding (default: not stated)")
    p.add_argument("--note", default="")
    p.add_argument("--affected", nargs="+", default=None,
                   help="trace records the defect touched (the outputs it reaches go in --object)")
    p.add_argument("--object", dest="objects", action="append", default=None, metavar="PATH",
                   help=OBJECT_HELP)
    p.add_argument("--faulty-default", dest="faulty_default", default=None,
                   help="the instruction file or default that produced the failure")
    p.set_defaults(fn=cmd_open); add_ledger_flag(p)
    p = sub.add_parser("mark"); p.add_argument("--id", nargs="+", required=True)
    p.add_argument("--fixed", action="store_true")
    p.add_argument("--by", default="", help="with --fixed or --state fixed: who made the fix, recorded as fixed_by; "
                   "with --state closed: who closed the row by hand, recorded on its repair claim")
    p.add_argument("--state", default=None, help="the state to set; omitted, the row keeps its state")
    p.add_argument("--note", default="")
    p.add_argument("--affected", nargs="+", default=None,
                   help="trace records the defect touched (the outputs it reaches go in --object)")
    p.add_argument("--object", dest="objects", action="append", default=None, metavar="PATH",
                   help=OBJECT_HELP + "; given here, the list replaces the row's")
    p.add_argument("--faulty-default", dest="faulty_default", default=None,
                   help="the instruction file or default that produced the failure")
    p.set_defaults(fn=cmd_mark); add_ledger_flag(p)
    p = sub.add_parser("draft-recheck",
                       help="write a recheck's evidence file from the row and the checker's words, and print it back")
    p.add_argument("id")
    p.add_argument("--checker", required=True, help="who rechecked: a person's name or a model id")
    p.add_argument("--person", action="store_true", help="the checker is a person")
    p.add_argument("--agent", action="store_true", help="the checker is a model; its recheck is advisory")
    p.add_argument("--authorized-by", dest="authorized_by", default="",
                   help="the named researcher who delegated the recheck to this checker, when one did; "
                        "the checker's own kind is still --person or --agent, because the evidence "
                        "records a human or a model")
    p.add_argument("--recorder", default="", help="the agent or person writing the file (default: not stated)")
    p.add_argument("--recorder-kind", dest="recorder_kind", choices=KINDS, default="model")
    p.add_argument("--said", default="", help="what the checker said, verbatim; kept on the event")
    p.add_argument("--criterion", default="", help="restate the criterion; else the row's label is proposed")
    p.add_argument("--method", default="", help="what the checker compared (default: not stated)")
    p.add_argument("--outcome", default="", help="pass or fail; left out when the checker stated none")
    p.add_argument("--evidence-ref", dest="evidence_ref", default="",
                   help="where the checker's evidence is (default: not stated)")
    p.add_argument("--limitations", default="", help="what the recheck did not look at (default: not stated)")
    p.add_argument("--contrary", default="",
                   help="unresolved contrary evidence, 'none' when the checker says so (default: not stated)")
    p.add_argument("--adequacy", default="", help=f"one of {', '.join(ADEQUACY)} (default: unknown)")
    p.add_argument("--object", default="",
                   help="the object this recheck examines: on a row that lists several, the one "
                        "rechecked (each in its own event; unnamed, it reads not stated); else the "
                        "corrected file, when the row names none")
    p.add_argument("--object-hash", dest="object_hash", default="",
                   help="the object's hash as the checker states it, for an object that is not a file in "
                        "the tree (a table or a section): the commit of the correction, or the sha256 of "
                        "the file that builds it; a file is otherwise hashed from its content")
    p.add_argument("--action", default="", help="the corrective action, when the row's note does not say")
    p.add_argument("--implementer", default="", help="who made the fix, when mark --fixed --by did not say")
    p.add_argument("--later-run", dest="later_run", default="",
                   help="a later trace record produced under the revised instruction, for a faulty default: "
                        "ai/records/<id>, which has to exist for the row to verify")
    p.add_argument("--instruction-version", dest="instruction_version", default="",
                   help="the commit of the revised instruction the later run ran under, which has to change "
                        "the faulty default, or the sha256 of the instruction's content at such a commit")
    p.add_argument("--out", default="", help="where to write the file (default: workspace/record-staging/recheck-<ID>.json)")
    p.set_defaults(fn=cmd_draft_recheck); add_ledger_flag(p)
    p = sub.add_parser("verify", help="record a targeted recheck; not acceptance")
    p.add_argument("id"); p.add_argument("--evidence", required=True)
    p.set_defaults(fn=cmd_verify); add_ledger_flag(p)
    p = sub.add_parser("status"); p.add_argument("--source", nargs="*", default=[])
    p.set_defaults(fn=cmd_status); add_ledger_flag(p)
    p = sub.add_parser("check"); p.set_defaults(fn=cmd_check); add_ledger_flag(p)
    p.add_argument("--json", action="store_true", help="emit the completed check as JSON")
    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
