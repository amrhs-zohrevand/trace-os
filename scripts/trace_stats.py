#!/usr/bin/env python3
"""Render a workflow-check snapshot as Markdown, without running or accepting work.

Exit 0 means a valid report was rendered, even if its checks failed.
Exit 2 writes an unavailable overview when the report cannot be interpreted.
The workflow check's own exit status remains the authority for its checks.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RECORDS_CHECK = "trace records"
# Reports written before 2026-09-20 name that check "contribution records".
# They are renamed on read rather than rejected, because a stored report is a
# record of a run, not something to rewrite.
LEGACY_RECORDS_CHECK = "contribution records"
# Reports written by 0.1.0-rc8 name the skill-handoff state "handoff_missing".
# The state is "handoff not stated" from rc9, and the stored key is renamed on
# read for the same reason: a report records a run that happened.
LEGACY_NOT_STATED = "handoff_missing"
LEGACY_CHECKS = ("portable test suite", RECORDS_CHECK, "registries", "review status",
                 "defect ledger", "governing actions", "build configuration")
CHECKS = ("portable test suite", "installed skill parity", RECORDS_CHECK, "registries",
          "review status", "defect ledger", "governing actions", "build configuration")
# Checks a later kit release added. A report written before one exists lacks it,
# which is a fact about when the report was written and not an incomplete
# report, so it is read without it. An extra check named nowhere here is still a
# report this renderer does not understand.
# "skill handoff" arrived in 0.1.0-rc8, "shared folder" after 0.1.0-rc20.
OPTIONAL_CHECKS = ("skill handoff", "shared folder")
STATES = ("open", "fixed", "closed", "verified", "recurred", "unresolved")
PRINCIPLES = ("T", "R", "A", "C", "E")
# The principle names since 12 October 2026 (DECISIONS.md).
PRINCIPLE_NAMES = {"T": "Track records", "R": "Review against sources",
                   "A": "Accept independently", "C": "Control transitions",
                   "E": "Evaluate corrections"}


def cell(value) -> str:
    """Keep report text literal inside a Markdown table or list."""
    value = " ".join(str(value).split())
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_|\[\]#])", r"\\\1", value)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("report timestamp is missing")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("report timestamp has no timezone")
    return parsed


def validate(report):
    if not isinstance(report, dict) or report.get("schema_version") not in (1, 2):
        raise ValueError("unsupported or missing workflow report schema")
    if not re.fullmatch(r"[0-9a-f]{40}", str(report.get("evaluated_commit", ""))):
        raise ValueError("report has no full evaluated commit")
    timestamp(report.get("generated_at"))
    if type(report.get("dirty")) is not bool or type(report.get("shared_mode")) is not bool:
        raise ValueError("report lacks working-tree or shared-mode information")
    checks = report.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict) and check.get("name") == LEGACY_RECORDS_CHECK:
                check["name"] = RECORDS_CHECK
            if isinstance(check, dict) and check.get("name") == "skill handoff":
                outcome = check.get("outcome")
                if isinstance(outcome, dict) and LEGACY_NOT_STATED in outcome \
                        and "handoff_not_stated" not in outcome:
                    outcome["handoff_not_stated"] = outcome.pop(LEGACY_NOT_STATED)
    expected = LEGACY_CHECKS if report["schema_version"] == 1 else CHECKS
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        raise ValueError("report does not contain a list of checks")
    names = [c.get("name", "") for c in checks]
    if sorted(names) != sorted(set(names)) or sorted(set(names) - set(OPTIONAL_CHECKS)) != sorted(expected):
        raise ValueError(f"report does not contain the {len(expected)} expected checks exactly once")
    for check in checks:
        allowed = ({"info", "warning", "error"} if check["name"] == "defect ledger"
                   else {"info", "error"} if check["name"] in ("review status", "governing actions",
                                                              "skill handoff", "shared folder")
                   else {"pass", "fail", "error"})
        if check.get("status") not in allowed:
            raise ValueError("unrecognized check status")
        if not isinstance(check.get("outcome"), dict):
            raise ValueError("check has no structured outcome")
    return {c["name"]: c for c in checks}


def count(outcome, key):
    value = outcome.get(key)
    if type(value) is not int or value < 0:
        raise ValueError("missing or invalid count: " + key)
    return value


def repository_state(root):
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", check=True).stdout != ""
        return head, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def render(report, source, head=None, dirty=None, now=None):
    checks = validate(report)
    now = now or dt.datetime.now(dt.timezone.utc)
    age = now - timestamp(report["generated_at"])
    if age.total_seconds() < -300:
        raise ValueError("report timestamp is in the future")
    freshness = []
    if head is None or dirty is None:
        freshness.append("Current checkout could not be compared")
    elif head != report["evaluated_commit"]:
        freshness.append("STALE: evaluated commit differs from this checkout")
    if age > dt.timedelta(hours=24):
        freshness.append("STALE: report is older than 24 hours")
    if report["dirty"] or dirty:
        freshness.append("Working-tree snapshot: local edits prevent a clean-commit claim")
    if not freshness:
        freshness.append("Matches this clean checkout; generated within 24 hours")
    lines = ["# Trace stats", "", "Mechanical check snapshot. Review coverage and defect states are reported separately.", "",
             f"- Evaluated commit: `{report['evaluated_commit']}`",
             f"- Check run: {cell(report['generated_at'])}",
             f"- Overview rendered: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
             f"- Freshness at rendering: {cell('; '.join(freshness))}",
             f"- Requested mode (report): {'shared inputs required' if report['shared_mode'] else 'portable; shared inputs optional'}",
             f"- Source report: {cell(source)}", "",
             "This file does not update itself. Regenerate it after changes; the 24-hour display rule is not an assurance threshold.", "",
             "| Check | Result | Recorded detail |", "| --- | --- | --- |"]
    details = []
    expected = LEGACY_CHECKS if report["schema_version"] == 1 else CHECKS
    for name in expected + tuple(n for n in OPTIONAL_CHECKS if n in checks):
        check = checks[name]
        outcome = check["outcome"]
        status = check["status"]
        detail = outcome.get("reason", "")
        if name == "portable test suite" and status != "error":
            counts = ", ".join(f"{count(outcome, k)} {label}" for k, label in
                               (("run", "run"), ("failures", "failures"),
                                ("errors", "errors"), ("skipped", "skipped")))
            not_run = outcome.get("not_run")
            if isinstance(not_run, dict):
                counts += f"; {count(not_run, 'count')} project test file(s) not run"
            detail = counts + ("; " + detail if detail else "")
            if count(outcome, "skipped") and not outcome.get("skip_reasons"):
                details.append("Skipped (portable test suite): reasons absent from report")
        if status not in {"error", "fail"}:
            if name == "portable test suite":
                if count(outcome, "run") == 0 or count(outcome, "failures") or count(outcome, "errors"):
                    raise ValueError("successful suite status contradicts its counts")
            elif name == "installed skill parity":
                if outcome.get("applicable") is False:
                    detail = "Not applicable: no starter install state"
                else:
                    detail = (f"{count(outcome, 'checked')} installed copies checked; "
                              f"{len(outcome.get('mismatched', []))} mismatched")
                    if outcome.get("mismatched") != []:
                        raise ValueError("successful skill-parity status has mismatches")
            elif name == RECORDS_CHECK:
                detail = f"{count(outcome, 'evaluated')} records checked for mechanical integrity"
                if outcome.get("with_problems") != []:
                    raise ValueError("successful record status has missing or conflicting findings")
            elif name == "registries":
                rows = outcome.get("rows") or {}
                detail = ", ".join(f"{rows.get(k) if isinstance(rows.get(k), int) else 'unreadable'} {k}"
                                   for k in ("acceptances", "intake", "transitions")) + " rows valid"
                if outcome.get("problems") != []:
                    raise ValueError("successful registry status has missing or conflicting findings")
            elif name == "review status":
                values = [count(outcome, k) for k in ("unreviewed", "reviewed_by_a_model", "accepted_by_a_person")]
                # Reports written before 2026-09-13 predate delegated acceptance and
                # lack the key; a missing key there is a zero, not incomplete evidence.
                # The same holds for the decision and undeclared-actor states of 2026-09-15.
                extra = {k: (count(outcome, k) if k in outcome else 0)
                         for k in ("accepted_by_a_delegated_actor", "returned", "refused",
                                   "reviewed_by_an_undeclared_actor")}
                if sum(values) + sum(extra.values()) != count(outcome, "commits_since_epoch"):
                    raise ValueError("review counts do not sum to the recorded total")
                detail = (f"{values[0]} unreviewed; {values[1]} reviewed by a model; "
                          f"{values[2]} accepted by a person; {extra['accepted_by_a_delegated_actor']} accepted by a delegated actor")
                if extra["returned"] or extra["refused"] or extra["reviewed_by_an_undeclared_actor"]:
                    detail += (f"; {extra['returned']} returned; {extra['refused']} refused; "
                               f"{extra['reviewed_by_an_undeclared_actor']} reviewed by an undeclared actor")
            elif name == "governing actions":
                totals = outcome.get("totals")
                if not isinstance(totals, dict) or set(totals) != set(PRINCIPLES):
                    raise ValueError("governing-actions report lacks the five principles")
                detail = (f"{count(outcome, 'commits_since_baseline')} commits since baseline; "
                          f"{count(outcome, 'commits_with_a_missing_record')} with a missing record")
            elif name == "skill handoff":
                if outcome.get("applicable") is False:
                    detail = "Not applicable: no skills directory"
                else:
                    stated = count(outcome, "handoff_stated")
                    not_stated = count(outcome, "handoff_not_stated")
                    if stated + not_stated != count(outcome, "checked"):
                        raise ValueError("skill-handoff counts do not sum to the skills read")
                    detail = (f"{count(outcome, 'checked')} skills read; {not_stated} state no "
                              "handoff to the record; the text only, not whether one was followed")
            elif name == "shared folder":
                if outcome.get("applicable") is False:
                    detail = "Not applicable: " + str(outcome.get("reason", "no shared-folder script"))
                else:
                    detail = f"{outcome.get('state', 'state not recorded')} on this machine"
                    if outcome.get("next"):
                        detail += "; next: " + str(outcome["next"])
            elif name == "defect ledger":
                states = outcome.get("states")
                if not isinstance(states, dict) or set(states) - set(STATES):
                    raise ValueError("invalid defect-state counts")
                detail = "; ".join(f"{count(states, s) if s in states else 0} {s}" for s in STATES)
            else:
                detail = "Configuration checked; PDF build not measured"
        elif name == "build configuration" and status == "error":
            # LESSON (AH-203): a checker that did not complete checked nothing.
            detail = "Execution error, configuration not checked: " + (detail or "reason not recorded")
        if not detail:
            detail = "; ".join(str(p) for p in outcome.get("with_problems", [])) or "See source report and check log"
        lines.append(f"| {cell(name)} | {cell(status)} | {cell(detail)} |")
        for reason in outcome.get("skip_reasons", []):
            details.append(f"Skipped ({name}): {reason}")
        bst = outcome.get("bst_staleness", {})
        if isinstance(bst, dict) and bst.get("status") == "unavailable":
            details.append("Skipped (bibliography staleness): " + str(bst.get("reason", "reason not recorded")))
        elif isinstance(bst, dict) and bst.get("status") == "not_checked":
            details.append("Skipped (bibliography-style staleness): " + str(bst.get("reason", "reason not recorded")))
        # The project's own test files the suite left alone, and kit test
        # modules the install lists but the tree lacks: informational (AH-203).
        not_run = outcome.get("not_run")
        if name == "portable test suite" and isinstance(not_run, dict):
            details.append(f"Not run ({name}): {count(not_run, 'count')} test file(s) under tests/, "
                           f"{not_run.get('reason', 'reason not recorded')}: "
                           + ", ".join(str(f) for f in not_run.get("files", [])))
        absent = outcome.get("kit_tests_absent")
        if name == "portable test suite" and absent:
            details.append(f"Absent ({name}): {len(absent)} kit test module(s) listed in "
                           f"{outcome.get('installation_state') or 'the installation state'}: "
                           + ", ".join(str(f) for f in absent))
        if status in {"error", "fail", "warning"}:
            details.append(f"{status.upper()} ({name}): {outcome.get('warning') or detail}")
    actions = checks["governing actions"]
    if actions["status"] == "info":
        totals = actions["outcome"]["totals"]
        lines += ["", "## Governing actions since the install baseline", "",
                  "One state per commit and principle. Applicability is declared, so a commit that "
                  "touches no protected path owes no acceptance or transition record. Recorded means a "
                  "joined record exists; it does not establish adequacy, independence or authorization.", "",
                  "| Principle | Recorded | Missing | Not applicable | Unknown |", "| --- | --- | --- | --- | --- |"]
        for p in PRINCIPLES:
            t = totals[p]
            if not isinstance(t, dict):
                raise ValueError("governing-actions totals are malformed")
            lines.append(f"| {PRINCIPLE_NAMES[p]} | {count(t, 'recorded')} | {count(t, 'missing')} | "
                         f"{count(t, 'not-applicable')} | {count(t, 'unknown')} |")
        led = actions["outcome"].get("ledger") or {}
        intake = actions["outcome"].get("intake") or {}
        trans = actions["outcome"].get("transitions") or {}
        lines += ["", f"- Ledger: {cell(led.get('closed_without_verification', 'unknown'))} closed without verification; "
                      f"{cell(led.get('fixed_without_recheck', 'unknown'))} fixed without recheck",
                  f"- Intake: {cell(intake.get('rows', 'unknown'))} rows, {cell(intake.get('checked_nothing_stated', 'unknown'))} with nothing checked"
                  # A report from rc22 on also says what no intake row names.
                  + "".join(f"; named by no row: {cell(unnamed['sources'].get('unnamed', 'unknown'))} data files added "
                            f"under sources/, {cell(unnamed['shared_inventory'].get('unnamed', 'unknown'))} shared-inventory "
                            f"files, {cell(unnamed['snapshots'].get('unnamed', 'unknown'))} snapshots"
                            for unnamed in [intake.get("unnamed_material")]
                            if isinstance(unnamed, dict) and all(isinstance(unnamed.get(k), dict)
                                                                 for k in ("sources", "shared_inventory", "snapshots"))),
                  f"- Transitions: {cell(trans.get('rows', 'unknown'))} rows"]
    lines += ["", "## Attention and limits", ""]
    lines += [f"- {cell(item)}" for item in details] or ["- No failure, execution error, warning or skip reported by these checks."]
    lines += ["", "Open defects and unreviewed work do not fail the workflow check. Model review is not human acceptance; observed closure is not verified correction.", "",
              "Not measured here: PDF build success, shared-storage synchronization, dataset access, open human review comments, or scientific validity.", "",
              "Regenerate from the repository root (render the report even if the check fails):", "", "```bash",
              "python3 scripts/check_workflow.py --out workflow-check.json",
              "python3 scripts/trace_stats.py --report workflow-check.json --out TRACE-STATS.md", "```", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.resolve() == args.out.resolve():
        parser.error("--out must not overwrite the source report")
    code = 0
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        head, dirty = repository_state(ROOT)
        result = render(report, args.report.name, head, dirty)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Replace any older overview so a failed render cannot leave it looking current.
        result = ("# Trace stats\n\n**UNAVAILABLE: workflow report could not be rendered.**\n\n"
                  f"Reason: {cell(type(exc).__name__)}: {cell(str(exc).replace(str(args.report), args.report.name))}\n\n"
                  "No passing check or acceptance can be inferred. Run check_workflow.py and regenerate this overview.\n")
        code = 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(result, encoding="utf-8")
    print(f"{'Unavailable overview' if code else 'Rendered snapshot'}: {args.out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
