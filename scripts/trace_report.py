#!/usr/bin/env python3
"""Governing-actions report: for each trace record and each TRACE principle, one of four states.

The workflow check validates records that exist. It never asked whether a
record should exist, so a project could run every check green having performed
none of the five governing actions. This report asks that question, one
trace record at a time, and answers it with one of four states:

  recorded         a record of the action exists and is joined to the work
  missing          the action applies here and no record is joined to it
  not-applicable   the action does not apply to this work, with the reason
  unknown          the report cannot tell, with the reason

Applicability is declared, never inferred from absence:

  T  every commit a machine executed since the install baseline needs a
     trace record, joined by a `Record:` line in the commit body; a commit
     with no execution trailer is a person's own, which Git already records
  R  every commit that changes a governed artifact needs a check row in its
     record whose artifact resolves or is declared; a recorded R says whether
     a row names the sources it compared, only a criterion, or neither, so a
     check that resolves an artifact alone is not read as review against sources
  A  work that others rely on, a commit touching a protected path, needs a
     recorded decision by an accepting actor: accept, return or refuse
  C  a commit touching a protected path needs a transition row that names it;
     recorded says which decision the row records, and flags a returned or
     refused object that is in paper/ anyway; the data files, shared-inventory
     files and snapshots no admitting intake row names are counted, one only a
     return or refuse row names is flagged as present anyway, and what is held
     in the incoming folder is counted apart as waiting, not as data
  E  a commit touching a standing instruction needs a default-adoption row
     that names it and a later run a declared observer looked at; a later
     run named with no observation is a bare link and reads missing. A
     commit whose message claims a fix of a ledger row someone claimed fixed
     (any state but open) is joined to that row as its correction, and needs
     a recorded recheck of it, of every object it lists, to read recorded; a
     fix claim is a fixing verb before the id (Fixes AH-001, also Fix,
     Fixed, Close, Closes, Closed, Correct, Corrects, Corrected, Resolve,
     Resolves, Resolved, with a list of ids after it), so a commit that only
     mentions the id is not joined, and a row a later sweep closed is never
     joined, since a sweep's closure waits on no recheck. When both apply,
     either missing reads missing. Ledger rows are also reported by state

Only "missing" counts against the project. The counting window is the install
baseline (`policy_epoch`), written once at setup and never advanced, so an
accepted commit never leaves it. Everything here is informational: the
workflow check reports it and never fails on it, because unperformed work is a
normal, publishable state whose only requirement is that it reads as such.

A recorded state says a record exists and is joined. It does not say the check
was adequate, the accepter independent, or the transition authorized in fact;
those stay recorded judgments, open to review.

    python3 scripts/trace_report.py            # table
    python3 scripts/trace_report.py --json     # the data the workflow check stores

stdlib only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import trace_record as hr  # noqa: E402
import defect_ledger as dl  # noqa: E402
from trace_defaults import POLICY_EPOCH  # noqa: E402

PRINCIPLES = ("T", "R", "A", "C", "E")
# The principle names since 12 October 2026 (DECISIONS.md); records keep the names they were written under.
NAMES = {"T": "track records", "R": "review against sources",
         "A": "accept independently", "C": "control transitions",
         "E": "evaluate corrections"}
STATES = ("recorded", "missing", "not-applicable", "unknown")
INSTRUCTION_PATHS = ("AGENTS.md", "CLAUDE.md", ".skills/", "ai/RECORD-CONVENTION.md", "WORKFLOW.md",
                     "ai/review-prose-rules.md")
# Artifacts whose change is research work a check should cover. Drafts and
# records of the process itself are not governed artifacts: a check row about
# a check row would be circular.
GOVERNED_DEFAULT = ("paper/", "sources/", "scripts/", "tests/", "presentations/", "empirics/")
LEDGER_STATES = ("open", "fixed", "closed", "verified", "recurred", "unresolved")
# The observer kinds `trace_record.py later-run` writes; a later_run with none is a bare link.
LATER_RUN_OBSERVERS = ("human", "agent", "delegated")
# Material an intake row should name: data files added under sources/ since the
# baseline, the files the shared inventory lists, and local snapshots. Notes and
# code are the project's own writing and are left out by suffix; a project may
# set its own list in ai/project.json under "data_suffixes".
DATA_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".parquet", ".feather", ".xlsx", ".xls",
                 ".dta", ".sav", ".rds", ".rdata", ".sqlite", ".db", ".zip", ".gz", ".pdf")
# The kit's own registries and inventory record material; they are not material.
KIT_SOURCES = ("sources/intake.jsonl", "sources/transitions.jsonl", "sources/shared-inventory.json")
SHARED_INVENTORY = "sources/shared-inventory.json"
SNAPSHOTS = "workspace/snapshots"
UNNAMED_EXAMPLES = 20


def _config_list(config: dict | None, key: str, default: tuple) -> tuple:
    if config and isinstance(config.get(key), list) and all(isinstance(x, str) for x in config[key]):
        return tuple(config[key])
    return default


def touches(files: list[str], prefixes: tuple) -> bool:
    return any(f == p.rstrip("/") or f.startswith(p) for f in files for p in prefixes)


def commits_since_baseline() -> list[dict]:
    """Every commit once; merge paths are compared with the first parent.

    The first parent is the branch receiving the merge. This includes both
    incoming changes and conflict resolutions, without dropping topic commits
    from the reporting window or counting a merge once per parent.
    """
    epoch = hr.project_epoch("policy_epoch", POLICY_EPOCH)
    log = hr.git("log", "--format=%x1e%H%x00%P%x00%s%x00%B%x00", "--name-only",
                 f"{epoch}..HEAD", check=True)
    rows = []
    for entry in log.split("\x1e"):
        if not entry.strip():
            continue
        sha, _, rest = entry.partition("\x00")
        parents, _, rest = rest.partition("\x00")
        subject, _, rest = rest.partition("\x00")
        body, _, files = rest.partition("\x00")
        if len(parents.split()) > 1:
            files = hr.git("diff", "--name-only", parents.split()[0], sha.strip(), check=True)
        paths = [ln.strip() for ln in files.splitlines() if ln.strip()]
        match = hr.RECORD_LINE_RE.search(body)
        rows.append({"commit": sha.strip(), "subject": subject, "files": paths,
                     "record": (hr.record_ref(match.group(1)) or None) if match else None,
                     "machine": hr.classify(body, exempt=False)[0] == "declared",
                     "message": body or subject})
    return rows


def record_checks(record: pathlib.Path) -> dict:
    """How many check rows the record has, how many lead somewhere, and of those how
    many name the sources they compared or, failing that, a stated criterion."""
    path = record / "checks.jsonl"
    total, usable, sourced, criterion = 0, 0, 0, 0
    if not path.exists():
        return {"total": 0, "usable": 0, "sourced": 0, "criterion_only": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("artifact"), str):
            continue
        total += 1
        if hr.resolve_artifact(entry["artifact"], entry.get("reference"), record) in ("resolved", "declared", "unavailable"):
            usable += 1
            if "sources" in entry and not hr.source_problems(entry["sources"]):
                sourced += 1
            elif hr.check_field_stated(entry.get("criterion")):
                criterion += 1
    return {"total": total, "usable": usable, "sourced": sourced, "criterion_only": criterion}


# What a recorded R rests on, from the best of its rows. The state stays recorded
# in every case; this says how much the record says about what was checked.
R_BASIS = ("names_a_source", "names_a_criterion_only", "resolves_an_artifact_only")
# How the table says each, of the commits counted.
R_BASIS_SAID = {"names_a_source": "with a row naming its sources",
                "names_a_criterion_only": "with a stated criterion and no source",
                "resolves_an_artifact_only": "on a resolved artifact alone"}


def review_basis(checks: dict) -> str:
    if checks.get("sourced"):
        return R_BASIS[0]
    if checks.get("criterion_only"):
        return R_BASIS[1]
    return R_BASIS[2]


def recheck_recorded(row: dict) -> bool:
    """Whether a ledger row's correction has a recorded recheck: an event stating
    an outcome, or a verification, and on a row that lists objects, each of them
    rechecked. A row with objects still waiting is not rechecked, as in the
    queue's E line; an event whose outcome is `not stated` is not a recheck."""
    if dl.row_objects(row) and dl.object_coverage(row)[1]:
        return False
    return row.get("state") == "verified" or dl.rechecked(row)


def ledger_rows(path: pathlib.Path) -> dict[str, dict]:
    """The ledger's readable rows by id; a line that is not a row is skipped,
    as `ledger_summary` counts it."""
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line) if line.strip() else None
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row.get("state") in LEDGER_STATES:
            rows[row["id"]] = row
    return rows


LEDGER_ID = r"[A-Z][A-Z0-9]*-\d+"
# A fix claim: a fixing verb, in any case, then one id or a list of them
# ("Fixes AH-001, AH-002 and AH-003"). An id the message only mentions, as an
# example or a reference, is not a claim that the commit corrects it.
FIX_CLAIM_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?i:fix(?:es|ed)?|close[sd]?|correct(?:s|ed)?|resolve[sd]?):?\s+"
    rf"((?:{LEDGER_ID})(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*|\s*/\s*){LEDGER_ID})*)(?![A-Za-z0-9_])")


def fix_claims(message: str) -> list[str]:
    """The ledger ids a commit message claims to fix, in order, each once."""
    return list(dict.fromkeys(i for m in FIX_CLAIM_RE.finditer(message or "")
                              for i in re.findall(LEDGER_ID, m.group(1))))


def swept_closed(row: dict) -> bool:
    """A row whose latest closure is a later sweep's observation: not a correction waiting on a recheck."""
    return row.get("state") == "closed" and dl.closure_basis(row) == "sweep"


def corrections_named(row: dict, ledger: dict[str, dict]) -> list[dict]:
    """The ledger rows a commit's message claims to fix that someone claimed fixed:
    the join from a correction to the row it corrects.

    Only a fix claim joins (Fix, Fixes, Fixed, Close, Closes, Closed, Correct,
    Corrects, Corrected, Resolve, Resolves or Resolved before the id), and a
    row a later sweep closed is never joined, since a sweep's closure waits on
    no recheck, as in `check` and the queue (rc22 release review, 96c230d F1, F2).
    """
    if not ledger:
        return []
    return [ledger[i] for i in fix_claims(row.get("message") or "")
            if i in ledger and ledger[i].get("state") != "open" and not swept_closed(ledger[i])]


def assess(row: dict, index: dict, covered: dict, protected: tuple, governed: tuple,
           ledger: dict[str, dict] | None = None) -> dict:
    """The five states for one commit, each with its reason."""
    files = row["files"]
    out = {}
    # The join `queue`, `brief` and `accept --record` make: a bare id is
    # ai/records/<id>, and this tree's absolute path is its relative one (AH-136).
    record = hr.record_path(row["record"]) if row["record"] else None
    exists = bool(record) and (record / "evidence.json").exists()
    if exists:
        out["T"] = {"state": "recorded", "why": f"Record: {row['record']}"}
    elif record:
        out["T"] = {"state": "missing", "why": f"names an absent record: {row['record']}"}
    elif not row.get("machine", True):
        # The same default reading as `backfill`'s none-declared: under the
        # rule a machine declares itself and a person types nothing.
        out["T"] = {"state": "not-applicable", "why": "a person's commit: Git records who, when and what"}
    else:
        out["T"] = {"state": "missing", "why": "no trace record named in the commit"}

    if not touches(files, governed):
        out["R"] = {"state": "not-applicable", "why": "changes no governed artifact"}
    elif not exists:
        out["R"] = {"state": "unknown", "why": "no record to read checks from"}
    else:
        checks = record_checks(record)
        if checks["usable"]:
            basis = review_basis(checks)
            out["R"] = {"state": "recorded", "basis": basis,
                        "why": f"{checks['usable']} of {checks['total']} check rows lead to an artifact; "
                               f"{checks['sourced']} name a source, {checks['criterion_only']} only a "
                               f"criterion"}
        elif checks["total"]:
            out["R"] = {"state": "missing", "why": "every check row names an absent artifact"}
        else:
            out["R"] = {"state": "missing", "why": "the record has no check rows"}

    # A record that exists is recorded whether or not the action was owed;
    # applicability only decides between missing and not applicable.
    relied_on = touches(files, protected) or row["commit"] in covered
    state = hr.commit_state_detail(row["commit"], index)
    if state in ("accepted-by-person", "accepted-delegated", "returned", "refused"):
        out["A"] = {"state": "recorded", "why": hr.STATE_LABELS[state]}
    elif not relied_on:
        out["A"] = {"state": "not-applicable", "why": "touches no protected path and no transition names it"}
    else:
        out["A"] = {"state": "missing", "why": hr.STATE_LABELS[state] + ", no accepting decision"}

    if row["commit"] in covered:
        # Recorded says a row exists; the decision it records is said with it,
        # and a returned or refused object found in paper/ anyway is flagged.
        here = covered[row["commit"]]
        why = "transition " + ", ".join(f"{t['id']} ({t['kind']}, {t.get('decision') or 'decision not stated'})"
                                        for t in here)
        flags = [flag for t in here for flag in hr.refused_yet_in_paper(t, [row["commit"]])]
        out["C"] = {"state": "recorded", "why": "; ".join([why, *flags])}
    elif not touches(files, protected):
        out["C"] = {"state": "not-applicable", "why": "touches no protected path"}
    else:
        out["C"] = {"state": "missing", "why": "no transition row names this commit"}

    if not touches(files, INSTRUCTION_PATHS):
        out["E"] = {"state": "not-applicable", "why": "changes no standing instruction"}
    else:
        rows_here = [t for t in covered.get(row["commit"], []) if t["kind"] == "default-adoption"]
        # A returned or refused adoption adopted nothing, so no later run can
        # check it and `later-run` refuses it (AH-139); it is reported as what
        # it recorded, not as an adoption waiting for a later run.
        adoptions = [t for t in rows_here if t.get("decision", "adopt") == "adopt"]
        not_adopted = [f"{t.get('id', '?')} recorded {t.get('decision')}" for t in rows_here if t not in adoptions]
        linked = [t for t in adoptions if (t.get("later_run") or "nothing stated") != "nothing stated"]
        # No observation, no link: a later run counts once a declared observer
        # said what the revised instruction did in it, the same reading as
        # `adoption_summary` and the queue's "E is waiting on".
        observed = [t for t in linked if t.get("later_run_kind") in LATER_RUN_OBSERVERS]
        if not adoptions and not_adopted:
            out["E"] = {"state": "missing", "why": "instruction change not adopted (" + ", ".join(not_adopted)
                        + "), so no later run applies to it"}
        elif not adoptions:
            out["E"] = {"state": "missing", "why": "instruction change with no default-adoption row"}
        elif observed:
            out["E"] = {"state": "recorded", "why": "adoption with an observed later run: " + ", ".join(t["id"] for t in observed)}
        elif linked:
            out["E"] = {"state": "missing", "why": "later run stated, nobody observed it: " + ", ".join(t["id"] for t in linked)}
        else:
            out["E"] = {"state": "missing", "why": "adoption recorded, no later-run check"}
    # A commit that names a fixed ledger row is the correction of that row, and
    # E asks whether the correction was rechecked, not only whether a default
    # was adopted. Either part missing reads missing.
    fixes = corrections_named(row, ledger or {})
    if fixes:
        waiting = [r["id"] for r in fixes if not recheck_recorded(r)]
        done = [r["id"] for r in fixes if r["id"] not in waiting]
        state = "missing" if waiting else "recorded"
        why = ("correction of " + ", ".join(f"{r['id']} ({r.get('state')})" for r in fixes)
               + (f": no recheck recorded for {', '.join(waiting)}" if waiting
                  else ": each has a recorded recheck")
               + (f"; {', '.join(done)} rechecked" if waiting and done else ""))
        if out["E"]["state"] == "not-applicable":
            out["E"] = {"state": state, "why": why}
        else:
            both = "missing" if "missing" in (state, out["E"]["state"]) else out["E"]["state"]
            out["E"] = {"state": both, "why": out["E"]["why"] + "; " + why}
    # A fix claim on a row a sweep closed is said, and leaves the state as it is.
    swept = [i for i in fix_claims(row.get("message") or "") if i in (ledger or {}) and swept_closed(ledger[i])]
    if swept:
        out["E"]["why"] += (f"; {', '.join(swept)} {'was' if len(swept) == 1 else 'were'} closed by a sweep, "
                            "which waits on no recheck")
    return out


def ledger_summary(path: pathlib.Path) -> dict:
    counts = {s: 0 for s in LEDGER_STATES}
    other, closed_unverified, fixed_unchecked, faulty, closed_unswept = 0, 0, 0, 0, 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                other += 1
                continue
            state = row.get("state") if isinstance(row, dict) else None
            if state in counts:
                counts[state] += 1
            else:
                other += 1
                continue
            if state == "closed":
                closed_unverified += 1
                # Closed by hand, or before the kit recorded how: not a sweep's
                # observation, and no recheck reached it.
                closed_unswept += dl.closed_without_recheck(row)
            if state == "fixed" and not recheck_recorded(row):
                # An event with outcome `not stated` is kept and is not a
                # recheck, and a row with objects still waiting is not rechecked.
                fixed_unchecked += 1
            if row.get("faulty_default"):
                faulty += 1
    return {"states": counts, "unrecognized_rows": other,
            "closed_without_verification": closed_unverified,
            "closed_other_than_by_a_sweep_without_recheck": closed_unswept,
            "fixed_without_recheck": fixed_unchecked,
            "rows_naming_a_faulty_default": faulty}


def adoption_summary(transitions: list[dict]) -> dict:
    """Adoption rows by what their later run says: observed by whom, stated
    without an observation (rows written before the observation existed), or
    awaiting. A count, never a judgment of the observation."""
    out = {"observed_by_a_person": 0, "observed_by_a_model": 0, "observed_by_a_delegated_actor": 0,
           "stated_without_an_observation": 0, "awaiting": 0}
    for t in transitions:
        if t.get("kind") != "default-adoption" or t.get("decision", "adopt") != "adopt":
            continue
        later = t.get("later_run") or "nothing stated"
        kind = t.get("later_run_kind")
        if later == "nothing stated":
            out["awaiting"] += 1
        elif kind == "human":
            out["observed_by_a_person"] += 1
        elif kind == "agent":
            out["observed_by_a_model"] += 1
        elif kind == "delegated":
            out["observed_by_a_delegated_actor"] += 1
        else:
            out["stated_without_an_observation"] += 1
    return out


def intake_names(rows: list) -> dict:
    """What the intake rows name: object paths, folders, digests and versions, and their text."""
    paths, folders, digests, texts = set(), [], set(), []
    for row in rows:
        if not isinstance(row, dict):
            continue
        obj = row.get("object")
        if isinstance(obj, str) and obj.strip():
            name = obj.strip().replace("\\", "/").removeprefix("./")
            if name.endswith("/") or pathlib.Path(name).is_dir():
                folders.append(name.rstrip("/") + "/")
            else:
                paths.add(name)
        for key in ("object_sha256", "version"):
            value = row.get(key)
            if isinstance(value, str) and value.strip() and value.strip() != "not stated":
                digests.add(value.strip().lower())
        texts.append(" ".join(str(row.get(k, "")) for k in ("what", "object", "version")))
    return {"paths": paths, "folders": folders, "digests": digests, "text": "\n".join(texts)}


def named_by_intake(path: str, digest: str | None, names: dict) -> bool:
    """True when an intake row names this path, a folder above it, its digest or version, or its path in words.

    The path in words is matched whole, so `data/a.csv` is not named by a row
    that names `data/a.csv.gz`.
    """
    if path in names["paths"] or any(path.startswith(f) for f in names["folders"]):
        return True
    if digest and digest.lower() in names["digests"]:
        return True
    return path in names["text"] and bool(
        re.search(rf"(?<![\w-]){re.escape(path)}(?![\w-]|\.\w)", names["text"]))


def admits(row) -> bool:
    """Whether an intake row admits what it names: a decision to admit, or a row
    written before intake took a decision, which was read as an admission then."""
    return isinstance(row, dict) and (row.get("decision") or "admit") == "admit"


def unnamed_material(rows: list, baseline: str, suffixes: tuple) -> dict:
    """Material the project tracks or inventories that no intake row names. Information only.

    Until rc22 an entry with no intake row was invisible: the report counted
    the rows that exist and never compared them with what the project holds.
    Three places are compared, each with its count and the first few paths:
    data files added under sources/ since the baseline (by suffix; renames
    show as additions, so a moved file is matched by its digest), the files
    sources/shared-inventory.json lists, and the snapshots under
    workspace/snapshots/ on this machine, whose identifier a row may give as
    its version. Unnamed is a fact about the registry, not a fault.

    Only a row that admits names a file as admitted (a row with no decision,
    written before intake took one, still does). Until rc22 any row did, so a
    copy of a returned version in the shared data read as named. A file only a
    return or refuse row names is counted apart and listed under
    `returned_or_refused_yet_present`, as the transitions' paper/ flag lists a
    turned-away object found in the manuscript. The incoming folder, where
    outside material waits for a decision (`incoming_root` in ai/project.json,
    else incoming/ inside the shared folder), is reported apart as well, with
    the decision each file there has, since what waits there is not the
    project's data.
    """
    admitted = intake_names([r for r in rows if admits(r)])
    turned = [(r, intake_names([r])) for r in rows
              if isinstance(r, dict) and r.get("decision") in ("return", "refuse")]
    incoming = hr.incoming_folder().strip("/")
    present: dict[str, dict] = {}

    def held(path: str) -> bool:
        return path == incoming or path.startswith(incoming + "/")

    def named(path: str, digests: list, names: dict, words: tuple = ()) -> bool:
        return any(w and w in names["text"] for w in words) or any(
            named_by_intake(path, d, names) for d in (digests or [None]))

    def turned_by(path: str, digests: list, words: tuple = ()) -> dict | None:
        return next((row for row, names in turned if named(path, digests, names, words)), None)

    def state(path: str, digests: list, where: str, words: tuple = ()) -> str:
        if named(path, digests, admitted, words):
            return "named"
        row = turned_by(path, digests, words)
        if row is None:
            return "unnamed"
        key = str(row.get("id") or "?")
        entry = present.setdefault(key, {"id": key, "decision": row["decision"], "found": []})
        entry["found"].append(f"{key} recorded {row['decision']}, yet {path} is {where}")
        return "turned away"

    def summary(items: list[tuple[str, str]]) -> dict:
        unnamed = [path for path, said in items if said == "unnamed"]
        return {"total": len(items), "unnamed": len(unnamed), "examples": unnamed[:UNNAMED_EXAMPLES]}

    added = hr.git("diff", "--name-only", "--no-renames", "--diff-filter=A", baseline, "HEAD", "--", "sources/")
    files = []
    for path in (ln.strip() for ln in added.splitlines()):
        if not path or path in KIT_SOURCES or held(path) \
                or not path.lower().endswith(tuple(s.lower() for s in suffixes)):
            continue
        local = pathlib.Path(path)
        try:
            digest = hr.sha256_file(local) if local.is_file() else None
        except OSError:
            digest = None
        files.append((path, state(path, [digest], "added under sources/")))
    out = {"sources": summary(files)}

    listed, waiting = [], {}
    try:
        inventory = json.loads(pathlib.Path(SHARED_INVENTORY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        inventory = None
    if isinstance(inventory, dict) and isinstance(inventory.get("files"), list):
        shared = str(inventory.get("shared_path") or "workspace/shared").rstrip("/")
        for f in inventory["files"]:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                continue
            path = f"{shared}/{f['path']}"
            if held(path):
                waiting[path] = f.get("sha256")
                continue
            listed.append((path, state(path, [f.get("sha256")], "listed in the shared inventory")))
    out["shared_inventory"] = summary(listed)

    snapshots = []
    for manifest in sorted(pathlib.Path(SNAPSHOTS).glob("*/snapshots/*/manifest.json")):
        identifier = manifest.parent.name
        path = manifest.parent.as_posix()
        try:
            digest = hr.sha256_file(manifest)
        except OSError:
            digest = None
        # The identifier is the digest of the manifest's canonical form; a row
        # may give it as its version or in words, name the folder, or carry
        # the manifest file's own digest.
        snapshots.append((path, state(path, [identifier, digest], "a snapshot on this machine",
                                      words=(identifier,))))
    out["snapshots"] = summary(snapshots)

    # What waits in the incoming folder: on this machine, and as the inventory lists it.
    module = hr.starter_script("shared_folder")
    for path in (module.incoming_files(pathlib.Path(".")) if module else []):
        waiting.setdefault(path, None)
    decided = {"awaiting_decision": [], "admitted": [], "returned": [], "refused": []}
    for path, digest in sorted(waiting.items()):
        if named(path, [digest], admitted):
            decided["admitted"].append(path)
            continue
        row = turned_by(path, [digest])
        decided["awaiting_decision" if row is None else
                "returned" if row["decision"] == "return" else "refused"].append(path)
    out["incoming"] = {"path": incoming, "total": len(waiting),
                       **{k: len(v) for k, v in decided.items()},
                       "examples": decided["awaiting_decision"][:UNNAMED_EXAMPLES]}
    out["returned_or_refused_yet_present"] = list(present.values())
    out["meaning"] = ("named means an admitting intake row's object, a folder above it, its digest or version, "
                      "or its text names the path (a row with no decision admits, as it did when written); "
                      "a path only a return or refuse row names is listed as returned or refused yet present; "
                      "material in the incoming folder waits for a decision and is not counted as data; "
                      "all of it is information, not a fault")
    return out


def build_report(records_dir: pathlib.Path | None = None, ledger: pathlib.Path | None = None) -> dict:
    """The whole report, as data. Runs from the project root."""
    config = hr.project_config()
    protected = _config_list(config, "protected_paths", hr.PROTECTED_PATHS)
    governed = _config_list(config, "governed_paths", GOVERNED_DEFAULT)
    index = hr.review_index()
    transitions = hr.read_registry(hr.TRANSITIONS, hr.validate_transitions)
    covered: dict[str, list[dict]] = {}
    for t in transitions:
        for sha in t.get("commits", []) or []:
            covered.setdefault(sha, []).append(t)
    commits = []
    totals = {p: {s: 0 for s in STATES} for p in PRINCIPLES}
    ledger_file = pathlib.Path(ledger) if ledger else pathlib.Path("ai/defects.jsonl")
    fixed_rows = ledger_rows(ledger_file)
    basis = {b: 0 for b in R_BASIS}
    for row in commits_since_baseline():
        if row["record"]:
            row["record"] = hr.record_key(row["record"])
        states = assess(row, index, covered, protected, governed, fixed_rows)
        for p in PRINCIPLES:
            totals[p][states[p]["state"]] += 1
        if states["R"].get("basis"):
            basis[states["R"]["basis"]] += 1
        commits.append({"commit": row["commit"], "subject": row["subject"], "record": row["record"],
                        "states": {p: states[p]["state"] for p in PRINCIPLES},
                        "why": {p: states[p]["why"] for p in PRINCIPLES}})
    intake = hr.read_registry(hr.INTAKE, hr.validate_intake)
    unchecked = sum(1 for r in intake if isinstance(r, dict) and r.get("checked") == "nothing stated")
    baseline = hr.project_epoch("policy_epoch", POLICY_EPOCH)
    # Which decision each row records; a row written before intake took a
    # decision has none, and is counted as such rather than read as an admission.
    intake_decisions = {d: 0 for d in hr.TRANSITION_DECISIONS["intake"]}
    intake_decisions["not recorded"] = 0
    for r in intake:
        if isinstance(r, dict):
            key = r.get("decision") if r.get("decision") in intake_decisions else "not recorded"
            intake_decisions[key] += 1
    turned_away_in_paper = [{"id": t.get("id"), "decision": t.get("decision"), "found": found}
                            for t in transitions for found in [hr.refused_yet_in_paper(t)] if found]
    return {
        "baseline": baseline,
        "commits_since_baseline": len(commits),
        "applicability": {
            "T": "every commit a machine executed", "R": f"commits changing {', '.join(governed)}",
            "A": f"commits touching {', '.join(protected)} or named by a transition",
            "C": f"commits touching {', '.join(protected)}",
            "E": (f"commits touching {', '.join(INSTRUCTION_PATHS)}, and commits whose message claims a fix "
                  "(Fixes <id>) of a ledger row claimed fixed and not closed by a sweep, recorded when each "
                  "such row has a recorded recheck; ledger rows by state"),
        },
        "totals": totals,
        "review_basis": basis,
        "commits": commits,
        "intake": {"rows": len(intake), "checked_nothing_stated": unchecked,
                   "decisions": intake_decisions,
                   "unnamed_material": unnamed_material(intake, baseline,
                                                        _config_list(config, "data_suffixes", DATA_SUFFIXES))},
        "transitions": {"rows": len(transitions),
                        "by_kind": {k: sum(1 for t in transitions if t.get("kind") == k) for k in hr.TRANSITION_KINDS},
                        "by_decision": {k: {d: sum(1 for t in transitions if t.get("kind") == k and t.get("decision") == d)
                                            for d in hr.TRANSITION_DECISIONS[k]} for k in hr.TRANSITION_KINDS},
                        "returned_or_refused_yet_in_paper": turned_away_in_paper,
                        "adoptions": adoption_summary(transitions)},
        "ledger": ledger_summary(ledger_file),
        "meaning": ("recorded means a joined record exists; it does not establish that the check was "
                    "adequate, the accepter independent, or the transition authorized in fact"),
    }


def unnamed_lines(intake: dict) -> list[str]:
    """The material no intake row names, under the intake line; nothing for a report without it.
    Then what is held in the incoming folder, and a returned or refused file present anyway,
    each only when there is one (a report written before rc22 has neither)."""
    unnamed = intake.get("unnamed_material")
    if not unnamed:
        return []
    lines = ["          named by no row: " + ", ".join(
        f"{unnamed[key]['unnamed']} of {unnamed[key]['total']} {label}"
        for key, label in (("sources", "data file(s) added under sources/"),
                           ("shared_inventory", "shared-inventory file(s)"),
                           ("snapshots", "snapshot(s) here")))]
    held = unnamed.get("incoming")
    if isinstance(held, dict) and held.get("total"):
        lines.append(f"          held in {held['path']}/, not the project's data: {held['total']} file(s), "
                     f"{held['awaiting_decision']} awaiting a decision, {held['returned']} returned, "
                     f"{held['refused']} refused, {held['admitted']} admitted and not yet moved")
    for flagged in unnamed.get("returned_or_refused_yet_present") or []:
        lines += [f"          {found}" for found in flagged["found"]]
    return lines


def decision_lines(transitions: dict) -> list[str]:
    """Which decision the transition rows record, and a returned or refused object in paper/ anyway."""
    decided = transitions.get("by_decision")
    if not decided:
        return []
    # A return or refusal is named with its kind, since every kind permits both.
    said = ", ".join(f"{v} {d if d == hr.DEFAULT_DECISION.get(k) else f'{k} {d}'}"
                     for k in decided for d, v in decided[k].items() if v)
    lines = [f"               decisions: {said or 'none'}"]
    for flagged in transitions.get("returned_or_refused_yet_in_paper") or []:
        lines += [f"               {found}" for found in flagged["found"]]
    return lines


def render(report: dict) -> str:
    lines = [f"governing actions over {report['commits_since_baseline']} commit(s) since baseline "
             f"{report['baseline'][:12]}", "",
             f"  {'principle':<38}{'recorded':>9}{'missing':>9}{'n/a':>7}{'unknown':>9}"]
    for p in PRINCIPLES:
        t = report["totals"][p]
        lines.append(f"  {p}  {NAMES[p]:<35}{t['recorded']:>9}{t['missing']:>9}{t['not-applicable']:>7}{t['unknown']:>9}")
    basis = report.get("review_basis")
    if basis:
        lines.append("     R recorded: " + ", ".join(f"{v} {R_BASIS_SAID.get(k, k)}"
                                                    for k, v in basis.items()))
    led = report["ledger"]
    lines += ["", "  ledger: " + ", ".join(f"{v} {k}" for k, v in led["states"].items()),
              f"          {led['closed_without_verification']} closed without verification"
              + (f" ({led['closed_other_than_by_a_sweep_without_recheck']} of them not by a sweep)"
                 if led.get("closed_other_than_by_a_sweep_without_recheck") else "") + ", "
              f"{led['fixed_without_recheck']} fixed without recheck, "
              f"{led['rows_naming_a_faulty_default']} naming a faulty default",
              f"  intake: {report['intake']['rows']} row(s), {report['intake']['checked_nothing_stated']} with nothing checked"
              + "".join("; decisions: " + (", ".join(f"{v} {k}" for k, v in decided.items() if v) or "none")
                        for decided in [report["intake"].get("decisions")] if decided),
              *unnamed_lines(report["intake"]),
              f"  transitions: {report['transitions']['rows']} row(s) "
              + ", ".join(f"{v} {k}" for k, v in report["transitions"]["by_kind"].items()),
              *decision_lines(report["transitions"])]
    adoptions = report["transitions"].get("adoptions")
    if adoptions:
        lines.append("  adoptions: later run " + ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in adoptions.items()))
    lines += ["", "  applicability:"]
    lines += [f"    {p}: {report['applicability'][p]}" for p in PRINCIPLES]
    lines += ["", "  " + report["meaning"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--out", help="also write the JSON report here")
    parser.add_argument("--limit", type=int, default=20, help="commits with a missing state to list")
    args = parser.parse_args(argv)
    try:
        report = build_report()
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report))
        return 0
    print(render(report))
    missing = [c for c in report["commits"] if "missing" in c["states"].values()]
    if missing:
        print(f"\n  commits with a missing record (first {min(args.limit, len(missing))} of {len(missing)}):")
        for c in missing[:args.limit]:
            gaps = ",".join(p for p in PRINCIPLES if c["states"][p] == "missing")
            print(f"    {c['commit'][:12]}  {gaps:<9} {c['subject'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
