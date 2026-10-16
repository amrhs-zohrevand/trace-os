"""Prompt/comment capture, decision accounts and readable trace-record history.

Used through trace_record.py. This module records supplied interpretations;
it neither infers intellectual credit nor grants acceptance or Git authority.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from input_provenance import input_account, input_lines


KINDS = {"human", "agent", "joint", "unknown"}
# The allowed values of a decision row's `type`, in the order a user reads them
# (`decision --help`, the error for an unknown type, RECORD-CONVENTION.md); they
# were stated nowhere a user read until rc22.
DECISION_TYPE_ORDER = ("direction", "method", "interpretation", "implementation",
                       "acceptance", "transition", "correction", "other")
DECISION_TYPES = set(DECISION_TYPE_ORDER)
RECORD_FILES = {"evidence.json", "summary.md", "coverage.json", "checks.jsonl", "decisions.jsonl", "publication.json"}
# What a decision row reads when nobody stated why the choice was taken.
REASON_NOT_STATED = "not stated"


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    pathlib.Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def owner_names(turns, refs):
    return list(dict.fromkeys(t["speaker"] for t in turns
                             if t["id"] in refs and nonempty(t.get("speaker"))
                             and t["speaker"] not in {"unattributed", "unknown", "?"}
                             and t.get("speaker_kind", "human") == "human"))


def human_direction(entry):
    """The direction a human gave, from either spelling of the field.

    `human_direction` is what the kit writes. `human_contribution` is what
    records written before 2026-09-20 carry, and they stay readable rather than
    being rewritten: the word retired, the history did not.
    """
    for key in ("human_direction", "human_contribution"):
        value = entry.get(key)
        if nonempty(value):
            return value
    return ""


def decision_reason(entry):
    """Why the selected choice was taken over the alternatives, as the row states it.

    Rows written before 2026-10-14 carry no `reason` field, and a new row whose
    author stated none is written with "not stated"; both read "not stated"
    rather than being refused or given an invented reason.
    """
    value = entry.get("reason")
    return value.strip() if nonempty(value) else REASON_NOT_STATED


def reason_stated(entry):
    return decision_reason(entry).lower() != REASON_NOT_STATED


def decision_inputs(entry):
    """What the choice used, as the row names it: paths, intake rows (IN-NNN)
    or earlier trace records (ai/records/<id>).

    The field is optional and dates from 2026-10-14; a row without it names no
    inputs, which reads "not stated" rather than an empty list.
    """
    values = entry.get("inputs")
    return [v.strip() for v in values if nonempty(v)] if isinstance(values, list) else []


def decision_problems(entry, evidence):
    if not isinstance(entry, dict):
        return ["decision must be an object"]
    errors = []
    for field in ("id", "question", "choice", "authority"):
        if not nonempty(entry.get(field)):
            errors.append(f"{field} must contain text")
    # Optional: an absent reason reads "not stated". One that is present says
    # why the choice was taken, so it must say something.
    if "reason" in entry and not nonempty(entry["reason"]):
        errors.append("reason must contain text when present: why the choice was taken "
                      "over the alternatives, or leave it out to record 'not stated'")
    if entry.get("type") not in DECISION_TYPES:
        errors.append(f"unknown decision type {entry.get('type')!r}: type is one of "
                      + ", ".join(DECISION_TYPE_ORDER))
    if entry.get("status") not in {"proposed", "selected"}:
        errors.append("status must be proposed or selected")
    ids = {t["id"] for t in evidence["turns"]}
    refs = entry.get("human_inputs")
    if not isinstance(refs, list) or any(not isinstance(r, str) or r not in ids for r in refs):
        errors.append("human_inputs must list existing turn IDs")
    elif len(set(refs)) != len(refs):
        errors.append("human_inputs contains duplicate turn IDs")
    if refs and not nonempty(human_direction(entry)):
        errors.append("describe the human direction and ideas, citing human_inputs")
    if not refs and human_direction(entry):
        errors.append("human_direction needs a captured human input")
    for field in ("proposed_by", "selected_by"):
        actor = entry.get(field)
        if not isinstance(actor, dict) or actor.get("kind") not in KINDS:
            errors.append(f"{field} needs a name and explicit kind")
        elif actor["kind"] != "unknown" and not nonempty(actor.get("name")):
            errors.append(f"{field} needs a name")
    selector = entry.get("selected_by")
    if entry.get("status") == "proposed" and isinstance(selector, dict) and selector.get("kind") != "unknown":
        errors.append("a proposal cannot already name a selector")
    for field in ("artifacts", "evidence"):
        values = entry.get(field)
        if not isinstance(values, list) or not values or any(not nonempty(v) for v in values):
            errors.append(f"{field} must list the inspected source or resulting work")
    # Optional, like the reason: an absent list names no inputs. One that is
    # present names each thing the choice used, once.
    if "inputs" in entry:
        values = entry["inputs"]
        if not isinstance(values, list) or not values or any(not nonempty(v) for v in values):
            errors.append("inputs must list the paths, intake rows (IN-NNN) or earlier records "
                          "(ai/records/<id>) the choice used, or leave it out")
        elif len({v.strip() for v in values}) != len(values):
            errors.append("inputs names the same input twice")
    owners = entry.get("owners")
    if not isinstance(owners, list) or any(not nonempty(v) for v in owners):
        errors.append("owners must be a list of names, or empty when unknown")
    if entry.get("owner_basis") not in {"input speakers", "explicit assignment", "unknown"}:
        errors.append("owner_basis must identify the ownership source")
    if entry.get("owner_basis") == "explicit assignment" and not nonempty(entry.get("owner_authority")):
        errors.append("an explicit owner assignment needs owner_authority")
    if isinstance(refs, list) and isinstance(owners, list) and entry.get("owner_basis") == "input speakers":
        if owners != owner_names(evidence["turns"], refs):
            errors.append("default owners differ from the cited human speakers")
    return errors


def load_decisions(record, evidence):
    path = pathlib.Path(record) / "decisions.jsonl"
    if not path.exists():
        return []
    entries, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        problems = decision_problems(entry, evidence)
        if isinstance(entry, dict) and entry.get("id") in seen:
            problems.append("duplicate decision ID")
        if problems:
            raise ValueError(f"{path}:{n}: {'; '.join(problems)}")
        seen.add(entry["id"])
        entries.append(entry)
    return entries


def cmd_comments(api, args):
    record = pathlib.Path(args.record)
    from record_privacy import private_capture_target
    private_capture_target(record)
    packet = read_json(args.input)
    items = packet.get("items") if isinstance(packet, dict) else packet
    if not isinstance(items, list):
        raise ValueError("comment input must be an array or an object with items")
    if (record / "evidence.json").exists():
        evidence = api.load_evidence(record)
    else:
        evidence = {"record_schema_version": api.TRACE_RECORD_SCHEMA_VERSION,
                    "record_id": record.name, "turns": [], "execution_agents": [],
                    "transcript_format": "review-comments", "capture": "verbatim, subject to declared redactions",
                    "consent": {"own_turns_by": "see each speaker", "third_party_review": "PENDING"}}
    known = {t.get("source", {}).get("capture_id") for t in evidence["turns"]}
    next_id = max((int(t["id"][1:]) for t in evidence["turns"]), default=0) + 1
    added = 0
    nonhuman = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("every comment must be an object")
        if item.get("speaker_kind", "human") != "human":
            nonhuman += 1
            continue
        if not nonempty(item.get("text")):
            raise ValueError("every comment needs its original text")
        if not nonempty(item.get("source_id")) or not nonempty(item.get("source")):
            raise ValueError("comments need source and source_id; use review_inbox.py collect --json")
        who = item.get("who") or "unattributed"
        if who in {"unattributed", "?", "unknown"} and args.speaker:
            who = args.speaker
        capture_id = digest([item["source_id"], item["text"], who])
        if capture_id in known:
            continue
        # Keep location evidence, not arbitrary packet context. raw_line remains
        # inspectable locally and must be screened/redacted along with text.
        allowed = {"source", "source_id", "kind", "file", "line", "url", "commit", "commit_id", "file_sha256", "raw_line", "created_at"}
        source = {k: v for k, v in item.items() if k in allowed}
        source["capture_id"] = capture_id
        source["packet_sha256"] = api.sha256_file(pathlib.Path(args.input))
        evidence["turns"].append({"id": f"T{next_id}", "timestamp": item.get("created_at"),
                                  "speaker": who, "speaker_kind": "human", "text": item["text"],
                                  "source": source})
        known.add(capture_id)
        next_id += 1
        added += 1
    if added or not (record / "evidence.json").exists():
        evidence["consent"]["third_party_review"] = "PENDING"
        record.mkdir(parents=True, exist_ok=True)
        write_json(record / "evidence.json", evidence)
    print(f"Captured {added} new comment(s) in {record}; originals retain speaker and source.")
    if nonhuman:
        print(f"{nonhuman} nonhuman comment(s) left in {args.input} for checks.jsonl; not added as human inputs.")
    if added:
        print("Read new inputs for redactions, clear consent, and refresh summary and coverage before promotion.")
    return 0


def cmd_decision(api, args):
    record = pathlib.Path(args.record)
    evidence = api.load_evidence(record)
    entries = load_decisions(record, evidence)
    entry = read_json(args.input)
    if not isinstance(entry, dict):
        raise ValueError("decision input must be an object")
    entry.setdefault("id", f"D{len(entries) + 1}")
    entry.setdefault("human_inputs", [])
    # A choice recorded without its reason is recorded, and says so.
    if entry.get("reason") is None or (isinstance(entry["reason"], str) and not entry["reason"].strip()):
        entry["reason"] = REASON_NOT_STATED
    if "owners" in entry:
        entry.setdefault("owner_basis", "explicit assignment")
    else:
        entry["owners"] = owner_names(evidence["turns"], entry["human_inputs"])
        entry["owner_basis"] = "input speakers" if entry["owners"] else "unknown"
    problems = decision_problems(entry, evidence)
    if any(d["id"] == entry["id"] for d in entries):
        problems.append("decision ID already exists; preserve the old entry and record a new decision")
    if "human_inputs must list existing turn IDs" in problems:
        # The usual cause mid-session: the turn that directed the choice came
        # after the last extraction, and a refresh adds it under a new id.
        problems.append("when the directing turn came after the last extraction, run "
                        "trace_record.py extract again into this staging directory and cite the id it adds")
    if problems:
        raise ValueError("; ".join(problems))
    scope = getattr(args, "scope", None)
    if scope is not None:
        if not nonempty(scope):
            raise ValueError("decision scope must contain text")
        if entries and evidence.get("decision_scope") not in (None, scope):
            raise ValueError("keep the recorded scope; use a separate record for a different counting scope")
        evidence["decision_scope"] = scope
        write_json(record / "evidence.json", evidence)
    entry["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with (record / "decisions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"Recorded {entry['id']}; owner(s): {', '.join(entry['owners']) or 'unknown'} ({entry['owner_basis']}).")
    if not reason_stated(entry):
        print("No reason was stated for the choice, so the row reads 'reason: not stated'.")
    print("This decision account does not create an acceptance or authorize a transition.")
    return 0


def inspect_record(api, record):
    from record_privacy import require_screened
    from record_privacy import safe_record_files
    import tempfile
    safe_record_files(record)
    files = {name: (record / name).read_text(encoding="utf-8")
             for name in RECORD_FILES if (record / name).exists()}
    with tempfile.TemporaryDirectory(prefix="record-render-") as directory:
        snapshot = pathlib.Path(directory)
        for name, text in files.items():
            (snapshot / name).write_text(text, encoding="utf-8")
        require_screened(snapshot)
        result = api.evaluate_record(snapshot)
        if not result["ok"]:
            raise ValueError(f"{record}: {'; '.join(result['problems'])}")
        evidence = api.load_evidence(snapshot)
        return evidence, load_decisions(snapshot, evidence), files


def decision_text(entry):
    proposed, selected = entry["proposed_by"], entry["selected_by"]
    names = lambda actor: f"{actor.get('name') or 'unknown'} ({actor['kind']})"
    lines = [f"{entry['id']}: {entry['question']}", f"Choice: {entry['choice']}",
             f"Reason: {decision_reason(entry)}",
             f"Proposed by: {names(proposed)}; selected by: {names(selected)}; status: {entry['status']}.",
             f"Owner(s): {', '.join(entry['owners']) or 'unknown'}; basis: {entry['owner_basis']}."]
    if entry.get("owner_authority"):
        lines.append(f"Ownership assignment: {entry['owner_authority']}")
    if entry["human_inputs"]:
        lines.append(f"Human direction [{', '.join(entry['human_inputs'])}]: {human_direction(entry)}")
    lines += [f"Authority: {entry['authority']}",
              f"Inputs: {', '.join(decision_inputs(entry)) or 'not stated'}",
              f"Work: {', '.join(entry['artifacts'])}",
              f"Evidence: {', '.join(entry['evidence'])}"]
    return lines


def draft_message(api, record, subject, out, execution_agents=None, baseline=None, notes=None):
    """Write a commit body from a checked trace record and return its text.

    The core of `draft-message`, lifted so that `keep` drafts the same body.
    `baseline`, when given, is what the record held at its last commit (turn,
    decision and check identities and the summary's lines): the body then
    carries only what is new since, with one line counting the commits that
    carry the rest. `notes`, one line each, go in a paragraph above the
    `Record:` line: `keep` says there that an execution-agent id is
    self-reported.
    """
    record = pathlib.Path(record)
    evidence, decisions, files = inspect_record(api, record)
    if not subject.strip() or "\n" in subject or "\r" in subject:
        raise ValueError("subject must be one nonempty line")
    # A placeholder such as `<synthetic>` in an older record names no model (AH-126).
    agents = execution_agents or [e["model"] for e in evidence.get("execution_agents", [])
                                  if api.is_model_id(e.get("model"))]
    if not agents:
        raise ValueError("no execution agent captured; pass --execution-agent <model-id>")
    if any(not nonempty(a) or "\n" in a or "\r" in a for a in agents):
        raise ValueError("execution agent must be a nonempty single-line model ID")
    notes = [note for note in notes or [] if note.strip()]
    if any("\n" in note or "\r" in note or api.TRAILER_LINE_RE.match(note) for note in notes):
        raise ValueError("a note must be one line that does not read as a trailer")
    summary = files["summary.md"].strip()
    summary = re.sub(r"\A#[^\n]*\n+", "", summary)
    checks = [line for line in files["checks.jsonl"].splitlines() if line.strip()]
    if baseline is None:
        lines = [subject, "", "Human direction account:", summary, ""]
        passages = input_account(evidence)
    else:
        known = baseline["summary"]
        fresh = [line for line in summary.splitlines() if line.strip() and line.strip() not in known]
        new_turns = [t for t in evidence["turns"] if t["id"] not in baseline["turns"]]
        decisions = [d for d in decisions if d["id"] not in baseline["decisions"]]
        checks = [line for line in checks if check_identity(line) not in baseline["checks"]]
        shas = baseline["commits"]
        named = ", ".join(sha[:8] for sha in shas[:3]) + (f" and {len(shas) - 3} more" if len(shas) > 3 else "")
        lines = [subject, "", "Human direction account, new since the record's last commit:",
                 "\n".join(fresh) if fresh else "(the summary adds nothing since the record's last commit)", "",
                 f"Earlier: {len(shas)} commit(s) name this record ({named}) and carry its account of "
                 f"{len(baseline['turns'])} turn(s), {len(baseline['decisions'])} decision(s) and "
                 f"{len(baseline['checks'])} check(s); this body adds "
                 f"{len(new_turns)} turn(s), {len(decisions)} decision(s) and {len(checks)} check(s).", ""]
        passages = input_account({**evidence, "turns": new_turns})
    if passages["annotated_turns"]:
        lines += input_lines(passages) + [""]
    if decisions:
        lines.append("Decisions and ownership:")
        for entry in decisions:
            lines += decision_text(entry) + [""]
    lines.append("Checks and review:")
    for line in checks:
        check = json.loads(line)
        method = check['method'].rstrip()
        stop = "" if method.endswith((".", "?", "!")) else "."
        lines.append(f"- {check['runner']}: {method}{stop} {check['result']} [{check['artifact']}]")
    if notes:
        lines += [""] + notes
    # The form every reader joins on: a record in this tree is ai/records/<id>
    # however it was named here, and a staging path stays one (AH-136).
    lines += ["", f"Record: {api.record_key(str(record))}", ""]
    lines += [f"Execution-agent: {a}" for a in dict.fromkeys(agents)]
    output = pathlib.Path(out)
    if output.resolve() in {(record / name).resolve() for name in RECORD_FILES}:
        raise ValueError("write the draft outside the trace record so inputs stay intact")
    rendered = "\n".join(lines).rstrip() + "\n"
    from record_privacy import text_findings
    if text_findings(rendered):
        raise ValueError("possible sensitive content in generated commit; inspect inputs locally")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return rendered


def check_identity(line):
    """One check row as text that does not depend on its key order or spacing."""
    try:
        return json.dumps(json.loads(line), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return line.strip()


def cmd_message(api, args):
    draft_message(api, args.record, args.subject, args.out, args.execution_agent)
    print(f"Drafted {args.out}; inspect its meaning before git commit --file. Git identity is unchanged.")
    return 0


def cmd_view(api, args):
    records = []
    for name in args.records or ["ai/records"]:
        path = pathlib.Path(name)
        if path.is_dir() and any((path / name).exists() for name in RECORD_FILES):
            records.append(path)
        elif path.is_dir():
            records.extend(p for p in sorted(path.iterdir()) if p.is_dir()
                           and any((p / name).exists() for name in RECORD_FILES))
        else:
            raise ValueError(f"no record directory: {path}")
    records = list({p.resolve(): p for p in records}.values())
    accounts, errors = [], []
    counts = Counter({kind: 0 for kind in sorted(KINDS)})
    by_type = {}
    for record in dict.fromkeys(records):
        try:
            evidence, decisions, files = inspect_record(api, record)
        except (ValueError, OSError, KeyError, TypeError, SystemExit) as exc:
            errors.append({"record": str(record), "error": str(exc)})
            continue
        for entry in decisions:
            if entry["status"] == "selected":
                kind = entry["selected_by"]["kind"]
                counts[kind] += 1
                by_type.setdefault(entry["type"], Counter())[kind] += 1
        accounts.append({"record": record.as_posix(), "record_id": evidence["record_id"],
                         "decision_scope": evidence.get("decision_scope", "not stated"),
                         "input_bound_by_coverage": "evidence_sha256" in json.loads(files["coverage.json"]),
                         "decisions": decisions,
                         "input_passages": input_account(evidence)})
    with_decisions = sum(bool(a["decisions"]) for a in accounts)
    recorded = [entry for a in accounts for entry in a["decisions"]]
    unreasoned = sum(not reason_stated(entry) for entry in recorded)
    with_inputs = sum(bool(decision_inputs(entry)) for entry in recorded)
    result = {"records_examined": len(dict.fromkeys(records)), "valid_records": len(accounts),
              "records_with_decisions": with_decisions,
              "records_without_decision_accounts": len(accounts) - with_decisions,
              "decisions_recorded": len(recorded), "decisions_without_reason": unreasoned,
              "decisions_naming_inputs": with_inputs,
              "records_without_input_binding": sum(not a["input_bound_by_coverage"] for a in accounts),
              "records_without_passage_annotations": sum(not a["input_passages"]["annotated_turns"] for a in accounts),
              "selected_decisions": sum(counts.values()), "selected_by_kind": dict(counts),
              "selected_by_type": by_type, "accounts": accounts, "errors": errors,
              "limit": "Counts describe recorded selections, not intellectual credit. Missing accounts are unknown, not zero AI or human involvement. Acceptance and transition ledgers remain authoritative for those acts."}
    if args.json:
        rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    else:
        lines = ["# Trace view", "",
                 f"{len(accounts)} valid records; {with_decisions} with decision accounts; "
                 f"{len(accounts) - with_decisions} without decision accounts; {len(errors)} unreadable or invalid.",
                 f"{sum(counts.values())} recorded selections: " + ", ".join(f"{kind} {count}" for kind, count in counts.items()) + ".", "", result["limit"], ""]
        lines += [f"{result['records_without_input_binding']} legacy records have no input-text binding in coverage.",
                  f"{unreasoned} of {len(recorded)} recorded decisions state no reason for the choice; "
                  "a reason is reported, never required.",
                  f"{with_inputs} of {len(recorded)} recorded decisions name the inputs the choice used; "
                  "inputs are optional.",
                  "The denominator is recorded selections within each session's stated scope; unlogged choices are outside this measure.", ""]
        if by_type:
            lines += ["| Decision type | Human | Agent | Joint | Unknown |", "| --- | --- | --- | --- | --- |"]
            for kind, values in sorted(by_type.items()):
                lines.append(f"| {kind} | {values['human']} | {values['agent']} | {values['joint']} | {values['unknown']} |")
            lines.append("")
        output_parent = pathlib.Path(args.out).resolve().parent if args.out else pathlib.Path.cwd()
        ledgers = [("Acceptance", "ai/records/acceptances.jsonl"),
                   ("Intake and use", "sources/intake.jsonl"), ("Corrections", "ai/defects.jsonl")]
        links = []
        for label, name in ledgers:
            path = pathlib.Path(name)
            if path.exists():
                link = pathlib.Path(os.path.relpath(path.resolve(), output_parent)).as_posix()
                links.append(f"[{label}]({link})")
        if links:
            lines += ["Related records: " + ", ".join(links) + ".", ""]
        for account in accounts:
            if not account["decisions"] and not account["input_passages"]["annotated_turns"]:
                continue
            link = pathlib.Path(os.path.relpath(pathlib.Path(account['record']).resolve(), output_parent)).as_posix()
            lines += [f"## {account['record_id']}", "",
                      f"Counting scope: {account['decision_scope']}", "",
                      f"[Human direction]({link}/summary.md), [original inputs]({link}/evidence.json), [decision account]({link}/decisions.jsonl).", ""]
            if account["input_passages"]["annotated_turns"]:
                lines += input_lines(account["input_passages"]) + [""]
            for entry in account["decisions"]:
                lines += ["### " + decision_text(entry)[0], ""] + ["- " + line for line in decision_text(entry)[1:]] + [""]
        for error in errors:
            lines += [f"ERROR {error['record']}: {error['error']}", ""]
        rendered = "\n".join(lines)
    from record_privacy import text_findings
    if text_findings(rendered):
        raise ValueError("possible sensitive content in generated overview; inspect records locally")
    if args.out:
        output = pathlib.Path(args.out)
        if any(output.resolve() == (record / name).resolve() for record in records for name in RECORD_FILES):
            raise ValueError("write the overview outside the trace records so inputs stay intact")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(rendered, end="")
    return 1 if errors else 0


def add_commands(sub, api):
    def invoke(func, args):
        try:
            return func(api, args)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Record error: {exc}", file=api.sys.stderr)
            return 1

    import input_provenance
    import record_privacy
    input_provenance.add_commands(sub, api, invoke)
    record_privacy.add_commands(sub, api, invoke)

    p = sub.add_parser("comments", help="capture human review comments as source-linked input turns")
    p.add_argument("record")
    p.add_argument("--input", required=True, help="JSON output of review_inbox.py collect --json")
    p.add_argument("--speaker", help="name an otherwise unattributed speaker from known context")
    p.set_defaults(func=lambda args: invoke(cmd_comments, args))
    p = sub.add_parser("decision", help="append one evidence-linked decision with default human ownership",
                       description="Append one evidence-linked decision with default human ownership. "
                                   "The row's `type` is one of: " + ", ".join(DECISION_TYPE_ORDER) + ".")
    p.add_argument("record")
    p.add_argument("--input", required=True,
                   help="JSON following decision.example.json; its type is one of "
                        + ", ".join(DECISION_TYPE_ORDER))
    p.add_argument("--scope", help="which consequential choices this session counts; retained with its evidence")
    p.set_defaults(func=lambda args: invoke(cmd_decision, args))
    p = sub.add_parser("draft-message", help="draft a commit narrative from checked trace records")
    p.add_argument("record")
    p.add_argument("--subject", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--execution-agent", action="append", help="actual model ID; defaults to the captured execution agents")
    p.set_defaults(func=lambda args: invoke(cmd_message, args))
    p = sub.add_parser("view", aliases=["overview"],
                       help="report decisions, human ideas, owners and evidence coverage; "
                            "the older name `overview` still runs it, because instructions "
                            "written before 0.1.0-rc9 name it")
    p.add_argument("records", nargs="*", help="record directories or their parent; default ai/records")
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda args: invoke(cmd_view, args))
