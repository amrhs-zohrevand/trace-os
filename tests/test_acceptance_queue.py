#!/usr/bin/env python3
"""The acceptance queue: what is waiting, what one record holds, and what a person decides.

Found by asking the tool the question it could not answer: `review-status`
met "what should I accept?" with 434 unaccepted commits, which is true and
useless to a person. The queue exists so that what is waiting can be read and
decided one trace record at a time.

Each test names the failure it prevents. The three things that must stay true:
a label triages and never accepts, an acceptance covers exactly the commits it
names, and a row written before any of this still reads.

These tests build real git repositories in temporary directories, because
`queue` and `brief` read git rather than a stand-in for it. The real acceptance
ledger is fingerprinted before anything runs and compared afterwards: a test
that recorded an acceptance would be forging the one act only a person may
perform.

Run:  python3 -X utf8 -I -S -m unittest discover -s tests -p test_acceptance_queue.py -t tests
stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import trace_record as hr  # noqa: E402

REAL_LEDGER = REPO / "ai" / "records" / "acceptances.jsonl"


def ledger_fingerprint():
    if not REAL_LEDGER.exists():
        return "absent"
    return hashlib.sha256(REAL_LEDGER.read_bytes()).hexdigest()


LEDGER_AT_IMPORT = ledger_fingerprint()


def tearDownModule():
    assert ledger_fingerprint() == LEDGER_AT_IMPORT, (
        f"{REAL_LEDGER} changed while the tests ran; a test wrote a real "
        "acceptance instead of using a temporary project"
    )


def run(*argv) -> tuple[int, str]:
    """One command, with its output captured, the way a person would run it."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hr.main(list(argv))
    return code, out.getvalue() + err.getvalue()


class Project:
    """A real repository, with a baseline commit and a project configuration."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self._git("init", "-b", "master")
        self._git("config", "user.name", "Temp Person")
        self._git("config", "user.email", "temp@example.test")
        self._git("config", "commit.gpgsign", "false")
        self.baseline = self.commit("baseline", {"README.md": "start\n"})
        self.write("ai/project.json", json.dumps({
            "schema_version": 1, "policy_epoch": self.baseline,
            "accept_epoch": self.baseline, "custom_bibliography": False,
            "shared_root": "workspace/shared"}) + "\n")

    def _git(self, *args, stdin: str | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                                text=True, input=stdin, encoding="utf-8")
        if result.returncode:
            raise AssertionError(f"git {args[0]} failed: {result.stderr}")
        return result.stdout.strip()

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def commit(self, message: str, files: dict[str, str], record: str | None = None,
               agent: str = "some-model-1") -> str:
        for name, text in files.items():
            self.write(name, text)
            self._git("add", "--", name)
        body = message
        if record:
            body += f"\n\nRecord: ai/records/{record}"
        if agent:
            # A commit a person typed carries no trailer (agent="").
            body += f"\n\nExecution-agent: {agent}"
        self._git("commit", "-F", "-", stdin=body)
        return self._git("rev-parse", "HEAD")

    def record(self, name: str, turns=(), decisions=(), checks=()) -> pathlib.Path:
        """A trace record on disk, with as much of it as a test needs."""
        folder = self.root / "ai" / "records" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "evidence.json").write_text(json.dumps({
            "record_schema_version": 2, "record_id": name,
            "turns": [{"id": f"T{n}", "speaker": "Temp Person",
                       "speaker_kind": "human", "text": text}
                      for n, text in enumerate(turns, 1)]}), encoding="utf-8")
        for filename, rows in (("decisions.jsonl", decisions), ("checks.jsonl", checks)):
            if rows:
                (folder / filename).write_text(
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return folder


@contextlib.contextmanager
def project():
    """A temporary project, entered as the working directory."""
    saved = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="acceptance-queue-") as tmp:
        root = pathlib.Path(tmp).resolve()
        os.chdir(root)
        try:
            yield Project(root)
        finally:
            os.chdir(saved)


# One choice a person selected. A record that logs no choice needs a person
# (`no choices recorded`), so a test about another trigger, or about `routine`,
# gives its record this row.
A_PERSONS_CHOICE = [{"id": "D1", "question": "which way", "choice": "this way",
                     "status": "selected", "owners": ["Temp Person"],
                     "selected_by": {"name": "Temp Person", "kind": "human"}}]


def queue_json() -> dict:
    code, text = run("queue", "--limit", "0", "--json")
    assert code == 0, text
    return json.loads(text)


def ledger_rows(root: pathlib.Path) -> list[dict]:
    path = root / "ai" / "records" / "acceptances.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def by_id(result: dict) -> dict:
    return {entry["id"]: entry for entry in result["records"]}


def shared_commit_prefix(repo: Project, count: int = 2000) -> str | None:
    """Four hex characters two commits of this repository begin with, or None.

    `count` commits with fixed content and dates go onto a branch of their own
    in one `git fast-import`, so the same pair shares a prefix on every run;
    with 2000 commits a shared four-character prefix is all but certain.
    """
    stream = "".join(f"commit refs/heads/prefixes\ncommitter T <t@example.test> 1700000000 +0000\n"
                     f"data {len(str(n)) + 1}\n{n}\n\n" for n in range(count))
    repo._git("fast-import", "--quiet", stdin=stream)
    seen: dict[str, str] = {}
    for sha in repo._git("rev-list", "refs/heads/prefixes").splitlines():
        if sha[:4] in seen:
            return sha[:4]
        seen[sha[:4]] = sha
    return None


class QueueSelection(unittest.TestCase):
    """LESSON: 434 unaccepted commits is a true answer nobody can act on.

    The queue groups them by the record that holds the human's words, and drops
    a record as soon as an accepting actor has decided on all of its commits.
    """

    def test_a_record_with_unaccepted_commits_is_listed_and_an_accepted_one_is_not(self):
        with project() as repo:
            repo.record("20260101-first", turns=["do the first thing"])
            repo.record("20260102-second", turns=["do the second thing"])
            repo.commit("first work", {"docs/one.md": "one\n"}, record="20260101-first")
            repo.commit("more first work", {"docs/two.md": "two\n"}, record="20260101-first")
            repo.commit("second work", {"docs/three.md": "three\n"}, record="20260102-second")

            listed = by_id(queue_json())
            self.assertEqual(sorted(listed), ["20260101-first", "20260102-second"])
            self.assertEqual(listed["20260101-first"]["count"], 2)
            self.assertEqual(listed["20260101-first"]["first_words"], "do the first thing")

            code, _ = run("accept", "--record", "20260101-first", "--person")
            self.assertEqual(code, 0)
            self.assertEqual(sorted(by_id(queue_json())), ["20260102-second"])

    def test_commits_naming_no_record_are_one_count_and_not_a_row(self):
        # LESSON: a commit with no record is not a record with no name. It is
        # counted, and reached with --range, so the queue stays a list of records.
        with project() as repo:
            repo.record("20260101-first", turns=["the direction"])
            repo.commit("recorded work", {"docs/one.md": "one\n"}, record="20260101-first")
            repo.commit("unrecorded work", {"docs/two.md": "two\n"})
            result = queue_json()
            self.assertEqual(result["records_total"], 1)
            self.assertEqual(result["commits_without_a_record"], 1)
            code, text = run("queue")
            self.assertIn("1 unaccepted commit(s) name no trace record", text)

    def test_commits_naming_no_record_are_named_with_who_executed_them(self):
        # LESSON: a bare count left it to each model whether to show the person
        # these commits; one listed them from git, another said nothing waited
        # (agent-parity matrix, 2026-10-15). The tool names them, so every agent
        # shows the same commits.
        with project() as repo:
            typed = repo.commit("typed by hand", {"docs/one.md": "one\n"}, agent="")
            ran = repo.commit("run by a model", {"docs/two.md": "two\n"}, agent="model-x-1")
            listed = {e["commit"]: e for e in queue_json()["commits_without_a_record_list"]}
            self.assertEqual(listed[typed]["executed_by"], None)
            self.assertEqual(listed[ran]["executed_by"], "model-x-1")
            for args in (("queue",), ("queue", "--list")):
                code, text = run(*args)
                self.assertIn(f"{ran[:7]}", text)
                self.assertIn("run by a model (executed by model-x-1)", text)
                self.assertIn("typed by hand (no execution trailer, read as typed by a person)", text)

    def test_past_the_shown_number_the_text_says_how_many_more_and_the_json_lists_all(self):
        # The text names the newest UNRECORDED_SHOWN, so a long history stays a
        # screen; the rest are counted, never dropped, and the JSON names them all.
        with project() as repo:
            made = [repo.commit(f"unrecorded {n}", {f"docs/{n}.md": f"{n}\n"})
                    for n in range(hr.UNRECORDED_SHOWN + 1)]
            listed = [e["commit"] for e in queue_json()["commits_without_a_record_list"]]
            self.assertEqual(sorted(listed), sorted(made))
            for args in (("queue",), ("queue", "--list")):
                code, text = run(*args)
                self.assertEqual(code, 0)
                self.assertIn(f"{hr.UNRECORDED_SHOWN + 1} unaccepted commit(s) name no trace record", text)
                self.assertIn("... and 1 more; queue --json lists all of them", text)
                named = [sha for sha in made if f"  {sha[:7]}  " in text]
                self.assertEqual(len(named), hr.UNRECORDED_SHOWN)
                # Newest first: the oldest is the one counted rather than named.
                self.assertNotIn(made[0], named)

    def test_a_commit_naming_two_records_reaches_both(self):
        # LESSON: the join is a line, not a field, and a session can carry two.
        with project() as repo:
            repo.record("20260101-first", turns=["one"])
            repo.record("20260101-other", turns=["two"])
            repo.commit("work for both\n\nRecord: ai/records/20260101-other",
                        {"docs/one.md": "one\n"}, record="20260101-first")
            self.assertEqual(sorted(by_id(queue_json())),
                             ["20260101-first", "20260101-other"])

    def test_a_record_the_commit_names_but_the_tree_lacks_is_still_listed(self):
        # LESSON: an absent record is exactly the case a reader must see, so it
        # is listed and marked rather than dropped for having no words to quote.
        with project() as repo:
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-absent")
            entry = by_id(queue_json())["20260101-absent"]
            self.assertFalse(entry["on_disk"])
            self.assertEqual(entry["first_words"], "")
            code, text = run("queue")
            self.assertIn("the record is not in this tree", text)


class QueueLabels(unittest.TestCase):
    """LESSON: a label triages and never accepts.

    Each trigger names a thing a person is the right reader of. `routine` says
    none of them fired; it never says the work is approved, and the queue's own
    output says so.
    """

    def label(self, result: dict, record: str) -> tuple[str, str]:
        entry = by_id(result)[record]
        return entry["label"], "; ".join(entry["reasons"])

    def test_an_instruction_change_needs_you(self):
        with project() as repo:
            repo.record("20260101-instruction", turns=["change the rule"])
            repo.commit("change the standing instruction", {"AGENTS.md": "a rule\n"},
                        record="20260101-instruction")
            label, why = self.label(queue_json(), "20260101-instruction")
            self.assertEqual(label, "needs you")
            self.assertIn("AGENTS.md", why)

    def test_a_skill_change_needs_you_because_a_skill_is_a_standing_instruction(self):
        with project() as repo:
            repo.record("20260101-skill", turns=["add a method"])
            repo.commit("add a skill", {".skills/thing/SKILL.md": "a method\n"},
                        record="20260101-skill")
            self.assertEqual(self.label(queue_json(), "20260101-skill")[0], "needs you")

    def test_a_manuscript_change_needs_you(self):
        with project() as repo:
            repo.record("20260101-paper", turns=["rewrite the section"])
            repo.commit("edit the manuscript", {"paper/sections/01.tex": "prose\n"},
                        record="20260101-paper")
            label, why = self.label(queue_json(), "20260101-paper")
            self.assertEqual(label, "needs you")
            self.assertIn("paper/sections/01.tex", why)

    def test_a_release_needs_you(self):
        with project() as repo:
            repo.record("20260101-release", turns=["ship it"])
            repo.commit("first manifest",
                        {"starter/export-manifest.json": '{\n "version": "0.1.0-rc1"\n}\n'},
                        record="20260101-release")
            repo.record("20260102-release", turns=["ship the next one"])
            repo.commit("bump the manifest",
                        {"starter/export-manifest.json": '{\n "version": "0.1.0-rc2"\n}\n'},
                        record="20260102-release")
            label, why = self.label(queue_json(), "20260102-release")
            self.assertEqual(label, "needs you")
            self.assertIn("a release", why)

    def test_a_choice_an_agent_selected_needs_you(self):
        with project() as repo:
            repo.record("20260101-choice", turns=["do the thing"], decisions=[
                {"question": "which way", "choice": "this way", "status": "selected",
                 "selected_by": {"name": "some-model-1", "kind": "agent"}}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-choice")
            label, why = self.label(queue_json(), "20260101-choice")
            self.assertEqual(label, "needs you")
            self.assertIn("agent selected", why)

    def test_a_choice_the_human_selected_is_not_a_trigger_on_its_own(self):
        with project() as repo:
            repo.record("20260101-human", turns=["do the thing"], decisions=[
                {"question": "which way", "choice": "this way", "status": "selected",
                 "selected_by": {"name": "Temp Person", "kind": "human"}}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-human")
            self.assertEqual(self.label(queue_json(), "20260101-human")[0], "routine")

    def test_admitted_outside_material_needs_you(self):
        with project() as repo:
            repo.record("20260101-intake", turns=["use this dataset"])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-intake")
            repo.write("sources/intake.jsonl", json.dumps({
                "id": "IN-001", "what": "a dataset", "admitted_by": "Temp Person",
                "at": "2026-01-01T00:00:00Z", "checked": "its digest",
                "authority": "T1 of ai/records/20260101-intake"}) + "\n")
            label, why = self.label(queue_json(), "20260101-intake")
            self.assertEqual(label, "needs you")
            self.assertIn("intake row(s)", why)

    def test_ordinary_work_reads_as_routine_and_routine_is_not_approval(self):
        with project() as repo:
            repo.record("20260101-routine", turns=["tidy the notes"], decisions=A_PERSONS_CHOICE)
            repo.commit("tidy", {"docs/notes.md": "notes\n"}, record="20260101-routine")
            label, why = self.label(queue_json(), "20260101-routine")
            self.assertEqual(label, "routine")
            self.assertIn("no instruction or manuscript change", why)
            self.assertIn("choices recorded and none agent-selected", why)
            code, text = run("queue")
            self.assertIn("`routine` is not approval", text)

    def test_a_record_that_logs_no_choice_needs_you_and_says_why(self):
        # LESSON (2026-10-14 review, A): "None recorded" triaged as routine, so
        # a record that said nothing about who chose what read like one whose
        # choices were all a person's. No logged choice is its own reason.
        with project() as repo:
            repo.record("20260101-silent", turns=["tidy the notes"])
            repo.commit("tidy", {"docs/notes.md": "notes\n"}, record="20260101-silent")
            entry = by_id(queue_json())["20260101-silent"]
            self.assertEqual(entry["label"], "needs you")
            self.assertEqual(entry["reasons"], ["no choices recorded"])
            self.assertEqual(entry["hangs_on"], "Needs you: it logs no choice, so what was chosen "
                                                "in it, and by whom, is not on record.")
            self.assertEqual(entry["agent_choices"], "None recorded.")
            code, text = run("queue", "--limit", "0", "--list")
            self.assertIn("no choices recorded", text)
            # A record the commits name but the tree lacks logs no choice either.
            repo.commit("more", {"docs/more.md": "more\n"}, record="20260101-absent")
            self.assertIn("no choices recorded", by_id(queue_json())["20260101-absent"]["reasons"])

    def test_the_watched_paths_are_the_reports_own_list(self):
        # LESSON: two lists of instruction paths would drift, and the quieter
        # one would decide who gets asked. There is one list.
        report = hr._trace_report()
        self.assertEqual(hr.queue_paths(),
                         tuple(report.INSTRUCTION_PATHS) + ("paper/",))


class BriefContents(unittest.TestCase):
    """LESSON: a decision aid that invents anything is worse than no aid.

    Every line of a brief comes from the record's own files or from git.
    """

    def test_a_brief_carries_the_words_the_choices_the_changes_and_the_checks(self):
        with project() as repo:
            long_turn = "please " + "read it all " * 40
            repo.record("20260101-brief", turns=[long_turn, "and then stop"],
                        decisions=[{"question": "which way", "choice": "this way",
                                    "status": "selected",
                                    "selected_by": {"name": "some-model-1", "kind": "agent"},
                                    "human_direction": "T1 asked for it"}],
                        checks=[{"kind": "code-test", "result": "PASS: 3 tests",
                                 "limits": "mechanical only"}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-brief")
            code, text = run("brief", "20260101-brief")
            self.assertEqual(code, 0)
            for heading in ("YOUR WORDS", "CHOICES", "CHANGES", "CHECKS", "COMMANDS"):
                self.assertIn(heading, text)
            self.assertIn("and then stop", text)
            self.assertIn("T1 asked for it", text)
            self.assertIn("PASS: 3 tests", text)
            self.assertIn("mechanical only", text)
            self.assertIn("accept --record 20260101-brief --person", text)
            # The long turn is trimmed, and the trim is visible.
            self.assertNotIn(long_turn, text)
            self.assertIn("please read it all", text)
            self.assertIn("...", text)

    def test_each_choice_shows_its_reason_apart_from_the_direction_it_answers(self):
        # LESSON (2026-10-14): the brief printed the human's direction as
        # "why:", so a person reading it took the instruction for the agent's
        # reason. The reason is the row's own field; the direction keeps its
        # own label; a row written before the field existed says "not stated".
        with project() as repo:
            repo.record("20260101-reasons", turns=["cut it at a sensible size"], decisions=[
                {"id": "D1", "question": "Which cut-point?", "choice": "150000",
                 "reason": "the smallest cut that keeps every firm in the 2019 panel",
                 "status": "selected", "selected_by": {"name": "some-model-1", "kind": "agent"},
                 "human_direction": "T1 asked for a sensible cut"},
                {"id": "D2", "question": "Which year?", "choice": "2019",
                 "status": "selected", "selected_by": {"name": "some-model-1", "kind": "agent"},
                 "human_contribution": "T1 asked for a sensible cut"},
                {"id": "D3", "question": "Which unit?", "choice": "firms",
                 "status": "selected", "selected_by": {"name": "some-model-1", "kind": "agent"},
                 "authority": "the brief delegates the sample"}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-reasons")
            code, text = run("brief", "20260101-reasons")
            self.assertEqual(code, 0)
            self.assertIn("reason: the smallest cut that keeps every firm in the 2019 panel", text)
            self.assertEqual(text.count("reason: not stated"), 2)
            # The same direction, under either spelling, is said once.
            self.assertEqual(text.count("direction: T1 asked for a sensible cut"), 1)
            self.assertIn("authority: the brief delegates the sample", text)
            self.assertNotIn("why:", text)

    def test_a_choice_shows_the_inputs_it_names_and_a_row_naming_none_adds_no_line(self):
        # LESSON (2026-10-14): track records keeps the inputs of each unit of
        # work, and a decision row had no place for them. A row may now name
        # them; the brief shows them under the choice, and the rows written
        # before the field existed keep the screen as it was.
        with project() as repo:
            repo.record("20260101-inputs", turns=["cut it at a sensible size"], decisions=[
                {"id": "D1", "question": "Which cut-point?", "choice": "150000",
                 "inputs": ["IN-001", "data/survey-2019.csv"],
                 "status": "selected", "selected_by": {"name": "some-model-1", "kind": "agent"}},
                {"id": "D2", "question": "Which year?", "choice": "2019",
                 "status": "selected", "selected_by": {"name": "some-model-1", "kind": "agent"}}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-inputs")
            code, text = run("brief", "20260101-inputs")
            self.assertEqual(code, 0)
            self.assertIn("inputs: IN-001, data/survey-2019.csv", text)
            self.assertEqual(text.count("inputs:"), 1)

    def test_the_lines_added_to_an_instruction_file_are_quoted_and_capped(self):
        # LESSON: accepting a changed standing instruction means reading the
        # words. Sixty added lines do not fit on one screen, so forty are shown
        # and the rest are counted rather than silently dropped.
        with project() as repo:
            repo.record("20260101-rule", turns=["change the rule"])
            repo.commit("change the rule",
                        {"AGENTS.md": "".join(f"rule line {n}\n" for n in range(60))},
                        record="20260101-rule")
            code, text = run("brief", "20260101-rule")
            self.assertEqual(code, 0)
            quoted = [ln for ln in text.splitlines() if ln.strip().startswith("| rule line")]
            self.assertEqual(len(quoted), 40)
            self.assertIn("and 20 more added line(s)", text)

    def test_a_brief_fits_on_a_screen_and_can_be_written_to_a_file(self):
        with project() as repo:
            repo.record("20260101-small", turns=["do it"],
                        checks=[{"kind": "build", "result": "PASS", "limits": "one run"}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-small")
            code, text = run("brief", "20260101-small")
            self.assertLess(len(text.splitlines()), 60)
            code, text = run("brief", "20260101-small", "--out", "out/brief.txt")
            self.assertEqual(code, 0)
            written = (repo.root / "out" / "brief.txt").read_text(encoding="utf-8")
            self.assertIn("YOUR WORDS", written)

    def test_a_record_with_no_files_says_so_instead_of_inventing_a_session(self):
        with project() as repo:
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-absent")
            code, text = run("brief", "20260101-absent")
            self.assertEqual(code, 0)
            self.assertIn("is not in this tree", text)
            self.assertIn("none recorded", text)


class DecisionTable(unittest.TestCase):
    """LESSON: a person deciding wants the decision in one sentence that stands
    on its own, then one sentence on what hangs on it, and the table first.

    The decision sentence says what the decision is rather than pointing at
    another decision, and the table is the default way of putting the question.
    Every cell comes from the record, git or the registries, and nothing in it
    recommends.
    """

    def rows(self, *extra) -> dict[str, list[str]]:
        code, text = run("queue", "--limit", "0", *extra)
        self.assertEqual(code, 0, text)
        out = {}
        for line in text.splitlines():
            if re.match(r"^\| \d+ \|", line):
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
                out[cells[1]] = cells
        return out

    def test_the_table_is_what_queue_prints_first(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine, please"])
            repo.commit("Add the background paragraph", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("queue")
            self.assertTrue(text.startswith("Here is the overview of the decisions waiting"), text[:80])
            self.assertIn("| # | record | the work to accept or return | what hangs on it | what the agent chose | where it comes from | owners |", text)
            self.assertIn("Nothing here recommends", text)
            self.assertIn("`routine` is not approval", text)
            code, listing = run("queue", "--list")
            self.assertIn("acceptance queue: 1 trace record(s)", listing)

    def test_the_decision_is_the_main_commits_own_account_and_stands_alone(self):
        # The subject of the commit that did the work, not a title that names
        # the session or another decision; the count of the rest beside it.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine, please"])
            repo.write("ai/records/20260101-mine/summary.md", "# 20260101-mine\n\nbody\n")
            repo.commit("Add the background paragraph and its two citations",
                        {"docs/a.md": "one\ntwo\nthree\n"}, record="20260101-mine")
            repo.commit("Tidy a heading", {"docs/b.md": "b\n"}, record="20260101-mine")
            n, rid, decision, hangs, agent, origin, owners = self.rows()["20260101-mine"]
            self.assertEqual(agent, "None recorded.")
            self.assertEqual(n, "1")
            self.assertEqual(decision, "Add the background paragraph and its two citations, with 1 more commit beside it.")
            # No choice is logged, which is a reason of its own, never routine.
            self.assertEqual(hangs, "Needs you: it logs no choice, so what was chosen in it, "
                                    "and by whom, is not on record.")
            self.assertEqual(origin, 'T1, Temp Person: "do mine, please"')
            # No decision row names an owner and the summary cites no turn.
            self.assertEqual(owners, "not stated")

    def test_record_and_evidence_files_do_not_decide_which_commit_is_main(self):
        # A record or evidence commit is large in bytes and small in substance.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("Add the thing | with a pipe", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.commit("Freeze the evidence", {"sources/coding/freeze/validation.json": json.dumps({"k": list(range(400))}),
                                                "ai/records/20260101-mine/checks.jsonl": "{}\n" * 50}, record="20260101-mine")
            decision = self.rows()["20260101-mine"][2]
            self.assertTrue(decision.startswith("Add the thing \\| with a pipe"), decision)

    def test_what_hangs_on_it_names_the_release_the_adoption_and_the_later_run(self):
        with project() as repo:
            repo.record("20260101-rules", turns=["change the rule"], decisions=A_PERSONS_CHOICE)
            repo.commit("first manifest", {"starter/export-manifest.json": '{\n "version": "0.1.0-rc1"\n}\n'})
            rules = repo.commit("change AGENTS.md and bump", {"AGENTS.md": "new rule\n",
                                "starter/export-manifest.json": '{\n "version": "0.1.0-rc2"\n}\n'},
                                record="20260101-rules")
            repo.record("20260102-later", turns=["check it under the new rule"], decisions=A_PERSONS_CHOICE)
            repo.commit("checked", {"docs/c.md": "c\n"}, record="20260102-later")
            repo.write("sources/transitions.jsonl", json.dumps({
                "id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "by": "t",
                "authority": "T1", "checks": "c", "decision": "adopt", "at": "t",
                "commits": [rules], "later_run": "ai/records/20260102-later"}) + "\n")
            rows = self.rows()
            hangs = rows["20260101-rules"][3]
            self.assertTrue(hangs.startswith("Needs you: changes AGENTS.md, which every later session works under"), hangs)
            self.assertIn("a release, so every installed project receives it on its next update", hangs)
            self.assertIn("adopted as TR-001, whose later run is 20260102-later", hangs)
            self.assertEqual(rows["20260102-later"][3], "Routine: it is the later run TR-001 relies on.")

    def transitions(self, repo, cases) -> dict[str, list[str]]:
        """One record and one commit per case, each covered by a transition row
        of that kind and decision, then the table."""
        lines = []
        for n, (name, kind, decision) in enumerate(cases, 1):
            repo.record(name, turns=["do it"], decisions=A_PERSONS_CHOICE)
            sha = repo.commit(f"work on {name}", {f"docs/{name}.md": "x\n"}, record=name)
            row = {"id": f"TR-{n:03d}", "kind": kind, "by": "Temp Person",
                   "authority": "T1", "checks": "c", "decision": decision,
                   "at": "t", "commits": [sha]}
            if kind == "promotion":
                row["object"] = "workspace/sandbox/draft.tex"
                row["destination"] = "paper/sections/01.tex"
            else:
                row["object"] = "AGENTS.md"
            lines.append(json.dumps(row))
        repo.write("sources/transitions.jsonl", "\n".join(lines) + "\n")
        return self.rows()

    def test_a_returned_or_refused_transition_is_not_shown_as_completed(self):
        # Both kinds permit return and refuse, so completed wording would
        # reverse the decision the registry row records.
        with project() as repo:
            rows = self.transitions(repo, [
                ("20260101-returned-rule", "default-adoption", "return"),
                ("20260101-refused-rule", "default-adoption", "refuse"),
                ("20260101-returned-draft", "promotion", "return"),
                ("20260101-refused-draft", "promotion", "refuse")])
            returned_rule = rows["20260101-returned-rule"][3]
            self.assertEqual(returned_rule, "Routine: returned before adoption, recorded as TR-001.")
            self.assertNotIn("adopted as", returned_rule)
            refused_rule = rows["20260101-refused-rule"][3]
            self.assertEqual(refused_rule, "Routine: refused adoption, recorded as TR-002.")
            self.assertNotIn("adopted as", refused_rule)
            returned_draft = rows["20260101-returned-draft"][3]
            self.assertEqual(returned_draft,
                             "Routine: returned before entering paper/sections/01.tex, recorded as TR-003.")
            self.assertNotIn("entered paper", returned_draft)
            refused_draft = rows["20260101-refused-draft"][3]
            self.assertEqual(refused_draft,
                             "Routine: refused entry to paper/sections/01.tex, recorded as TR-004.")
            self.assertNotIn("entered paper", refused_draft)
            # The brief reads the same sentence, so it reverses nothing either.
            code, text = run("brief", "20260101-refused-draft")
            self.assertEqual(code, 0, text)
            self.assertIn("hangs on:  Routine: refused entry to paper/sections/01.tex, "
                          "recorded as TR-004.", text)

    def test_a_refused_draft_that_is_in_paper_anyway_is_said(self):
        # LESSON (rc22): a transition row records a decision and stops
        # nothing, so the draft a person refused can be in paper/ at the very
        # commit the row covers. The sentence says so, beside the refusal.
        with project() as repo:
            repo.record("20260101-refused-yet-in", turns=["do it"])
            sha = repo.commit("promote it", {"paper/sections/01.tex": "x\n"}, record="20260101-refused-yet-in")
            repo.write("sources/transitions.jsonl", json.dumps({
                "id": "TR-001", "kind": "promotion", "object": "workspace/sandbox/draft.tex",
                "destination": "paper/sections/01.tex", "by": "Temp Person", "authority": "T1",
                "checks": "c", "decision": "refuse", "at": "t", "commits": [sha]}) + "\n")
            hangs = self.rows()["20260101-refused-yet-in"][3]
            self.assertIn("refused entry to paper/sections/01.tex, recorded as TR-001", hangs)
            self.assertIn(f"TR-001 recorded refuse, yet paper/sections/01.tex is in paper/ at {sha[:12]}", hangs)

    def test_a_refused_folder_named_without_its_slash_is_not_called_a_file(self):
        # LESSON (rc22 review, cf4950e F3): `git cat-file -e` holds for a
        # folder too, so a row naming paper/sections read as a file "in
        # paper/" at any commit where the folder existed. A folder is read by
        # what the commit changes in it.
        with project() as repo:
            repo.record("20260101-refused-folder", turns=["do it"])
            sha = repo.commit("promote it", {"paper/sections/01.tex": "x\n"},
                              record="20260101-refused-folder")
            other = repo.commit("elsewhere", {"docs/a.md": "a\n"})
            row = {"id": "TR-001", "kind": "promotion", "object": "workspace/sandbox/draft",
                   "destination": "paper/sections", "decision": "refuse", "commits": [sha]}
            self.assertEqual(hr.refused_yet_in_paper(row),
                             [f"TR-001 recorded refuse, yet {sha[:12]} changes 1 file(s) "
                              "in paper/sections/"])
            # LESSON (rc22 recheck, 0a731a6 F1): written with its trailing
            # slash, the same folder fell through to the whole-paper/ reading.
            self.assertEqual(hr.refused_yet_in_paper(dict(row, destination="paper/sections/")),
                             hr.refused_yet_in_paper(row))
            # The folder is there at the later commit, which changes nothing in it.
            self.assertEqual(hr.refused_yet_in_paper(dict(row, commits=[other])), [])
            # A file is still named as one.
            self.assertEqual(hr.refused_yet_in_paper(dict(row, destination="paper/sections/01.tex")),
                             [f"TR-001 recorded refuse, yet paper/sections/01.tex is in paper/ "
                              f"at {sha[:12]}"])

    def test_an_adopted_or_promoted_transition_still_reads_as_completed(self):
        with project() as repo:
            rows = self.transitions(repo, [
                ("20260101-adopted-rule", "default-adoption", "adopt"),
                ("20260101-promoted-draft", "promotion", "promote")])
            self.assertEqual(rows["20260101-adopted-rule"][3],
                             "Routine: adopted as TR-001, with no later run recorded yet.")
            self.assertEqual(rows["20260101-promoted-draft"][3],
                             "Routine: entered paper/sections/01.tex as TR-002.")

    def test_where_it_comes_from_is_the_turn_the_decisions_cite_first(self):
        # A transcript can carry an earlier phase; the record's own decisions
        # say which turn directed this work.
        with project() as repo:
            repo.record("20260101-mine", turns=["earlier phase, recorded elsewhere", "now do mine"],
                        decisions=[{"question": "how", "choice": "so", "status": "selected",
                                    "human_inputs": ["T2"],
                                    "selected_by": {"name": "some-model-1", "kind": "agent"}}])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            cells = self.rows()["20260101-mine"]
            self.assertEqual(cells[5], 'T2, Temp Person: "now do mine"')
            self.assertEqual(cells[3], "Needs you: 1 choice in it was the agent's, not a person's.")
            self.assertEqual(cells[4], "D1 how Chose so (no alternative named).")

    def test_where_it_comes_from_skips_a_turn_coverage_excludes(self):
        # AH-084: a postcheck record cites no turn in its decisions and
        # excludes the earlier phase in coverage.json, so falling back to the
        # first turn of the transcript quoted an unrelated question.
        with project() as repo:
            folder = repo.record("20260101-postcheck",
                                 turns=["the earlier question, recorded elsewhere",
                                        "now check what the last session left"])
            (folder / "coverage.json").write_text(json.dumps({
                "record_id": "20260101-postcheck",
                "turns": [{"turn": "T1", "status": "excluded",
                           "reason": "recorded in the earlier session's record"},
                          {"turn": "T2", "status": "summarized"}]}), encoding="utf-8")
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-postcheck")
            cells = self.rows()["20260101-postcheck"]
            self.assertEqual(cells[5],
                             'T2, Temp Person: "now check what the last session left"')
            code, text = run("brief", "20260101-postcheck")
            self.assertEqual(code, 0, text)
            self.assertIn('from:      T2, Temp Person: "now check what the last session left"', text)

    def test_a_record_excluding_every_turn_says_so_instead_of_quoting_one(self):
        # The other half of AH-084: with nothing left to prefer, the cell names
        # the first turn and says why it is not quoting it.
        with project() as repo:
            folder = repo.record("20260101-allexcluded", turns=["recorded elsewhere"])
            (folder / "coverage.json").write_text(json.dumps({
                "record_id": "20260101-allexcluded",
                "turns": [{"turn": "T1", "status": "excluded",
                           "reason": "recorded in the earlier session's record"}]}), encoding="utf-8")
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-allexcluded")
            self.assertEqual(self.rows()["20260101-allexcluded"][5],
                             "T1, Temp Person: coverage.json excludes every turn here, "
                             "so none is quoted.")

    def test_a_record_with_no_coverage_manifest_still_quotes_its_first_turn(self):
        # The fix reads a manifest where there is one; a record without one
        # keeps the reading it had.
        with project() as repo:
            repo.record("20260101-nomanifest", turns=["do mine, please"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-nomanifest")
            self.assertEqual(self.rows()["20260101-nomanifest"][5],
                             'T1, Temp Person: "do mine, please"')

    def test_what_the_agent_chose_is_the_records_own_question_choice_and_alternative(self):
        # LESSON: "we ned to know agent decided between what and what"; the
        # cell quotes the row and says when the row names no alternative.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"], decisions=[
                {"id": "D1", "question": "Which profile does it ship under?", "status": "selected",
                 "choice": "core, like the earlier examples. Alternatives not taken: academic only, or both with two manifests.",
                 "selected_by": {"name": "some-model-1", "kind": "agent"}},
                {"id": "D2", "question": "Does the fetcher ship?", "status": "selected",
                 "choice": "Not in this release.", "selected_by": {"name": "some-model-1", "kind": "agent"}},
                {"id": "D3", "question": "Which scope?", "status": "selected", "choice": "C, everything.",
                 "selected_by": {"name": "Temp Person", "kind": "human"}}])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            agent = self.rows()["20260101-mine"][4]
            self.assertEqual(agent,
                             "D1 Which profile does it ship under? Chose core, like the earlier examples; "
                             "not academic only, or both with two manifests. "
                             "D2 Does the fetcher ship? Chose Not in this release (no alternative named).")
            code, text = run("brief", "20260101-mine")
            self.assertIn("agent:     D1 Which profile does it ship under? Chose core", text)

    def test_no_agent_selection_is_not_a_persons_choice(self):
        # The decision vocabulary holds joint and unknown selectors, and a
        # proposed row nobody selected. None of the three is a person choosing,
        # and the cell says which of them the record holds.
        with project() as repo:
            repo.record("20260101-joint", turns=["do mine"], decisions=[
                {"id": "D1", "question": "how", "choice": "so", "status": "selected",
                 "selected_by": {"name": "Temp Person with some-model-1", "kind": "joint"}}])
            repo.commit("joint work", {"docs/a.md": "a\n"}, record="20260101-joint")
            repo.record("20260101-unknown", turns=["do mine"], decisions=[
                {"id": "D1", "question": "how", "choice": "so", "status": "selected",
                 "selected_by": {"name": "unknown", "kind": "unknown"}}])
            repo.commit("unattributed work", {"docs/b.md": "b\n"}, record="20260101-unknown")
            repo.record("20260101-proposed", turns=["do mine"], decisions=[
                {"id": "D1", "question": "how", "choice": "so", "status": "proposed",
                 "selected_by": {"name": "unknown", "kind": "unknown"}},
                {"id": "D2", "question": "how else", "choice": "so", "status": "selected",
                 "selected_by": {"name": "Temp Person", "kind": "human"}}])
            repo.commit("proposed work", {"docs/c.md": "c\n"}, record="20260101-proposed")
            repo.record("20260101-hers", turns=["do mine"], decisions=[
                {"id": "D1", "question": "how", "choice": "so", "status": "selected",
                 "selected_by": {"name": "Temp Person", "kind": "human"}}])
            repo.commit("her work", {"docs/d.md": "d\n"}, record="20260101-hers")
            rows = self.rows()
            self.assertEqual(rows["20260101-joint"][4],
                             "None agent-selected; 1 with a joint or unknown selector.")
            self.assertEqual(rows["20260101-unknown"][4],
                             "None agent-selected; 1 with a joint or unknown selector.")
            self.assertEqual(rows["20260101-proposed"][4],
                             "None agent-selected; 1 a person's; 1 proposed, not selected.")
            self.assertEqual(rows["20260101-hers"][4],
                             "None; every recorded choice was a person's.")

    def test_the_brief_opens_with_the_same_three_sentences(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"], decisions=A_PERSONS_CHOICE)
            repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("brief", "20260101-mine")
            self.assertEqual(code, 0)
            self.assertIn("THE DECISION", text)
            self.assertIn("decide:    Add the thing.", text)
            self.assertIn("hangs on:  Routine: nothing else recorded depends on it.", text)
            self.assertIn('from:      T1, Temp Person: "do mine"', text)
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["decision"], "Add the thing.")


class DecisionSaidToAnAgent(unittest.TestCase):
    """LESSON: the person decides in their own words; an agent may hold the keyboard.

    a researcher, 2026-09-22: "i thought for acceptance the process is that, a human
    asks what are the outstanding things that it needs to review or accept,
    then the agent providdes a super brief summary and then the human says yes
    or no." The row keeps the words, names the agent that typed it, and stays
    the person's decision. What the agent may never do is decide, infer a
    decision nobody stated, or record it under another name.
    """

    def test_a_person_says_it_and_the_agent_records_it_in_their_name(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            head = repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "Temp Person", "--recorded-by", "some-model-1",
                             "--words", "yes to mine, record it in my name")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["kind"], "human")
            self.assertEqual(row["kind_basis"], "declared")
            self.assertEqual(row["reviewer"], "Temp Person")
            self.assertEqual(row["recorded_by"], "some-model-1")
            self.assertEqual(row["words"], "yes to mine, record it in my name")
            self.assertEqual(row["decision"], "accept")
            self.assertEqual(row["covers"], [head])
            self.assertIn("Recorded as a person's decision: accept", text)
            self.assertIn("Recorded by some-model-1 from Temp Person's words", text)
            # The recording agent also executed the commit: said, not hidden.
            self.assertIn("also executed covered commit(s)", text)
            # The state is a person's acceptance; the keystroke is counted apart.
            code, status = run("review-status")
            self.assertIn("accepted by a person              1", status)
            self.assertIn("of which an agent recorded from the person's words   1", status)
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            self.assertIn("1 acceptance row(s) a person decided and an agent recorded from their words", text)
            self.assertEqual(by_id(queue_json()), {})

    def test_a_return_said_to_an_agent_is_recorded_as_a_return(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "Temp Person", "--recorded-by", "some-model-1",
                             "--decision", "return", "--words", "no, the second half is wrong",
                             "--objections", "the second half is wrong")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["decision"], "return")
            self.assertEqual(row["recorded_by"], "some-model-1")
            self.assertEqual(sorted(by_id(queue_json())), ["20260101-mine"])

    def test_the_words_are_what_make_it_the_persons(self):
        # Without the words, the row would be an agent's claim about a person.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "Temp Person", "--recorded-by", "some-model-1")
            self.assertEqual(code, 1)
            self.assertIn("pass --words", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_recorded_by_goes_with_a_persons_decision_only(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--agent",
                             "--by", "some-model-2", "--recorded-by", "some-model-1",
                             "--words", "fine")
            self.assertEqual(code, 1)
            self.assertIn("goes with --person", text)
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "some-model-1", "--recorded-by", "some-model-1",
                             "--words", "fine")
            self.assertEqual(code, 1)
            self.assertIn("two actors", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_the_recording_agent_is_one_actor_however_its_id_is_written(self):
        # The trailer says some-model-1; the CLI at the keyboard says it with a
        # bracketed suffix. Same actor on both checks, and the row keeps the id.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "some-model-1", "--recorded-by", "Some-Model-1[1m]",
                             "--words", "fine")
            self.assertEqual(code, 1)
            self.assertIn("two actors", text)
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "Temp Person", "--recorded-by", "some-model-1[1m]",
                             "--words", "yes to mine")
            self.assertEqual(code, 0, text)
            self.assertIn("also executed covered commit(s)", text)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["recorded_by"], "some-model-1[1m]")

    def test_a_row_claiming_recorded_by_without_words_is_reported(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            head = repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.write("ai/records/acceptances.jsonl", json.dumps({
                "kind": "human", "kind_basis": "declared", "reviewer": "Temp Person",
                "at": "2026-01-01T00:00:00Z", "covers": [head], "count": 1, "scope": "x",
                "producer_agents": ["some-model-1"], "self_review": False,
                "decision": "accept", "recorded_by": "some-model-1"}) + "\n")
            code, text = run("registries")
            self.assertEqual(code, 1)
            self.assertIn("recorded_by without the person's words", text)


class WordsInACapturedTurn(unittest.TestCase):
    """LESSON: the words a row keeps can be looked up where they were said.

    a researcher, 2026-09-29, on whether a decision an agent recorded from a person's
    words should say if those words appear in a captured human turn: "OKay not
    sure about this but let's implelemt it." `registries` says it in a note.
    Only human turns count, and not found is a note like any other: it never
    fails the registries and never moves a commit out of accepted by a person.
    """

    def row(self, words: str) -> dict:
        return {"kind": "human", "kind_basis": "declared", "reviewer": "Temp Person",
                "recorded_by": "some-model-1", "words": words, "decision": "accept"}

    def evidence(self, repo: Project, name: str, turns) -> None:
        repo.write(f"ai/records/{name}/evidence.json",
                   json.dumps({"record_id": name, "turns": turns}))

    def test_words_said_verbatim_in_a_later_session_are_found_there(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.record("20260102-later", turns=["looking at it", "yes to mine, record it in my name"])
            # A person's own decision keeps no recorded_by, so it gets no note.
            own = {"kind": "human", "reviewer": "Temp Person", "words": "yes to mine"}
            note, = hr.words_in_human_turns([own, self.row("yes to mine, record it in my name")])
            self.assertEqual(note, "acceptances row 2: the person's words occur in a captured "
                                   "human turn, ai/records/20260102-later T2")

    def test_whitespace_case_and_curly_quotes_are_folded(self):
        with project() as repo:
            repo.record("20260102-later", turns=["Yes  to\n\u201cmine\u201d, it\u2019s   FINE."])
            note, = hr.words_in_human_turns([self.row("yes to \"mine\", it's fine")])
            self.assertTrue(note.endswith("occur in a captured human turn, ai/records/20260102-later T1"), note)

    def test_words_captured_nowhere_are_not_found_and_that_is_no_fault(self):
        with project() as repo:
            repo.record("20260102-later", turns=["something else", "[REDACTED: private]"])
            note, = hr.words_in_human_turns([self.row("yes to mine")])
            self.assertEqual(note, "acceptances row 1: the person's words occur in no captured "
                                   "human turn; the session they were said in may not be captured "
                                   "yet, or the words redacted")

    def test_words_only_an_agent_or_the_harness_wrote_are_not_found(self):
        with project() as repo:
            self.evidence(repo, "20260102-later", [
                {"id": "T1", "speaker": "a peer session", "speaker_kind": "agent", "text": "yes to mine"},
                {"id": "T2", "speaker": "harness", "speaker_kind": "system", "text": "yes to mine"},
                {"id": "T3", "speaker": "Temp Person", "speaker_kind": "human", "text": "carry on"}])
            note, = hr.words_in_human_turns([self.row("yes to mine")])
            self.assertIn("occur in no captured human turn", note)

    def test_no_records_directory_reads_as_not_found(self):
        with project() as repo:
            self.assertFalse((repo.root / "ai" / "records").exists())
            note, = hr.words_in_human_turns([self.row("yes to mine")])
            self.assertIn("occur in no captured human turn", note)

    def test_a_malformed_evidence_file_is_skipped_and_the_rest_are_read(self):
        with project() as repo:
            repo.write("ai/records/20260101-broken/evidence.json", "{not json")
            repo.write("ai/records/20260101-list/evidence.json", "[\"yes to mine\"]")
            (repo.root / "ai/records/20260101-bytes").mkdir()
            (repo.root / "ai/records/20260101-bytes/evidence.json").write_bytes(b"\xff\xfe{")
            repo.write("ai/records/20260101-turns/evidence.json", json.dumps({"turns": "yes to mine"}))
            self.evidence(repo, "20260101-odd", ["yes to mine", {"id": "T1", "text": None}])
            repo.record("20260102-later", turns=["yes to mine"])
            note, = hr.words_in_human_turns([self.row("yes to mine")])
            self.assertTrue(note.endswith("occur in a captured human turn, ai/records/20260102-later T1"), note)

    def test_registries_says_it_in_a_note_and_the_decision_stands_either_way(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.record("20260102-accepting", turns=["Yes to mine, record it in my name."])
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "Temp Person", "--recorded-by", "some-model-1",
                             "--words", "yes to mine, record it in my name")
            self.assertEqual(code, 0, text)
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            self.assertIn("note  acceptances row 1: the person's words occur in a captured "
                          "human turn, ai/records/20260102-accepting T1", text)
            (repo.root / "ai/records/20260102-accepting/evidence.json").unlink()
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            self.assertIn("note  acceptances row 1: the person's words occur in no captured", text)
            self.assertIn("registries valid", text)
            code, status = run("review-status")
            self.assertIn("accepted by a person              1", status)
            self.assertIn("of which an agent recorded from the person's words   1", status)


class RecordBasedAcceptance(unittest.TestCase):
    """LESSON: an acceptance covers the commits it lists, and nothing else.

    A record's commits are not always contiguous, so the row keeps the explicit
    list it has always kept. `review-status` counts a record-based row exactly
    as it counts a range-based one, because both are read through `covers`.
    """

    def test_accept_by_record_covers_that_records_commits_and_no_others(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.record("20260101-other", turns=["do theirs"])
            first = repo.commit("mine one", {"docs/a.md": "a\n"}, record="20260101-mine")
            middle = repo.commit("theirs", {"docs/b.md": "b\n"}, record="20260101-other")
            last = repo.commit("mine two", {"docs/c.md": "c\n"}, record="20260101-mine")

            code, text = run("accept", "--record", "20260101-mine", "--person")
            self.assertEqual(code, 0)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["kind"], "human")
            self.assertEqual(row["kind_basis"], "declared")
            self.assertEqual(sorted(row["covers"]), sorted([first, last]))
            self.assertNotIn(middle, row["covers"])
            self.assertEqual(row["record"], "ai/records/20260101-mine")
            self.assertEqual(row["scope"], "brief of 20260101-mine")

            code, status = run("review-status")
            self.assertEqual(code, 0)
            self.assertIn("accepted by a person              2", status)
            self.assertIn("not reviewed                      1", status)
            self.assertEqual(run("registries")[0], 0)

    def test_the_covered_commits_need_not_be_contiguous(self):
        # LESSON: this is why the row keeps a list of commits and never a range
        # string. Two sessions interleaved in one afternoon is the normal case.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.record("20260101-other", turns=["do theirs"])
            first = repo.commit("mine one", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.commit("theirs", {"docs/b.md": "b\n"}, record="20260101-other")
            last = repo.commit("mine two", {"docs/c.md": "c\n"}, record="20260101-mine")
            run("accept", "--record", "20260101-mine", "--person")
            row, = ledger_rows(repo.root)
            self.assertEqual(len(row["covers"]), 2)
            reachable = subprocess.run(["git", "rev-list", f"{first}..{last}"],
                                       cwd=repo.root, capture_output=True, text=True).stdout.split()
            self.assertEqual(len(reachable), 2, "the two commits are not one range")
            self.assertEqual(sorted(by_id(queue_json())), ["20260101-other"])

    def test_a_person_does_not_type_their_own_name(self):
        # LESSON: the burden was the point of the complaint. Git knows the name;
        # the actor kind is still declared by --person and never read off it.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person")
            self.assertEqual(code, 0)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["reviewer"], "Temp Person")
            self.assertEqual(row["kind_basis"], "declared")

    def test_a_name_is_still_required_without_person(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine")
            self.assertEqual(code, 1)
            self.assertIn("name a reviewer with --by", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_a_model_review_of_a_record_is_recorded_as_model_review(self):
        # LESSON: the new unit changes what is covered, not who may accept.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--by",
                             "some-model-2", "--agent")
            self.assertEqual(code, 0)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["kind"], "agent")
            self.assertNotIn("decision", row)
            self.assertIn("Recorded as model review", text)
            # Still in the queue: a model review is not an acceptance.
            self.assertEqual(sorted(by_id(queue_json())), ["20260101-mine"])

    def test_a_returned_record_stays_in_the_queue(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--decision", "return", "--objections", "fix the second half")
            self.assertEqual(code, 0)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["decision"], "return")
            self.assertEqual(sorted(by_id(queue_json())), ["20260101-mine"])

    def test_a_returned_record_needs_you_and_names_the_return_and_its_ledger_row(self):
        # LESSON: a returned record read `routine` and "nothing else recorded
        # depends on it", while the acceptance registry held the return and the
        # ledger held the row it opened.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            run("accept", "--record", "20260101-mine", "--person",
                "--decision", "return", "--objections", "fix the second half")
            row, = ledger_rows(repo.root)
            said = f"returned by Temp Person, ledger row {row['defect']} opened from the objection"
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["label"], "needs you")
            self.assertIn(said, entry["reasons"])
            self.assertIn(said, entry["hangs_on"])
            self.assertNotIn("nothing else recorded depends on it", entry["hangs_on"])
            self.assertIn(said, run("queue", "--limit", "0", "--list")[1])
            self.assertIn(said, run("brief", "20260101-mine")[1])

    def test_a_later_acceptance_of_the_returned_work_ends_the_return(self):
        # The latest decision on a commit stands. A fix committed after the
        # return leaves the return standing on the returned commit; accepting
        # the record then takes it out of the queue.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            run("accept", "--record", "20260101-mine", "--person",
                "--decision", "return", "--objections", "fix the second half")
            # Later means later in time (AH-135): the rows carry one-second
            # times, and two decisions in one second resolve to the stricter,
            # so the return is dated a second before the acceptance below.
            path = repo.root / "ai" / "records" / "acceptances.jsonl"
            row, = ledger_rows(repo.root)
            path.write_text(json.dumps(dict(row, at="2026-01-01T00:00:00Z")) + "\n", encoding="utf-8")
            repo.commit("fix the second half", {"docs/a.md": "b\n"}, record="20260101-mine")
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["label"], "needs you")
            self.assertTrue(entry["reasons"][0].startswith("returned by Temp Person"))
            run("accept", "--record", "20260101-mine", "--person")
            self.assertEqual(queue_json()["records_total"], 0)
            self.assertNotIn("returned by", run("brief", "20260101-mine")[1])

    def test_a_refusal_reads_as_a_refusal(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"], decisions=A_PERSONS_CHOICE)
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            run("accept", "--record", "20260101-mine", "--person",
                "--decision", "refuse", "--objections", "out of scope")
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["label"], "needs you")
            self.assertEqual(entry["reasons"], ["refused by Temp Person"])
            self.assertEqual(entry["hangs_on"], "Needs you: refused by Temp Person.")

    def test_record_and_range_are_two_batches_and_the_tool_says_so(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            head = repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--range", f"{repo.baseline}..{head}")
            self.assertEqual(code, 1)
            self.assertIn("Choose one", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_a_record_no_commit_names_is_reported_not_recorded(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"})
            code, text = run("accept", "--record", "20260101-mine", "--person")
            self.assertEqual(code, 1)
            self.assertIn("no commit in the acceptance window names", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_a_record_named_through_a_full_stop_is_found(self):
        # LESSON: `Record: ai/records/20260921-base-case-rc11.` on two commits
        # left `brief` with "no commit names this record" and `accept --record`
        # with nothing to cover, because the capture kept the full stop.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.write("docs/a.md", "a\n")
            repo._git("add", "--", "docs/a.md")
            repo._git("commit", "-F", "-", stdin="work\n\nRecord: ai/records/20260101-mine.\n\nExecution-agent: some-model-1")
            head = repo._git("rev-parse", "HEAD")
            self.assertEqual(sorted(by_id(queue_json())), ["20260101-mine"])
            code, text = run("brief", "20260101-mine")
            self.assertEqual(code, 0)
            self.assertIn("1 commit(s), 0 accepted", text)
            code, text = run("accept", "--record", "20260101-mine.", "--person")
            self.assertEqual(code, 0)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["covers"], [head])
            self.assertEqual(row["record"], "ai/records/20260101-mine")

    def test_a_range_based_row_written_before_any_of_this_still_reads(self):
        # LESSON: the queue is new; the ledger is not. A row with no kind_basis
        # and no record field is read as written, counts as accepted, and takes
        # its commits out of the queue.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            first = repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            second = repo.commit("more", {"docs/b.md": "b\n"}, record="20260101-mine")
            repo.write("ai/records/acceptances.jsonl", json.dumps({
                "kind": "human", "reviewer": "Temp Person",
                "at": "2026-01-01T00:00:00Z", "covers": [first, second],
                "count": 2, "scope": "the old way"}) + "\n")
            self.assertEqual(run("registries")[0], 0)
            code, status = run("review-status")
            self.assertIn("accepted by a person              2", status)
            self.assertEqual(queue_json()["records_total"], 0)
            code, text = run("queue")
            self.assertIn("nothing awaiting a person's acceptance", text)


class WhatEIsWaitingOn(unittest.TestCase):
    """LESSON (2026-09-22): the queue was the one place that listed what waited,
    and it listed A only. A fixed ledger row nobody rechecked and an adoption
    with no later run were visible only in the tail of the report, which is
    not a list anyone works from.
    """

    def test_a_fixed_row_awaiting_a_recheck_names_fixer_and_raiser_and_needs_you(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.write("ai/defects.jsonl", json.dumps({
                "id": "AH-001", "label": "the share is a mean of percentages", "severity": "medium",
                "quote": "", "state": "fixed", "first_seen": "2026-01-01", "runs": ["manual:Temp Person"],
                "note": "", "opened_by": "Temp Person", "fixed_by": "Jane Roe",
                "affected": ["ai/records/20260101-mine"]}) + "\n")
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["label"], "needs you")
            self.assertIn("1 ledger row(s) fixed and awaiting a recheck", entry["reasons"])
            self.assertIn("AH-001 is fixed by Jane Roe and awaits a recheck by Temp Person", entry["hangs_on"])

    def test_a_verified_row_and_a_row_naming_another_record_do_not_count(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"], decisions=A_PERSONS_CHOICE)
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.write("ai/defects.jsonl", "".join(json.dumps(r) + "\n" for r in [
                {"id": "AH-001", "label": "x", "severity": "low", "quote": "", "state": "verified",
                 "first_seen": "2026-01-01", "runs": ["m"], "note": "", "affected": ["20260101-mine"],
                 "verifications": [{"outcome": "pass", "decision": "verified"}]},
                {"id": "AH-002", "label": "y", "severity": "low", "quote": "", "state": "fixed",
                 "first_seen": "2026-01-01", "runs": ["m"], "note": "", "affected": ["ai/records/20260101-other"]}]))
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["label"], "routine")
            self.assertNotIn("awaiting a recheck", entry["hangs_on"])

    def test_the_overview_ends_with_what_e_is_waiting_on(self):
        with project() as repo:
            repo.record("20260101-rules", turns=["change the rule"])
            rules = repo.commit("rule", {"AGENTS.md": "new rule\n"}, record="20260101-rules")
            repo.write("sources/transitions.jsonl", json.dumps({
                "id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "by": "t",
                "authority": "T1", "checks": "c", "decision": "adopt", "at": "t",
                "commits": [rules], "later_run": "nothing stated"}) + "\n")
            repo.write("ai/defects.jsonl", json.dumps({
                "id": "AH-007", "label": "x", "severity": "low", "quote": "", "state": "fixed",
                "first_seen": "2026-01-01", "runs": ["m"], "note": ""}) + "\n")
            result = queue_json()
            self.assertEqual(result["e_waiting"], {"fixed_awaiting_recheck": ["AH-007"],
                                                   "adoptions_awaiting_observation": ["TR-001"]})
            code, text = run("queue", "--limit", "0")
            self.assertEqual(code, 0, text)
            self.assertIn("E is waiting on 1 fixed ledger row(s) with no recheck (AH-007) and "
                          "1 adoption(s) with no later-run observation (TR-001).", text)

    def test_nothing_waiting_says_so(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("queue", "--limit", "0")
            self.assertIn("E is waiting on nothing", text)

    def test_a_recheck_with_no_stated_outcome_keeps_the_row_waiting(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.write("ai/defects.jsonl", json.dumps({
                "id": "AH-001", "label": "x", "severity": "low", "quote": "", "state": "fixed",
                "first_seen": "2026-01-01", "runs": ["m"], "note": "", "affected": ["20260101-mine"],
                "verifications": [{"outcome": "not stated", "decision": "recorded"}]}) + "\n")
            result = queue_json()
            self.assertEqual(result["e_waiting"]["fixed_awaiting_recheck"], ["AH-001"])
            self.assertEqual(by_id(result)["20260101-mine"]["label"], "needs you")

    def test_a_bare_link_with_no_observer_is_still_waiting_and_says_so(self):
        with project() as repo:
            repo.record("20260101-rules", turns=["change the rule"])
            rules = repo.commit("rule", {"AGENTS.md": "new rule\n"}, record="20260101-rules")
            repo.write("sources/transitions.jsonl", json.dumps({
                "id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "by": "t",
                "authority": "T1", "checks": "c", "decision": "adopt", "at": "t",
                "commits": [rules], "later_run": "ai/records/20260102-later"}) + "\n")
            result = queue_json()
            self.assertEqual(result["e_waiting"]["adoptions_awaiting_observation"], ["TR-001"])
            self.assertIn("adopted as TR-001, whose later run is 20260102-later, stated without an observation",
                          by_id(result)["20260101-rules"]["hangs_on"])

    def test_the_e_line_is_printed_when_nothing_awaits_acceptance_and_in_the_listing(self):
        with project() as repo:
            repo.write("ai/defects.jsonl", json.dumps({
                "id": "AH-009", "label": "x", "severity": "low", "quote": "", "state": "fixed",
                "first_seen": "2026-01-01", "runs": ["m"], "note": ""}) + "\n")
            code, text = run("queue", "--limit", "0")
            self.assertEqual(code, 0, text)
            self.assertIn("E is waiting on 1 fixed ledger row(s) with no recheck (AH-009)", text)
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("queue", "--limit", "0", "--list")
            self.assertEqual(code, 0, text)
            self.assertIn("E is waiting on 1 fixed ledger row(s) with no recheck (AH-009)", text)

    def test_a_row_closed_by_hand_with_no_recheck_stays_in_the_e_line(self):
        # LESSON (2026-10-14): `mark --state closed` by hand took a row out of
        # what E is waiting on, which listed fixed rows only. A sweep's closure
        # is still the observation `closed` names and is not listed.
        with project() as repo:
            repo.write("ai/defects.jsonl", "".join(json.dumps(r) + "\n" for r in [
                {"id": "AH-001", "label": "x", "severity": "low", "quote": "", "state": "closed",
                 "first_seen": "2026-01-01", "runs": ["m", "manual:Jane Roe:closed"], "note": "",
                 "repair_claims": [{"state": "closed", "by": "Jane Roe", "note": "", "recorded": "t"}]},
                {"id": "AH-002", "label": "y", "severity": "low", "quote": "", "state": "closed",
                 "first_seen": "2026-01-01", "runs": ["m", "deep-1:absent"], "note": ""},
                {"id": "AH-003", "label": "z", "severity": "low", "quote": "", "state": "closed",
                 "first_seen": "2026-01-01", "runs": ["m"], "note": ""}]))
            result = queue_json()
            self.assertEqual(result["e_waiting"]["closed_without_recheck"], ["AH-001", "AH-003"])
            code, text = run("queue", "--limit", "0")
            self.assertEqual(code, 0, text)
            self.assertIn("E is waiting on 2 ledger row(s) closed other than by a sweep, with no recheck "
                          "(AH-001, AH-003).", text)

    def test_a_row_rechecked_on_one_of_its_three_objects_still_waits_and_says_how_far(self):
        # LESSON (2026-10-14): one passing recheck cleared a fixed row from
        # what E is waiting on, although the defect reached three tables and
        # two were never rechecked.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo.write("ai/defects.jsonl", "".join(json.dumps(r) + "\n" for r in [
                {"id": "AH-007", "label": "x", "severity": "low", "quote": "", "state": "fixed",
                 "first_seen": "2026-01-01", "runs": ["m"], "note": ""},
                {"id": "AH-008", "label": "the share divides by firms", "severity": "high", "quote": "",
                 "state": "fixed", "first_seen": "2026-01-01", "runs": ["m"], "note": "",
                 "opened_by": "Ana Reviewer", "fixed_by": "Jane Roe", "affected": ["20260101-mine"],
                 "objects": ["tables/t1.md", "tables/t2.md", "tables/t3.md"],
                 "verifications": [{"outcome": "pass", "decision": "recorded", "state_before": "fixed",
                                    "state_after": "fixed", "covers": "tables/t1.md"}]}]))
            result = queue_json()
            self.assertEqual(result["e_waiting"]["fixed_awaiting_recheck"], ["AH-007"])
            self.assertEqual(result["e_waiting"]["fixed_objects_awaiting_recheck"],
                             [{"id": "AH-008", "rechecked": 1, "objects": 3}])
            entry = by_id(result)["20260101-mine"]
            self.assertEqual(entry["label"], "needs you")
            self.assertIn("AH-008 is fixed by Jane Roe and awaits a recheck by Ana Reviewer "
                          "(1 of 3 objects rechecked)", entry["hangs_on"])
            code, text = run("queue", "--limit", "0")
            self.assertEqual(code, 0, text)
            self.assertIn("E is waiting on 1 fixed ledger row(s) with no recheck (AH-007) and "
                          "1 fixed ledger row(s) with objects still to recheck "
                          "(AH-008: 1 of 3 objects rechecked).", text)

    def test_a_return_lists_the_objects_the_person_named(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person", "--decision", "return",
                             "--objections", "the share divides by firms in all three tables",
                             "--object", "tables/t1.md", "--object", "tables/t2.md", "--object", "tables/t3.md")
            self.assertEqual(code, 0, text)
            self.assertIn("It lists the 3 object(s) the person named", text)
            finding, = [json.loads(ln) for ln in (repo.root / "ai" / "defects.jsonl").read_text().splitlines()]
            self.assertEqual(finding["objects"], ["tables/t1.md", "tables/t2.md", "tables/t3.md"])
            self.assertEqual(finding["affected"], ["ai/records/20260101-mine"])
            # Without --object the row lists none, as before: nothing is inferred.
            repo.commit("more", {"docs/b.md": "b\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person", "--decision", "return",
                             "--objections", "again")
            self.assertEqual(code, 0, text)
            second = [json.loads(ln) for ln in (repo.root / "ai" / "defects.jsonl").read_text().splitlines()][-1]
            self.assertNotIn("objects", second)


class LaterRunObservation(unittest.TestCase):
    """LESSON (2026-09-22): the later record does not exist when the adoption row
    is written, so `--later-run` at adoption time was a promise, and there was
    no way to keep it. The link is added when the run exists, by whoever
    observed it, and a link without an observation is not written at all.
    """

    def adoption(self, repo, later_run="nothing stated", decision="adopt") -> str:
        repo.record("20260101-rules", turns=["change the rule"])
        rules = repo.commit("rule", {"AGENTS.md": "new rule\n"}, record="20260101-rules")
        digest = hashlib.sha256(b"new rule\n").hexdigest()
        repo.write("sources/transitions.jsonl", json.dumps({
            "id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "object_sha256": digest,
            "by": "t", "authority": "T1", "checks": "c", "decision": decision, "at": "t",
            "commits": [rules], "later_run": later_run}) + "\n")
        return rules

    def later_record(self, repo, name: str, digest: str, base: str) -> None:
        folder = repo.record(name, turns=["work under the rule"])
        data = json.loads((folder / "evidence.json").read_text())
        data["instructions"] = {"base_commit": base, "files": {"AGENTS.md": digest}}
        (folder / "evidence.json").write_text(json.dumps(data))
        repo.commit(f"work on {name}", {f"docs/{name}.md": "x\n"}, record=name)

    def transitions(self) -> list[dict]:
        return [json.loads(ln) for ln in pathlib.Path("sources/transitions.jsonl").read_text().splitlines() if ln.strip()]

    def test_candidates_are_found_by_the_instruction_digest_and_nothing_is_written(self):
        with project() as repo:
            rules = self.adoption(repo)
            digest = hashlib.sha256(b"new rule\n").hexdigest()
            self.later_record(repo, "20260102-same", digest, rules)
            self.later_record(repo, "20260103-newer", "f" * 64, rules)
            before = pathlib.Path("sources/transitions.jsonl").read_bytes()
            code, text = run("later-run", "TR-001")
            self.assertEqual(code, 0, text)
            self.assertIn("produced under this version of it: 20260102-same", text)
            self.assertIn("produced under a later version of it: 20260103-newer", text)
            self.assertIn("No observation, no link", text)
            self.assertEqual(pathlib.Path("sources/transitions.jsonl").read_bytes(), before)

    def test_the_observation_is_recorded_with_a_declared_observer_and_the_queue_says_so(self):
        with project() as repo:
            rules = self.adoption(repo)
            self.later_record(repo, "20260102-later", hashlib.sha256(b"new rule\n").hexdigest(), rules)
            code, text = run("later-run", "TR-001", "--record", "20260102-later", "--person",
                             "--observed", "the record's checks cite the rule and the commit body follows it")
            self.assertEqual(code, 0, text)
            row, = self.transitions()
            self.assertEqual(row["later_run"], "ai/records/20260102-later")
            self.assertEqual(row["later_run_by"], "Temp Person")
            self.assertEqual(row["later_run_kind"], "human")
            self.assertEqual(row["later_run_observed"], "the record's checks cite the rule and the commit body follows it")
            self.assertIn("observed by Temp Person (a person)", text)
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            hangs = by_id(queue_json())["20260101-rules"]["hangs_on"]
            self.assertIn("adopted as TR-001, whose later run is 20260102-later, observed by a person", hangs)
            self.assertNotIn("TR-001", queue_json()["e_waiting"]["adoptions_awaiting_observation"])

    def test_no_observation_no_link_and_no_declared_kind_no_link(self):
        with project() as repo:
            self.adoption(repo)
            before = pathlib.Path("sources/transitions.jsonl").read_bytes()
            code, text = run("later-run", "TR-001", "--record", "20260102-later", "--person")
            self.assertEqual(code, 1)
            self.assertIn("No observation, no link", text)
            code, text = run("later-run", "TR-001", "--record", "20260102-later", "--observed", "it held")
            self.assertEqual(code, 1)
            self.assertIn("declare the observer's kind", text)
            self.assertEqual(pathlib.Path("sources/transitions.jsonl").read_bytes(), before)

    def test_a_models_observation_reads_as_a_models_and_an_earlier_link_is_kept(self):
        with project() as repo:
            self.adoption(repo, later_run="ai/records/20260101-promised")
            code, text = run("later-run", "TR-001", "--record", "20260102-later", "--agent",
                             "--by", "some-model", "--observed", "the rule shaped the record")
            self.assertEqual(code, 0, text)
            row, = self.transitions()
            self.assertEqual(row["later_run_kind"], "agent")
            self.assertEqual(row["later_run_previous"][0]["later_run"], "ai/records/20260101-promised")
            self.assertIn("advisory", text)

    def test_an_id_two_rows_share_is_not_resolved_to_either(self):
        """Two clones numbered a row before syncing and the union kept both, so
        TR-001 names two rows; later-run neither reads nor rewrites the first."""
        with project() as repo:
            rules = self.adoption(repo)
            with open("sources/transitions.jsonl", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps({"id": "TR-001", "kind": "promotion", "object": "paper/x.tex", "by": "b",
                                     "authority": "T2", "checks": "c", "decision": "promote", "at": "t",
                                     "commits": [rules]}) + "\n")
            before = pathlib.Path("sources/transitions.jsonl").read_bytes()
            for extra in ([], ["--record", "20260102-later", "--person", "--observed", "it held"]):
                code, text = run("later-run", "TR-001", *extra)
                self.assertEqual(code, 1, text)
                self.assertIn("transitions id TR-001 is on rows 1, 2", text)
                self.assertIn('row 1: kind "default-adoption"; object "AGENTS.md"', text)
                self.assertIn('row 2: kind "promotion"; object "paper/x.tex"', text)
                self.assertEqual(pathlib.Path("sources/transitions.jsonl").read_bytes(), before)
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            self.assertIn("a lookup by TR-001 refuses to pick one", text)

    def test_a_returned_adoption_takes_no_later_run(self):
        with project() as repo:
            self.adoption(repo, decision="return")
            code, text = run("later-run", "TR-001", "--record", "x", "--person", "--observed", "y")
            self.assertEqual(code, 1)
            self.assertIn("not an adoption", text)

    def test_a_declared_observer_without_an_observation_is_a_malformed_row(self):
        with project() as repo:
            rules = self.adoption(repo)
            repo.write("sources/transitions.jsonl", json.dumps({
                "id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "by": "t",
                "authority": "T1", "checks": "c", "decision": "adopt", "at": "t", "commits": [rules],
                "later_run": "ai/records/x", "later_run_kind": "human", "later_run_by": "t"}) + "\n")
            code, text = run("registries")
            self.assertEqual(code, 1)
            self.assertIn("has no later_run_observed", text)


class RecordLineForms(unittest.TestCase):
    """LESSON (AH-136): a Record line names its record as a bare id, as
    ai/records/<id>, or as this repository's absolute path to it, and each
    reader took the text as written. Two commits here say `Record:
    20260922-trace-review-agentic`: `queue` counted them, `brief` found none,
    `accept --record` had nothing to cover, `verify-range` and the report read
    a record on disk as absent. Every reader now takes one canonical form.
    """

    def commit_naming(self, repo: Project, message: str, name: str, line: str) -> str:
        repo.write(name, message + "\n")
        repo._git("add", "--", name)
        repo._git("commit", "-F", "-", stdin=f"{message}\n\nRecord: {line}\n\nExecution-agent: some-model-1")
        return repo._git("rev-parse", "HEAD")

    def test_a_bare_id_an_absolute_path_and_the_usual_form_name_one_record(self):
        import trace_report
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            bare = self.commit_naming(repo, "bare", "docs/a.md", "20260101-mine")
            absolute = self.commit_naming(repo, "absolute", "docs/b.md",
                                          f"{repo.root}/ai/records/20260101-mine")
            usual = repo.commit("usual", {"docs/c.md": "c\n"}, record="20260101-mine")
            result = queue_json()
            self.assertEqual(result["records_total"], 1, "one record is one row")
            self.assertEqual(by_id(result)["20260101-mine"]["count"], 3)
            self.assertIn("3 commit(s), 0 accepted", run("brief", "20260101-mine")[1])
            links = hr.record_links(f"{repo.baseline}..HEAD")
            self.assertEqual([row["record_exists"] for row in links], [True, True, True])
            self.assertEqual({row["record"] for row in links}, {"ai/records/20260101-mine"})
            report = trace_report.build_report()
            by = {c["commit"]: c for c in report["commits"]}
            for sha in (bare, absolute, usual):
                self.assertEqual(by[sha]["states"]["T"], "recorded", sha)
                self.assertEqual(by[sha]["record"], "ai/records/20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(sorted(row["covers"]), sorted([bare, absolute, usual]))
            self.assertEqual(row["record"], "ai/records/20260101-mine")

    def test_a_staging_location_stays_distinct_from_the_record(self):
        with project() as repo:
            staging = "workspace/record-staging/20260101-mine"
            self.assertEqual(hr.record_path(staging).as_posix(), staging)
            self.assertEqual(hr.record_path(f"{repo.root}/{staging}/").as_posix(), staging)
            elsewhere = repo.root.parent / "elsewhere" / "ai" / "records" / "20260101-mine"
            self.assertEqual(hr.record_path(str(elsewhere)), elsewhere, "outside this tree, as written")

    def test_draft_message_names_the_record_as_this_repository_does(self):
        import trace_story as story
        with project() as repo:
            folder = repo.root / "ai" / "records" / "20260101-mine"
            folder.mkdir(parents=True)
            (folder / "evidence.json").write_text(json.dumps({
                "record_id": "20260101-mine", "consent": {"third_party_review": "cleared"},
                "execution_agents": [{"model": "some-model-1"}],
                "turns": [{"id": "T1", "speaker": "Temp Person", "text": "do mine"}]}), encoding="utf-8")
            (folder / "summary.md").write_text("# Direction\n\n[T1] Temp Person asked for it.\n", encoding="utf-8")
            (folder / "checks.jsonl").write_text(json.dumps({
                "kind": "self-test", "runner": "unittest", "method": "ran it", "result": "pass",
                "artifact": "README.md"}) + "\n", encoding="utf-8")
            out = repo.root / "workspace" / "message.txt"
            with contextlib.redirect_stdout(io.StringIO()):
                hr.cmd_cover(argparse.Namespace(record=str(folder)))
                code = story.cmd_message(hr, argparse.Namespace(
                    record=str(folder), subject="Do mine", out=str(out), execution_agent=None))
            self.assertEqual(code, 0)
            self.assertIn("\nRecord: ai/records/20260101-mine\n", out.read_text(encoding="utf-8"))


class BriefAgreesWithTheQueue(unittest.TestCase):
    """LESSON (AH-143): the queue labels a record from its commits nobody has
    accepted, and the brief labelled it from all of them, so an accepted change
    to AGENTS.md made the brief say `needs you` beside a queue row saying
    `routine`. The brief opens with the queue's cells and says, apart from
    them, what the whole record holds and what `accept --record` covers.
    """

    def test_the_brief_opens_on_what_is_waiting_and_keeps_the_whole_record_apart(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["change the rule"], decisions=A_PERSONS_CHOICE)
            rule = repo.commit("Change the rule", {"AGENTS.md": "rule\n"}, record="20260101-mine")
            code, text = run("accept", "--range", f"{rule}^!", "--person")
            self.assertEqual(code, 0, text)
            repo.commit("Tidy docs", {"docs/a.md": "a\n"}, record="20260101-mine")
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["label"], "routine")
            code, text = run("brief", "20260101-mine")
            self.assertEqual(code, 0)
            self.assertIn("2 commit(s), 1 accepted, 1 awaiting a decision    routine", text)
            self.assertIn(f"decide:    {entry['decision']}", text)
            self.assertIn(f"hangs on:  {entry['hangs_on']}", text)
            self.assertNotIn("needs you", text)
            self.assertIn("CHANGES  (2 file(s) over 2 commit(s))", text)
            self.assertIn("accept --record covers all 2 commit(s) naming this record, "
                          "the 1 already accepted included", text)

    def test_a_record_with_nothing_waiting_says_so(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-mine")
            run("accept", "--record", "20260101-mine", "--person")
            code, text = run("brief", "20260101-mine")
            self.assertIn("1 commit(s), 1 accepted, 0 awaiting a decision    nothing waiting", text)
            self.assertIn("decide:    Add the thing.", text)


class DecisionOrderAndBaseline(unittest.TestCase):
    """LESSON (AH-135, AH-131): which decision stands, and whether anything is
    waiting at all, must not depend on who merged last or on a baseline the
    clone does not hold."""

    def test_review_status_says_when_a_same_second_conflict_was_resolved(self):
        with project() as repo:
            sha = repo.commit("work", {"docs/a.md": "a\n"})
            at = "2026-09-30T07:14:34Z"
            rows = [{"kind": "human", "kind_basis": "declared", "reviewer": name, "decision": decision,
                     "at": at, "covers": [sha], "count": 1, "self_review": False}
                    for name, decision in (("Ann", "accept"), ("Bob", "return"))]
            for order in (rows, rows[::-1]):
                repo.write("ai/records/acceptances.jsonl", "".join(json.dumps(r) + "\n" for r in order))
                code, status = run("review-status")
                self.assertEqual(code, 0)
                self.assertIn(f"  {'returned for correction':<32}{1:3d}", status)
                self.assertIn("in the same second", status)
                self.assertIn("refuse over return over accept", status)

    def test_a_clone_without_the_baseline_reports_an_error_not_an_empty_queue(self):
        # LESSON (AH-131): `rev-list <baseline>..HEAD` failed quietly where the
        # built-in baseline is not in the history, so review-status said no
        # commits and the queue said nothing was waiting, beside unaccepted
        # commits touching paper/. An unreadable history is an execution error.
        with project() as repo:
            (repo.root / "ai" / "project.json").unlink()
            repo.commit("work", {"paper/a.tex": "a\n"})
            for command in (["review-status"], ["queue"], ["accept", "--person"]):
                code, text = run(*command)
                self.assertEqual(code, 2, (command, text))
                self.assertIn("ERROR", text)
                self.assertNotIn("nothing awaiting", text)
                self.assertNotIn("no commits since", text)
            self.assertEqual(ledger_rows(repo.root), [])


class DecisionAgainstTheVersionSeen(unittest.TestCase):
    """LESSON (2026-10-14): a decision is recorded "against the version it
    covers" (the technical appendix, Section 2), yet `accept --record` read the
    record's commits when the decision was recorded, so a commit that landed
    after the person read the brief was covered by a decision made before it
    existed. The brief now names its version, `--through` binds a decision to
    it, and a row written without a bound reads as it always did.
    """

    def test_the_brief_names_the_version_it_shows(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("First part", {"docs/a.md": "a\n"}, record="20260101-mine")
            head = repo.commit("Second part", {"docs/b.md": "b\n"}, record="20260101-mine")
            code, text = run("brief", "20260101-mine")
            self.assertEqual(code, 0, text)
            self.assertIn(f'version:  covers 2 commit(s) through {head[:12]}, "Second part", '
                          "the newest naming this record", text)
            self.assertIn(f"accept --record 20260101-mine --person --through {head[:12]}", text)
            self.assertIn(f'--words "what you said" --through {head[:12]}.', text)
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["through"], head)
            self.assertIn(f"2 commit(s) waiting, version through {head[:12]}",
                          run("queue", "--list")[1])

    def test_the_queue_names_the_version_the_brief_names_when_the_newest_is_accepted(self):
        # LESSON (rc22 review, 4949c1c F2): queue read the bound off the
        # commits still waiting and brief off every commit naming the record,
        # so when the newest was already accepted the two named different
        # commits for one record. The queue now reads it as the brief does.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            first = repo.commit("First part", {"docs/a.md": "a\n"}, record="20260101-mine")
            newest = repo.commit("Second part", {"docs/b.md": "b\n"}, record="20260101-mine")
            code, text = run("accept", "--range", f"{newest}^!", "--person")
            self.assertEqual(code, 0, text)
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["commits"], [first])
            self.assertEqual(entry["through"], newest)
            self.assertIn(f"covers 2 commit(s) through {newest[:12]}", run("brief", "20260101-mine")[1])
            self.assertIn(f"1 commit(s) waiting, version through {newest[:12]}",
                          run("queue", "--list")[1])

    def test_through_bounds_a_range_and_the_default_batch_as_it_bounds_a_record(self):
        # LESSON (rc22 review, 4949c1c F3): `--through` filters whatever batch
        # was selected, and only `--record` was tested.
        for selector in (("--range", "BASELINE..HEAD"), ()):
            with self.subTest(selector=selector or "default batch"), project() as repo:
                one = repo.commit("One", {"docs/a.md": "a\n"})
                two = repo.commit("Two", {"docs/b.md": "b\n"})
                three = repo.commit("Three", {"docs/c.md": "c\n"})
                argv = [part.replace("BASELINE", repo.baseline) for part in selector]
                code, text = run("accept", *argv, "--person", "--through", two[:12])
                self.assertEqual(code, 0, text)
                row, = ledger_rows(repo.root)
                self.assertEqual((row["covers"], row["through"]), ([one, two], two))
                self.assertIn(f"{three[:12]}  Three", text)
                code, status = run("review-status")
                self.assertIn("accepted by a person              2", status)
                self.assertIn("not reviewed                      1", status)

    def test_an_abbreviation_two_commits_share_is_said_to_be_ambiguous(self):
        # LESSON (rc22 review, 4949c1c F3): an ambiguous abbreviated sha was
        # reported as naming no commit, which sends the person looking for a
        # commit that exists.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-mine")
            prefix = shared_commit_prefix(repo)
            if prefix is None:
                self.skipTest("no two of the generated commits share a four-character prefix")
            code, text = run("accept", "--record", "20260101-mine", "--person", "--through", prefix)
            self.assertEqual(code, 1, text)
            self.assertIn(f"--through {prefix} is ambiguous: ", text)
            self.assertNotIn("names no commit", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_a_commit_that_lands_after_the_brief_is_not_covered_and_stays_waiting(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            seen = repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-mine")
            self.assertIn(f"through {seen[:12]}", run("brief", "20260101-mine")[1])
            later = repo.commit("Change it after she read it", {"docs/a.md": "b\n"},
                                record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--by", "Temp Person", "--recorded-by", "some-model-2",
                             "--words", "yes to mine", "--through", seen[:12])
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["covers"], [seen])
            self.assertEqual(row["count"], 1)
            self.assertEqual(row["through"], seen)
            self.assertEqual(row["words"], "yes to mine")
            self.assertIn(f"Bound by --through to {seen[:12]}", text)
            self.assertIn("1 later commit(s) are not covered by this decision", text)
            self.assertIn(f"{later[:12]}  Change it after she read it", text)
            entry = by_id(queue_json())["20260101-mine"]
            self.assertEqual(entry["commits"], [later])
            code, status = run("review-status")
            self.assertIn("accepted by a person              1", status)
            self.assertIn("not reviewed                      1", status)
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            self.assertIn("1 acceptance row(s) bound to a stated version with --through", text)

    def test_without_through_the_decision_covers_what_it_covered_before_and_says_how_far(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            first = repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-mine")
            later = repo.commit("Change it", {"docs/a.md": "b\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["covers"], [first, later])
            self.assertNotIn("through", row)
            self.assertIn(f"The newest commit covered is {later[:12]}.", text)
            self.assertIn("--through <the commit the brief showed>", text)
            self.assertEqual(queue_json()["records_total"], 0)

    def test_a_bound_that_names_no_commit_or_none_of_the_records_writes_nothing(self):
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-mine")
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--through", "no-such-commit")
            self.assertEqual(code, 1)
            self.assertIn("names no commit", text)
            code, text = run("accept", "--record", "20260101-mine", "--person",
                             "--through", repo.baseline)
            self.assertEqual(code, 1)
            self.assertIn("nothing recorded", text)
            self.assertEqual(ledger_rows(repo.root), [])

    def test_commits_on_two_merged_lines_are_bound_by_the_commit_checked_out(self):
        # Neither of two commits made on parallel branches is the other's
        # ancestor, so the newest alone would leave one out.
        with project() as repo:
            repo.record("20260101-mine", turns=["do mine"])
            repo._git("checkout", "-q", "-b", "side")
            side = repo.commit("Side part", {"docs/b.md": "b\n"}, record="20260101-mine")
            repo._git("checkout", "-q", "master")
            main = repo.commit("Main part", {"docs/a.md": "a\n"}, record="20260101-mine")
            repo._git("merge", "-q", "--no-ff", "-m", "Merge the side part", "side")
            head = repo._git("rev-parse", "HEAD")
            code, text = run("brief", "20260101-mine")
            self.assertIn(f"covers 2 commit(s) through {head[:12]}, the commit checked out", text)
            self.assertEqual(by_id(queue_json())["20260101-mine"]["through"], head)
            code, text = run("accept", "--record", "20260101-mine", "--person", "--through", head[:12])
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(sorted(row["covers"]), sorted([side, main]))
            self.assertNotIn("later commit(s)", text)

    def test_rows_without_a_bound_still_validate_and_a_malformed_bound_is_reported(self):
        with project() as repo:
            sha = repo.commit("work", {"docs/a.md": "a\n"})
            old = [{"kind": "human", "reviewer": "Temp Person", "at": "2026-01-01T00:00:00Z",
                    "covers": [sha], "count": 1, "scope": "the old way"},
                   {"kind": "human", "kind_basis": "declared", "reviewer": "Temp Person",
                    "at": "2026-01-02T00:00:00Z", "covers": [sha], "count": 1,
                    "decision": "accept", "self_review": False,
                    "recorded_by": "some-model-1", "words": "yes"}]
            repo.write("ai/records/acceptances.jsonl", "".join(json.dumps(r) + "\n" for r in old))
            code, text = run("registries")
            self.assertEqual(code, 0, text)
            self.assertNotIn("bound to a stated version", text)
            code, status = run("review-status")
            self.assertIn("accepted by a person              1", status)
            bad = dict(old[1], at="2026-01-03T00:00:00Z", through=sha[:12])
            with (repo.root / "ai/records/acceptances.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(bad) + "\n")
            code, text = run("registries")
            self.assertEqual(code, 1)
            self.assertIn("acceptances row 3: through must be a full commit sha", text)


class OwnersShown(unittest.TestCase):
    """LESSON (2026-10-14 review, A): "give each unit of work an owner" was
    recorded per choice row only, and `queue`, `brief` and `accept` never showed
    it. Each record now shows its owners: the decision rows' owners, else the
    speakers of the turns it cites, else "not stated"."""

    def test_the_decision_rows_owners_are_shown_in_every_view(self):
        with project() as repo:
            repo.record("20260101-owned", turns=["do mine"], decisions=[
                dict(A_PERSONS_CHOICE[0], owners=["Temp Person", "Jane Roe"])])
            repo.commit("Add the thing", {"docs/a.md": "a\n"}, record="20260101-owned")
            entry = by_id(queue_json())["20260101-owned"]
            self.assertEqual(entry["owners"], ["Temp Person", "Jane Roe"])
            self.assertEqual(entry["owner_basis"], "decision rows")
            cells = DecisionTable.rows(self)["20260101-owned"]
            self.assertEqual(cells[6], "Temp Person, Jane Roe")
            self.assertIn("owners: Temp Person, Jane Roe", run("queue", "--list")[1])
            self.assertIn("owners:    Temp Person, Jane Roe", run("brief", "20260101-owned")[1])

    def test_without_owners_the_speakers_of_the_cited_turns_are_named_with_that_basis(self):
        with project() as repo:
            folder = repo.record("20260101-cited", decisions=[
                dict(A_PERSONS_CHOICE[0], owners=[], human_inputs=["T1"])])
            (folder / "evidence.json").write_text(json.dumps({
                "record_schema_version": 2, "record_id": "20260101-cited", "turns": [
                    {"id": "T1", "speaker": "Temp Person", "speaker_kind": "human", "text": "start"},
                    {"id": "T2", "speaker": "unknown", "speaker_kind": "human", "text": "a wrapper"},
                    {"id": "T3", "speaker": "Jane Roe", "speaker_kind": "human", "text": "and this"},
                    {"id": "T4", "speaker": "Ann Other", "speaker_kind": "human", "text": "aside"},
                    {"id": "T5", "speaker": "Rob Ex", "speaker_kind": "human", "text": "excluded"}]}),
                encoding="utf-8")
            (folder / "summary.md").write_text("# x\n\nJane asked (T3); a wrapper (T2); (T5).\n",
                                               encoding="utf-8")
            (folder / "coverage.json").write_text(json.dumps({"turns": [
                {"turn": "T5", "status": "excluded", "reason": "recorded elsewhere"}]}), encoding="utf-8")
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-cited")
            entry = by_id(queue_json())["20260101-cited"]
            self.assertEqual(entry["owners"], ["Temp Person", "Jane Roe"])
            self.assertEqual(entry["owner_basis"], "speakers of the cited turns")
            self.assertEqual(DecisionTable.rows(self)["20260101-cited"][6],
                             "Temp Person, Jane Roe (speakers of the cited turns)")

    def test_nobody_named_reads_not_stated(self):
        with project() as repo:
            repo.commit("work", {"docs/a.md": "a\n"}, record="20260101-absent")
            entry = by_id(queue_json())["20260101-absent"]
            self.assertEqual((entry["owners"], entry["owner_basis"]), ([], "not stated"))
            self.assertIn("owners:    not stated", run("brief", "20260101-absent")[1])


class OwnWorkAndSelection(unittest.TestCase):
    """LESSON (2026-10-14 review, A): the producer is not the accepter.

    A person accepting a commit she typed was recorded as accepted by a person
    with nothing said, and `accept` compared execution trailers only, so an
    agent that selected a recorded choice could accept the work it chose.
    agentic_review.py already read the selectors for its reviewer.
    """

    def test_a_person_accepting_a_commit_she_typed_is_her_decision_and_says_so(self):
        with project() as repo:
            repo.record("20260101-hand", turns=["I wrote this part"], decisions=A_PERSONS_CHOICE)
            hand = repo.commit("Edit by hand", {"docs/a.md": "a\n"}, record="20260101-hand", agent="")
            repo.commit("Edit by the agent", {"docs/b.md": "b\n"}, record="20260101-hand")
            # LESSON (rc22 release checks): the rehearsal sets GIT_AUTHOR_NAME,
            # so the commit's author and git config user.name differ there; the
            # person named is the commit's own author.
            author = subprocess.run(["git", "-C", str(repo.root), "log", "-1", "--format=%an", hand],
                                    capture_output=True, text=True, check=True).stdout.strip()
            code, text = run("accept", "--record", "20260101-hand", "--person", "--by", author)
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual((row["kind"], row["decision"]), ("human", "accept"),
                             "never refused, never downgraded")
            self.assertFalse(row["self_review"])
            self.assertEqual(row["own_work"], [hand])
            self.assertIn("decided about their own work", text)
            self.assertEqual(hr.validate_acceptances([row])[0], [])
            code, status = run("review-status")
            self.assertIn("accepted by a person", status)
            self.assertIn("of which the person accepted their own work   1", status)

    def test_another_person_accepting_it_is_not_own_work(self):
        with project() as repo:
            repo.record("20260101-hand", turns=["I wrote this part"], decisions=A_PERSONS_CHOICE)
            repo.commit("Edit by hand", {"docs/a.md": "a\n"}, record="20260101-hand", agent="")
            code, text = run("accept", "--record", "20260101-hand", "--person", "--by", "Jane Roe")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertNotIn("own_work", row)
            self.assertNotIn("own work", run("review-status")[1])

    def picked(self, repo) -> str:
        repo.record("20260101-picked", turns=["choose a sample"], decisions=[
            {"id": "D1", "question": "which sample", "choice": "the 2019 panel", "status": "selected",
             "selected_by": {"name": "some-model-2", "kind": "agent"}},
            dict(A_PERSONS_CHOICE[0], id="D2")])
        return repo.commit("Draw the sample", {"docs/a.md": "a\n"}, record="20260101-picked")

    def test_an_actor_that_selected_a_recorded_choice_cannot_accept_the_work(self):
        with project() as repo:
            self.picked(repo)
            code, text = run("accept", "--record", "20260101-picked", "--by", "some-model-2",
                             "--authorized-by", "Temp Person", "--authority", "T1: decide on it")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual(row["kind"], "agent")
            self.assertEqual(row["declared_kind"], "delegated")
            self.assertTrue(row["self_review"])
            self.assertEqual(row["decision"], hr.SELF_REVIEW_DECISION)
            self.assertEqual(row["producer_agents"], ["some-model-1"])
            self.assertIn("some-model-2", row["selecting_actors"])
            self.assertIn("NOT independent: some-model-2 selected a choice recorded", text)
            self.assertNotIn("executed commit(s)", text)

    def test_selection_is_read_over_a_range_and_ignores_case(self):
        with project() as repo:
            sha = self.picked(repo)
            code, text = run("accept", "--range", f"{sha}^!", "--by", "SOME-MODEL-2", "--person")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual((row["kind"], row["declared_kind"]), ("agent", "human"))
            self.assertTrue(row["self_review"])

    def test_a_model_review_by_the_selector_is_flagged_as_self_review(self):
        with project() as repo:
            self.picked(repo)
            code, text = run("accept", "--record", "20260101-picked", "--by", "some-model-2", "--agent")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertTrue(row["self_review"])
            self.assertIn("Flagged as self-review", text)

    def test_a_persons_own_selection_is_direction_not_production(self):
        # The person who selected D2, recorded as a person's choice, decides
        # about the work in her own name; another model stays independent.
        with project() as repo:
            self.picked(repo)
            code, text = run("accept", "--record", "20260101-picked", "--person")
            self.assertEqual(code, 0, text)
            code, text = run("accept", "--record", "20260101-picked", "--by", "some-model-3",
                             "--authorized-by", "Temp Person", "--authority", "T1: decide on it")
            self.assertEqual(code, 0, text)
            person, delegated = ledger_rows(repo.root)
            self.assertEqual((person["kind"], person["decision"]), ("human", "accept"))
            self.assertFalse(person["self_review"])
            self.assertEqual((delegated["kind"], delegated["decision"]), ("delegated", "accept"))
            self.assertFalse(delegated["self_review"])

    def kindless(self, repo, selector: str) -> None:
        repo.record("20260101-kindless", turns=["choose a sample"], decisions=[
            {"id": "D1", "question": "which sample", "choice": "the 2019 panel",
             "status": "selected", "selected_by": {"name": selector}}])
        repo.commit("Draw the sample", {"docs/a.md": "a\n"}, record="20260101-kindless")

    def test_a_kindless_selector_under_the_persons_own_name_is_her_choice(self):
        # LESSON (rc22 review, d530637 F3): a `selected_by` that named the
        # person and omitted the kind read as kind unknown, so the person's
        # own acceptance of the work she chose was recorded as self-review.
        with project() as repo:
            self.kindless(repo, "Temp Person")
            code, text = run("accept", "--record", "20260101-kindless", "--person")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual((row["kind"], row["decision"]), ("human", "accept"))
            self.assertFalse(row["self_review"])
            self.assertIn("names Temp Person as its selector with no kind stated",
                          row["selection_note"])
            self.assertIn("Note: a choice in the covered records names Temp Person", text)
            self.assertEqual(hr.validate_acceptances([row])[0], [])

    def test_a_kindless_selector_whose_name_reads_as_a_models_stays_a_selector(self):
        with project() as repo:
            self.kindless(repo, "some-model-2")
            code, text = run("accept", "--record", "20260101-kindless", "--person",
                             "--by", "some-model-2")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual((row["kind"], row["declared_kind"]), ("agent", "human"))
            self.assertTrue(row["self_review"])
            self.assertNotIn("selection_note", row)

    def test_a_kindless_selector_under_another_name_changes_nothing(self):
        with project() as repo:
            self.kindless(repo, "Jane Roe")
            code, text = run("accept", "--record", "20260101-kindless", "--person")
            self.assertEqual(code, 0, text)
            row, = ledger_rows(repo.root)
            self.assertEqual((row["kind"], row["self_review"]), ("human", False))
            self.assertNotIn("selection_note", row)


class TheFileRunsWhole(unittest.TestCase):
    def kit_test_files(self):
        """Every test file of the kit: in this repository its own and the
        starter's, in an installed project the ones the install lists, so a
        project's own test file is never judged here."""
        if (REPO / "starter" / "export-manifest.json").is_file():
            return sorted([*(REPO / "tests").glob("test*.py"),
                           *(REPO / "starter" / "files" / "tests").glob("test*.py")])
        # .trace-os/install.json, or the same file in an earlier folder a project has not yet moved.
        state = next((REPO / folder / "install.json" for folder in (".trace-os", ".traceware", ".trace-kit")
                      if (REPO / folder / "install.json").is_file()), REPO / ".trace-os" / "install.json")
        if state.is_file():
            files = json.loads(state.read_text(encoding="utf-8")).get("files") or {}
            return sorted(REPO / name for name in files
                          if re.fullmatch(r"tests/test[^/]*\.py", name) and (REPO / name).is_file())
        return [REPO / "tests" / name for name in ("test_acceptance_queue.py", "test_trace_report.py")]

    def test_running_a_test_file_directly_runs_every_class_in_it(self):
        # LESSON (AH-147): `unittest.main()` sat above later classes, so running
        # the file directly skipped them and still printed OK; discovery ran them.
        # The check covers every kit test file, not the two first found.
        files = self.kit_test_files()
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path.name):
                body = ast.parse(path.read_text(encoding="utf-8")).body
                guards = [i for i, node in enumerate(body) if isinstance(node, ast.If)
                          and "__main__" in ast.unparse(node.test)]
                for index in guards:
                    self.assertEqual(index, len(body) - 1,
                                     f"{path.name}: the __main__ guard at line {body[index].lineno} "
                                     "is not the last statement")


class TheBriefShowsWhatEachCheckWasHeldAgainst(unittest.TestCase):
    """LESSON (rc22): the brief showed a check's kind, result and limits and hid its
    candidate and criterion, so a person deciding saw that a check passed and not
    what it was run on or held against."""

    def test_each_check_shows_its_candidate_criterion_and_sources(self):
        with project() as repo:
            repo.record("20260101-checked", turns=["check the table"],
                        checks=[{"kind": "person-check", "result": "all cells match",
                                 "candidate": "paper/tables/t2.tex",
                                 "criterion": "every cell matches the survey data",
                                 "limits": "the totals row only",
                                 "sources": [{"path": "workspace/shared/survey.csv",
                                              "sha256": "ab" * 32}]}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-checked")
            code, text = run("brief", "20260101-checked")
            self.assertEqual(code, 0)
            self.assertIn("candidate: paper/tables/t2.tex", text)
            self.assertIn("criterion: every cell matches the survey data", text)
            self.assertIn("sources: workspace/shared/survey.csv (sha256 abababababab)", text)

    def test_a_row_that_states_neither_says_so(self):
        with project() as repo:
            repo.record("20260101-old", turns=["do it"],
                        checks=[{"kind": "build", "result": "PASS", "limits": "one run"}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-old")
            code, text = run("brief", "20260101-old")
            self.assertEqual(code, 0)
            self.assertIn("candidate: not stated", text)
            self.assertIn("criterion: not stated", text)
            self.assertNotIn("sources:", text)
            # A row written before `check` existed names who ran it, not a checker.
            self.assertIn("checker: not stated", text)
            self.assertIn("examined: not stated", text)

    def test_each_check_shows_its_checker_and_what_was_examined(self):
        # LESSON (rc22, Stage 2): the brief hid who made a check and what they
        # looked at, so a passing result was read without its checker or method.
        with project() as repo:
            repo.record("20260101-who", turns=["check the table"], checks=[
                {"kind": "person-check", "result": "all cells match", "checker": "Jane Roe",
                 "checker_kind": "human", "recorded_by": "some-model-1",
                 "runner": "Jane Roe (person), recorded by some-model-1",
                 "method": "each cell of table 2 against the panel"},
                {"kind": "code-test", "result": "PASS", "runner": "unittest", "method": "ran the suite"}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-who")
            code, text = run("brief", "20260101-who")
            self.assertEqual(code, 0)
            self.assertIn("checker: Jane Roe (person), recorded by some-model-1", text)
            self.assertIn("examined: each cell of table 2 against the panel", text)
            self.assertIn("checker: unittest", text)
            self.assertIn("examined: ran the suite", text)


class TheBriefShowsReasonsAndSourcesWhole(unittest.TestCase):
    """LESSON (rc22, Stage 2): the brief cut a choice's reason at 110 characters
    and a check's sources at 100, so a snapshot file's name was hidden behind
    "...". Both are shown in full, wrapped under their label."""

    def test_a_long_reason_and_a_long_source_path_are_shown_whole(self):
        reason = ("the corrected panel matches all fifty sample firms on their identifiers, while the "
                  "earlier delivery used the provider's old identifiers for three of them, so every "
                  "estimate built on it would drop those firms without saying so")
        snapshot = "workspace/snapshots/panel/snapshots/" + "ab" * 32 + "/data/firmdata_panel_v4.csv"
        with project() as repo:
            repo.record("20260101-long", turns=["use the right panel"], decisions=[
                {"id": "D1", "question": "Which delivery?", "choice": "v4", "reason": reason,
                 "status": "selected", "selected_by": {"name": "some-model-1", "kind": "agent"}}],
                checks=[{"kind": "person-check", "result": "matches",
                         "sources": [{"path": snapshot, "sha256": "cd" * 32, "snapshot": "ab" * 32}]}])
            repo.commit("work", {"docs/one.md": "one\n"}, record="20260101-long")
            code, text = run("brief", "20260101-long")
            self.assertEqual(code, 0)
            flat = " ".join(text.split())
            self.assertIn("reason: " + reason, flat)
            self.assertIn(f"sources: {snapshot} (sha256 cdcdcdcdcdcd, verified snapshot abababababab)", flat)
            self.assertIn(snapshot, text, "a path is never broken across lines")
            self.assertTrue(all(len(line) <= 110 for line in text.splitlines()
                                if line.lstrip().startswith("reason:")))


class TheManuscriptIsNotAnInstruction(unittest.TestCase):
    """LESSON (rc22, Stage 2): queue and brief grouped paper/ with the instruction
    files, so queue said a manuscript file is one "which every later session works
    under" and brief listed manuscript lines as added to the instruction files.
    The two are watched alike and said apart."""

    def test_queue_says_a_manuscript_change_apart_from_an_instruction_change(self):
        with project() as repo:
            repo.record("20260101-both", turns=["revise"], decisions=A_PERSONS_CHOICE)
            repo.commit("revise", {"paper/sections/01.tex": "prose\n", "AGENTS.md": "rule\n"},
                        record="20260101-both")
            repo.record("20260102-paper", turns=["revise the section"], decisions=A_PERSONS_CHOICE)
            repo.commit("edit", {"paper/sections/02.tex": "more prose\n"}, record="20260102-paper")
            records = by_id(queue_json())
            both = records["20260101-both"]
            self.assertEqual(both["label"], "needs you")
            self.assertIn("changes AGENTS.md", both["reasons"])
            self.assertIn("changes the manuscript: paper/sections/01.tex", both["reasons"])
            paper = records["20260102-paper"]
            self.assertEqual(paper["reasons"], ["changes the manuscript: paper/sections/02.tex"])
            self.assertEqual(paper["hangs_on"], "Needs you: changes the manuscript: paper/sections/02.tex, "
                                                "the shared text the coauthors rely on.")
            self.assertNotIn("every later session", paper["hangs_on"])
            self.assertIn("which every later session works under", both["hangs_on"])

    def test_brief_groups_and_quotes_the_manuscript_on_its_own(self):
        with project() as repo:
            repo.record("20260101-paper", turns=["revise"], decisions=A_PERSONS_CHOICE)
            repo.commit("revise", {"paper/sections/01.tex": "a new sentence\n",
                                   "AGENTS.md": "a new rule\n"}, record="20260101-paper")
            code, text = run("brief", "20260101-paper")
            self.assertEqual(code, 0)
            self.assertRegex(text, r"\n  manuscript +1 file\(s\)")
            self.assertRegex(text, r"\n  instructions +1 file\(s\)")
            self.assertIn("lines these commits added to the instruction files:\n    | a new rule", text)
            self.assertIn("lines these commits added to the manuscript:\n    | a new sentence", text)
            self.assertNotIn("| a new sentence\n\n  lines these commits added to the instruction", text)


if __name__ == "__main__":
    unittest.main()
