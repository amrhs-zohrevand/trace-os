#!/usr/bin/env python3
"""Regression tests for the agentic reviewer's host-side checks.

The script's whole job is to keep apart what a reviewer showed and what it only
said. A bug here does not crash: it quietly upgrades an assertion into evidence,
which is the failure this project is about. So every label is pinned, including
the ones that must NOT be awarded.

Nothing here launches a reviewer, reaches the network, or writes to the
repository's own records. The transcripts are small fixtures in the shape each
backend emits: the codex one from the probe in
.agent-reviews/agentic-review-design-20260920/schema-probe.jsonl, the claude one
trimmed from the live probes of 2026-09-22.

Run:  python3 -m unittest tests.test_agentic_review -v
stdlib only; no pytest and no network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import agentic_review as ar  # noqa: E402

# LESSON (AH-167): each run of this suite left a temporary directory per test that
# made one, 936 of them empty. Every directory a test makes now lives under one
# root the module removes when it is done.
SCRATCH: pathlib.Path | None = None


def setUpModule():
    global SCRATCH
    SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="test-agentic-review-"))


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def scratch() -> pathlib.Path:
    """A fresh directory under the module's scratch root."""
    return pathlib.Path(tempfile.mkdtemp(dir=SCRATCH))


DOUBLE = chr(34)
FILE_TEXT = "alpha beta gamma\nthe second line says something\nthird\n"


def item(command: str, exit_code: int, output: str, status: str = "completed") -> str:
    return json.dumps({"type": "item.completed",
                       "item": {"id": "item_1", "type": "command_execution", "command": command,
                                "exit_code": exit_code, "aggregated_output": output,
                                "status": status}})


TRANSCRIPT = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "01a0"}),
    json.dumps({"type": "turn.started"}),
    item("/bin/zsh -lc \"nl -ba scripts/x.py | sed -n '1,12p'\"", 0,
         "     1\tthe second line says something\n"),
    item("/bin/zsh -lc 'python3 scripts/defect_ledger.py check'", 0,
         "no recurrences, nothing unresolved\n"),
    json.dumps({"type": "turn.completed", "usage": {}}),
]) + "\n"


def finding(**over) -> dict:
    base = {"id": "F001", "severity": "low", "claim": "c", "file": "scripts/x.py",
            "line_start": 2, "line_end": 2, "quote": "the second line says something",
            "why": "w", "ask": "a"}
    base.update(over)
    return base


def reply(**over) -> dict:
    """A reviewer's reply carrying every key the schema requires.

    AH-096: the host holds every loaded findings object against that schema before
    it labels anything, in a fresh run as in a replay, so a fixture that stands in
    for a reply is complete.
    """
    base = {"revision": SHA, "target": "a commit", "criteria": [],
            "evidence_examined": {"paths": [], "commands": []}, "checks": [],
            "evidence_unavailable": [], "findings": [], "unresolved_objections": [],
            "disposition": "advisory", "confidence": 0.5}
    base.update(over)
    return base


def result_for(transcript: str = TRANSCRIPT, findings: list[dict] | None = None,
               checks: list[dict] | None = None, backend: str = "codex") -> dict:
    """A verified result built without Git, by labelling the parts the host owns."""
    ran, label, malformed = ar.transcript_facts(transcript, backend)
    rows = []
    for row in findings if findings is not None else [finding()]:
        verdict, note = ar.classify_quote(FILE_TEXT, row)
        rows.append(dict(row, quote_label=verdict, quote_note=note,
                         seen_label=ar.seen_label(str(row.get("quote") or ""), ran)))
    return {"findings": rows, "ran": ran, "transcript": label, "malformed": malformed,
            "checks": [dict(c, **ar.check_label(c, ran)) for c in checks or []],
            "outside": ar.outside_packet(ran), "modified": [],
            "denied": ar.denied_calls(transcript, backend)}


def one_dir(path) -> list[str]:
    """`--out-dir` and `--review-dir` naming one directory, so a test finds every
    file a run writes in one place, as before the two were split (2026-10-14),
    and nothing lands in this repository's own ai/reviews/."""
    return ["--out-dir", str(path), "--review-dir", str(path)]


def meta_for(**over) -> dict:
    base = {"sha": "6eefd2a170ab67b0fe9012d3e98a77a08f1641ab", "subject": "s",
            "model": "gpt-6-astra", "backend": "codex", "identity": "requested",
            "at": "20260920T120000Z",
            "sandbox": "read-only", "records": "minimal", "authority": "", "authorized_by": "",
            "decision": "", "criteria": "", "objections": "", "involvement": [], "producers": [],
            "selectors": [], "findings_json": {}}
    base.update(over)
    return base


class QuoteLabels(unittest.TestCase):
    """Three outcomes, because conflating them would libel a reviewer."""

    def test_a_quote_on_its_cited_lines_is_verified(self):
        self.assertEqual(ar.classify_quote(FILE_TEXT, finding())[0], "verified")

    def test_a_quote_split_over_a_newline_still_verifies(self):
        row = finding(line_start=1, line_end=2, quote="alpha beta gamma\n  the second line")
        self.assertEqual(ar.classify_quote(FILE_TEXT, row)[0], "verified")

    def test_real_text_at_the_wrong_lines_is_misplaced_not_invented(self):
        label, note = ar.classify_quote(FILE_TEXT, finding(line_start=3, line_end=3))
        self.assertEqual(label, "misplaced")
        self.assertIn("lines 3-3", note)

    def test_text_that_is_nowhere_in_the_file_is_invented(self):
        self.assertEqual(ar.classify_quote(FILE_TEXT, finding(quote="a line nobody wrote"))[0],
                         "invented")

    def test_a_file_the_commit_does_not_have_is_invented_and_says_which(self):
        label, note = ar.classify_quote(None, finding(file="scripts/gone.py"))
        self.assertEqual(label, "invented")
        self.assertIn("scripts/gone.py", note)

    def test_a_finding_with_no_quote_is_invented_rather_than_excused(self):
        self.assertEqual(ar.classify_quote(FILE_TEXT, finding(quote=""))[0], "invented")

    def test_case_is_not_folded_so_a_retyped_quote_is_not_called_verified(self):
        self.assertEqual(ar.classify_quote(FILE_TEXT, finding(quote="The Second Line Says"))[0],
                         "invented")


class SeenInOutput(unittest.TestCase):
    """A path substring meant nothing: a printf satisfied it. The quote must appear."""

    def test_a_quote_the_reviewer_printed_is_seen(self):
        ran, _, _ = ar.transcript_facts(TRANSCRIPT)
        self.assertEqual(ar.seen_label("the second line says something", ran), "seen in output")

    def test_a_quote_no_command_printed_is_not_seen(self):
        ran, _, _ = ar.transcript_facts(TRANSCRIPT)
        self.assertEqual(ar.seen_label("alpha beta gamma", ran), "not seen in output")

    def test_an_empty_quote_is_never_seen(self):
        ran, _, _ = ar.transcript_facts(TRANSCRIPT)
        self.assertEqual(ar.seen_label("", ran), "not seen in output")


class ReportedChecks(unittest.TestCase):
    """A reported check is a claim until the transcript shows the command completing."""

    def test_a_check_that_ran_carries_the_exit_code_the_transcript_recorded(self):
        ran, _, _ = ar.transcript_facts(TRANSCRIPT)
        row = ar.check_label({"name": "ledger", "command": "python3 scripts/defect_ledger.py check",
                              "outcome": "clean"}, ran)
        self.assertEqual(row["label"], "matched (exit 0)")
        self.assertEqual(row["exit_code"], 0)
        self.assertIn("no recurrences", row["tail"])

    def test_a_check_that_ran_and_failed_is_matched_not_reported_as_unrun(self):
        """Codex marks a non-zero exit `failed`, so reading only `completed` hid
        exactly the checks that failed. The first real run, on 6eefd2a, reported a
        test suite that had run and failed as a check nobody ran."""
        ran, _, _ = ar.transcript_facts(
            item("/bin/zsh -lc 'python3 -m unittest discover -s tests'", 1,
                 "FAILED (failures=8, errors=2)\n", status="failed") + "\n")
        row = ar.check_label({"name": "suite", "command": "python3 -m unittest discover -s tests",
                              "outcome": "8 failures"}, ran)
        self.assertEqual(row["label"], "matched (exit 1)")
        self.assertEqual(row["exit_code"], 1)

    def test_a_check_nobody_ran_is_visible_as_having_no_command(self):
        ran, _, _ = ar.transcript_facts(TRANSCRIPT)
        row = ar.check_label({"name": "suite", "command": "python3 -m unittest discover -s tests",
                              "outcome": "all green"}, ran)
        self.assertEqual(row["label"], "no matching command")
        self.assertIsNone(row["exit_code"])


class IssueOneCheckMatching(unittest.TestCase):
    """Codex round 3, issue 1: a check earned `matched` for text inside an echo."""

    def matched(self, command: str, want: str = "python3 scripts/defect_ledger.py check") -> str:
        ran, _, _ = ar.transcript_facts(item(command, 0, "") + "\n")
        return ar.check_label({"name": "ledger", "command": want, "outcome": "clean"}, ran)["label"]

    def test_a_check_quoted_inside_an_echo_is_not_matched(self):
        self.assertEqual(
            self.matched("""/bin/zsh -lc 'echo "python3 scripts/defect_ledger.py check"'"""),
            "no matching command")

    def test_a_compound_command_does_not_credit_the_check_it_ends_with(self):
        self.assertEqual(
            self.matched("/bin/zsh -lc 'cd x && python3 scripts/defect_ledger.py check'"),
            "no matching command")

    def test_the_command_the_shell_actually_ran_is_matched(self):
        self.assertEqual(
            self.matched("/bin/zsh -lc 'python3 scripts/defect_ledger.py check'"),
            "matched (exit 0)")

    def test_trailing_arguments_and_a_pipe_match_without_claiming_the_exit(self):
        self.assertEqual(
            self.matched("/bin/zsh -lc 'python3 scripts/defect_ledger.py check 2>&1 | tail -2'"),
            "matched, exit not isolated (shell exit 0)")

    def test_a_nested_shell_wrapper_is_peeled_before_comparing(self):
        nested = "/bin/zsh -lc " + '"' + "sh -c 'python3 scripts/defect_ledger.py check'" + '"'
        self.assertEqual(self.matched(nested), "matched (exit 0)")


class FindingOneExitIsolation(unittest.TestCase):
    """Round 4, F001: a prefix match credited `--help`, and a pipeline's exit
    status was reported as the check's."""

    def matched(self, command: str, want: str, exit_code: int = 0) -> str:
        ran, _, _ = ar.transcript_facts(item(command, exit_code, "") + "\n")
        return ar.check_label({"name": "c", "command": want, "outcome": "x"}, ran)["label"]

    def test_extra_arguments_do_not_earn_the_checks_own_exit_status(self):
        label = self.matched("/bin/zsh -lc 'python3 scripts/defect_ledger.py check --help'",
                             "python3 scripts/defect_ledger.py check")
        self.assertNotIn("matched (exit", label)
        self.assertEqual(label, "matched, exit not isolated (shell exit 0)")

    def test_a_pipeline_reports_the_shell_exit_not_the_checks(self):
        inner = "python3 -c 'raise SystemExit(7)'"
        piped = '/bin/zsh -lc ' + DOUBLE + inner + ' | cat' + DOUBLE
        self.assertEqual(self.matched(piped, inner),
                         "matched, exit not isolated (shell exit 0)")

    def test_the_exact_command_still_carries_its_own_exit_status(self):
        inner = "python3 -c 'raise SystemExit(7)'"
        plain = '/bin/zsh -lc ' + DOUBLE + inner + DOUBLE
        self.assertEqual(self.matched(plain, inner, exit_code=7), "matched (exit 7)")

    def test_an_isolated_match_keeps_the_exit_code_and_a_loose_one_does_not(self):
        ran, _, _ = ar.transcript_facts(
            item("/bin/zsh -lc 'python3 scripts/defect_ledger.py check --help'", 0, "") + "\n")
        row = ar.check_label({"command": "python3 scripts/defect_ledger.py check"}, ran)
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["shell_exit"], 0)


class AnExitTheTranscriptNeverRecorded(unittest.TestCase):
    """AH-094: a row whose exit code is None used to be labelled `matched (exit
    None)`, so a tool-level failure carrying no execution evidence earned a matched
    check with an exit status nobody recorded."""

    WANT = "python3 scripts/defect_ledger.py check"

    def ran(self, exit_code) -> list[dict]:
        return [{"command": self.WANT, "exit_code": exit_code, "output": "some output\n",
                 "status": "failed" if exit_code else "completed"}]

    def test_a_row_with_no_recorded_exit_says_so_instead_of_naming_one(self):
        row = ar.check_label({"name": "ledger", "command": self.WANT, "outcome": "clean"},
                             self.ran(None))
        self.assertEqual(row["label"], "matched, exit not recorded")
        self.assertIsNone(row["exit_code"])
        self.assertIsNone(row["shell_exit"])
        self.assertNotIn("None", row["label"])

    def test_a_row_with_a_recorded_exit_is_unchanged(self):
        row = ar.check_label({"name": "ledger", "command": self.WANT, "outcome": "clean"},
                             self.ran(2))
        self.assertEqual(row["label"], "matched (exit 2)")
        self.assertEqual(row["exit_code"], 2)

    def test_the_tally_counts_the_unrecorded_exits_apart(self):
        checks = [dict(entry, **ar.check_label(entry, self.ran(None)))
                  for entry in [{"name": "ledger", "command": self.WANT, "outcome": "clean"}]]
        result = dict(result_for(), checks=checks)
        _, matched, _ = ar.tally(result)
        self.assertIn("checks: 1 of 1 matched a command in the transcript", matched)
        self.assertIn("1 of them without a recorded exit", matched)
        self.assertNotIn("without a recorded exit", ar.tally(result_for(checks=[
            {"name": "ledger", "command": self.WANT, "outcome": "clean"}]))[1])

    def test_a_claude_failure_with_no_exit_line_is_such_a_row(self):
        """The case the live run produced: an is_error result whose content does not
        start `Exit code N` leaves the reader nothing to record."""
        transcript = "\n".join([
            claude_use("toolu_11", "Bash", command=self.WANT),
            claude_reply("toolu_11", "the tool failed before the shell started", is_error=True),
        ]) + "\n"
        ran, _, _ = ar.transcript_facts(transcript, "claude")
        self.assertIsNone(ran[0]["exit_code"])
        self.assertEqual(ar.check_label({"name": "ledger", "command": self.WANT,
                                         "outcome": "clean"}, ran)["label"],
                         "matched, exit not recorded")


class IssueFiveTranscriptShape(unittest.TestCase):
    """Codex round 3, issue 5: a bare marker plus rubbish read as auditable."""

    def test_a_bare_completion_marker_is_not_auditable(self):
        ran, label, malformed = ar.transcript_facts('{"type":"item.completed"}\nnot json\n')
        self.assertEqual(ran, [])
        self.assertTrue(label.startswith("unauditable"), label)
        self.assertEqual(malformed["completion_without_a_typed_item"], 1)
        self.assertEqual(malformed["invalid_json"], 1)

    def test_malformed_lines_are_counted_and_named_in_the_label(self):
        ran, label, malformed = ar.transcript_facts(
            item("true", 0, "") + "\nnot json\n{\"type\":\"item.completed\"}\n")
        self.assertEqual(len(ran), 1)
        self.assertEqual(label, "auditable, 2 malformed line(s) skipped")
        self.assertEqual(sum(malformed.values()), 2)

    def test_a_clean_transcript_carries_no_malformed_count(self):
        _, label, malformed = ar.transcript_facts(TRANSCRIPT)
        self.assertEqual(label, "auditable")
        self.assertEqual(sum(malformed.values()), 0)


class TranscriptAndPacket(unittest.TestCase):
    def test_a_codex_transcript_with_completed_items_is_auditable(self):
        ran, label, _ = ar.transcript_facts(TRANSCRIPT)
        self.assertEqual(label, "auditable")
        self.assertEqual(len(ran), 2)

    def test_a_transcript_with_no_completed_items_is_unauditable_not_an_empty_run(self):
        ran, label, _ = ar.transcript_facts("not json\n{\"type\": \"turn.started\"}\n")
        self.assertTrue(label.startswith("unauditable"))
        self.assertEqual(ran, [])

    def test_a_command_naming_the_main_checkout_is_flagged_after_the_fact(self):
        ran, _, _ = ar.transcript_facts(item(f"ls {ROOT}/paper", 0, "") + "\n")
        self.assertEqual(len(ar.outside_packet(ran, ROOT)), 1)

    def test_a_command_naming_a_records_path_is_flagged_too(self):
        ran, _, _ = ar.transcript_facts(item("cat ai/records/20260919-x/evidence.json", 0, "") + "\n")
        self.assertEqual(len(ar.outside_packet(ran, ROOT)), 1)

    def test_a_command_inside_the_packet_is_not_flagged(self):
        ran, _, _ = ar.transcript_facts(TRANSCRIPT)
        self.assertEqual(ar.outside_packet(ran, ROOT), [])

    def test_a_command_that_exited_non_zero_is_still_a_command_it_ran(self):
        ran, label, _ = ar.transcript_facts(item("false", 1, "", status="failed") + "\n")
        self.assertEqual(label, "auditable")
        self.assertEqual([r["exit_code"] for r in ran], [1])

    def test_byte_code_caches_are_not_counted_as_paths_the_reviewer_modified(self):
        """80 .pyc files buried the one write worth seeing in the first real run."""
        with tempfile.TemporaryDirectory() as directory:
            packet = pathlib.Path(directory)
            (packet / "scripts").mkdir()
            (packet / "scripts" / "a.py").write_text("x", encoding="utf-8")
            before = ar.manifest(packet)
            (packet / "scripts" / "__pycache__").mkdir()
            (packet / "scripts" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00")
            (packet / "scripts" / "b.py").write_text("y", encoding="utf-8")
            self.assertEqual(ar.modified(before, ar.manifest(packet)), ["added scripts/b.py"])

    def test_a_write_inside_the_packet_shows_in_the_manifest_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = pathlib.Path(directory)
            (packet / "kept.txt").write_text("same", encoding="utf-8")
            (packet / "edited.txt").write_text("before", encoding="utf-8")
            before = ar.manifest(packet)
            (packet / "edited.txt").write_text("after", encoding="utf-8")
            (packet / "new.txt").write_text("new", encoding="utf-8")
            self.assertEqual(ar.modified(before, ar.manifest(packet)),
                             ["added new.txt", "changed edited.txt"])

    def test_a_read_only_run_that_wrote_nothing_reports_an_empty_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = pathlib.Path(directory)
            (packet / "kept.txt").write_text("same", encoding="utf-8")
            before = ar.manifest(packet)
            self.assertEqual(ar.modified(before, ar.manifest(packet)), [])

    def test_a_replay_says_the_packet_was_not_hashed_rather_than_nothing_changed(self):
        self.assertIsNone(ar.modified({}, {}))


class AFileToolCallOutsideThePacket(unittest.TestCase):
    """AH-101: a claude reviewer reaches a path with Read, Grep or Glob rather than
    with a shell, so reading only the commands left such a call invisible to every
    label while the row said a read elsewhere was detected."""

    def outside(self, transcript: str, backend: str = "claude") -> list[dict]:
        ran, _, _ = ar.transcript_facts(transcript, backend)
        return ar.outside_packet(ran, ROOT, transcript, backend)

    def test_a_read_of_the_main_checkout_is_listed_by_its_tool_name(self):
        transcript = claude_use("toolu_21", "Read", file_path=f"{ROOT}/paper/main.tex") + "\n"
        found = self.outside(transcript)
        self.assertEqual([row["command"] for row in found], [f"Read {ROOT}/paper/main.tex"])
        self.assertEqual(found[0]["status"], "file-tool call")

    def test_a_grep_or_glob_naming_a_records_path_is_listed_too(self):
        transcript = "\n".join([
            claude_use("toolu_22", "Grep", pattern="secret", path="ai/records"),
            claude_use("toolu_23", "Glob", glob="ai/records/**/evidence.json"),
        ]) + "\n"
        self.assertEqual([row["command"] for row in self.outside(transcript)],
                         ["Grep ai/records", "Glob ai/records/**/evidence.json"])

    def test_a_call_inside_the_packet_is_not_listed(self):
        transcript = "\n".join([
            claude_use("toolu_24", "Read", file_path="scripts/agentic_review.py"),
            claude_use("toolu_25", "Grep", pattern="def main", path="scripts"),
        ]) + "\n"
        self.assertEqual(self.outside(transcript), [])

    def test_one_call_is_listed_once_however_many_of_its_inputs_name_it(self):
        transcript = claude_use("toolu_26", "Grep", pattern="ai/records",
                                path=f"{ROOT}/ai") + "\n"
        self.assertEqual(len(self.outside(transcript)), 1)

    def test_a_command_and_a_call_are_both_listed_and_counted(self):
        transcript = "\n".join([
            claude_use("toolu_27", "Bash", command="cat ai/records/x/evidence.json"),
            claude_reply("toolu_27", "text"),
            claude_use("toolu_28", "Read", file_path="ai/records/x/summary.md"),
        ]) + "\n"
        found = self.outside(transcript)
        self.assertEqual([row["command"] for row in found],
                         ["cat ai/records/x/evidence.json", "Read ai/records/x/summary.md"])

    def test_the_codex_transcript_is_read_for_commands_only(self):
        """codex has no file tools, and nothing in its stream declares one, so
        nothing is claimed there."""
        ran, _, _ = ar.transcript_facts(item("cat ai/records/x/evidence.json", 0, "") + "\n")
        self.assertEqual(len(ar.outside_packet(ran, ROOT, TRANSCRIPT, "codex")), 1)
        self.assertEqual(ar.file_tool_calls(CLAUDE_TRANSCRIPT, "codex", ROOT), [])

    def test_the_report_and_the_row_say_a_call_is_covered_as_well_as_a_command(self):
        transcript = claude_use("toolu_29", "Read", file_path=f"{ROOT}/ai/records/x/t.json") + "\n"
        result = dict(result_for(transcript, backend="claude"),
                      outside=self.outside(transcript))
        meta = meta_for(backend="claude", identity="observed",
                        sandbox=ar.sandbox_of("claude", True))
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta, result,
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("## Commands and file-tool calls naming the main checkout or a records path",
                      text)
        self.assertIn(f"- `Read {ROOT}/ai/records/x/t.json`", text)
        self.assertIn("a command or a file-tool call that only names the string is listed too",
                      text)
        row = ar.check_row(meta, result, pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertIn("a command or a file-tool call names the main checkout", row["limits"])


class RecordsView(unittest.TestCase):
    """The minimal view says what it omits, and never claims to have cleaned free text."""

    def build(self, directory: str) -> dict:
        record = pathlib.Path(directory) / "ai" / "records" / "20260920-x"
        record.mkdir(parents=True)
        (record / "evidence.json").write_text(json.dumps(
            {"record_id": "20260920-x",
             "turns": [{"id": "T1", "text": "SECRET HUMAN PROMPT about a colleague"},
                       {"id": "T2", "text": "ANOTHER PRIVATE TURN"}]}), encoding="utf-8")
        (record / "decisions.jsonl").write_text(json.dumps(
            {"question": "Which default?", "choice": "the minimal view", "status": "selected",
             "human_input": "SECRET HUMAN PROMPT about a colleague",
             "human_direction": "SECRET HUMAN PROMPT about a colleague",
             "human_contribution": "SECRET HUMAN PROMPT about a colleague",
             "human_inputs": ["T1"]}) + "\n", encoding="utf-8")
        (record / "checks.jsonl").write_text(json.dumps(
            {"kind": "code-test", "runner": "pytest", "method": "m",
             "result": "PASS: 12 tests", "artifact": "a"}) + "\n", encoding="utf-8")
        return ar.minimal_view("ai/records/20260920-x", ["abc1234"],
                               root=pathlib.Path(directory))

    def test_the_view_carries_the_id_the_commits_the_turn_count_and_the_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            view = self.build(directory)
        self.assertEqual(view["record"], "ai/records/20260920-x")
        self.assertEqual(view["commits_under_review"], ["abc1234"])
        self.assertNotIn("commits", view, "AH-341: a bare `commits` read as the record's whole list")
        self.assertEqual(view["turn_count"], 2)
        self.assertEqual(view["decisions"][0]["choice"], "the minimal view")
        self.assertEqual(view["checks"], [{"name": "code-test", "outcome": "PASS: 12 tests"}])

    def test_a_decisions_human_input_field_and_the_turns_are_not_exported(self):
        """Codex round 3, issue 3. The fields named here are the ones the view can
        actually withhold; a question or choice is free text and is not cleaned."""
        with tempfile.TemporaryDirectory() as directory:
            view = self.build(directory)
        rendered = json.dumps(view)
        self.assertNotIn("human_input", rendered)
        # Both spellings of the direction field: human_direction is what the kit
        # writes since 2026-09-20, human_contribution what records before it carry.
        self.assertNotIn("human_direction", rendered)
        self.assertNotIn("human_contribution", rendered)
        self.assertNotIn("turns", view)
        self.assertNotIn("SECRET HUMAN PROMPT", rendered)
        self.assertNotIn("ANOTHER PRIVATE TURN", rendered)
        # The agent's stated reason goes too since rc22 (Miros, 14 October 2026); it is the
        # agent's own account, like the question and the choice.
        self.assertEqual(sorted(view["decisions"][0]), ["choice", "question", "reason", "status"])

    def test_the_view_states_the_limit_it_cannot_enforce(self):
        with tempfile.TemporaryDirectory() as directory:
            view = self.build(directory)
        self.assertIn("may paraphrase the human's direction", view["limit"])
        self.assertIn("coverage manifest", view["withheld"])

    def test_records_full_refuses_until_a_person_is_named_as_having_inspected_them(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ar.main(["6eefd2a", "--records", "full", "--no-record"])
        self.assertEqual(code, 2)
        self.assertIn("--inspected-by", err.getvalue())


class TheRecordedRow(unittest.TestCase):
    """The advisory row is the only place the evidence link lands, so it is pinned here."""

    def call(self, **over) -> list[str]:
        checks = [{"name": "ledger", "command": "python3 scripts/defect_ledger.py check",
                   "outcome": "clean"},
                  {"name": "suite", "command": "python3 -m unittest discover -s tests",
                   "outcome": "all green"}]
        result = result_for(checks=checks)
        return ar.record_argv(meta_for(**over), result, pathlib.Path("x/report.md"), "d1ge57")

    def test_the_range_covers_exactly_one_commit_merge_or_root_included(self):
        argv = self.call()
        self.assertIn("--range", argv)
        self.assertEqual(argv[argv.index("--range") + 1],
                         "6eefd2a170ab67b0fe9012d3e98a77a08f1641ab^!")

    def test_the_default_run_is_advisory_model_review(self):
        argv = self.call()
        self.assertIn("--agent", argv)
        self.assertNotIn("--authorized-by", argv)
        self.assertNotIn("--decision", argv)

    def test_a_delegated_run_passes_the_authorizations_words_as_authority(self):
        # LESSON (rc22): trace_record.py accept records a delegated decision
        # without --authority as advisory, and this call passed the words
        # only inside --independence, so every delegated run read advisory.
        argv = self.call(authorized_by="Jane Roe", authority="T3: accept within the stated criteria",
                         decision="accept")
        self.assertIn("--authorized-by", argv)
        self.assertEqual(argv[argv.index("--authority") + 1], "T3: accept within the stated criteria")

    def test_the_scope_carries_the_counts_the_mode_and_the_report_digest(self):
        argv = self.call()
        scope = argv[argv.index("--scope") + 1]
        self.assertIn("1 findings (1 quote-verified, 1 seen in output)", scope)
        self.assertIn("checks: 1 of 2 matched a command in the transcript", scope)
        self.assertIn("records: minimal", scope)
        self.assertIn("outside-packet commands: 0", scope)
        self.assertIn("transcript auditable", scope)
        self.assertIn("report x/report.md sha256 d1ge57", scope)
        self.assertIn("objections only, not acceptance", scope)

    def test_a_delegated_run_passes_the_decision_the_evidence_and_the_authority(self):
        argv = self.call(authorized_by="Miros Zohrehvand", decision="return",
                         authority="Authorized in this session's prompt.",
                         criteria="the five governing principles",
                         objections="one high finding stands")
        self.assertNotIn("--agent", argv)
        self.assertEqual(argv[argv.index("--decision") + 1], "return")
        self.assertEqual(argv[argv.index("--authorized-by") + 1], "Miros Zohrehvand")
        self.assertEqual(argv[argv.index("--evidence") + 1], "x/report.md")
        self.assertEqual(argv[argv.index("--objections") + 1], "one high finding stands")
        self.assertIn("Authorized in this session's prompt.",
                      argv[argv.index("--independence") + 1])
        self.assertIn("delegated decision recorded", argv[argv.index("--scope") + 1])


class TheCheckRow(unittest.TestCase):
    """A run is a run of the kit's trace-review procedure, so the host writes the
    skill run contract's row itself, and the version comes off the skill file."""

    def row(self, version="sha256:" + "ab" * 32, replay=False, **over) -> dict:
        checks = [{"name": "ledger", "command": "python3 scripts/defect_ledger.py check",
                   "outcome": "clean"}]
        result = result_for(checks=checks)
        return ar.check_row(meta_for(**over), result, pathlib.Path("x") / "report.md", "d1ge57",
                            version, replay)

    def test_the_row_carries_every_field_the_run_contract_names(self):
        row = self.row()
        for key in ("kind", "runner", "method", "result", "artifact", "candidate", "criterion",
                    "limits", "procedure", "procedure_version"):
            self.assertTrue(isinstance(row[key], str) and row[key].strip(), key)
        self.assertEqual(row["procedure"], "trace-review")
        self.assertEqual(row["procedure_version"], "sha256:" + "ab" * 32)
        self.assertEqual(row["reference"], "path")
        self.assertEqual(row["artifact"], "x/report.md")
        # An empty list fails the record validator, and the ids exist only once a
        # finding enters correction work, so the row leaves the field to the agent.
        self.assertNotIn("defect_ids", row)
        self.assertIn("6eefd2a170ab67b0fe9012d3e98a77a08f1641ab", row["candidate"])
        self.assertIn("1 findings (1 quote-verified, 1 seen in output)", row["result"])
        self.assertIn("checks: 1 of 1 matched a command in the transcript", row["result"])
        self.assertIn("report sha256 d1ge57", row["result"])
        self.assertIn("model review, not acceptance", row["limits"])

    def test_the_criterion_is_the_researchers_when_set_and_the_contracts_otherwise(self):
        row = self.row(criteria="every claim quotes its source",
                       findings_json={"criteria": ["hygiene"]})
        self.assertIn("requested: every claim quotes its source", row["criterion"])
        self.assertIn("reviewer reported applying: hygiene", row["criterion"])
        self.assertIn("criteria set: every claim quotes its source", row["method"])
        self.assertIn("agentic-review-contract.md", self.row()["criterion"])

    def test_no_skill_file_means_no_version_and_the_limits_say_so(self):
        row = self.row(version=None)
        self.assertNotIn("procedure_version", row)
        self.assertIn("no procedure_version is derived", row["limits"])
        self.assertEqual(row["procedure"], "trace-review")

    def test_a_replay_and_an_involved_reviewer_are_written_into_the_limits(self):
        row = self.row(replay=True, involvement=["gpt-6-astra executed the covered commit"])
        self.assertIn("a replay", row["limits"])
        self.assertIn("NOT independent: gpt-6-astra executed the covered commit", row["limits"])
        self.assertIn("delegated decision return",
                      self.row(authorized_by="Miros Zohrehvand", decision="return")["result"])

    def test_the_version_is_read_off_the_skill_file_the_project_holds(self):
        root = scratch()
        self.assertIsNone(ar.procedure_version(root))
        (root / ".skills" / "trace-review").mkdir(parents=True)
        # write_bytes, not write_text: on Windows the text mode turns the newline
        # into CRLF while procedure_version digests the bytes on disk, so the
        # expected sha256(b"text\n") no longer matched (both Windows runners of
        # GitHub rehearsal run 35734397953 on bf2bd8e1).
        (root / ".skills" / "trace-review" / "SKILL.md").write_bytes(b"text\n")
        import hashlib
        self.assertEqual(ar.procedure_version(root), "sha256:" + hashlib.sha256(b"text\n").hexdigest())


class TheCheckRowThroughTheValidator(unittest.TestCase):
    """AH-085: the generated row is read by trace_record.py's own validator, so the
    row is passed through it here, in a synthetic record, with and without a
    derivable version. An empty defect_ids list was what it rejected."""

    DIGEST = "ab" * 32

    def evaluate(self, version):
        import trace_record as tr
        root = scratch()
        record = root / "record"
        record.mkdir()
        (record / "summary.md").write_text("The author set the direction (T1).", encoding="utf-8")
        (record / "evidence.json").write_text(json.dumps({
            "record_id": "test", "record_schema_version": 2,
            "consent": {"third_party_review": "cleared"},
            "instructions": {"files": {".skills/trace-review/SKILL.md": self.DIGEST},
                             "basis": "file state at extraction time"},
            "turns": [{"id": "T1", "text": "first"}]}), encoding="utf-8")
        row = ar.check_row(meta_for(), result_for(checks=[
            {"name": "ledger", "command": "python3 scripts/defect_ledger.py check",
             "outcome": "clean"}]), pathlib.Path("x") / "report.md", "d1ge57", version)
        (record / "checks.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            tr.cmd_cover(type("A", (), {"record": str(record)})())
        return tr.evaluate_record(record)

    def test_the_row_passes_the_validator_with_a_matching_version(self):
        found = self.evaluate("sha256:" + self.DIGEST)
        self.assertEqual([p for p in found["problems"] if "checks.jsonl" in p], [])
        self.assertTrue(found["ok"], found["problems"])
        self.assertTrue(any("1 matching this record's own digest" in note
                            for note in found["notes"]), found["notes"])

    def test_the_row_passes_the_validator_without_a_version(self):
        found = self.evaluate(None)
        self.assertEqual([p for p in found["problems"] if "checks.jsonl" in p], [])
        self.assertTrue(found["ok"], found["problems"])


class TheCheckRowOnDisk(unittest.TestCase):
    """The row reaches the run directory always, and the named record on request."""

    def replay(self, *extra) -> tuple[int, pathlib.Path]:
        directory = scratch()
        (directory / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "f.json").write_text(json.dumps(reply()), encoding="utf-8")
        with mock.patch.object(ar, "git", fake_git), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = ar.main([SHA, "--replay", "--transcript", str(directory / "t.jsonl"),
                            "--findings", str(directory / "f.json"), "--no-record",
                            *one_dir(directory / "out"), *extra])
        return code, directory

    def test_check_row_json_is_written_beside_the_record_call(self):
        code, directory = self.replay()
        self.assertEqual(code, 0)
        row = json.loads((directory / "out" / "check-row.json").read_text(encoding="utf-8"))
        self.assertEqual(row["procedure"], "trace-review")
        self.assertTrue((directory / "out" / "record-call.json").exists())

    def test_checks_jsonl_appends_one_line_per_run_and_never_overwrites(self):
        target = scratch() / "staging" / "checks.jsonl"
        code, _ = self.replay("--checks-jsonl", str(target))
        self.assertEqual(code, 0)
        self.replay("--checks-jsonl", str(target))
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            row = json.loads(line)
            self.assertEqual(row["procedure"], "trace-review")
            self.assertEqual(row["artifact"], row["artifact"].replace("\\", "/"))
            self.assertIn("candidate", row)

    def test_a_failed_recording_appends_nothing_and_keeps_the_row_in_the_run_directory(self):
        target = scratch() / "checks.jsonl"
        directory = scratch()
        (directory / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "f.json").write_text(json.dumps(reply()), encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar, "run_recorder", return_value=1), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--replay", "--transcript", str(directory / "t.jsonl"),
                            "--findings", str(directory / "f.json"),
                            *one_dir(directory / "out"), "--checks-jsonl", str(target)])
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())
        self.assertIn("not appended", err.getvalue())
        self.assertTrue((directory / "out" / "check-row.json").exists())

    def test_a_refused_run_appends_nothing(self):
        target = scratch() / "checks.jsonl"
        directory = scratch()
        (directory / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "f.json").write_text(json.dumps(reply(revision="another")), encoding="utf-8")
        with mock.patch.object(ar, "git", fake_git), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = ar.main([SHA, "--replay", "--transcript", str(directory / "t.jsonl"),
                            "--findings", str(directory / "f.json"), "--no-record",
                            *one_dir(directory / "out"), "--checks-jsonl", str(target)])
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())


class Involvement(unittest.TestCase):
    """An actor that produced the work is not independent of it, however it is named."""

    def test_executing_a_covered_commit_counts_however_the_trailer_is_cased(self):
        self.assertEqual(len(ar.involved(["GPT-6-Astra"], [], "gpt-6-astra")), 1)

    def test_selecting_a_recorded_decision_counts_as_well_as_executing(self):
        hits = ar.involved([], ["gpt-6-astra"], "gpt-6-astra")
        self.assertEqual(len(hits), 1)
        self.assertIn("selected a recorded decision", hits[0])

    def test_a_reviewer_with_no_part_in_the_work_gets_no_involvement_note(self):
        self.assertEqual(ar.involved(["claude-opus-5"], ["claude-opus-5"], "gpt-6-astra"), [])

    def test_a_bracketed_or_dated_cli_id_still_matches_the_trailer_it_names(self):
        """AH-100: the trailers here are short names and a CLI reports a dated or
        bracketed id, so an exact match let a reviewer of its own work pass as
        independent. Case, a trailing `[...]` and a trailing date come off, and
        nothing else does."""
        self.assertEqual(len(ar.involved(["claude-opus-5"], [], "claude-opus-5[1m]")), 1)
        self.assertEqual(len(ar.involved(["claude-haiku-4-5"], [],
                                         "claude-haiku-4-5-20251001")), 1)
        self.assertEqual(len(ar.involved(["Claude-Opus-5"], [], "claude-opus-5-20260514")), 1)

    def test_a_different_model_of_the_same_family_is_not_the_same_actor(self):
        """No prefix and no lineage matching: claude-fable-5-1 did not execute the
        commit claude-opus-5 executed, and a shortened id is a different model."""
        self.assertEqual(ar.involved(["claude-opus-5"], [], "claude-fable-5-1"), [])
        self.assertEqual(ar.involved(["claude-opus-5-1"], [], "claude-opus-5"), [])
        self.assertEqual(ar.involved(["claude-haiku-4-5-20251001"], [], "claude-haiku-4"), [])

    def test_an_involved_reviewer_records_advisory_and_states_the_involvement(self):
        hits = ar.involved(["gpt-6-astra"], [], "gpt-6-astra")
        argv = ar.record_argv(meta_for(involvement=hits, producers=["gpt-6-astra"],
                                       authorized_by="", decision="return"),
                              result_for(), pathlib.Path("x/report.md"), "d1ge57")
        self.assertIn("--agent", argv)
        self.assertNotIn("--decision", argv)
        self.assertIn("reviewer participated in producing the covered work",
                      argv[argv.index("--scope") + 1])
        self.assertIn("NOT independent", argv[argv.index("--independence") + 1])


class IssueFourTheReport(unittest.TestCase):
    """Codex round 3, issue 4: the report printed the claim and dropped the evidence."""

    def render(self, **over) -> str:
        checks = [{"name": "ledger", "command": "python3 scripts/defect_ledger.py check",
                   "outcome": "FICTIONAL OUTCOME the reviewer typed"}]
        result = result_for(checks=checks)
        meta = meta_for(findings_json={
            "evidence_unavailable": ["Git history is absent from the packet."],
            "unresolved_objections": ["One high finding stands."]}, **over)
        with tempfile.TemporaryDirectory() as directory:
            return ar.write_report(pathlib.Path(directory), meta, result,
                                   ["the tree"]).read_text(encoding="utf-8")

    def test_the_captured_line_is_printed_beside_the_claimed_outcome(self):
        text = self.render()
        self.assertIn("FICTIONAL OUTCOME the reviewer typed", text)
        self.assertIn("last line captured from the transcript: no recurrences, nothing unresolved",
                      text)

    def test_evidence_unavailable_and_unresolved_objections_reach_the_report(self):
        text = self.render()
        self.assertIn("Git history is absent from the packet.", text)
        self.assertIn("One high finding stands.", text)

    def test_a_findings_why_and_ask_are_not_dropped(self):
        text = self.render()
        self.assertIn("- why: w", text)
        self.assertIn("- ask: a", text)
        self.assertIn("- quote: verified", text)


class TheCleanTranscriptLine(unittest.TestCase):
    """AH-099: with nothing malformed the report said 'every completion row carried
    a typed item' for every backend, a check only the codex reader performs. The
    sentence is built from the counters the reader kept, so it cannot drift."""

    def test_each_backend_states_the_check_its_own_reader_made(self):
        self.assertEqual(ar.transcript_clean(dict.fromkeys(ar.MALFORMED["codex"], 0)),
                         "Every line parsed, and every completion row carried a typed item.")
        self.assertEqual(ar.transcript_clean(dict.fromkeys(ar.MALFORMED["claude"], 0)),
                         "Every line parsed, and every tool result matched a tool use.")

    def test_every_counter_a_reader_keeps_has_its_own_words(self):
        for backend, names in ar.MALFORMED.items():
            self.assertIn(names[1], ar.CLEAN_TRANSCRIPT, backend)

    def report(self, transcript: str, backend: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            return ar.write_report(pathlib.Path(directory),
                                   meta_for(backend=backend),
                                   result_for(transcript, backend=backend),
                                   ["the tree"]).read_text(encoding="utf-8")

    def test_a_clean_claude_run_does_not_claim_a_check_of_the_codex_stream(self):
        text = self.report(CLAUDE_TRANSCRIPT, "claude")
        self.assertIn("Every line parsed, and every tool result matched a tool use.", text)
        self.assertNotIn("completion row", text)

    def test_a_clean_codex_run_keeps_the_sentence_it_had(self):
        self.assertIn("Every line parsed, and every completion row carried a typed item.",
                      self.report(TRANSCRIPT, "codex"))

    def test_a_malformed_count_is_still_printed_in_place_of_the_sentence(self):
        text = self.report(claude_reply("toolu_missing", "x") + "\n", "claude")
        self.assertIn("- tool result without a tool use: 1", text)
        self.assertNotIn("Every line parsed", text)


class IssueSixRecordingFailure(unittest.TestCase):
    """Codex round 3, issue 6: a failed registry write returned success."""

    def test_a_failed_recording_is_propagated_not_swallowed(self):
        err = io.StringIO()
        with mock.patch.object(ar.subprocess, "run",
                               return_value=type("R", (), {"returncode": 7})()), \
                contextlib.redirect_stderr(err):
            code = ar.run_recorder(["true"], pathlib.Path("/tmp/out"))
        self.assertEqual(code, 7)
        self.assertIn("recording failed (exit 7)", err.getvalue())

    def test_a_successful_recording_returns_zero_and_says_nothing(self):
        err = io.StringIO()
        with mock.patch.object(ar.subprocess, "run",
                               return_value=type("R", (), {"returncode": 0})()), \
                contextlib.redirect_stderr(err):
            code = ar.run_recorder(["true"], pathlib.Path("/tmp/out"))
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")


SHA = "6eefd2a170ab67b0fe9012d3e98a77a08f1641ab"


def fake_git(*args):
    """Answer the three questions main asks Git, so the suite needs no repository."""
    return {"rev-parse": SHA + "\n", "rev-list": SHA + "\n",
            "log": "a subject with no Record: line\n"}[args[0]]


class IssueTwoReplayBinding(unittest.TestCase):
    """Codex round 3, issue 2: a replay could attach another commit's findings."""

    def replay(self, revision: str, *extra) -> tuple[int, str, pathlib.Path]:
        directory = scratch()
        (directory / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "f.json").write_text(json.dumps(reply(revision=revision,
                                                           disposition="accept")),
                                          encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--replay", "--transcript", str(directory / "t.jsonl"),
                            "--findings", str(directory / "f.json"),
                            *one_dir(directory / "out"), *extra])
        return code, err.getvalue(), directory / "out"

    def test_findings_naming_another_commit_refuse_to_record(self):
        code, err, out = self.replay("unrelated-commit", "--no-record")
        self.assertEqual(code, 2)
        self.assertIn("refusing to record", err)
        self.assertIn("unrelated-commit", err)
        self.assertFalse((out / "record-call.json").exists())

    def test_a_replay_never_records_a_delegated_decision(self):
        code, err, out = self.replay(SHA, "--no-record", "--authorized-by", "Miros Zohrehvand",
                                     "--decision", "accept", "--authority", "authorized in T4")
        self.assertEqual(code, 0)
        self.assertIn("a replay re-reads an earlier examination", err)
        call = json.loads((out / "record-call.json").read_text(encoding="utf-8"))
        self.assertIn("--agent", call)
        self.assertNotIn("--authorized-by", call)
        self.assertNotIn("--decision", call)

    def test_a_replay_of_the_right_commit_still_records_advisory(self):
        code, _, out = self.replay(SHA, "--no-record")
        self.assertEqual(code, 0)
        call = json.loads((out / "record-call.json").read_text(encoding="utf-8"))
        self.assertIn("--agent", call)
        self.assertEqual(call[call.index("--range") + 1], f"{SHA}^!")


class FindingTwoFreshRunIsolation(unittest.TestCase):
    """Round 4, F002: a failed fresh run reused another commit's findings and
    still built a delegated accept call."""

    def fresh(self, *extra, launch_code: int = 0, revision: str = SHA,
              preexisting: bool = False) -> tuple[int, str, pathlib.Path]:
        out = scratch() / "out"
        out.mkdir(parents=True)
        result = reply(revision=revision, disposition="accept")
        if preexisting:
            (out / "findings.json").write_text(json.dumps(result), encoding="utf-8")

        def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
            (directory / "findings.json").write_text(json.dumps(result), encoding="utf-8")
            (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
            return launch_code

        err = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "build_packet", return_value=({}, ["the tree"])), \
                mock.patch.object(ar, "manifest", return_value={}), \
                mock.patch.object(ar, "launch", fake_launch), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--no-record", *one_dir(out), *extra])
        return code, err.getvalue(), out

    def test_a_fresh_run_refuses_an_out_dir_that_already_holds_results(self):
        code, err, out = self.fresh(preexisting=True)
        self.assertEqual(code, 2)
        self.assertIn("refusing to start", err)
        self.assertIn("findings.json", err)
        self.assertFalse((out / "record-call.json").exists())

    def test_a_failed_launcher_verifies_nothing_and_records_nothing(self):
        code, err, out = self.fresh(launch_code=9)
        self.assertEqual(code, 9)
        self.assertIn("Nothing is verified and nothing is recorded", err)
        self.assertFalse((out / "report.md").exists())
        self.assertFalse((out / "record-call.json").exists())

    def test_a_fresh_run_refuses_findings_that_name_another_commit(self):
        code, err, out = self.fresh(revision="another-commit",
                                    *("--authorized-by", "Miros Zohrehvand",
                                      "--decision", "accept"))
        self.assertEqual(code, 2)
        self.assertIn("refusing to record", err)
        self.assertIn("another-commit", err)
        self.assertFalse((out / "record-call.json").exists())

    def test_a_clean_fresh_run_still_reports_and_builds_its_call(self):
        code, _, out = self.fresh()
        self.assertEqual(code, 0)
        self.assertTrue((out / "report.md").exists())
        self.assertIn("--agent", json.loads((out / "record-call.json").read_text()))


class FindingThreeCriteria(unittest.TestCase):
    """Round 4, F003: the caller's criteria never reached the reviewer, yet were
    recorded as the criteria it applied."""

    def test_the_researchers_criteria_reach_the_prompt(self):
        text = ar.render_prompt("abc123", "a subject", [], "minimal", ["the tree"],
                                "authorized in T4", "the five governing principles")
        self.assertIn("Criteria the researcher set:", text)
        self.assertIn("the five governing principles", text)

    def test_the_recorded_criteria_carry_both_the_request_and_the_report(self):
        argv = ar.record_argv(
            meta_for(authorized_by="Miros Zohrehvand", decision="accept",
                     criteria="the five governing principles",
                     findings_json={"criteria": ["quotes are verbatim", "checks were run"]}),
            result_for(), pathlib.Path("x/report.md"), "d1ge57")
        recorded = argv[argv.index("--criteria") + 1]
        self.assertEqual(recorded, "requested: the five governing principles; reviewer reported "
                                   "applying: quotes are verbatim; checks were run")

    def test_with_no_request_the_reviewers_own_criteria_stand_alone(self):
        argv = ar.record_argv(
            meta_for(authorized_by="Miros Zohrehvand", decision="accept",
                     findings_json={"criteria": ["quotes are verbatim"]}),
            result_for(), pathlib.Path("x/report.md"), "d1ge57")
        self.assertEqual(argv[argv.index("--criteria") + 1], "quotes are verbatim")


class ThePrompt(unittest.TestCase):
    def test_the_checks_reach_the_reviewer_with_their_exact_commands(self):
        text = ar.render_prompt("abc123", "a subject", ar.DEFAULT_CHECKS, "minimal",
                                ["the tree"], "")
        self.assertIn("python3 scripts/defect_ledger.py check", text)
        self.assertIn("Records mode: minimal.", text)
        self.assertIn("No delegation. Set `disposition` to `advisory`.", text)

    def test_a_delegation_reaches_the_reviewer_verbatim(self):
        text = ar.render_prompt("abc123", "a subject", [], "excluded", ["the tree"],
                                "Miros authorized a return on this range.")
        self.assertIn("Miros authorized a return on this range.", text)
        self.assertIn("Return `checks` as an empty list.", text)

    def test_neither_prompt_carries_a_schema_section(self):
        """Both remaining CLIs enforce the schema themselves, so the inline copy the
        third backend needed came out with it, in both modes."""
        self.assertNotIn("## Schema", ar.render_prompt("abc123", "s", [], "minimal", ["t"], ""))
        self.assertNotIn("## Schema", ar.render_path_prompt(["notes/a.md"], "paths:aa", ["t"]))
        self.assertNotIn("the host supplies it inline",
                         ar.CONTRACT.read_text(encoding="utf-8"))


class ShippedBesideTheScript(unittest.TestCase):
    """The contract and the schema resolve from the script's own directory, not
    from a repository root, so a project that installed only the core profile
    finds them where the script is. An export that dropped either would fail here."""

    def test_the_contract_and_the_schema_sit_next_to_the_script(self):
        here = pathlib.Path(ar.__file__).resolve().parent
        self.assertEqual(ar.CONTRACT.parent, here)
        self.assertEqual(ar.SCHEMA.parent, here)
        self.assertTrue(ar.CONTRACT.is_file(), ar.CONTRACT)
        self.assertTrue(ar.SCHEMA.is_file(), ar.SCHEMA)

    def test_the_schema_parses_and_names_the_four_dispositions(self):
        schema = json.loads(ar.SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["disposition"]["enum"],
                         ["advisory", "accept", "return", "refuse"])
        self.assertIn("revision", schema["required"])


STITCH_SOURCE = ("The reviewer must quote the source it read.\n"
                 "A sentence stands between the two that the quote joins.\n"
                 "The host labels every quote it is given.\n")
STITCH_QUOTE = ("The reviewer must quote the source it read. "
                "The host labels every quote it is given.")


def path_root(files: dict[str, str]) -> pathlib.Path:
    """A throwaway repository root holding the given files.

    No Git: a path run builds its packet from the files themselves, and the two
    calls that do ask Git are patched by the test that needs them.
    """
    root = scratch().resolve()
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # write_bytes, not write_text: on Windows the text mode turns the newline
        # into CRLF, and these bytes are the ones the listing's digests are over.
        target.write_bytes(text.encode("utf-8"))
    return root


class TheStitchedLabel(unittest.TestCase):
    """A quote that joins real passages is a citation fault, not a fabrication, and
    the separation must not rescue a quote that was retyped or padded."""

    def stitch_finding(self, quote: str = STITCH_QUOTE) -> dict:
        return finding(file="notes/a.md", line_start=1, line_end=3, quote=quote)

    # The commit-mode tests patch `git_bytes`: the cited blob is read as bytes
    # since AH-155, so a patch of `git` would no longer reach it.
    def test_a_joint_of_real_passages_is_stitched_in_commit_mode(self):
        with mock.patch.object(ar, "git_bytes", lambda *args: STITCH_SOURCE.encode("utf-8")):
            label, note = ar.quote_label("abc1234", self.stitch_finding())
        self.assertEqual(label, "stitched")
        self.assertIn("the span as quoted is not", note)

    def test_the_same_quote_is_stitched_in_path_mode(self):
        label, note = ar.path_quote_label({"notes/a.md": STITCH_SOURCE.encode("utf-8")},
                                          self.stitch_finding())
        self.assertEqual(label, "stitched")
        self.assertIn("notes/a.md", note)

    def test_a_retyped_quote_stays_invented_because_case_is_not_folded(self):
        """The refinement reads the same case-preserving text as the label it
        revisits, so a quote nobody copied is not upgraded by it."""
        with mock.patch.object(ar, "git_bytes", lambda *args: STITCH_SOURCE.encode("utf-8")):
            label, _ = ar.quote_label("abc1234", self.stitch_finding(STITCH_QUOTE.upper()))
        self.assertEqual(label, "invented")
        self.assertEqual(ar.path_quote_label({"notes/a.md": FILE_TEXT.encode("utf-8")},
                                             finding(file="notes/a.md",
                                                     quote="The Second Line Says Something"))[0],
                         "invented")

    def test_one_fabricated_part_leaves_the_whole_quote_invented(self):
        """No length filter: a real sentence with a short invention appended is not
        a joint of real passages, and calling it stitched would excuse the invention."""
        padded = "The reviewer must quote the source it read. FAKE."
        with mock.patch.object(ar, "git_bytes", lambda *args: STITCH_SOURCE.encode("utf-8")):
            label, _ = ar.quote_label("abc1234", self.stitch_finding(padded))
        self.assertEqual(label, "invented")

    def test_a_single_passage_that_is_nowhere_stays_invented(self):
        self.assertEqual(ar.path_quote_label({"notes/a.md": STITCH_SOURCE.encode("utf-8")},
                                             self.stitch_finding("a line nobody wrote at all"))[0],
                         "invented")

    def test_verified_and_misplaced_are_untouched_by_the_refinement(self):
        files = {"notes/a.md": FILE_TEXT.encode("utf-8")}
        row = finding(file="notes/a.md")
        self.assertEqual(ar.path_quote_label(files, row)[0], "verified")
        self.assertEqual(ar.path_quote_label(files, dict(row, line_start=3, line_end=3))[0],
                         "misplaced")

    def test_a_file_the_run_never_sent_is_invented_and_says_so(self):
        label, note = ar.path_quote_label({"notes/a.md": b"x"}, finding(file="scripts/other.py"))
        self.assertEqual(label, "invented")
        self.assertIn("not among the paths reviewed", note)


class PathCandidateSelection(unittest.TestCase):
    """The candidate is a set of files, named the same way on any machine."""

    def test_an_absolute_path_under_the_root_is_stored_relative_and_sorted(self):
        root = path_root({"notes/a.md": "a\n", "scripts/x.py": "b\n"})
        with mock.patch.object(ar, "ROOT", root):
            self.assertEqual(ar.candidate_paths([str(root / "scripts/x.py"), "notes/a.md"]),
                             ["notes/a.md", "scripts/x.py"])

    def test_the_same_file_named_twice_is_one_candidate(self):
        root = path_root({"notes/a.md": "a\n"})
        with mock.patch.object(ar, "ROOT", root):
            self.assertEqual(ar.candidate_paths(["notes/a.md", str(root / "notes/a.md")]),
                             ["notes/a.md"])

    def test_a_path_outside_the_repository_is_refused(self):
        root = path_root({"notes/a.md": "a\n"})
        outside = scratch().resolve() / "elsewhere.md"
        outside.write_text("x", encoding="utf-8")
        with mock.patch.object(ar, "ROOT", root), self.assertRaises(ValueError):
            ar.candidate_paths([str(outside)])

    def test_a_directory_and_a_missing_file_are_refused(self):
        root = path_root({"notes/a.md": "a\n"})
        with mock.patch.object(ar, "ROOT", root):
            for name in ("notes", "notes/gone.md"):
                with self.assertRaises(ValueError):
                    ar.candidate_paths([name])

    def test_the_revision_is_the_digest_of_the_listing_not_of_a_file(self):
        import hashlib
        root = path_root({"notes/a.md": "alpha\n", "notes/b.md": "beta\n"})
        with mock.patch.object(ar, "ROOT", root):
            files = ar.candidate_files(["notes/a.md", "notes/b.md"])
            listing = ar.candidate_listing(files)
            revision = ar.candidate_revision(listing)
        digest = {name: hashlib.sha256(name.encode("utf-8") + b"\n").hexdigest()
                  for name in ("alpha", "beta")}
        self.assertEqual(listing.splitlines(),
                         [digest["alpha"] + "  notes/a.md", digest["beta"] + "  notes/b.md"])
        self.assertEqual(revision, "paths:" + hashlib.sha256(listing.encode("utf-8")).hexdigest())
        self.assertNotIn(hashlib.sha256(b"alpha\n").hexdigest(), revision)

    def test_editing_one_candidate_changes_the_revision_of_the_set(self):
        root = path_root({"notes/a.md": "alpha\n", "notes/b.md": "beta\n"})
        with mock.patch.object(ar, "ROOT", root):
            before = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files(["notes/a.md", "notes/b.md"])))
            (root / "notes/b.md").write_text("beta changed\n", encoding="utf-8")
            after = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files(["notes/a.md", "notes/b.md"])))
        self.assertNotEqual(before, after)

    def test_the_check_rows_candidate_names_every_path_with_its_digest(self):
        field = ar.candidate_field("aa11  notes/a.md\nbb22  notes/b.md\n")
        self.assertEqual(field, "paths: notes/a.md (sha256 aa11), notes/b.md (sha256 bb22)")


class ThePathPacket(unittest.TestCase):
    """The packet holds the files and the listing, and nothing a commit run has."""

    def build(self) -> pathlib.Path:
        root = path_root({"notes/a.md": "alpha\n", "scripts/x.py": "print(1)\n"})
        packet = scratch() / "packet"
        with mock.patch.object(ar, "ROOT", root):
            files = ar.candidate_files(["notes/a.md", "scripts/x.py"])
            self.listing = ar.candidate_listing(files)
            self.before, self.sent = ar.build_path_packet(files, self.listing, packet)
        return packet

    def test_each_file_is_copied_to_the_same_relative_path(self):
        packet = self.build()
        self.assertEqual((packet / "notes" / "a.md").read_text(encoding="utf-8"), "alpha\n")
        self.assertEqual((packet / "scripts" / "x.py").read_text(encoding="utf-8"), "print(1)\n")

    def test_the_listing_sits_in_the_packet_and_the_manifest_covers_it(self):
        packet = self.build()
        self.assertEqual((packet / ".review" / "candidate.txt").read_text(encoding="utf-8"),
                         self.listing)
        self.assertIn(".review/candidate.txt", self.before)
        self.assertIn("notes/a.md", self.before)

    def test_no_git_no_diff_and_no_commit_text_reach_a_path_packet(self):
        packet = self.build()
        for absent in (".git", ".review/diff.patch", ".review/commit.txt", ".review/records"):
            self.assertFalse((packet / absent).exists(), absent)

    def test_what_was_sent_lists_the_copied_paths_and_the_listing(self):
        self.build()
        self.assertEqual(self.sent[:2], ["notes/a.md: the file as this run read it",
                                         "scripts/x.py: the file as this run read it"])
        self.assertIn(".review/candidate.txt", self.sent[2])
        self.assertIn(ar.candidate_revision(self.listing), self.sent[2])

    def test_the_prompt_binds_the_revision_to_the_listing_and_asks_for_no_checks(self):
        self.build()
        revision = ar.candidate_revision(self.listing)
        text = ar.render_path_prompt(["notes/a.md", "scripts/x.py"], revision, self.sent)
        self.assertIn(f"Set `revision` to `{revision}`", text)
        self.assertIn("Return `checks` as an empty list.", text)
        self.assertIn("No delegation. Set `disposition` to `advisory`.", text)
        self.assertIn("Records mode: none", text)
        self.assertNotIn("Target commit", text)


class ThePathCommandEachBackendRuns(unittest.TestCase):
    """A path dry run prints the same backend's own command, over the candidate."""

    def dry(self, backend: str, *extra) -> tuple[str, str]:
        root = path_root({"notes/a.md": "alpha beta gamma\n"})
        printed = io.StringIO()
        with mock.patch.object(ar, "ROOT", root), \
                mock.patch.object(ar, "git", lambda *args: ""), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            code = ar.main(["--path", "notes/a.md", "--backend", backend, "--dry-run", *extra])
        self.assertEqual(code, 0)
        return printed.getvalue().splitlines()[0], printed.getvalue()

    def test_each_backend_names_its_own_cli_and_not_the_others(self):
        for backend in ar.BACKENDS:
            arguments = self.dry(backend)[0].split(" ")
            self.assertEqual(arguments[0], backend)
            for other in set(ar.BACKENDS) - {backend}:
                self.assertNotIn(other, arguments)

    def test_a_path_run_is_read_only_whichever_backend_opens_it(self):
        for backend, want in (("codex", "read-only"), ("claude", "--tools Read,Grep,Glob")):
            line = self.dry(backend)[0]
            self.assertIn(want, line)
            self.assertNotIn("Bash", line)

    def test_no_backend_is_sent_a_schema_in_the_prompt(self):
        """Both remaining CLIs enforce the schema themselves, so the inline copy the
        third one needed came out with it."""
        for backend in ar.BACKENDS:
            self.assertNotIn("## Schema", self.dry(backend)[1])


class PathModeRefusals(unittest.TestCase):
    """One candidate per run, and a path candidate has nothing to run a check against."""

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main(argv)
        return code, err.getvalue()

    def test_naming_no_candidate_at_all_is_refused(self):
        code, err = self.run_main(["--no-record"])
        self.assertEqual(code, 2)
        self.assertIn("neither", err)

    def test_naming_both_a_commit_and_a_path_is_refused(self):
        code, err = self.run_main([SHA, "--path", "README.md"])
        self.assertEqual(code, 2)
        self.assertIn("both", err)

    def test_checks_with_a_path_is_refused_because_the_packet_holds_no_repository(self):
        code, err = self.run_main(["--path", "README.md", "--checks"])
        self.assertEqual(code, 2)
        self.assertIn("no repository to check", err)

    def test_a_path_that_is_not_a_file_is_refused_by_name(self):
        # No reviewer CLI on PATH, as on most laptops: the refusal still names the
        # missing file, because the candidate is checked before the CLI (AH-116).
        root = path_root({"notes/a.md": "alpha\n"})
        with mock.patch.object(ar, "ROOT", root), \
                mock.patch.object(ar.shutil, "which", return_value=None):
            code, err = self.run_main(["--path", "notes/gone.md"])
        self.assertEqual(code, 2)
        self.assertIn("notes/gone.md", err)


class PathInvolvement(unittest.TestCase):
    """What Git can say about a file, and what it cannot say about these bytes."""

    def test_producers_come_from_the_last_commit_touching_each_path(self):
        seen = []

        def fake(*args):
            seen.append(args)
            return ("s\n\nExecution-agent: gpt-6-astra\n" if args[-1] == "notes/a.md" else "s\n")

        with mock.patch.object(ar, "git", fake):
            producers, selectors, hits = ar.path_involvement(["notes/a.md", "notes/b.md"],
                                                             "gpt-6-astra")
        self.assertEqual(producers, ["gpt-6-astra"])
        self.assertEqual(selectors, [])
        self.assertEqual(len(hits), 1)
        # Bounded to the last commit: the whole history of a file names everyone who
        # ever touched it, which says nothing about the copy under review.
        for call in seen:
            self.assertIn("-1", call)
            self.assertEqual(call[:1], ("log",))

    def test_a_model_named_in_the_explanation_is_not_a_producer(self):
        # AH-123: only the trailer block declares who executed the commit.
        body = ("s\n\nThe earlier pass was\nExecution-agent: gpt-6-astra and this one is not.\n\n"
                "Execution-agent: claude-opus-5-5\n")
        with mock.patch.object(ar, "git", lambda *args: body):
            producers, _, hits = ar.path_involvement(["notes/a.md"], "gpt-6-astra")
        self.assertEqual(producers, ["claude-opus-5-5"])
        self.assertEqual(hits, [])

    def test_an_untracked_path_yields_no_producers_rather_than_a_guess(self):
        with mock.patch.object(ar, "git", lambda *args: ""):
            producers, selectors, hits = ar.path_involvement(["notes/new.md"], "gpt-6-astra")
        self.assertEqual((producers, selectors, hits), ([], [], []))

    def test_bytes_that_are_not_heads_bytes_are_declared(self):
        files = {"tracked.md": b"same\n", "edited.md": b"new\n", "untracked.md": b"x\n"}
        head = {"tracked.md": b"same\n", "edited.md": b"old\n"}
        with mock.patch.object(ar, "head_bytes", head.get):
            self.assertEqual(ar.differs_from_head(files), ["edited.md", "untracked.md"])


class APathRun(unittest.TestCase):
    """End to end: the packet the reviewer got, the binding, and what is recorded."""

    def setUp(self):
        self.root = path_root({"notes/a.md": "alpha beta gamma\nthe second line says something\n"})
        with mock.patch.object(ar, "ROOT", self.root):
            self.revision = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files(["notes/a.md"])))

    def payload(self, revision: str | None = None) -> dict:
        return reply(revision=revision or self.revision, target="a path candidate")

    def fresh(self, *extra, revision: str | None = None) -> tuple[int, str, pathlib.Path,
                                                                 mock.MagicMock]:
        out = self.root / "out"
        payload = self.payload(revision)
        seen = {}

        def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
            seen["command"], seen["cwd"] = command, cwd
            # The packet as the reviewer has it: the run removes it when it ends.
            packet = pathlib.Path(command[command.index("-C") + 1])
            seen["packet"] = packet
            seen["files"] = {p.relative_to(packet).as_posix(): p.read_bytes()
                             for p in packet.rglob("*") if p.is_file()}
            (directory / "findings.json").write_text(json.dumps(payload), encoding="utf-8")
            (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
            return 0

        recorder = mock.MagicMock()
        err = io.StringIO()
        with mock.patch.object(ar, "ROOT", self.root), \
                mock.patch.object(ar, "git", lambda *args: ""), \
                mock.patch.object(ar, "head_bytes", lambda rel: None), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "launch", fake_launch), \
                mock.patch.object(ar, "run_recorder", recorder), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main(["--path", "notes/a.md", *one_dir(out), *extra])
        self.seen = seen
        return code, err.getvalue(), out, recorder

    def test_a_fresh_run_records_no_acceptance_row_and_still_writes_the_check_row(self):
        target = self.root / "staging" / "checks.jsonl"
        code, err, out, recorder = self.fresh("--checks-jsonl", str(target))
        self.assertEqual(code, 0)
        recorder.assert_not_called()
        self.assertFalse((out / "record-call.json").exists())
        self.assertIn("no acceptance-registry row", err)
        self.assertTrue((out / "check-row.json").exists())
        self.assertEqual(len(target.read_text(encoding="utf-8").splitlines()), 1)

    def test_the_row_names_the_paths_with_their_digests_and_states_its_limits(self):
        target = self.root / "staging" / "checks.jsonl"
        self.fresh("--checks-jsonl", str(target))
        row = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(row["candidate"].startswith("paths: notes/a.md (sha256 "),
                        row["candidate"])
        self.assertEqual(row["procedure"], "trace-review")
        self.assertIn("records none; path candidate", row["method"])
        self.assertIn("labels computed over the bytes hashed in the candidate listing",
                      row["limits"])
        self.assertIn("execution trailers on the last commit touching each path: none",
                      row["limits"])
        self.assertIn("candidate differs from HEAD for: notes/a.md", row["limits"])
        self.assertIn("model review, not acceptance", row["limits"])

    def test_the_reviewer_gets_the_files_the_listing_hashed_and_a_read_only_run(self):
        self.fresh()
        command = self.seen["command"]
        files = self.seen["files"]
        self.assertIn("read-only", command)
        self.assertNotIn("workspace-write", command)
        self.assertEqual(files["notes/a.md"], (self.root / "notes" / "a.md").read_bytes())
        self.assertIn(self.revision.removeprefix("paths:"),
                      ar.candidate_revision(files[".review/candidate.txt"].decode("utf-8")))

    def test_a_path_run_needs_the_backends_cli_on_path_too(self):
        """The check is the same one a commit run makes, and a fresh path run
        reaches the reviewer by the same route."""
        err = io.StringIO()
        with mock.patch.object(ar, "ROOT", self.root), \
                mock.patch.object(ar.shutil, "which", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main(["--path", "notes/a.md", *one_dir(self.root / "none")])
        self.assertEqual(code, 2)
        self.assertIn("needs `codex` on PATH", err.getvalue())

    def test_findings_naming_another_revision_are_refused_in_a_fresh_run(self):
        code, err, out, recorder = self.fresh(revision="paths:deadbeef")
        self.assertEqual(code, 2)
        self.assertIn("refusing to record", err)
        self.assertIn("paths:deadbeef", err)
        recorder.assert_not_called()
        self.assertFalse((out / "check-row.json").exists())

    def replay(self, revision: str, *extra) -> tuple[int, str, pathlib.Path]:
        out = self.root / "replay-out"
        (self.root / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (self.root / "f.json").write_text(json.dumps(self.payload(revision)), encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(ar, "ROOT", self.root), \
                mock.patch.object(ar, "git", lambda *args: ""), \
                mock.patch.object(ar, "head_bytes", lambda rel: None), \
                mock.patch.object(ar, "run_recorder", mock.MagicMock()), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main(["--path", "notes/a.md", "--replay",
                            "--transcript", str(self.root / "t.jsonl"),
                            "--findings", str(self.root / "f.json"),
                            *one_dir(out), *extra])
        return code, err.getvalue(), out

    def test_a_replay_of_the_same_candidate_is_verified_and_reported(self):
        code, _, out = self.replay(self.revision)
        self.assertEqual(code, 0)
        self.assertIn("a replay", json.loads(
            (out / "check-row.json").read_text(encoding="utf-8"))["limits"])

    def test_a_replay_after_the_file_changed_is_refused_by_the_revision_check(self):
        (self.root / "notes" / "a.md").write_text("alpha beta gamma\nedited\n", encoding="utf-8")
        code, err, out = self.replay(self.revision)
        self.assertEqual(code, 2)
        self.assertIn("refusing to record", err)
        self.assertFalse((out / "check-row.json").exists())


class ThePathReport(unittest.TestCase):
    """The report says which paths, and what the labels over them establish."""

    def render(self, **over) -> str:
        meta = meta_for(findings_json={}, **over)
        with tempfile.TemporaryDirectory() as directory:
            return ar.write_report(pathlib.Path(directory), meta, result_for(),
                                   ["notes/a.md: the file as this run read it",
                                    ".review/candidate.txt: each path above with its sha256"]
                                   ).read_text(encoding="utf-8")

    def test_a_commit_run_keeps_the_title_it_had(self):
        self.assertIn("# Agentic review of 6eefd2a: s", self.render())

    def test_a_path_run_names_the_count_and_the_first_path(self):
        text = self.render(title="Agentic review of 2 path(s): notes/a.md",
                           records="none; path candidate")
        self.assertIn("# Agentic review of 2 path(s): notes/a.md", text)
        self.assertIn("Records none; path candidate.", text)

    def test_the_evidence_section_lists_the_copied_paths_and_the_listing(self):
        text = self.render()
        self.assertIn("- notes/a.md: the file as this run read it", text)
        self.assertIn("- .review/candidate.txt: each path above with its sha256", text)

    def test_the_labels_section_explains_stitched_in_one_line(self):
        text = self.render()
        self.assertIn("`stitched` says every part of the quote is in the file and the span as "
                      "quoted is not", text)

    def test_a_path_runs_own_limits_reach_the_labels_section(self):
        text = self.render(limits_extra=["labels computed over the bytes hashed in the candidate "
                                         "listing", "candidate differs from HEAD for: notes/a.md"])
        self.assertIn("labels computed over the bytes hashed in the candidate listing", text)
        self.assertIn("candidate differs from HEAD for: notes/a.md", text)


class ThePathCheckRowThroughTheValidator(unittest.TestCase):
    """The path row is read by trace_record.py's own validator, as the commit row is:
    a candidate that is a file set must not be what breaks a record."""

    DIGEST = "ab" * 32

    def test_the_path_row_passes_the_validator(self):
        import trace_record as tr
        root = scratch()
        record = root / "record"
        record.mkdir()
        (record / "summary.md").write_text("The author set the direction (T1).", encoding="utf-8")
        (record / "evidence.json").write_text(json.dumps({
            "record_id": "test", "record_schema_version": 2,
            "consent": {"third_party_review": "cleared"},
            "instructions": {"files": {".skills/trace-review/SKILL.md": self.DIGEST},
                             "basis": "file state at extraction time"},
            "turns": [{"id": "T1", "text": "first"}]}), encoding="utf-8")
        row = ar.check_row(
            meta_for(records="none; path candidate", sandbox="read-only",
                     title="Agentic review of 1 path(s): notes/a.md",
                     candidate=ar.candidate_field("cd" * 32 + "  notes/a.md\n"),
                     limits_extra=["labels computed over the bytes hashed in the candidate "
                                   "listing", "candidate differs from HEAD for: notes/a.md"]),
            result_for(), pathlib.Path("out") / "report.md", "d1ge57",
            "sha256:" + self.DIGEST)
        (record / "checks.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            tr.cmd_cover(type("A", (), {"record": str(record)})())
        found = tr.evaluate_record(record)
        self.assertEqual([p for p in found["problems"] if "checks.jsonl" in p], [])
        self.assertTrue(found["ok"], found["problems"])
        self.assertTrue(row["candidate"].startswith("paths: notes/a.md (sha256 "))
def claude_use(ident: str, name: str, **fields) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": name, "input": fields}]}})


def claude_reply(ident: str, content, is_error: bool = False) -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ident, "content": content,
         "is_error": is_error}]}})


DENIED = ("Permission to use Bash has been denied because Claude Code is running in don't ask "
          "mode")
REPLY = {"revision": SHA, "target": "a commit", "criteria": ["the contract"],
         "evidence_examined": {"paths": ["scripts/x.py"], "commands": ["cat note.txt"]},
         "checks": [], "evidence_unavailable": [], "findings": [],
         "unresolved_objections": [], "disposition": "advisory", "confidence": 0.5}
# Trimmed from the live probes of 2026-09-22 (claude-probe, claude-probe-b): the
# init line, a zero exit, a non-zero exit with its output, a denial, a tool that
# runs nothing, and the result event carrying the validated object.
CLAUDE_TRANSCRIPT = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001",
                "cwd": "/packet", "permissionMode": "dontAsk"}),
    claude_use("toolu_01", "Bash", command="cat note.txt"),
    claude_reply("toolu_01", "the second line says something"),
    claude_use("toolu_02", "Bash", command="python3 scripts/defect_ledger.py check"),
    claude_reply("toolu_02", "Exit code 2\nstderr line\nstdout line", is_error=True),
    claude_use("toolu_03", "Bash", command="ls /"),
    json.dumps({"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
                "tool_use_id": "toolu_03", "decision_reason_type": "mode",
                "message": DENIED + ". IMPORTANT: You *may* attempt to accomplish this action "
                                    "using other tools."}),
    claude_reply("toolu_03", DENIED + ".", is_error=True),
    claude_use("toolu_04", "Read", file_path="scripts/x.py"),
    claude_reply("toolu_04", [{"type": "text", "text": "alpha beta gamma"}]),
    json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "permission_denials": [{"tool_name": "Bash", "tool_use_id": "toolu_03"}],
                "result": json.dumps(REPLY), "structured_output": REPLY}),
]) + "\n"


class ClaudeTranscriptRows(unittest.TestCase):
    """The same rows the codex reader returns, from a stream that says none of the
    same words. The three outcomes a Bash call has here are kept apart."""

    def rows(self, transcript: str = CLAUDE_TRANSCRIPT):
        return ar.transcript_facts(transcript, "claude")

    def test_a_zero_exit_carries_the_output_and_no_exit_line(self):
        ran, label, _ = self.rows()
        self.assertEqual(label, "auditable")
        self.assertEqual(ran[0], {"command": "cat note.txt", "exit_code": 0,
                                  "output": "the second line says something",
                                  "status": "completed"})

    def test_a_non_zero_exit_gives_the_code_and_the_output_below_the_exit_line(self):
        ran, _, _ = self.rows()
        self.assertEqual(ran[1]["exit_code"], 2)
        self.assertEqual(ran[1]["output"], "stderr line\nstdout line")
        self.assertEqual(ran[1]["status"], "failed")

    def test_a_denied_call_is_not_a_command_the_reviewer_ran(self):
        """It never ran, so it must not reach the rows the checks are matched
        against, nor the output a quote is looked for in."""
        ran, _, _ = self.rows()
        self.assertEqual([row["command"] for row in ran],
                         ["cat note.txt", "python3 scripts/defect_ledger.py check"])
        self.assertEqual(ar.check_label({"name": "listing", "command": "ls /",
                                         "outcome": "fine"}, ran)["label"],
                         "no matching command")
        self.assertEqual(ar.seen_label(DENIED, ran), "not seen in output")

    def test_a_denial_is_read_off_the_event_the_cli_declares_it_in(self):
        denied = ar.denied_calls(CLAUDE_TRANSCRIPT, "claude")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["tool"], "Bash")
        self.assertEqual(denied[0]["command"], "ls /")
        self.assertEqual(denied[0]["reason"], DENIED)

    def test_a_command_that_prints_the_denial_text_is_not_read_as_denied(self):
        """The text is the model's to print; the event is the CLI's to declare."""
        transcript = "\n".join([
            claude_use("toolu_07", "Bash", command="echo denied"),
            claude_reply("toolu_07", DENIED + "."),
        ]) + "\n"
        ran, _, _ = self.rows(transcript)
        self.assertEqual(ar.denied_calls(transcript, "claude"), [])
        self.assertEqual([row["exit_code"] for row in ran], [0])

    def test_the_result_events_own_list_names_a_denial_with_no_event_of_its_own(self):
        transcript = "\n".join([
            claude_use("toolu_08", "Bash", command="ls /etc"),
            json.dumps({"type": "result", "subtype": "success", "permission_denials": [
                {"tool_name": "Bash", "tool_use_id": "toolu_08",
                 "tool_input": {"command": "ls /etc"}}]}),
        ]) + "\n"
        denied = ar.denied_calls(transcript, "claude")
        self.assertEqual(denied[0]["command"], "ls /etc")
        self.assertIn("permission_denials", denied[0]["reason"])

    def test_the_codex_backend_declares_no_denials(self):
        self.assertEqual(ar.denied_calls(TRANSCRIPT, "codex"), [])

    def test_a_tool_that_runs_nothing_conforms_without_a_row(self):
        transcript = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5"}),
            claude_use("toolu_09", "Read", file_path="scripts/x.py"),
            claude_reply("toolu_09", [{"type": "text", "text": "alpha"}]),
        ]) + "\n"
        ran, label, _ = self.rows(transcript)
        self.assertEqual(ran, [])
        self.assertEqual(label, "auditable")

    def test_a_result_for_a_call_the_transcript_never_showed_is_counted(self):
        ran, label, malformed = self.rows(claude_reply("toolu_missing", "x") + "\nnot json\n")
        self.assertEqual(ran, [])
        self.assertTrue(label.startswith("unauditable"), label)
        self.assertEqual(malformed["tool_result_without_a_tool_use"], 1)
        self.assertEqual(malformed["invalid_json"], 1)

    def test_the_reviewer_is_the_model_the_init_event_names_not_the_alias(self):
        self.assertEqual(ar.reviewer_id(CLAUDE_TRANSCRIPT, "claude", "opus"),
                         ("claude-haiku-4-5-20251001", "observed"))

    def test_a_transcript_naming_no_model_leaves_the_flag_standing_and_says_so(self):
        self.assertEqual(ar.reviewer_id("", "claude", "opus"), ("opus", "requested"))

    def test_the_codex_stream_names_no_model_so_the_identity_is_requested(self):
        self.assertEqual(ar.reviewer_id(TRANSCRIPT, "codex", "gpt-6-astra"),
                         ("gpt-6-astra", "requested"))

    def test_a_run_that_answered_without_calling_a_tool_is_auditable(self):
        """AH-102: the claude reader counted tool_use blocks only, so a reviewer
        that answered from the files it was given was called unauditable, where the
        equivalent codex run is auditable because its agent message is a typed item.
        """
        transcript = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5"}),
            json.dumps({"type": "result", "subtype": "success", "structured_output": REPLY}),
        ]) + "\n"
        ran, label, malformed = self.rows(transcript)
        self.assertEqual(ran, [])
        self.assertEqual(label, "auditable")
        self.assertEqual(sum(malformed.values()), 0)

    def test_a_transcript_with_neither_a_tool_call_nor_a_result_is_unauditable(self):
        transcript = json.dumps({"type": "system", "subtype": "init",
                                 "model": "claude-opus-5"}) + "\n"
        self.assertEqual(self.rows(transcript)[1], "unauditable")


class FindingsOffTheTranscript(unittest.TestCase):
    """The claude backend writes no findings file, so the host takes the reply off
    the transcript and says why when it cannot."""

    def test_claude_returns_the_object_the_result_event_validated(self):
        found, why = ar.claude_findings(CLAUDE_TRANSCRIPT)
        self.assertEqual(why, "")
        self.assertEqual(found["revision"], SHA)

    def test_claude_with_no_structured_output_names_the_subtype_that_says_why(self):
        transcript = json.dumps({"type": "result",
                                 "subtype": "error_max_structured_output_retries"}) + "\n"
        found, why = ar.claude_findings(transcript)
        self.assertIsNone(found)
        self.assertIn("error_max_structured_output_retries", why)

    def test_claude_with_no_result_event_says_so(self):
        found, why = ar.claude_findings("")
        self.assertIsNone(found)
        self.assertIn("no result event", why)

    def test_the_validator_reads_the_keywords_the_schema_uses(self):
        rule = {"type": "object", "additionalProperties": False, "required": ["a"],
                "properties": {"a": {"type": "array", "items": {"type": "integer"}},
                               "b": {"type": "string", "enum": ["x"]}}}
        self.assertEqual(ar.schema_violation({"a": [1, 2], "b": "x"}, rule), "")
        self.assertIn("is missing a", ar.schema_violation({"b": "x"}, rule))
        self.assertIn("a[1] is not integer", ar.schema_violation({"a": [1, "two"]}, rule))
        self.assertIn("b is not one of x", ar.schema_violation({"a": [], "b": "y"}, rule))
        self.assertIn("carries c", ar.schema_violation({"a": [], "c": 1}, rule))
        # A boolean is an int to Python, and it is not an integer to the schema.
        self.assertIn("a[0] is not integer", ar.schema_violation({"a": [True]}, rule))


class TheSchemaGate(unittest.TestCase):
    """AH-096: every loaded reply is held against the schema before it is labelled,
    in a fresh run as in a replay and whichever backend answered. Validation used to
    sit inside one backend's reader, so a replay carrying a malformed check reached
    the labelling code and raised there, with the reply no longer in hand."""

    def test_a_complete_reply_passes(self):
        self.assertEqual(ar.schema_refusal(reply()), "")

    def test_a_check_that_is_not_an_object_is_named_and_refused(self):
        refusal = ar.schema_refusal(reply(checks=["not an object"]))
        self.assertIn("checks[0]", refusal)
        self.assertIn("nothing is recorded", refusal)

    def test_a_missing_required_key_and_a_reply_that_is_not_an_object_are_refused(self):
        short = {key: value for key, value in reply().items() if key != "disposition"}
        self.assertIn("disposition", ar.schema_refusal(short))
        self.assertIn("not object", ar.schema_refusal(["a list"]))

    def test_a_finding_whose_line_number_is_not_a_number_is_refused(self):
        refusal = ar.schema_refusal(reply(findings=[dict(finding(), line_start="two")]))
        self.assertIn("findings[0].line_start is not integer", refusal)

    def replay(self, payload: dict, *extra) -> tuple[int, str, pathlib.Path]:
        directory = scratch()
        (directory / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "f.json").write_text(json.dumps(payload), encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--replay", "--transcript", str(directory / "t.jsonl"),
                            "--findings", str(directory / "f.json"), "--no-record",
                            *one_dir(directory / "out"), *extra])
        return code, err.getvalue(), directory / "out"

    def test_a_replay_of_a_malformed_reply_verifies_nothing_and_records_nothing(self):
        code, err, out = self.replay(reply(checks=["not an object"]))
        self.assertEqual(code, 2)
        self.assertIn("refusing to verify", err)
        self.assertFalse((out / "report.md").exists())
        self.assertFalse((out / "record-call.json").exists())
        self.assertFalse((out / "check-row.json").exists())

    def test_the_gate_holds_for_a_claude_replay_too(self):
        code, err, _ = self.replay(reply(evidence_examined={"paths": "not an array",
                                                            "commands": []}),
                                   "--backend", "claude")
        self.assertEqual(code, 2)
        self.assertIn("evidence_examined.paths", err)

    def test_a_complete_replay_still_runs(self):
        code, _, out = self.replay(reply())
        self.assertEqual(code, 0)
        self.assertTrue((out / "check-row.json").exists())

    def test_a_fresh_run_holds_the_reviewers_own_file_against_the_schema(self):
        out = scratch() / "out"

        def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
            (directory / "findings.json").write_text(
                json.dumps(reply(checks=["not an object"])), encoding="utf-8")
            (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
            return 0

        err = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "build_packet", return_value=({}, ["the tree"])), \
                mock.patch.object(ar, "manifest", return_value={}), \
                mock.patch.object(ar, "launch", fake_launch), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--no-record", *one_dir(out)])
        self.assertEqual(code, 2)
        self.assertIn("checks[0]", err.getvalue())
        self.assertFalse((out / "report.md").exists())


class TheCommandEachBackendRuns(unittest.TestCase):
    """A dry run prints the command that would run, and it names one CLI only."""

    def dry(self, backend: str, *extra) -> str:
        out = scratch() / "out"
        printed = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar, "build_packet", return_value=({}, ["the tree"])), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            code = ar.main([SHA, "--backend", backend, "--dry-run", *one_dir(out), *extra])
        self.assertEqual(code, 0)
        return printed.getvalue().splitlines()[0]

    def test_each_backend_names_its_own_cli_and_not_the_others(self):
        # Compared argument by argument: a checkout under `.claude` puts that name
        # in a path, and a path is not the program the command runs.
        for backend in ar.BACKENDS:
            arguments = self.dry(backend, "--checks").split(" ")
            self.assertEqual(arguments[0], backend)
            for other in set(ar.BACKENDS) - {backend}:
                self.assertNotIn(other, arguments)

    def test_the_claude_command_restricts_the_tools_and_holds_the_schema(self):
        line = self.dry("claude", "--checks")
        self.assertIn("--restricted", line)
        self.assertIn("--tools Bash,Read,Grep,Glob", line)
        self.assertIn("--allowedTools Bash,Read,Grep,Glob", line)
        self.assertIn("--permission-mode dontAsk", line)
        self.assertIn("--json-schema <agentic_review.schema.json without its $schema key>", line)

    def test_the_claude_schema_drops_the_meta_schema_key_and_nothing_else(self):
        # LESSON (2026-09-22): the claude CLI's validator rejects a schema that names
        # the 2020-12 meta-schema, so the live run failed before the reviewer started.
        sent = json.loads(ar.claude_schema())
        kept = json.loads(ar.SCHEMA.read_text(encoding="utf-8"))
        self.assertNotIn("$schema", sent)
        kept.pop("$schema")
        self.assertEqual(sent, kept)

    def test_the_claude_command_without_checks_names_no_shell_tool(self):
        line = self.dry("claude")
        self.assertIn("--tools Read,Grep,Glob", line)
        self.assertNotIn("Bash", line)

    def test_the_codex_command_is_the_one_it_always_was(self):
        line = self.dry("codex", "--checks")
        self.assertIn("codex exec -C ", line)
        self.assertIn("-s workspace-write", line)
        self.assertIn("--output-schema", line)
        self.assertTrue(line.endswith(" -"), line)

    def test_a_backend_with_no_cli_on_the_path_records_nothing(self):
        err = io.StringIO()
        with mock.patch.object(ar.shutil, "which", return_value=None), \
                contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--backend", "claude"])
        self.assertEqual(code, 2)
        self.assertIn("claude", err.getvalue())
        self.assertIn("nothing was recorded", err.getvalue())

    def test_each_backend_has_its_own_default_model(self):
        for backend, row in ar.BACKENDS.items():
            self.assertEqual(ar.parse_args([SHA, "--backend", backend]).model, row["model"])
        self.assertEqual(ar.parse_args([SHA, "--backend", "claude", "--model", "sonnet"]).model,
                         "sonnet")

    def test_the_two_backends_are_the_whole_of_the_choice(self):
        """The third backend came out on 2026-09-22 (plan Part 5): codex and claude
        already give two subscription routes and two lineages. A backend the table
        does not name is refused, and every table the run reads holds the same two,
        so a reader that outlived its entry cannot be reached."""
        self.assertEqual(sorted(ar.BACKENDS), ["claude", "codex"])
        self.assertEqual(sorted(ar.MALFORMED), ["claude", "codex"])
        self.assertEqual(sorted(ar.READERS), ["claude", "codex"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ar.parse_args([SHA, "--backend", "another-cli"])

    def test_the_timeout_help_says_codex_gained_a_wall_clock_it_did_not_have(self):
        """AH-097: the codex path did change, and the words now say so."""
        self.assertIn("codex included, which had none before", ar.__doc__)
        text = io.StringIO()
        with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
            ar.parse_args(["--help"])
        self.assertIn("codex included, which had none before", " ".join(text.getvalue().split()))


class WhatTheRunSaysItHeldBack(unittest.TestCase):
    """One of the two backends has no operating-system sandbox, so the row and the
    report say what did hold the reviewer in, and what did not."""

    def test_the_sandbox_string_is_the_backends_own(self):
        self.assertEqual(ar.sandbox_of("codex", True), "workspace-write")
        self.assertEqual(ar.sandbox_of("codex", False), "read-only")
        self.assertEqual(ar.sandbox_of("claude", True),
                         "restricted tools Bash,Read,Grep,Glob, no operating-system sandbox")
        self.assertEqual(ar.sandbox_of("claude", False),
                         "restricted tools Read,Grep,Glob, no shell")

    def test_a_run_without_an_operating_system_sandbox_says_so_in_the_limits(self):
        row = ar.check_row(meta_for(backend="claude", sandbox=ar.sandbox_of("claude", True)),
                           result_for(), pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertIn("a write outside the packet is not prevented", row["limits"])

    def test_a_sandboxed_run_claims_no_such_limit(self):
        row = ar.check_row(meta_for(), result_for(), pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertNotIn("no operating-system sandbox", row["limits"])

    def test_the_report_header_names_the_reviewer_the_backend_and_the_basis(self):
        meta = meta_for(model="claude-haiku-4-5-20251001", backend="claude",
                        identity="observed")
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta, result_for(),
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("Reviewer: claude-haiku-4-5-20251001 via claude (identity observed).", text)

    def test_the_codex_header_says_the_identity_was_only_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta_for(), result_for(),
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("Reviewer: gpt-6-astra via codex (identity requested).", text)

    def test_the_independence_declaration_names_the_reviewer_the_backend_and_the_basis(self):
        argv = ar.record_argv(meta_for(model="claude-haiku-4-5-20251001", backend="claude",
                                       identity="observed"),
                              result_for(), pathlib.Path("x/report.md"), "d1ge57")
        self.assertIn("reviewer claude-haiku-4-5-20251001 via claude, identity observed",
                      argv[argv.index("--independence") + 1])
        self.assertEqual(argv[argv.index("--by") + 1], "claude-haiku-4-5-20251001")

    def test_the_check_rows_runner_carries_the_identity_basis(self):
        """AH-095: the report header and the independence declaration said whether
        the identity was observed or only requested, and the row did not, so
        checks.jsonl lost the qualification."""
        row = ar.check_row(meta_for(), result_for(), pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertIn("reviewer gpt-6-astra via codex (identity requested) with tools in a packet",
                      row["runner"])
        observed = ar.check_row(meta_for(model="claude-opus-5", backend="claude",
                                         identity="observed"),
                                result_for(), pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertIn("reviewer claude-opus-5 via claude (identity observed) with tools in a "
                      "packet", observed["runner"])

    def test_the_packet_claim_says_what_a_read_elsewhere_would_and_would_not_show(self):
        argv = ar.record_argv(meta_for(), result_for(), pathlib.Path("x/report.md"), "d1ge57")
        stated = argv[argv.index("--independence") + 1]
        self.assertIn("was supplied only the packet", stated)
        self.assertIn("detected only when a command or a file-tool call names the main checkout",
                      stated)
        row = ar.check_row(meta_for(), result_for(), pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertIn("was supplied only the packet", row["limits"])
        self.assertIn("detected only when a command or a file-tool call names the main checkout",
                      row["limits"])

    def test_the_modified_section_says_it_saw_the_packet_only(self):
        meta = meta_for(backend="claude", sandbox=ar.sandbox_of("claude", True))
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta, result_for(),
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("both manifests are of the packet alone", text)
        self.assertIn("No operating-system sandbox: a write outside the packet is not prevented",
                      text)

    def test_the_denied_calls_have_a_section_and_a_count_of_their_own(self):
        result = result_for(CLAUDE_TRANSCRIPT, backend="claude")
        meta = meta_for(backend="claude", sandbox=ar.sandbox_of("claude", True))
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta, result,
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("## Calls the reviewer was denied", text)
        self.assertIn("- Bash: `ls /`", text)
        row = ar.check_row(meta, result, pathlib.Path("x/report.md"), "d1ge57", None)
        self.assertIn("1 calls the reviewer was denied", row["result"])
        argv = ar.record_argv(meta, result, pathlib.Path("x/report.md"), "d1ge57")
        self.assertIn("1 calls the reviewer was denied", argv[argv.index("--scope") + 1])


def fresh_commit_run(*extra, disposition: str = "advisory", launch=None):
    """A fresh commit run with the reviewer stubbed: it answers `disposition`.
    Returns the exit code, stderr, the run directory and the command launched."""
    out = scratch() / "out"
    seen = {}

    def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
        seen["command"] = command
        if launch:
            return launch(command, directory)
        (directory / "findings.json").write_text(
            json.dumps(reply(disposition=disposition)), encoding="utf-8")
        (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        return 0

    err = io.StringIO()
    with mock.patch.object(ar, "git", fake_git), \
            mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
            mock.patch.object(ar, "build_packet", return_value=({}, ["the tree"])), \
            mock.patch.object(ar, "manifest", return_value={}), \
            mock.patch.object(ar, "launch", fake_launch), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = ar.main([SHA, "--no-record", *one_dir(out), *extra])
    return code, err.getvalue(), out, seen.get("command")


def packet_of(command: list[str]) -> pathlib.Path:
    """The packet a codex command names, from its `-C` argument."""
    return pathlib.Path(command[command.index("-C") + 1])


class ADelegatedRowRecordsTheReviewersOwnDecision(unittest.TestCase):
    """LESSON (AH-154): `--decision` replaced the reviewer's disposition, so a
    reviewer's refusal was recorded as its acceptance, and `--authorized-by` with
    no `--authority` recorded a delegated decision after the prompt had told the
    reviewer there was no delegation. The row now carries the reviewer's own
    disposition, a differing `--decision` is reported and recorded as nobody's,
    and a run without both halves of a delegation stays advisory and says why."""

    AUTHORITY = "Jane Roe authorized this reviewer to decide on the commit, in T4."

    def call(self, out: pathlib.Path) -> list[str]:
        return json.loads((out / "record-call.json").read_text(encoding="utf-8"))

    def test_a_decision_that_differs_from_the_reviewers_is_never_recorded_in_its_name(self):
        code, err, out, _ = fresh_commit_run("--authorized-by", "Jane Roe", "--authority",
                                             self.AUTHORITY, "--decision", "accept",
                                             disposition="refuse")
        self.assertEqual(code, 0)
        call = self.call(out)
        self.assertEqual(call[call.index("--decision") + 1], "refuse")
        self.assertEqual(call[call.index("--authorized-by") + 1], "Jane Roe")
        # The row is the model's; the caller's accept is nobody's, and the
        # authorizing researcher is not made the person who decided it.
        self.assertEqual(call[call.index("--by") + 1], "gpt-6-astra")
        self.assertNotIn("--person", call)
        self.assertIn("--decision accept differs from the reviewer's own disposition, refuse", err)
        self.assertIn("recorded as nobody's decision", err)
        self.assertIn("--person", err)
        self.assertIn("--words", err)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Delegated decision: **refuse**", report)
        self.assertIn("recorded as nobody's decision", report)
        row = json.loads((out / "check-row.json").read_text(encoding="utf-8"))
        self.assertIn("delegated decision refuse", row["result"])
        self.assertIn("recorded as nobody's decision", row["limits"])

    def test_authorized_by_without_authority_stays_advisory_and_says_why(self):
        code, err, out, _ = fresh_commit_run("--authorized-by", "Jane Roe",
                                             "--decision", "accept", disposition="accept")
        self.assertEqual(code, 0)
        call = self.call(out)
        self.assertIn("--agent", call)
        self.assertNotIn("--authorized-by", call)
        self.assertNotIn("--decision", call)
        self.assertIn("recording advisory model review rather than a delegated decision", err)
        self.assertIn("--authority", err)
        prompt = (out / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("No delegation. Set `disposition` to `advisory`.", prompt)

    def test_authority_without_a_researcher_stays_advisory_too(self):
        code, err, out, _ = fresh_commit_run("--authority", self.AUTHORITY, disposition="accept")
        self.assertEqual(code, 0)
        self.assertIn("--agent", self.call(out))
        self.assertIn("names no researcher", err)
        self.assertNotIn(self.AUTHORITY, (out / "prompt.md").read_text(encoding="utf-8"))

    def test_a_complete_delegation_records_the_decision_the_reviewer_made(self):
        code, err, out, _ = fresh_commit_run("--authorized-by", "Jane Roe", "--authority",
                                             self.AUTHORITY, disposition="return")
        self.assertEqual(code, 0)
        call = self.call(out)
        self.assertEqual(call[call.index("--decision") + 1], "return")
        self.assertEqual(call[call.index("--authorized-by") + 1], "Jane Roe")
        self.assertIn(self.AUTHORITY, (out / "prompt.md").read_text(encoding="utf-8"))
        self.assertNotIn("differs", err)
        self.assertNotIn("recording advisory", err)

    def test_a_decision_that_matches_the_reviewers_is_no_override(self):
        _, err, out, _ = fresh_commit_run("--authorized-by", "Jane Roe", "--authority",
                                          self.AUTHORITY, "--decision", "return",
                                          disposition="return")
        self.assertNotIn("differs", err)
        self.assertNotIn("nobody's decision", (out / "report.md").read_text(encoding="utf-8"))

    def test_the_help_no_longer_offers_to_override_the_reviewer(self):
        text = io.StringIO()
        with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
            ar.parse_args(["--help"])
        flat = " ".join(text.getvalue().split())
        self.assertNotIn("override the reviewer's own disposition", flat)
        self.assertIn("never replaces the reviewer's own disposition", flat)


def alive(pid: int) -> bool:
    """Whether the process runs; a zombie waiting to be reaped does not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                           text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# A tool that ignores SIGTERM, started by a CLI that does not.
STUBBORN = ("import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "while True: time.sleep(0.05)\n")
CLI = ("import subprocess, sys, time\n"
       "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
       "while True: time.sleep(0.05)\n")


class TheTimeoutKillsTheToolsTheReviewerStarted(unittest.TestCase):
    """LESSON (AH-153): on timeout the group got SIGTERM, the CLI died of it, and
    the wait returned before SIGKILL, so a tool ignoring SIGTERM kept running after
    the script had said the reviewer was killed."""

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_a_tool_that_ignores_sigterm_does_not_outlive_the_reviewer(self):
        pidfile = scratch() / "tool.pid"
        proc = subprocess.Popen([sys.executable, "-c", CLI, STUBBORN, str(pidfile)],
                                start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(pidfile.read_text())
        self.addCleanup(kill_quietly, pid)
        ar.terminate(proc)
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid), "the tool that ignored SIGTERM is still running")
        self.assertIsNotNone(proc.returncode)

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_a_group_already_gone_is_not_an_error(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        ar.terminate(proc)
        self.assertEqual(proc.returncode, 0)

    def test_without_process_groups_the_whole_tree_is_killed(self):
        """The Windows branch, driven with stand-ins; it has not run on Windows."""
        calls = []
        proc = mock.MagicMock(pid=4242)
        proc.poll.return_value = None
        with mock.patch.object(ar, "POSIX_GROUPS", False), \
                mock.patch.object(ar.subprocess, "run",
                                  side_effect=lambda command, **kw: calls.append(command)):
            ar.terminate(proc)
        self.assertEqual(calls, [["taskkill", "/T", "/F", "/PID", "4242"]])
        proc.kill.assert_called_once()
        proc.wait.assert_called()


def locale_host(raw: bytes, locale_encoding: str = "latin-1"):
    """`subprocess.run` as a host whose locale is not UTF-8 answers it: output asked
    for as text is decoded with the locale unless the call names an encoding."""
    def run(command, **kw):
        out = raw
        if kw.get("text") or kw.get("encoding"):
            out = raw.decode(kw.get("encoding") or locale_encoding, kw.get("errors") or "strict")
        return subprocess.CompletedProcess(command, 0, out, b"")
    return run


class GitIsReadAsUtf8WhateverTheLocale(unittest.TestCase):
    """LESSON (AH-155): git's output was decoded with the host's locale, so under a
    Latin-1 or cp1252 locale an en dash became three characters and a verbatim
    quote was labelled invented; a byte that is not UTF-8 raised where nothing
    caught it."""

    SOURCE = "Ranges take an en dash: 10–20.\n"

    def test_git_output_is_decoded_as_utf8_on_a_latin1_host(self):
        with mock.patch.object(ar.subprocess, "run", locale_host(self.SOURCE.encode("utf-8"))):
            self.assertEqual(ar.git("show", "HEAD:AGENTS.md"), self.SOURCE)

    def test_a_quote_with_an_en_dash_verifies_on_a_latin1_host(self):
        row = finding(file="AGENTS.md", line_start=1, line_end=1,
                      quote="Ranges take an en dash: 10–20.")
        with mock.patch.object(ar.subprocess, "run", locale_host(self.SOURCE.encode("utf-8"))):
            self.assertEqual(ar.quote_label(SHA, row), ("verified", ""))

    def test_a_file_that_is_not_utf8_is_not_compared_and_says_so(self):
        row = finding(file="notes/latin.md", line_start=1, line_end=1, quote="caf")
        data = "café au lait\n".encode("latin-1")
        with mock.patch.object(ar, "git_bytes", lambda *args: data):
            label, note = ar.quote_label(SHA, row)
        self.assertEqual(label, "not compared")
        self.assertIn("notes/latin.md is not UTF-8 as the commit has it (byte 3)", note)
        label, note = ar.path_quote_label({"notes/latin.md": data}, row)
        self.assertEqual(label, "not compared")
        self.assertIn("the reviewed copy", note)

    def test_the_report_says_what_not_compared_means(self):
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta_for(), result_for(),
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("`not compared` says the file is not UTF-8", text)


RECORD_FILE = "ai/records/20260930-x/evidence.json"


class ARecordFileNamedAsACandidate(unittest.TestCase):
    """LESSON (AH-157): `--path` sent a trace record or a registry to the reviewer
    whole, with no inspection declared, while `--records full` asks for one, and
    the prompt said no trace record was sent."""

    def setUp(self):
        self.root = path_root({RECORD_FILE: '{"turns": [{"text": "SECRET HUMAN PROMPT"}]}\n',
                               "sources/intake.jsonl": "{}\n",
                               "sources/transitions.jsonl": "{}\n",
                               "notes/a.md": "alpha beta gamma\n"})

    def dry(self, *argv) -> tuple[int, str, str]:
        printed, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ar, "ROOT", self.root), \
                mock.patch.object(ar, "git", lambda *args: ""), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(err):
            code = ar.main(["--dry-run", *argv])
        return code, printed.getvalue(), err.getvalue()

    def test_a_record_or_registry_path_needs_a_person_named_as_having_inspected_it(self):
        for name in (RECORD_FILE, "sources/intake.jsonl", "sources/transitions.jsonl"):
            code, printed, err = self.dry("--path", name, "notes/a.md")
            self.assertEqual(code, 2, name)
            self.assertIn("--inspected-by", err)
            self.assertIn(name, err)
            self.assertNotIn("SECRET HUMAN PROMPT", printed)

    def test_the_protected_paths_are_the_ones_a_commit_packet_leaves_out(self):
        self.assertEqual(ar.protected_paths(
            [RECORD_FILE, "AI/Records/x/summary.md", "ai/records", "sources/intake.jsonl",
             "sources/intake.jsonl.bak", "ai/recordsX/y.md", "notes/a.md"]),
            [RECORD_FILE, "AI/Records/x/summary.md", "ai/records", "sources/intake.jsonl"])

    def test_with_an_inspection_the_prompt_says_what_went_and_who_read_it_first(self):
        code, printed, _ = self.dry("--path", RECORD_FILE, "notes/a.md",
                                    "--inspected-by", "Jane Roe")
        self.assertEqual(code, 0)
        self.assertNotIn("no trace record was sent", printed)
        self.assertIn("Records mode: trace-record or registry files sent whole as candidates, "
                      "so people's own words went to the model, inspected first by Jane Roe: "
                      + RECORD_FILE, printed)
        self.assertIn(f"- {RECORD_FILE}: the file as this run read it; a trace-record or "
                      "registry file, so the words in it went to the model. Inspected first by "
                      "Jane Roe", printed)
        self.assertIn("- notes/a.md: the file as this run read it\n", printed)

    def test_the_row_of_such_a_run_states_the_disclosure(self):
        revision = None
        with mock.patch.object(ar, "ROOT", self.root):
            revision = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files([RECORD_FILE])))
        out = self.root / "out"

        def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
            (directory / "findings.json").write_text(
                json.dumps(reply(revision=revision, target="a path candidate")), encoding="utf-8")
            (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
            return 0

        with mock.patch.object(ar, "ROOT", self.root), \
                mock.patch.object(ar, "git", lambda *args: ""), \
                mock.patch.object(ar, "head_bytes", lambda rel: None), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "launch", fake_launch), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = ar.main(["--path", RECORD_FILE, "--inspected-by", "Jane Roe",
                            *one_dir(out)])
        self.assertEqual(code, 0)
        row = json.loads((out / "check-row.json").read_text(encoding="utf-8"))
        self.assertIn("inspected first by Jane Roe", row["method"])
        self.assertIn("Records trace-record or registry files sent whole",
                      (out / "report.md").read_text(encoding="utf-8"))


class TheLatestMatchingRunLabelsTheCheck(unittest.TestCase):
    """LESSON (AH-162): the first matching run gave the label, so a check that
    passed, and then failed after the reviewer edited the packet, read as passed,
    and nothing mentioned the second run."""

    WANT = "python3 scripts/defect_ledger.py check"

    def label(self, *rows) -> dict:
        ran, _, _ = ar.transcript_facts("\n".join(rows) + "\n")
        return ar.check_label({"name": "ledger", "command": self.WANT, "outcome": "1"}, ran)

    def test_the_last_run_gives_the_label_and_the_earlier_one_stays_visible(self):
        row = self.label(item(f"/bin/zsh -lc '{self.WANT}'", 0, "no recurrences\n"),
                         item(f"/bin/zsh -lc '{self.WANT}'", 1, "1 recurrence\n", "failed"))
        self.assertEqual(row["label"], "matched (exit 1)")
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["tail"], "1 recurrence")
        self.assertEqual(row["repeats"], "2 matching runs with different exits, in order: "
                                         "matched (exit 0); matched (exit 1); the label is the last")

    def test_a_repeat_with_the_same_exit_is_listed_without_the_warning(self):
        row = self.label(item(f"/bin/zsh -lc '{self.WANT}'", 0, "a\n"),
                         item(f"/bin/zsh -lc '{self.WANT}'", 0, "b\n"))
        self.assertEqual(row["label"], "matched (exit 0)")
        self.assertTrue(row["repeats"].startswith("2 matching runs, in order"))

    def test_an_isolated_run_and_a_compound_one_keep_their_own_kinds_of_exit(self):
        row = self.label(item(f"/bin/zsh -lc '{self.WANT}'", 1, "failed\n", "failed"),
                         item(f"/bin/zsh -lc '{self.WANT} | tail -1'", 0, "ok\n"))
        self.assertEqual(row["label"], "matched, exit not isolated (shell exit 0)")
        self.assertIsNone(row["exit_code"])
        self.assertIn("with different exits", row["repeats"])
        self.assertIn("matched (exit 1); matched, exit not isolated (shell exit 0)",
                      row["repeats"])

    def test_one_run_carries_no_repeats(self):
        self.assertEqual(self.label(item(f"/bin/zsh -lc '{self.WANT}'", 0, "a\n"))["repeats"], "")

    def test_the_tally_and_the_report_name_a_check_that_changed_between_runs(self):
        transcript = "\n".join([item(f"/bin/zsh -lc '{self.WANT}'", 0, "a\n"),
                                item(f"/bin/zsh -lc '{self.WANT}'", 1, "b\n", "failed")]) + "\n"
        result = result_for(transcript, checks=[{"name": "ledger", "command": self.WANT,
                                                 "outcome": "clean"}])
        self.assertIn("1 of them run more than once with different exits", ar.tally(result)[1])
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta_for(), result,
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("- runs: 2 matching runs with different exits", text)


class AQuoteTheReviewerReadWithAFileTool(unittest.TestCase):
    """LESSON (AH-163): only Bash output was searched, so a quote a claude reviewer
    saw through Read, Grep or Glob was never `seen in output`, and on a read-only
    run, which has no shell, the label could not fire at all. `ran` stays
    commands only, as ClaudeTranscriptRows pins."""

    def transcript(self, *, is_error: bool = False, denied: bool = False) -> str:
        rows = [json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5"}),
                claude_use("toolu_41", "Read", file_path="scripts/x.py"),
                claude_reply("toolu_41", [{"type": "text", "text":
                                           "     2\tthe second line says something"}],
                             is_error=is_error)]
        if denied:
            rows.append(json.dumps({"type": "system", "subtype": "permission_denied",
                                    "tool_name": "Read", "tool_use_id": "toolu_41"}))
        rows.append(json.dumps({"type": "result", "subtype": "success",
                                "structured_output": REPLY}))
        return "\n".join(rows) + "\n"

    def seen(self, transcript: str) -> tuple[str, list]:
        result = ar.verify(SHA, {"findings": [finding()]}, transcript, {}, {}, "claude",
                           label_quote=lambda row: ("verified", ""))
        return result["findings"][0]["seen_label"], result["ran"]

    def test_a_quote_a_read_call_returned_is_seen(self):
        label, ran = self.seen(self.transcript())
        self.assertEqual(label, "seen in output")
        self.assertEqual(ran, [])

    def test_a_failed_or_denied_read_returned_no_file_text(self):
        self.assertEqual(self.seen(self.transcript(is_error=True))[0], "not seen in output")
        self.assertEqual(self.seen(self.transcript(denied=True))[0], "not seen in output")

    def test_codex_has_no_file_tools_to_read(self):
        self.assertEqual(ar.file_tool_output(TRANSCRIPT, "codex"), [])

    def test_the_report_says_the_label_covers_a_file_tools_output(self):
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta_for(), result_for(),
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("or in what a Read, Grep or Glob call returned", text)


class AWindowsPathNamesTheCheckout(unittest.TestCase):
    """LESSON (AH-165): the root was compared as `str(root)`, so on Windows a
    forward-slashed root and a backslashed `ai\\records` both went unflagged."""

    ROOT = pathlib.PureWindowsPath(r"C:\Users\ana\traceware")

    def test_either_separator_names_the_root_or_a_records_path(self):
        for text in (r"Get-Content C:/Users/ana/traceware/AGENTS.md",
                     r"Get-Content C:\Users\ana\traceware\AGENTS.md",
                     r"Get-Content ai\records\20260930-x\evidence.json",
                     r"type ai/records\20260930-x\summary.md"):
            self.assertTrue(ar.names_outside(text, self.ROOT), text)

    def test_a_path_inside_the_packet_is_still_not_flagged(self):
        self.assertFalse(ar.names_outside(r"Get-Content scripts\x.py", self.ROOT))
        self.assertFalse(ar.names_outside("", self.ROOT))

    def test_a_windows_command_is_listed_as_outside_the_packet(self):
        ran, _, _ = ar.transcript_facts(
            item(r"Get-Content C:/Users/ana/traceware/AGENTS.md", 0, "") + "\n")
        self.assertEqual(len(ar.outside_packet(ran, self.ROOT)), 1)


class TheCheckRowIsWrittenWithLf(unittest.TestCase):
    """LESSON (AH-166): the append opened the file in text mode without
    `newline`, which on Windows writes every row with CRLF."""

    def test_the_append_asks_for_lf_line_endings(self):
        target = scratch() / "checks.jsonl"
        seen = []
        real = pathlib.Path.open

        def spy(path, *args, **kwargs):
            seen.append(kwargs.get("newline"))
            return real(path, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "open", spy), \
                contextlib.redirect_stderr(io.StringIO()):
            ar.append_check_row(target, {"kind": "agent-review"}, target.parent, 0)
        self.assertEqual(seen, ["\n"])
        self.assertEqual(target.read_bytes(), b'{"kind": "agent-review"}\n')


class ThePacketIsRemovedWhenTheRunEnds(unittest.TestCase):
    """LESSON (AH-167): no packet was ever removed, 132M for one commit and, under
    `--records full`, the human's own words; the suite left one directory per
    patched run. The packet goes however the run ends, and the run directory
    stays."""

    def test_a_clean_run_removes_its_packet_and_keeps_its_run_directory(self):
        code, err, out, command = fresh_commit_run()
        self.assertEqual(code, 0)
        self.assertFalse(packet_of(command).parent.exists())
        self.assertIn("packet removed", err)
        for name in ("prompt.md", "transcript.jsonl", "findings.json", "report.md"):
            self.assertTrue((out / name).exists(), name)

    def test_a_failed_reviewer_removes_its_packet(self):
        code, _, _, command = fresh_commit_run(launch=lambda command, directory: 9)
        self.assertEqual(code, 9)
        self.assertFalse(packet_of(command).parent.exists())

    def test_an_exception_removes_its_packet(self):
        def boom(command, directory):
            raise RuntimeError("the launcher broke")

        seen = {}
        with self.assertRaises(RuntimeError):
            fresh_commit_run(launch=lambda command, directory: seen.update(
                packet=packet_of(command)) or boom(command, directory))
        self.assertFalse(seen["packet"].parent.exists())

    def test_a_packet_that_failed_to_build_is_removed(self):
        made = {}

        def half_built(sha, shas, packet, mode, inspected_by, sources=()):
            made["packet"] = packet
            (packet / "partial").mkdir(parents=True)
            raise subprocess.CalledProcessError(128, ["git", "archive"])

        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "build_packet", half_built), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(subprocess.CalledProcessError):
            ar.main([SHA, "--no-record", *one_dir(scratch() / "out")])
        self.assertFalse(made["packet"].parent.exists())

    def path_dry_run(self, *extra) -> tuple[pathlib.Path, str]:
        root = path_root({"notes/a.md": "alpha\n"})
        printed, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ar, "ROOT", root), mock.patch.object(ar, "git", lambda *a: ""), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(err):
            self.assertEqual(ar.main(["--path", "notes/a.md", "--dry-run", *extra]), 0)
        return packet_of(printed.getvalue().splitlines()[0].split(" ")), err.getvalue()

    def test_a_dry_run_removes_the_packet_it_built(self):
        packet, _ = self.path_dry_run()
        self.assertFalse(packet.parent.exists())
        # A commit dry run too, whose packet directory the run made even though
        # the export itself is stubbed here.
        out = scratch() / "out"
        printed = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar, "build_packet", return_value=({}, ["the tree"])), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            ar.main([SHA, "--dry-run", *one_dir(out)])
        self.assertFalse(packet_of(printed.getvalue().splitlines()[0].split(" ")).parent.exists())

    def test_keep_packet_keeps_it_and_says_where(self):
        packet, err = self.path_dry_run("--keep-packet")
        self.addCleanup(shutil.rmtree, packet.parent, True)
        self.assertEqual((packet / "notes" / "a.md").read_text(encoding="utf-8"), "alpha\n")
        self.assertIn(f"packet kept in {packet.parent} (--keep-packet)", err)

    def test_a_read_only_directory_in_the_packet_does_not_keep_it_alive(self):
        holder = scratch() / "holder"
        (holder / "packet" / "locked").mkdir(parents=True)
        (holder / "packet" / "locked" / "f.txt").write_text("x", encoding="utf-8")
        os.chmod(holder / "packet" / "locked", 0o500)
        # So the module's own cleanup can still remove it if the run does not.
        self.addCleanup(lambda: (holder / "packet" / "locked").exists()
                        and os.chmod(holder / "packet" / "locked", 0o700))
        with contextlib.redirect_stderr(io.StringIO()):
            ar.discard(holder, keep=False)
        self.assertFalse(holder.exists())


class AStreamRedirectKeepsTheChecksExit(unittest.TestCase):
    """AH-262: a live claude reviewer ran both checks as `... 2>&1`, and each was
    labelled as a line whose exit was not the check's, though a stream redirect
    leaves the exit status the command's own."""

    WANT = "python3 scripts/defect_ledger.py check"

    def label(self, command: str, want: str = WANT, exit_code: int = 1) -> str:
        ran = [{"command": command, "exit_code": exit_code, "output": "", "status": "failed"}]
        return ar.check_label({"name": "c", "command": want, "outcome": "x"}, ran)["label"]

    def test_a_trailing_stream_redirect_keeps_the_checks_own_exit(self):
        for suffix in (" 2>&1", " 2>/dev/null", " >&2", " 2>&1 >/dev/null"):
            self.assertEqual(self.label(self.WANT + suffix), "matched (exit 1)", suffix)

    def test_a_codex_shell_wrapper_with_a_redirect_is_matched_too(self):
        self.assertEqual(self.label(f"/bin/zsh -lc '{self.WANT} 2>&1'"), "matched (exit 1)")

    def test_a_redirect_into_a_pipeline_still_reports_the_shell_exit(self):
        self.assertEqual(self.label(f"{self.WANT} 2>&1 | tail -2"),
                         "matched, exit not isolated (shell exit 1)")

    def test_a_redirect_to_a_file_is_not_dropped(self):
        self.assertEqual(self.label(f"{self.WANT} > out.txt"),
                         "matched, exit not isolated (shell exit 1)")

    def test_a_check_stated_with_a_redirect_matches_the_bare_run(self):
        self.assertEqual(self.label(self.WANT, want=self.WANT + " 2>&1"), "matched (exit 1)")


class AnEmptySectionSaysNone(unittest.TestCase):
    """AH-263: with nothing flagged, two sections printed a blank line where every
    other empty section says `None.`"""

    def test_no_outside_call_and_no_modified_path_each_read_none(self):
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta_for(),
                                   dict(result_for(), outside=[], modified=[]),
                                   ["the tree"]).read_text(encoding="utf-8")
        for title in ("## Commands and file-tool calls naming the main checkout or a records path",
                      "## Paths the reviewer modified"):
            self.assertIn(title + "\n\nNone.\n", text)

    def test_a_flagged_call_and_a_modified_path_are_listed_without_none(self):
        with tempfile.TemporaryDirectory() as directory:
            text = ar.write_report(pathlib.Path(directory), meta_for(),
                                   dict(result_for(), outside=[{"command": "cat ai/records/x"}],
                                        modified=["changed a.py"]),
                                   ["the tree"]).read_text(encoding="utf-8")
        self.assertIn("- `cat ai/records/x`", text)
        self.assertIn("- changed a.py", text)
        self.assertNotIn("records path\n\nNone.", text)
        self.assertNotIn("## Paths the reviewer modified\n\nNone.", text)


# LESSON (R, 2026-10-14): an advisory run wrote its report to the ignored
# .agent-reviews/, so the tracked check row and acceptance row kept counts and a
# hash of a file no other clone had, and `verify` read the artifact as
# unavailable. The reviewer's `evidence_examined` was required and never printed.
# A run now writes what a reader needs to the tracked review directory, keeps its
# stream in the ignored run directory, and withholds the reviewer's text from the
# tracked copy when the packet carried people's own words or ignored files.

SECRET = "SECRET HUMAN WORDS"


def split_commit_run(*extra, payload: dict | None = None, root: pathlib.Path | None = None,
                     dirs: bool = True) -> tuple[int, str, pathlib.Path, pathlib.Path]:
    """A fresh commit run with the reviewer stubbed, its run directory and review
    directory apart. With `root`, the run happens under that root; with `dirs`
    false, the run picks both directories itself. Returns the exit code, stderr,
    the run directory and the review directory."""
    base = root or scratch()
    run, review = base / "run", base / "review"
    payload = payload or reply()

    def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
        (directory / "findings.json").write_text(json.dumps(payload), encoding="utf-8")
        (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "stderr.txt").write_text("codex says nothing\n", encoding="utf-8")
        return 0

    err = io.StringIO()
    where = ["--out-dir", str(run), "--review-dir", str(review)] if dirs else []
    with mock.patch.object(ar, "ROOT", root or ar.ROOT), mock.patch.object(ar, "git", fake_git), \
            mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
            mock.patch.object(ar, "build_packet", return_value=({}, ["the tree"])), \
            mock.patch.object(ar, "manifest", return_value={}), \
            mock.patch.object(ar, "launch", fake_launch), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = ar.main([SHA, "--no-record", *where, *extra])
    return code, err.getvalue(), run, review


def only_child(directory: pathlib.Path) -> pathlib.Path:
    found = list(directory.iterdir())
    assert len(found) == 1, found
    return found[0]


def sha256_text(path: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TheReportIsTrackedAndTheStreamIsNot(unittest.TestCase):
    """Every run, advisory or delegated, writes the parts a reader needs to the
    tracked ai/reviews/ and keeps its stream in the ignored run directory."""

    def default_run(self, *extra, payload=None):
        root = scratch().resolve()
        code, err, _, _ = split_commit_run(*extra, payload=payload, root=root, dirs=False)
        self.assertEqual(code, 0, err)
        run = only_child(root / ".agent-reviews" / "agentic-review")
        review = only_child(root / "ai" / "reviews")
        return root, run, review

    def test_an_advisory_run_writes_its_report_under_ai_reviews_by_the_run_name(self):
        _, run, review = self.default_run()
        self.assertEqual(run.name, review.name)
        self.assertTrue(review.name.startswith(SHA[:7] + "-"), review.name)
        self.assertEqual(sorted(p.name for p in review.iterdir()),
                         ["check-row.json", "findings.json", "prompt.md", "record-call.json",
                          "report.md"])
        self.assertEqual(sorted(p.name for p in run.iterdir()),
                         ["findings.json", "prompt.md", "stderr.txt", "transcript.jsonl"])
        self.assertEqual((review / "findings.json").read_bytes(),
                         (run / "findings.json").read_bytes())

    def test_a_delegated_run_keeps_its_transcript_out_of_the_tracked_directory_too(self):
        _, run, review = self.default_run(
            "--authorized-by", "Jane Roe", "--authority", "Jane Roe authorized a decision in T4.",
            payload=reply(disposition="return"))
        self.assertTrue((review / "report.md").exists())
        self.assertFalse((review / "transcript.jsonl").exists())
        self.assertTrue((run / "transcript.jsonl").exists())
        call = json.loads((review / "record-call.json").read_text(encoding="utf-8"))
        self.assertEqual(call[call.index("--decision") + 1], "return")

    def test_both_rows_point_at_the_tracked_report_and_its_digest(self):
        _, run, review = self.default_run()
        report = f"ai/reviews/{review.name}/report.md"
        digest = sha256_text(review / "report.md")
        row = json.loads((review / "check-row.json").read_text(encoding="utf-8"))
        self.assertEqual(row["artifact"], report)
        self.assertEqual(row["reference"], "path")
        self.assertIn(f"report sha256 {digest}", row["result"])
        self.assertIn(f"in .agent-reviews/agentic-review/{run.name}", row["limits"])
        call = json.loads((review / "record-call.json").read_text(encoding="utf-8"))
        self.assertIn("--agent", call)
        self.assertEqual(call[call.index("--evidence") + 1], report)
        self.assertIn(f"report {report} sha256 {digest}", call[call.index("--scope") + 1])

    def test_the_report_names_the_stream_it_does_not_carry_with_each_digest(self):
        _, run, review = self.default_run()
        text = (review / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Kept out of the review directory", text)
        for name in ("transcript.jsonl", "stderr.txt"):
            self.assertIn(f"`.agent-reviews/agentic-review/{run.name}/{name}`, sha256 "
                          f"{sha256_text(run / name)}", text)

    def test_a_run_named_with_out_dir_keeps_its_name_under_ai_reviews(self):
        root = scratch().resolve()
        code, err, _, _ = split_commit_run("--out-dir", ".agent-reviews/agentic-review/my-run",
                                           root=root, dirs=False)
        self.assertEqual(code, 0, err)
        self.assertTrue((root / "ai" / "reviews" / "my-run" / "report.md").exists())
        self.assertTrue((root / ".agent-reviews" / "agentic-review" / "my-run"
                         / "transcript.jsonl").exists())

    def test_a_path_run_writes_its_review_directory_too(self):
        root = path_root({"notes/a.md": FILE_TEXT})
        with mock.patch.object(ar, "ROOT", root):
            revision = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files(["notes/a.md"])))
        (root / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (root / "f.json").write_text(json.dumps(reply(
            revision=revision, findings=[finding(file="notes/a.md")])), encoding="utf-8")
        with mock.patch.object(ar, "ROOT", root), mock.patch.object(ar, "git", lambda *a: ""), \
                mock.patch.object(ar, "head_bytes", lambda rel: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = ar.main(["--path", "notes/a.md", "--replay", "--transcript",
                            str(root / "t.jsonl"), "--findings", str(root / "f.json")])
        self.assertEqual(code, 0)
        review = only_child(root / "ai" / "reviews")
        self.assertTrue(review.name.startswith("paths-"), review.name)
        row = json.loads((review / "check-row.json").read_text(encoding="utf-8"))
        self.assertEqual(row["artifact"], f"ai/reviews/{review.name}/report.md")
        text = (review / "report.md").read_text(encoding="utf-8")
        self.assertIn("the second line says something", text)
        # Under the root, a path is named relative to it, as the rows name the report.
        self.assertIn("- transcript: `t.jsonl`, sha256 ", text)
        # A replay writes nothing it has not got: no run directory, no prompt.
        self.assertFalse((root / ".agent-reviews").exists())
        self.assertFalse((review / "prompt.md").exists())

    def test_a_fresh_run_refuses_a_review_directory_an_earlier_run_left(self):
        base = scratch()
        (base / "review").mkdir()
        (base / "review" / "report.md").write_text("an earlier report\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(ar, "git", fake_git), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "launch", side_effect=AssertionError("launched")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main([SHA, "--no-record", "--out-dir", str(base / "run"),
                            "--review-dir", str(base / "review")])
        self.assertEqual(code, 2)
        self.assertIn("refusing to start: an earlier run left", err.getvalue())
        self.assertIn("report.md", err.getvalue())
        self.assertEqual((base / "review" / "report.md").read_text(encoding="utf-8"),
                         "an earlier report\n")


class TheReportPrintsWhatTheReviewerExamined(unittest.TestCase):
    """`evidence_examined` and both sets of criteria reach the report."""

    def render(self, **over) -> str:
        with tempfile.TemporaryDirectory() as directory:
            return ar.write_report(pathlib.Path(directory), meta_for(**over), result_for(),
                                   ["the tree"]).read_text(encoding="utf-8")

    def test_the_paths_and_commands_the_reviewer_states_are_printed(self):
        text = self.render(findings_json={"evidence_examined": {
            "paths": ["scripts/x.py", ".review/diff.patch"],
            "commands": ["python3 scripts/defect_ledger.py check"]}})
        self.assertIn("## Evidence the reviewer examined", text)
        self.assertIn("Paths it says it opened or compared:\n\n- `scripts/x.py`\n"
                      "- `.review/diff.patch`", text)
        self.assertIn("Commands it says it ran:\n\n- `python3 scripts/defect_ledger.py check`",
                      text)
        self.assertIn("This is the reviewer's own account.", text)

    def test_an_empty_list_and_a_missing_field_each_say_so(self):
        text = self.render(findings_json={"evidence_examined": {"paths": [], "commands": []}})
        self.assertIn("Paths it says it opened or compared:\n\n- none stated", text)
        self.assertIn("Not stated: the reply carries no `evidence_examined`.", self.render())

    def test_the_criteria_requested_and_applied_stand_side_by_side(self):
        text = self.render(criteria="every claim quotes its source",
                           findings_json={"criteria": ["hygiene", "quotes"]})
        self.assertIn("## Criteria\n\n- requested: every claim quotes its source\n"
                      "- reported applied: hygiene\n- reported applied: quotes", text)
        text = self.render()
        self.assertIn("- requested: none set, so the contract's standing criterion applied", text)
        self.assertIn("- reported applied: not stated", text)

    def test_a_report_with_no_stream_named_prints_no_such_section(self):
        self.assertNotIn("## Kept out of the review directory", self.render())


class TheReviewersTextIsWithheldWhereItCanCarryPrivateWords(unittest.TestCase):
    """Nothing the run kept private before is written to the tracked directory:
    when the packet carried people's own words or files Git ignores, or a finding
    cites such a file, the tracked copy carries the labels and counts alone."""

    PAYLOAD = reply(criteria=[f"applied {SECRET}"],
                    evidence_examined={"paths": [f".review/records/{SECRET}.json"],
                                       "commands": [f"grep '{SECRET}' scripts/x.py"]},
                    checks=[{"name": "ledger", "command": "python3 scripts/defect_ledger.py check",
                             "outcome": f"clean {SECRET}"}],
                    evidence_unavailable=[f"gap {SECRET}"],
                    unresolved_objections=[f"objection {SECRET}"],
                    findings=[finding(claim=f"claim {SECRET}", why=f"why {SECRET}",
                                      ask=f"ask {SECRET}", quote=f"quote {SECRET}")])

    def assert_withheld(self, run: pathlib.Path, review: pathlib.Path, because: str) -> str:
        tracked = (review / "report.md").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, tracked)
        self.assertIn("## Withheld from this copy", tracked)
        self.assertIn(because, tracked)
        self.assertFalse((review / "findings.json").exists())
        complete = (run / "report.md").read_text(encoding="utf-8")
        self.assertIn(f"quote {SECRET}", complete)
        self.assertIn(f"grep '{SECRET}' scripts/x.py", complete)
        self.assertIn(f"- complete report: `{(run / 'report.md').as_posix()}`, sha256 "
                      f"{sha256_text(run / 'report.md')}", tracked)
        self.assertIn(f"sha256 {sha256_text(run / 'findings.json')}", tracked)
        # The rows land in tracked files as well, so they carry none of it either.
        for name in ("check-row.json", "record-call.json", "prompt.md"):
            self.assertNotIn(SECRET, (review / name).read_text(encoding="utf-8"), name)
        row = json.loads((review / "check-row.json").read_text(encoding="utf-8"))
        self.assertIn("1 criteria, withheld with its text", row["criterion"])
        self.assertIn("withheld from the tracked report", row["limits"])
        return tracked

    def test_records_full_keeps_the_reviewers_text_in_the_run_directory(self):
        code, err, run, review = split_commit_run(
            "--records", "full", "--inspected-by", "Jane Roe", payload=self.PAYLOAD)
        self.assertEqual(code, 0, err)
        tracked = self.assert_withheld(run, review, "--records full")
        # The labels and counts stay readable.
        self.assertIn("- F001 (low), scripts/x.py lines 2-2: quote invented, not seen in output",
                      tracked)
        self.assertIn("- check 1: matched (exit 0)", tracked)
        self.assertIn("1 path(s) and 1 command(s) stated, withheld.", tracked)
        self.assertIn("1 objection(s), withheld.", tracked)
        self.assertIn("- reported applied: 1 stated, withheld", tracked)
        # The prompt is the host's own text and names what went without carrying it.
        self.assertTrue((review / "prompt.md").exists())
        self.assertIn("The reviewer's text is withheld from it", err)

    def test_a_delegated_run_counts_the_reviewers_criteria_and_objections_too(self):
        # LESSON (rc22 review, ee9a334 F001): the delegated branch of
        # record_argv copied the reviewer's own criteria and unresolved
        # objections into record-call.json and the acceptance row, both
        # tracked, while the report and the check row withheld them; only
        # advisory runs were tested.
        payload = dict(self.PAYLOAD, disposition="return")
        code, err, run, review = split_commit_run(
            "--records", "full", "--inspected-by", "Jane Roe", "--authorized-by", "Jane Roe",
            "--authority", "Jane Roe authorized a decision in T4.", "--criteria", "quotes hold",
            payload=payload)
        self.assertEqual(code, 0, err)
        self.assert_withheld(run, review, "--records full")
        call = json.loads((review / "record-call.json").read_text(encoding="utf-8"))
        self.assertEqual(call[call.index("--decision") + 1], "return")
        criteria = call[call.index("--criteria") + 1]
        self.assertIn("requested: quotes hold; reviewer reported applying: 1 criteria, withheld "
                      "with its text", criteria)
        objections = call[call.index("--objections") + 1]
        self.assertTrue(objections.startswith("1 objection(s), withheld with its text"), objections)
        digest = sha256_text(review / "report.md")
        self.assertIn(f"sha256 {digest}", objections)
        # The researcher's own words are the caller's, and travel as before.
        self.assertEqual(call[call.index("--authority") + 1], "Jane Roe authorized a decision in T4.")

    def test_a_finding_citing_a_record_file_withholds_the_text_in_a_minimal_run(self):
        payload = dict(self.PAYLOAD, findings=[finding(file="ai/records/x/evidence.json",
                                                       quote=f"quote {SECRET}")])
        code, err, run, review = split_commit_run(payload=payload)
        self.assertEqual(code, 0, err)
        self.assert_withheld(run, review, "A finding cites files whose text stays out of Git: "
                             "ai/records/x/evidence.json (a trace-record or registry file)")

    def test_a_record_file_named_as_a_path_candidate_is_withheld_too(self):
        root = path_root({RECORD_FILE: f'{{"turns": [{{"text": "{SECRET}"}}]}}\n'})
        with mock.patch.object(ar, "ROOT", root):
            revision = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files([RECORD_FILE])))
        payload = reply(revision=revision, findings=[finding(
            file=RECORD_FILE, line_start=1, line_end=1, quote=SECRET)])
        (root / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (root / "f.json").write_text(json.dumps(payload), encoding="utf-8")
        run, review = root / "run", root / "review"
        with mock.patch.object(ar, "ROOT", root), mock.patch.object(ar, "git", lambda *a: ""), \
                mock.patch.object(ar, "head_bytes", lambda rel: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = ar.main(["--path", RECORD_FILE, "--replay", "--transcript",
                            str(root / "t.jsonl"), "--findings", str(root / "f.json"),
                            "--out-dir", str(run), "--review-dir", str(review)])
        self.assertEqual(code, 0)
        tracked = (review / "report.md").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, tracked)
        self.assertIn(f"The candidates include files whose text stays out of Git: {RECORD_FILE} "
                      "(a trace-record or registry file)", tracked)
        # The finding cites the candidate, which is named once; nothing stated reads so.
        self.assertNotIn("a finding cites", tracked)
        self.assertIn("- reported applied: not stated", tracked)
        self.assertIn("## Evidence the reviewer examined\n\nNone stated.", tracked)
        self.assertIn(f"- F001 (low), {RECORD_FILE} lines 1-1: quote verified", tracked)
        self.assertIn(SECRET, (run / "report.md").read_text(encoding="utf-8"))
        self.assertFalse((review / "findings.json").exists())

    def test_one_directory_for_both_keeps_nothing_apart_and_says_so(self):
        out = scratch() / "out"
        code, err, _, _ = split_commit_run("--records", "full", "--inspected-by", "Jane Roe",
                                           *one_dir(out), payload=self.PAYLOAD)
        self.assertEqual(code, 0, err)
        self.assertIn("the review directory is the run directory, so nothing is kept apart", err)
        text = (out / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("## Withheld from this copy", text)
        self.assertIn(f"quote {SECRET}", text)

    def test_an_ordinary_advisory_run_withholds_nothing(self):
        code, err, run, review = split_commit_run(payload=reply(findings=[finding()]))
        self.assertEqual(code, 0, err)
        text = (review / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("## Withheld from this copy", text)
        self.assertIn("the second line says something", text)
        self.assertNotIn("withheld", json.loads(
            (review / "check-row.json").read_text(encoding="utf-8"))["limits"])


class WhichFilesStayOutOfGit(unittest.TestCase):
    """`private_why` names the files whose text is not copied into a tracked file."""

    def test_records_registries_and_the_packets_view_of_them(self):
        root = scratch()
        for name in ("ai/records/x/evidence.json", "AI/Records/x/summary.md",
                     "sources/intake.jsonl", ".review/records/x.json",
                     "./.review/records/x/evidence.json"):
            self.assertEqual(ar.private_why(name, root), "a trace-record or registry file", name)

    def test_a_path_outside_the_repository(self):
        root = scratch()
        self.assertEqual(ar.private_why("../elsewhere.txt", root), "outside the repository")
        self.assertEqual(ar.private_why(str(scratch() / "x.txt"), root), "outside the repository")

    def test_a_file_git_ignores_and_one_it_does_not(self):
        root = scratch()
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        (root / ".gitignore").write_text("secret/\n.env\n", encoding="utf-8")
        self.assertEqual(ar.private_why("secret/a.txt", root), "ignored by Git")
        self.assertEqual(ar.private_why(".env", root), "ignored by Git")
        self.assertEqual(ar.private_why("notes/a.md", root), "")
        self.assertEqual(ar.private_why("", root), "")

    def test_without_a_repository_nothing_is_called_ignored(self):
        self.assertEqual(ar.private_why("notes/a.md", scratch()), "")

    def test_the_reason_names_each_file_and_why(self):
        root = scratch()
        with mock.patch.object(ar, "ROOT", root):
            self.assertEqual(ar.withheld_reason("minimal", ["notes/a.md"], ["scripts/x.py"]), "")
            reason = ar.withheld_reason("full", [], ["ai/records/x/t.json", "ai/records/x/t.json"])
        self.assertIn("(--records full)", reason)
        self.assertIn("a finding cites files whose text stays out of Git: ai/records/x/t.json "
                      "(a trace-record or registry file)", reason)
        self.assertEqual(reason.count("ai/records/x/t.json"), 1)

    def test_the_root_is_the_one_in_force_when_the_question_is_asked(self):
        # LESSON (rc22 review, ee9a334 F002): private_why bound its root when
        # the module loaded, so a run under a patched ROOT asked Git about the
        # checkout the tests ran from, and a file only that root ignores was
        # sent to the tracked report.
        root = scratch()
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        (root / ".gitignore").write_text("held-back/\n", encoding="utf-8")
        with mock.patch.object(ar, "ROOT", root):
            self.assertEqual(ar.private_why("held-back/a.md"), "ignored by Git")
            reason = ar.withheld_reason("minimal", [], ["held-back/a.md"])
        self.assertIn("a finding cites files whose text stays out of Git: held-back/a.md "
                      "(ignored by Git)", reason)


class HistoricalRowsKeepTheirMeaning(unittest.TestCase):
    """A row written before the review directory existed reads as it did: a check
    row pointing at .agent-reviews/ is unavailable on a clone without it, and an
    advisory acceptance row with no evidence field is still valid."""

    def evaluate(self, artifacts: list[str]) -> dict:
        import trace_record as tr
        root = scratch().resolve()
        before = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, before)
        (root / "ai" / "reviews" / "6eefd2a-20261014T120000Z").mkdir(parents=True)
        (root / "ai" / "reviews" / "6eefd2a-20261014T120000Z" / "report.md").write_text(
            "# a report\n", encoding="utf-8")
        record = root / "ai" / "records" / "test"
        record.mkdir(parents=True)
        (record / "summary.md").write_text("The author set the direction (T1).", encoding="utf-8")
        (record / "evidence.json").write_text(json.dumps({
            "record_id": "test", "record_schema_version": 2,
            "consent": {"third_party_review": "cleared"},
            "turns": [{"id": "T1", "text": "first"}]}), encoding="utf-8")
        rows = [ar.check_row(meta_for(), result_for(), pathlib.Path(artifact), "d1ge57", None)
                for artifact in artifacts]
        (record / "checks.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows),
                                             encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            tr.cmd_cover(type("A", (), {"record": str(record)})())
        return tr.evaluate_record(record)

    def test_a_tracked_report_resolves_and_a_historical_run_directory_is_unavailable(self):
        found = self.evaluate(["ai/reviews/6eefd2a-20261014T120000Z/report.md",
                               ".agent-reviews/agentic-review/6eefd2a-20260920T145120Z/report.md"])
        self.assertTrue(found["ok"], found["problems"])
        self.assertIn("artifacts: 1 resolved, 0 declared, 1 unavailable, 0 nonexistent",
                      found["notes"])

    def test_a_historical_row_alone_still_passes(self):
        found = self.evaluate([".agent-reviews/agentic-review/6eefd2a-20260920T145120Z/"
                               "report.md"])
        self.assertTrue(found["ok"], found["problems"])
        self.assertIn("artifacts: 0 resolved, 0 declared, 1 unavailable, 0 nonexistent",
                      found["notes"])

    def test_an_advisory_row_with_and_without_the_evidence_field_validates(self):
        import trace_record as tr
        base = {"kind": "agent", "kind_basis": "declared", "reviewer": "gpt-6-astra",
                "at": "2026-10-14T12:00:00Z", "covers": [SHA], "count": 1,
                "scope": "agentic review", "producer_agents": [], "self_review": False}
        problems, _ = tr.validate_acceptances(
            [base, dict(base, evidence="ai/reviews/6eefd2a-20261014T120000Z/report.md")])
        self.assertEqual(problems, [])


def linked_root(files: dict[str, str], shared: dict[str, str]) -> tuple[pathlib.Path, pathlib.Path]:
    """A repository root whose workspace/shared is a link to a folder outside it, as
    a sync app's folder is, holding the given files. Skips where links cannot be made."""
    root = path_root(files)
    outside = scratch().resolve() / "OneDrive" / "project"
    outside.mkdir(parents=True)
    for name, text in shared.items():
        target = outside / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    (root / "workspace").mkdir(exist_ok=True)
    try:
        (root / "workspace" / "shared").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        raise unittest.SkipTest(f"no symbolic link on this machine: {exc}")
    return root, outside


def sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SURVEY = "firm,employees\nalpha,1240\nbeta,77\n"


class AFileInTheSharedFolder(unittest.TestCase):
    """LESSON (rc22): `--path` refused every file in a linked shared folder as outside
    the repository, because the link resolves to the sync app's folder, and AGENTS.md
    tells a team to keep its data and cited papers there."""

    def test_it_is_named_by_the_path_the_project_knows_it_under(self):
        root, outside = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        self.assertEqual(ar.project_path("workspace/shared/data/survey.csv", root),
                         "workspace/shared/data/survey.csv")
        # An absolute path into the folder the link points at is named the same way.
        self.assertEqual(ar.project_path(str(outside / "data" / "survey.csv"), root),
                         "workspace/shared/data/survey.csv")

    def test_a_link_inside_the_shared_folder_that_leads_out_is_still_refused(self):
        root, outside = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        elsewhere = scratch().resolve() / "elsewhere.csv"
        elsewhere.write_text("x\n", encoding="utf-8")
        (outside / "escape.csv").symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            ar.project_path("workspace/shared/escape.csv", root)

    def test_a_path_candidate_may_lie_in_the_shared_folder(self):
        root, _ = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        with mock.patch.object(ar, "ROOT", root):
            rels = ar.candidate_paths(["workspace/shared/data/survey.csv", "notes/a.md"])
            files = ar.candidate_files(rels)
        self.assertEqual(rels, ["notes/a.md", "workspace/shared/data/survey.csv"])
        self.assertEqual(files["workspace/shared/data/survey.csv"], SURVEY.encode("utf-8"))

    def test_git_is_not_asked_about_a_path_through_the_link(self):
        root, _ = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        self.assertEqual(ar.private_why("workspace/shared/data/survey.csv", root),
                         "in the shared folder, which Git does not hold")


class TheSourcesARunSends(unittest.TestCase):
    """A source is named by its project path, with the sha256 of the bytes read."""

    def test_a_folder_stands_for_its_files_and_the_sync_apps_housekeeping_is_left_out(self):
        root, _ = linked_root({"notes/a.md": "a\n"},
                              {"data/survey.csv": SURVEY, "data/codebook.md": "codes\n",
                               "data/.DS_Store": "x"})
        found = ar.collect_sources(["workspace/shared/data", "notes/a.md"], root)
        self.assertEqual([(e["path"], e["sha256"], e["where"]) for e in found], [
            ("notes/a.md", sha("a\n"), "the repository"),
            ("workspace/shared/data/codebook.md", sha("codes\n"), "the shared folder"),
            ("workspace/shared/data/survey.csv", sha(SURVEY), "the shared folder")])
        # Where the bytes are on this machine stays out of every row.
        self.assertEqual(ar.source_rows(found)[0], {"path": "notes/a.md", "sha256": sha("a\n")})

    def test_a_path_outside_the_project_and_a_missing_one_are_refused(self):
        root = path_root({"notes/a.md": "a\n"})
        elsewhere = scratch().resolve() / "x.csv"
        elsewhere.write_text("x\n", encoding="utf-8")
        for name, phrase in ((str(elsewhere), "is outside"), ("notes/gone.csv", "not a file")):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                ar.collect_sources([name], root)
            self.assertIn(phrase, str(caught.exception))

    def snapshot_root(self, extra: dict[str, str] | None = None) -> tuple[pathlib.Path, dict]:
        snapshots = ar.starter_script("shared_snapshot")
        if snapshots is None:
            self.skipTest("scripts/shared_snapshot.py is not in this project")
        root, outside = linked_root({"notes/a.md": "a\n"},
                                    dict({"data/survey.csv": SURVEY}, **(extra or {})))
        manifest = snapshots.create_manifest(outside / "data", "survey")
        made = snapshots.materialize(outside / "data", manifest,
                                     root / "workspace" / "snapshots" / "survey")
        return root, made

    def test_a_snapshot_store_stands_for_its_current_verified_snapshot(self):
        root, made = self.snapshot_root()
        found = ar.collect_sources(["workspace/snapshots/survey"], root)
        self.assertEqual(len(found), 1)
        entry = found[0]
        self.assertEqual(entry["path"],
                         f"workspace/snapshots/survey/snapshots/{made['snapshot']}/data/survey.csv")
        self.assertEqual(entry["sha256"], sha(SURVEY))
        self.assertEqual(entry["snapshot"], made["snapshot"])
        self.assertIn(f"verified snapshot {made['snapshot'][:12]} of survey", entry["where"])
        self.assertEqual(ar.source_rows(found)[0]["snapshot"], made["snapshot"])

    def test_a_snapshot_that_no_longer_matches_its_manifest_is_refused(self):
        root, made = self.snapshot_root()
        (pathlib.Path(made["data"]) / "survey.csv").write_text("tampered\n", encoding="utf-8")
        for name in ("workspace/snapshots/survey",
                     f"workspace/snapshots/survey/snapshots/{made['snapshot']}/data/survey.csv"):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                ar.collect_sources([name], root)
            self.assertIn("does not verify", str(caught.exception))

    def test_a_snapshots_own_folder_its_data_and_a_folder_in_it_are_that_snapshot(self):
        # LESSON (rc22 review, f1ca7a5 F001): only a file below a snapshot's
        # data folder was read as that snapshot, so naming the snapshot's own
        # folder or its data folder sent its files unchecked against the
        # manifest, its manifest included, and a folder below data failed on
        # a raw IsADirectoryError.
        root, made = self.snapshot_root({"data/waves/w1.csv": "wave,1\n"})
        folder = f"workspace/snapshots/survey/snapshots/{made['snapshot']}"
        data = f"{folder}/data"
        both = [f"{data}/survey.csv", f"{data}/waves/w1.csv"]
        for name, expected in ((folder, both), (folder + "/", both), (data, both),
                               (f"{data}/waves", [f"{data}/waves/w1.csv"])):
            with self.subTest(name=name):
                found = ar.collect_sources([name], root)
                self.assertEqual([entry["path"] for entry in found], expected)
                for entry in found:
                    self.assertEqual(entry["snapshot"], made["snapshot"])
                    self.assertIn(f"verified snapshot {made['snapshot'][:12]} of survey",
                                  entry["where"])
        # A path inside the snapshot that is not there is said to be so.
        with self.assertRaises(ValueError) as caught:
            ar.collect_sources([f"{data}/gone.csv"], root)
        self.assertIn("not a file or a folder", str(caught.exception))
        # And a tampered snapshot named by its folder is refused, as by its store.
        (pathlib.Path(made["data"]) / "survey.csv").write_text("tampered\n", encoding="utf-8")
        for name in (folder, data):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                ar.collect_sources([name], root)
            self.assertIn("does not verify", str(caught.exception))

    def test_the_packet_carries_each_source_and_a_listing_of_their_digests(self):
        root, _ = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        found = ar.collect_sources(["workspace/shared/data/survey.csv"], root)
        packet = scratch() / "packet"
        (packet / ".review").mkdir(parents=True)
        sent = ar.pack_sources(found, packet)
        self.assertEqual((packet / ".review/sources/workspace/shared/data/survey.csv")
                         .read_text(encoding="utf-8"), SURVEY)
        self.assertEqual((packet / ".review/sources.txt").read_text(encoding="utf-8"),
                         f"{sha(SURVEY)}  workspace/shared/data/survey.csv\n")
        self.assertIn(f"sha256 {sha(SURVEY)}", sent[0])
        self.assertIn("from the shared folder", sent[0])

    def test_a_source_changed_while_it_was_copied_stops_the_run(self):
        root, outside = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        found = ar.collect_sources(["workspace/shared/data/survey.csv"], root)
        (outside / "data" / "survey.csv").write_text("changed\n", encoding="utf-8")
        packet = scratch() / "packet"
        (packet / ".review").mkdir(parents=True)
        with self.assertRaises(ValueError):
            ar.pack_sources(found, packet)


class TheSourceQuoteIsLabelledToo(unittest.TestCase):
    """LESSON (rc22): a finding carried one quote, from the candidate, so the source
    side of a mismatch was the reviewer's word alone."""

    def setUp(self):
        root, self.outside = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        self.sources = ar.collect_sources(["workspace/shared/data/survey.csv"], root)

    def label(self, **over) -> tuple[str, str]:
        return ar.source_label(self.sources, finding(source_file="workspace/shared/data/survey.csv",
                                                     **over))

    def test_a_quote_in_the_source_is_verified_and_one_that_is_not_is_invented(self):
        self.assertEqual(self.label(source_quote="alpha,1240"), ("verified", ""))
        self.assertEqual(self.label(source_quote="alpha,1204")[0], "invented")
        # The packet path is read as the source it names.
        self.assertEqual(ar.source_label(self.sources, finding(
            source_file=".review/sources/workspace/shared/data/survey.csv",
            source_quote="beta,77"))[0], "verified")

    def test_a_finding_that_quotes_no_source_is_labelled_none(self):
        self.assertEqual(ar.source_label(self.sources, finding()), ("none", ""))
        self.assertEqual(ar.source_label(self.sources, finding(source_file="", source_quote="")),
                         ("none", ""))

    def test_a_source_the_run_did_not_send_is_invented_unless_the_fallback_holds_it(self):
        row = finding(source_file="data/other.csv", source_quote="alpha")
        self.assertEqual(ar.source_label(self.sources, row)[0], "invented")
        held = ar.source_label(self.sources, row, lambda name: (b"alpha\n", "the commit"))
        self.assertEqual(held, ("verified", ""))

    def test_a_source_changed_after_it_was_sent_or_not_text_is_not_compared(self):
        (self.outside / "data" / "survey.csv").write_text("alpha,1240\nchanged\n",
                                                          encoding="utf-8")
        label, note = self.label(source_quote="alpha,1240")
        self.assertEqual(label, ar.NOT_COMPARED)
        self.assertIn("changed after the run sent it", note)
        self.assertEqual(ar.source_label(self.sources, finding(source_file="data/x.pdf",
                                                               source_quote="alpha"),
                                         lambda name: (b"%PDF\xff\xfe", "the commit"))[0],
                         ar.NOT_COMPARED)


class APathRunWithSources(unittest.TestCase):
    """End to end: the reviewer gets the sources, and the digests reach the prompt,
    the report and the check row."""

    def setUp(self):
        self.root, _ = linked_root({"paper/t2.md": "The sample holds 1,204 firms.\n"},
                                   {"data/survey.csv": SURVEY})
        with mock.patch.object(ar, "ROOT", self.root):
            self.revision = ar.candidate_revision(ar.candidate_listing(
                ar.candidate_files(["paper/t2.md"])))

    def fresh(self, findings: list[dict], *extra) -> tuple[int, pathlib.Path, dict]:
        out = self.root / "out"
        payload = reply(revision=self.revision, target="a path candidate", findings=findings)
        seen = {}

        def fake_launch(command, directory, cwd, env, feed_stdin, timeout):
            packet = pathlib.Path(command[command.index("-C") + 1])
            seen["files"] = {p.relative_to(packet).as_posix(): p.read_bytes()
                             for p in packet.rglob("*") if p.is_file()}
            seen["prompt"] = (directory / "prompt.md").read_text(encoding="utf-8")
            (directory / "findings.json").write_text(json.dumps(payload), encoding="utf-8")
            (directory / "transcript.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
            return 0

        with mock.patch.object(ar, "ROOT", self.root), \
                mock.patch.object(ar, "git", lambda *args: ""), \
                mock.patch.object(ar, "head_bytes", lambda rel: None), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(ar, "launch", fake_launch), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = ar.main(["--path", "paper/t2.md", "--source", "workspace/shared/data/survey.csv",
                            *one_dir(out), *extra])
        return code, out, seen

    def test_the_reviewer_gets_the_source_and_its_digest_is_in_the_prompt(self):
        code, _, seen = self.fresh([])
        self.assertEqual(code, 0)
        self.assertEqual(seen["files"][".review/sources/workspace/shared/data/survey.csv"],
                         SURVEY.encode("utf-8"))
        self.assertIn(f"sha256 {sha(SURVEY)}", seen["prompt"])
        self.assertIn("`source_quote`", seen["prompt"])

    def test_the_row_names_the_source_and_the_report_labels_both_quotes(self):
        quoted = finding(file="paper/t2.md", line_start=1, line_end=1,
                         quote="The sample holds 1,204 firms.",
                         source_file="workspace/shared/data/survey.csv", source_quote="alpha,1240")
        made_up = finding(id="F002", file="paper/t2.md", line_start=1, line_end=1,
                          quote="The sample holds 1,204 firms.",
                          source_file="workspace/shared/data/survey.csv", source_quote="alpha,9")
        code, out, _ = self.fresh([quoted, made_up])
        self.assertEqual(code, 0)
        row = json.loads((out / "check-row.json").read_text(encoding="utf-8"))
        self.assertEqual(row["sources"], [{"path": "workspace/shared/data/survey.csv",
                                           "sha256": sha(SURVEY)}])
        self.assertIn("1 source(s) sent", row["limits"])
        self.assertIn("2 quoting a source, 1 of them verified in it", row["result"])
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn(f"sha256 {sha(SURVEY)}", report)
        self.assertIn("- source quote: verified", report)
        self.assertIn("- source quote: invented", report)
        self.assertIn("Quoted from the source:", report)
        self.assertIn(ar.SOURCE_LABELS_ESTABLISH, report)

    def test_a_reply_from_before_source_quotes_still_verifies(self):
        old = {key: value for key, value in finding(file="paper/t2.md", line_start=1, line_end=1,
                                                    quote="The sample holds 1,204 firms.").items()}
        self.assertNotIn("source_file", old)
        code, out, _ = self.fresh([old])
        self.assertEqual(code, 0)
        row = json.loads((out / "check-row.json").read_text(encoding="utf-8"))
        self.assertIn("1 findings (1 quote-verified, 0 seen in output)", row["result"])
        self.assertNotIn("quoting a source", row["result"])

    def test_a_source_kept_out_of_git_keeps_the_reviewers_text_out_of_the_tracked_report(self):
        reason = ar.withheld_reason("", ["paper/t2.md"], [], ["workspace/shared/data/survey.csv"])
        self.assertIn("the sources include files whose text stays out of Git: "
                      "workspace/shared/data/survey.csv (", reason)

    def test_the_shared_folder_stays_out_of_git_where_git_cannot_be_asked(self):
        # LESSON (rc22 release checks): in an archive unpacked without Git, a
        # source under a plain shared folder read as tracked, so the release's
        # own suite failed in the update preflight and every update stopped.
        root = scratch()
        (root / "workspace" / "shared" / "data").mkdir(parents=True)
        self.assertEqual(ar.private_why("workspace/shared/data/survey.csv", root),
                         "in the shared folder, which Git does not hold")


class ACommitRunWithSources(unittest.TestCase):
    """A commit run takes `--source` too, and a replay labels the source quotes again."""

    def test_the_prompt_carries_the_source_instruction_only_when_sources_are_sent(self):
        root, _ = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        found = ar.collect_sources(["workspace/shared/data/survey.csv"], root)
        with_sources = ar.render_prompt("abc123", "s", [], "minimal",
                                        ["the tree"] + ar.source_sent(found), "", "", found)
        without = ar.render_prompt("abc123", "s", [], "minimal", ["the tree"], "")
        self.assertIn(f".review/sources/workspace/shared/data/survey.csv: a source to check the "
                      f"candidate against, from the shared folder, sha256 {sha(SURVEY)}",
                      with_sources)
        self.assertIn("`source_file`", with_sources.split("## Run", 1)[1])
        self.assertNotIn("Sources to check the candidate against", without)

    def test_a_replay_names_the_sources_in_the_row(self):
        root, _ = linked_root({"notes/a.md": "a\n"}, {"data/survey.csv": SURVEY})
        directory = scratch()
        (directory / "t.jsonl").write_text(TRANSCRIPT, encoding="utf-8")
        (directory / "f.json").write_text(json.dumps(reply(findings=[finding(
            source_file="workspace/shared/data/survey.csv", source_quote="beta,77")])),
            encoding="utf-8")
        with mock.patch.object(ar, "ROOT", root), mock.patch.object(ar, "git", fake_git), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = ar.main([SHA, "--replay", "--transcript", str(directory / "t.jsonl"),
                            "--findings", str(directory / "f.json"), "--no-record",
                            "--source", "workspace/shared/data/survey.csv",
                            *one_dir(directory / "out")])
        self.assertEqual(code, 0)
        out = directory / "out"
        row = json.loads((out / "check-row.json").read_text(encoding="utf-8"))
        self.assertEqual(row["sources"][0]["sha256"], sha(SURVEY))
        self.assertIn("1 quoting a source, 1 of them verified in it", row["result"])
        call = json.loads((out / "record-call.json").read_text(encoding="utf-8"))
        self.assertIn("sources: 1, each named with its sha256", call[call.index("--scope") + 1])
        self.assertIn("read again for this replay",
                      (out / "report.md").read_text(encoding="utf-8"))

    def test_a_row_without_sources_carries_no_sources_field(self):
        row = ar.check_row(meta_for(findings_json=reply()), result_for(findings=[]),
                           pathlib.Path("r.md"), "d1ge57", None)
        self.assertNotIn("sources", row)


class TheProseRulesAreTheProjects(unittest.TestCase):
    """LESSON (rc22): the shipped contract imposed this repository's prose rules (the
    em dash, the AI-tell words) on every project the kit was installed in."""

    def test_the_shipped_contract_imposes_no_prose_rule(self):
        text = ar.CONTRACT.read_text(encoding="utf-8")
        for rule in ("em dash", "AI-tell", "delve", "tapestry"):
            self.assertNotIn(rule, text)
        self.assertIn("a project that sets none has none", text)

    def test_a_project_without_the_file_sets_none(self):
        root = path_root({"notes/a.md": "a\n"})
        with mock.patch.object(ar, "ROOT", root):
            prompt = ar.render_path_prompt(["notes/a.md"], "paths:aa", ["t"])
            named = ar.prose_named()
            row = ar.check_row(meta_for(findings_json=reply(), prose=named),
                               result_for(findings=[]), pathlib.Path("r.md"), "d", None)
        self.assertNotIn("## Prose rules", prompt)
        self.assertEqual(named, "")
        self.assertNotIn("prose rules", row["method"])
        self.assertIn("- prose rules: none set by this project",
                      ar.criteria_section(meta_for(findings_json=reply())))

    def test_a_project_that_sets_them_has_them_in_the_prompt_the_report_and_the_row(self):
        rules = "No passive voice in the abstract.\n"
        root = path_root({"notes/a.md": "a\n", "ai/review-prose-rules.md": rules})
        with mock.patch.object(ar, "ROOT", root):
            prompt = ar.render_prompt("abc123", "s", [], "minimal", ["t"], "")
            named = ar.prose_named()
        self.assertIn("## Prose rules this project sets", prompt)
        self.assertIn("No passive voice in the abstract.", prompt.split("## Run", 1)[0])
        self.assertEqual(named, f"ai/review-prose-rules.md (sha256 {sha(rules)})")
        row = ar.check_row(meta_for(findings_json=reply(), prose=named),
                           result_for(findings=[]), pathlib.Path("r.md"), "d", None)
        self.assertIn(f"prose rules: {named}", row["method"])
        self.assertIn(f"- prose rules: {named}",
                      ar.criteria_section(meta_for(findings_json=reply(), prose=named)))


class TheViewSaysWhichCommitsAreReviewed(unittest.TestCase):
    """LESSON (AH-341, 15 October 2026): the view named only the reviewed commit,
    under `commits`, and a reviewer of 97787c5d read it as the record's whole
    commit list and reported the record's two other commits as missing. The view
    now carries the reviewed commits and every commit naming the record, under
    names that say which is which, and the contract says so."""

    def test_the_reviewed_commits_and_every_commit_naming_the_record_are_kept_apart(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,  # noqa: E731
                                            capture_output=True, text=True).stdout.strip()
            run("init", "-q", "-b", "master")
            run("config", "user.name", "Test Person")
            run("config", "user.email", "t@example.test")
            run("config", "commit.gpgsign", "false")
            record = root / "ai" / "records" / "20261015-x"
            record.mkdir(parents=True)
            (record / "evidence.json").write_text(json.dumps({"turns": [{"id": "T1"}]}), encoding="utf-8")
            shas = []
            for n, line in enumerate(("Record: ai/records/20261015-x", "Record: ai/records/20261015-x",
                                      "Record: ai/records/20261015-other", "Record: 20261015-x",
                                      "Mentions 20261015-x without a Record line")):
                (root / f"f{n}.txt").write_text(f"{n}\n", encoding="utf-8")
                run("add", "-A")
                run("commit", "-q", "-m", f"Change {n}\n\n{line}")
                shas.append(run("rev-parse", "HEAD"))
            view = ar.minimal_view("ai/records/20261015-x", [shas[1]], root=root)
        self.assertEqual(view["commits_under_review"], [shas[1]])
        self.assertEqual(view["commits_naming_the_record"], [shas[0], shas[1], shas[3]])
        self.assertIn("the reviewed ones included", view["commits_note"])

    def test_the_reviewer_is_told_which_list_is_which(self):
        contract = (pathlib.Path(ar.__file__).parent / "agentic-review-contract.md").read_text(encoding="utf-8")
        for key in ("commits_under_review", "commits_naming_the_record"):
            self.assertIn(key, contract)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            record = root / "ai" / "records" / "20261015-x"
            record.mkdir(parents=True)
            (record / "evidence.json").write_text(json.dumps({"turns": []}), encoding="utf-8")
            review = root / "packet" / ".review"
            with mock.patch.object(ar, "ROOT", root), \
                    mock.patch.object(ar, "records_in", return_value={"ai/records/20261015-x": ["abc1234"]}):
                sent = ar.records_evidence(["abc1234"], review, "minimal", "")
            view = json.loads((review / "records" / "20261015-x.json").read_text(encoding="utf-8"))
        self.assertIn("(commits_under_review)", sent[0])
        self.assertIn("(commits_naming_the_record)", sent[0])
        self.assertEqual(view["commits_under_review"], ["abc1234"])


class TheReducedViewCarriesTheAgentsReason(unittest.TestCase):
    """Miros, 14 October 2026: send the agent's stated reasons to outside reviewers."""

    def test_a_stated_reason_is_sent_and_a_missing_one_reads_not_stated(self):
        with tempfile.TemporaryDirectory() as directory:
            record = pathlib.Path(directory) / "ai" / "records" / "20261014-x"
            record.mkdir(parents=True)
            (record / "evidence.json").write_text(json.dumps({"turns": []}), encoding="utf-8")
            (record / "decisions.jsonl").write_text(
                json.dumps({"question": "q1", "choice": "c1", "status": "selected",
                            "reason": "the smaller change keeps the old rows readable"}) + "\n"
                + json.dumps({"question": "q2", "choice": "c2", "status": "selected"}) + "\n",
                encoding="utf-8")
            view = ar.minimal_view("ai/records/20261014-x", ["abc1234"],
                                   root=pathlib.Path(directory))
        self.assertEqual([row["reason"] for row in view["decisions"]],
                         ["the smaller change keeps the old rows readable", "not stated"])
        self.assertIn("reason", view["limit"])


class ARecordFileNamedAsASource(unittest.TestCase):
    """A trace-record file sent as a source carries people's own words, as one sent as
    a candidate does, so it asks for the same declaration (AH-157)."""

    def test_it_needs_the_person_who_inspected_it_and_then_says_so(self):
        root = path_root({"notes/a.md": "a\n", RECORD_FILE: '{"turns": []}\n'})
        err = io.StringIO()
        with mock.patch.object(ar, "ROOT", root), \
                mock.patch.object(ar.shutil, "which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ar.main(["--path", "notes/a.md", "--source", RECORD_FILE, "--dry-run",
                            *one_dir(root / "out")])
        self.assertEqual(code, 2)
        self.assertIn("--inspected-by", err.getvalue())
        found = ar.collect_sources([RECORD_FILE], root)
        sent = ar.source_sent(found, inspected_by="Jane Roe")
        self.assertIn("so the words in it went to the model. Inspected first by Jane Roe", sent[0])


if __name__ == "__main__":
    unittest.main()
