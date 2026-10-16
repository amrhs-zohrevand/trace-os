"""The governing-actions report must say missing where a record is absent and
not applicable where the action was never owed, over a repository it did not
write itself.

Added 2026-09-15. The workflow check validated records that existed and never
asked whether one should exist, so a project could run every check green while
performing none of the five governing actions.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trace_record as hr  # noqa: E402
import trace_report as tr  # noqa: E402


class GoverningActions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        before = Path.cwd(); os.chdir(self.root); self.addCleanup(os.chdir, before)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Test Researcher", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="Test Researcher", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        self.write("README.md", "start")
        self.git("add", "-A"); self.git("commit", "-qm", "baseline")
        epoch = self.git("rev-parse", "HEAD")
        (self.root / "ai" / "records").mkdir(parents=True)
        (self.root / "ai" / "project.json").write_text(json.dumps(
            {"schema_version": 1, "policy_epoch": epoch, "accept_epoch": epoch}))
        (self.root / "sources").mkdir()
        saved = (hr.ACCEPTANCES, hr.INTAKE, hr.TRANSITIONS)
        hr.ACCEPTANCES = Path("ai/records/acceptances.jsonl")
        hr.INTAKE = Path("sources/intake.jsonl")
        hr.TRANSITIONS = Path("sources/transitions.jsonl")
        self.addCleanup(lambda: setattr(hr, "ACCEPTANCES", saved[0]))
        self.addCleanup(lambda: setattr(hr, "INTAKE", saved[1]))
        self.addCleanup(lambda: setattr(hr, "TRANSITIONS", saved[2]))

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, text=True).strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, name, text, message):
        self.write(name, text)
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def record(self, rid, checks):
        rec = self.root / "ai" / "records" / rid
        rec.mkdir(parents=True)
        (rec / "evidence.json").write_text(json.dumps({"record_id": rid, "turns": []}))
        (rec / "checks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in checks))
        return f"ai/records/{rid}"

    def test_states_are_declared_per_principle(self):
        rec = self.record("s1", [{"kind": "code-test", "runner": "unittest", "method": "m",
                                  "result": "pass", "artifact": "README.md"}])
        # A scratch note: T applies, nothing else does.
        scratch = self.commit("workspace/notes.md", "x", "Scratch note\n\nExecution-agent: t")
        # A manuscript change with a record, a delegated acceptance and a transition.
        paper = self.commit("paper/sections/01.tex", "x", f"Draft\n\nRecord: {rec}\n\nExecution-agent: t")
        hr.ACCEPTANCES.write_text(json.dumps({"kind": "delegated", "kind_basis": "declared",
            "reviewer": "release-check", "authorized_by": "A", "decision": "accept",
            "at": "t", "covers": [paper], "count": 1, "self_review": False}) + "\n")
        hr.TRANSITIONS.write_text(json.dumps({"id": "TR-001", "kind": "promotion",
            "object": "paper/sections/01.tex", "by": "t", "authority": "T1", "checks": "c",
            "decision": "promote", "destination": "paper/", "commits": [paper], "at": "t"}) + "\n")
        # An instruction change with no adoption row.
        rules = self.commit("AGENTS.md", "new rule", "Change a rule\n\nExecution-agent: t")
        # A manuscript change naming a record that does not exist.
        dangling = self.commit("paper/sections/02.tex", "y", "Draft two\n\nRecord: ai/records/nope\n\nExecution-agent: t")

        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c["states"] for c in report["commits"]}
        self.assertEqual(report["commits_since_baseline"], 4)
        self.assertEqual(by[scratch], {"T": "missing", "R": "not-applicable", "A": "not-applicable",
                                       "C": "not-applicable", "E": "not-applicable"})
        self.assertEqual(by[paper], {"T": "recorded", "R": "recorded", "A": "recorded",
                                     "C": "recorded", "E": "not-applicable"})
        self.assertEqual(by[rules]["E"], "missing")
        self.assertEqual(by[rules]["C"], "missing")
        self.assertEqual(by[dangling], {"T": "missing", "R": "unknown", "A": "missing",
                                        "C": "missing", "E": "not-applicable"})
        self.assertEqual(report["totals"]["T"], {"recorded": 1, "missing": 3, "not-applicable": 0, "unknown": 0})
        self.assertEqual(report["transitions"]["rows"], 1)

    def test_a_persons_own_commit_owes_no_trace_record(self):
        # No trailer is a person's commit (backfill's none-declared); Git already
        # holds who, when and what, so T does not apply. An agent commit with no
        # record is still missing, and so is a person's commit naming an absent one.
        hand = self.commit("workspace/sandbox/note.md", "x", "My sentence")
        agent = self.commit("workspace/sandbox/note.md", "y", "Agent sentence\n\nExecution-agent: t")
        dangling = self.commit("workspace/sandbox/note.md", "z", "Mine\n\nRecord: ai/records/nope")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        self.assertEqual(by[hand]["states"]["T"], "not-applicable")
        self.assertIn("a person's commit", by[hand]["why"]["T"])
        self.assertEqual(by[agent]["states"]["T"], "missing")
        self.assertEqual(by[dangling]["states"]["T"], "missing")

    def test_the_report_reads_a_commit_as_backfill_does(self):
        # LESSON (AH-127, AH-137): the report called a commit a machine's
        # when backfill and verify-range called it a person's: a post-rule
        # commit with only the vendor's Co-Authored-By trailer, and a hand
        # commit that says `Execution-agent: none`. One classification now.
        coauthor = self.commit("workspace/sandbox/note.md", "a",
                               "Agent work\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>")
        none = self.commit("workspace/sandbox/note.md", "b", "Changed slide 8 by hand\n\nExecution-agent: none")
        both = self.commit("workspace/sandbox/note.md", "c",
                           "Agent work\n\nExecution-agent: none\nExecution-agent: claude-opus-5")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        for sha in (coauthor, none):
            self.assertEqual(by[sha]["states"]["T"], "not-applicable")
            self.assertIn("a person's commit", by[sha]["why"]["T"])
            self.assertEqual(hr.classify(self.git("log", "-1", "--format=%B", sha), exempt=False)[0],
                             "none-declared")
        self.assertEqual(by[both]["states"]["T"], "missing", "`none` never hides a real declaration")

    def test_a_record_named_through_a_full_stop_is_the_same_record(self):
        # LESSON: two commits on 2026-09-21 ended their Record line with a
        # sentence's full stop, and the report read the record as absent while
        # `brief` found no commit for it. Punctuation after the name is not
        # part of the name.
        rec = self.record("s2", [{"kind": "code-test", "runner": "unittest", "method": "m",
                                  "result": "pass", "artifact": "README.md"}])
        dotted = self.commit("docs/a.md", "x", f"Note\n\nRecord: {rec}.\n\nExecution-agent: t")
        bracketed = self.commit("docs/b.md", "y", f"Note\n\nRecord: {rec} (T1, D1)\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        for sha in (dotted, bracketed):
            self.assertEqual(by[sha]["record"], rec)
            self.assertEqual(by[sha]["states"]["T"], "recorded")

    def test_a_return_is_a_recorded_decision_and_an_adoption_needs_a_later_run(self):
        rec = self.record("s2", [])
        paper = self.commit("paper/x.tex", "x", f"Draft\n\nRecord: {rec}\n\nExecution-agent: t")
        rules = self.commit(".skills/a/SKILL.md", "x", "Adopt a skill\n\nExecution-agent: t")
        hr.ACCEPTANCES.write_text(json.dumps({"kind": "human", "kind_basis": "declared",
            "reviewer": "A", "decision": "return", "at": "t", "covers": [paper], "count": 1,
            "self_review": False}) + "\n")
        hr.TRANSITIONS.write_text(json.dumps({"id": "TR-001", "kind": "default-adoption",
            "object": ".skills/a/SKILL.md", "by": "t", "authority": "T2", "checks": "c",
            "decision": "adopt", "destination": "skills", "commits": [rules], "at": "t",
            "later_run": "nothing stated"}) + "\n")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        self.assertEqual(by[paper]["states"]["A"], "recorded")
        self.assertIn("returned", by[paper]["why"]["A"])
        self.assertEqual(by[paper]["states"]["R"], "missing", "a record with no check rows")
        self.assertEqual(by[rules]["states"]["C"], "recorded")
        self.assertEqual(by[rules]["states"]["E"], "missing")
        self.assertIn("no later-run check", by[rules]["why"]["E"])

    def test_ledger_and_intake_summaries_and_the_rendered_table(self):
        (self.root / "ai" / "defects.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"id": "AH-001", "state": "closed"}, {"id": "AH-002", "state": "fixed"},
            {"id": "AH-003", "state": "unresolved", "faulty_default": "AGENTS.md"}]))
        hr.INTAKE.write_text(json.dumps({"what": "d", "admitted_by": "a", "at": "t",
                                         "checked": "nothing stated"}) + "\n")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        self.assertEqual(report["ledger"]["states"]["unresolved"], 1)
        self.assertEqual(report["ledger"]["closed_without_verification"], 1)
        self.assertEqual(report["ledger"]["fixed_without_recheck"], 1)
        self.assertEqual(report["ledger"]["rows_naming_a_faulty_default"], 1)
        self.assertEqual({k: report["intake"][k] for k in ("rows", "checked_nothing_stated")},
                         {"rows": 1, "checked_nothing_stated": 1})
        # A row written before intake took a decision is counted as such, never read as an admission.
        self.assertEqual(report["intake"]["decisions"], {"admit": 0, "return": 0, "refuse": 0, "not recorded": 1})
        text = tr.render(report)
        self.assertIn("governing actions over 0 commit(s)", text)
        self.assertIn("1 closed without verification", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tr.main([]), 0)
        self.assertIn("applicability", out.getvalue())

    def test_every_standing_instruction_is_a_protected_path_for_c(self):
        # LESSON (rc22): WORKFLOW.md was in E's list and not in C's, so a
        # change to it read E missing and C not applicable. One reading now.
        self.assertTrue(set(tr.INSTRUCTION_PATHS) <= set(hr.PROTECTED_PATHS),
                        set(tr.INSTRUCTION_PATHS) - set(hr.PROTECTED_PATHS))
        routine = self.commit("WORKFLOW.md", "new step\n", "Change the routine\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        states = {c["commit"]: c["states"] for c in report["commits"]}[routine]
        self.assertEqual((states["C"], states["E"]), ("missing", "missing"))
        self.assertIn("WORKFLOW.md", report["applicability"]["C"])

    def test_recorded_says_which_decision_and_flags_a_refused_object_in_paper(self):
        # LESSON (rc22): a refuse row on a commit already in paper/ read C
        # recorded, with nothing saying the row refused what the tree holds.
        draft = self.commit("paper/sections/01.tex", "x\n", "Promote a draft\n\nExecution-agent: t")
        kept = self.commit("paper/sections/02.tex", "y\n", "Promote another\n\nExecution-agent: t")
        sandbox = self.commit("workspace/sandbox/03.tex", "z\n", "Draft three\n\nExecution-agent: t")
        rows = [{"id": "TR-001", "kind": "promotion", "object": "paper/sections/01.tex", "by": "t",
                 "authority": "T1", "checks": "c", "decision": "refuse", "destination": "paper/",
                 "commits": [draft], "at": "t"},
                {"id": "TR-002", "kind": "promotion", "object": "paper/sections/02.tex", "by": "t",
                 "authority": "T1", "checks": "c", "decision": "promote", "destination": "paper/",
                 "commits": [kept], "at": "t"},
                {"id": "TR-003", "kind": "promotion", "object": "workspace/sandbox/03.tex", "by": "t",
                 "authority": "T1", "checks": "c", "decision": "return", "destination": "paper/",
                 "commits": [sandbox], "at": "t"}]
        hr.TRANSITIONS.write_text("".join(json.dumps(r) + "\n" for r in rows))
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        self.assertEqual(by[draft]["states"]["C"], "recorded")
        self.assertIn("TR-001 (promotion, refuse)", by[draft]["why"]["C"])
        self.assertIn("TR-001 recorded refuse, yet paper/sections/01.tex is in paper/ at " + draft[:12],
                      by[draft]["why"]["C"])
        self.assertEqual(by[kept]["why"]["C"], "transition TR-002 (promotion, promote)")
        # A returned draft that stayed in the sandbox is not flagged.
        self.assertEqual(by[sandbox]["why"]["C"], "transition TR-003 (promotion, return)")
        self.assertEqual(report["transitions"]["by_decision"]["promotion"],
                         {"promote": 1, "return": 1, "refuse": 1})
        flagged = report["transitions"]["returned_or_refused_yet_in_paper"]
        self.assertEqual([(f["id"], f["decision"]) for f in flagged], [("TR-001", "refuse")])
        text = tr.render(report)
        self.assertIn("decisions: 1 promote, 1 promotion return, 1 promotion refuse", text)
        self.assertIn("TR-001 recorded refuse, yet paper/sections/01.tex is in paper/", text)

    def test_material_no_intake_row_names_is_listed_as_information(self):
        # LESSON (rc22): material entering without an intake row was invisible;
        # the report counted the rows and never compared them with the tree.
        named = self.commit("sources/data/wave1.csv", "id\n1\n", "Add wave 1\n\nExecution-agent: t")
        self.commit("sources/data/wave2.csv", "id\n2\n", "Add wave 2\n\nExecution-agent: t")
        self.commit("sources/data/moved.csv", "id\n3\n", "Add a moved file\n\nExecution-agent: t")
        self.commit("sources/data/wave2.csv.gz", "gz", "Add a compressed wave\n\nExecution-agent: t")
        self.commit("sources/memos/note.md", "a note", "A note is not data\n\nExecution-agent: t")
        self.commit("sources/corpus/a.json", "{}", "A folder named whole\n\nExecution-agent: t")
        self.write("sources/shared-inventory.json", json.dumps({"schema_version": 1, "shared_path": "workspace/shared",
            "files": [{"path": "papers/smith.pdf", "size": 1, "sha256": "a" * 64},
                      {"path": "papers/jones.pdf", "size": 1, "sha256": "b" * 64}]}))
        snap = self.root / "workspace" / "snapshots" / "survey" / "snapshots"
        for identifier in ("c" * 64, "d" * 64):
            (snap / identifier).mkdir(parents=True)
            (snap / identifier / "manifest.json").write_text("{}\n")
        hr.INTAKE.write_text("".join(json.dumps(r) + "\n" for r in [
            {"what": "wave 1", "admitted_by": "a", "at": "t", "checked": "c", "object": "sources/data/wave1.csv"},
            {"what": "a file later moved", "admitted_by": "a", "at": "t", "checked": "c",
             "object": "workspace/shared/old.csv", "object_sha256": hr.sha256_text("id\n3\n")},
            {"what": "the corpus", "admitted_by": "a", "at": "t", "checked": "c", "object": "sources/corpus/"},
            # Named in words, and whole: this names the .gz and not the plain file.
            {"what": "the archive copy, sources/data/wave2.csv.gz", "admitted_by": "a", "at": "t", "checked": "c"},
            {"what": "the Smith paper, workspace/shared/papers/smith.pdf", "admitted_by": "a", "at": "t",
             "checked": "c"},
            {"id": "IN-005", "what": "survey snapshot", "admitted_by": "a", "at": "t", "checked": "c",
             "decision": "admit", "version": "c" * 64}]))
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        unnamed = report["intake"]["unnamed_material"]
        self.assertEqual(unnamed["sources"], {"total": 5, "unnamed": 1, "examples": ["sources/data/wave2.csv"]})
        self.assertEqual(unnamed["shared_inventory"], {"total": 2, "unnamed": 1,
                                                       "examples": ["workspace/shared/papers/jones.pdf"]})
        self.assertEqual(unnamed["snapshots"], {"total": 2, "unnamed": 1,
                                                "examples": ["workspace/snapshots/survey/snapshots/" + "d" * 64]})
        self.assertIn("named by no row: 1 of 5 data file(s) added under sources/, 1 of 2 shared-inventory file(s), "
                      "1 of 2 snapshot(s) here", tr.render(report))
        # Information only: the C state of a data commit is untouched.
        self.assertEqual({c["commit"]: c["states"]["C"] for c in report["commits"]}[named], "not-applicable")
        # Rows written before intake took a decision admit what they name, as
        # they did when written: nothing is listed as turned away or held.
        self.assertEqual(unnamed["returned_or_refused_yet_present"], [])
        self.assertEqual(unnamed["incoming"]["total"], 0)
        self.assertNotIn("held in", tr.render(report))

    def test_only_an_admitting_row_names_a_file_and_a_returned_copy_in_the_data_is_flagged(self):
        # LESSON (rc22, Stage 2 of the appendix examples): any intake row named
        # a file whatever its decision, so a copy of a returned version in the
        # shared data read as named, and nothing said where material waits
        # before a decision. Only an admitting row names a file as admitted; a
        # file only a return or refuse row names is flagged as present anyway;
        # what is held in incoming/ is counted apart, by decision, not as data.
        x, y, z = "1" * 64, "2" * 64, "3" * 64
        self.write("sources/shared-inventory.json", json.dumps({"schema_version": 1, "shared_path": "workspace/shared",
            "files": [{"path": "data/panel.csv", "size": 1, "sha256": y},
                      {"path": "data/panel_old.csv", "size": 1, "sha256": x},
                      {"path": "data/scrape.csv", "size": 1, "sha256": z},
                      {"path": "incoming/panel_v3.csv", "size": 1, "sha256": x},
                      {"path": "incoming/panel_v4.csv", "size": 1, "sha256": y}]}))
        # On this machine, a delivery nobody has decided on yet, not in the inventory.
        self.write("workspace/shared/incoming/new_delivery.csv", "id\n")
        self.write("workspace/shared/incoming/.DS_Store", "noise")
        hr.INTAKE.write_text("".join(json.dumps(r) + "\n" for r in [
            {"id": "IN-001", "what": "panel v3", "admitted_by": "a", "at": "t", "checked": "ids",
             "decision": "return", "object": "workspace/shared/incoming/panel_v3.csv", "object_sha256": x},
            {"id": "IN-002", "what": "panel v4", "admitted_by": "a", "at": "t", "checked": "ids",
             "decision": "admit", "object": "workspace/shared/incoming/panel_v4.csv", "object_sha256": y},
            {"id": "IN-003", "what": "the scrape, workspace/shared/data/scrape.csv", "admitted_by": "a",
             "at": "t", "checked": "nothing stated", "decision": "refuse"}]))
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        unnamed = report["intake"]["unnamed_material"]
        # The admitted version is named by its digest wherever it went; the two
        # turned-away files are named, but not as admitted, and not as unnamed.
        self.assertEqual(unnamed["shared_inventory"], {"total": 3, "unnamed": 0, "examples": []})
        self.assertEqual(unnamed["returned_or_refused_yet_present"], [
            {"id": "IN-001", "decision": "return",
             "found": ["IN-001 recorded return, yet workspace/shared/data/panel_old.csv is listed in the "
                       "shared inventory"]},
            {"id": "IN-003", "decision": "refuse",
             "found": ["IN-003 recorded refuse, yet workspace/shared/data/scrape.csv is listed in the "
                       "shared inventory"]}])
        self.assertEqual(unnamed["incoming"], {
            "path": "workspace/shared/incoming", "total": 3, "awaiting_decision": 1, "admitted": 1,
            "returned": 1, "refused": 0, "examples": ["workspace/shared/incoming/new_delivery.csv"]})
        text = tr.render(report)
        self.assertIn("held in workspace/shared/incoming/, not the project's data: 3 file(s), "
                      "1 awaiting a decision, 1 returned, 0 refused, 1 admitted and not yet moved", text)
        self.assertIn("IN-001 recorded return, yet workspace/shared/data/panel_old.csv is listed", text)
        # The registry is unchanged and still valid: a report, never a refusal.
        self.assertTrue(hr.validate_registries()["ok"])

    def test_the_holding_folder_is_a_project_setting(self):
        # incoming_root in ai/project.json names another folder below the project;
        # a data file added there under sources/ is held, not counted as data.
        config = json.loads((self.root / "ai" / "project.json").read_text())
        config["incoming_root"] = "sources/incoming"
        (self.root / "ai" / "project.json").write_text(json.dumps(config))
        self.commit("sources/incoming/delivery.csv", "id\n", "Receive a delivery\n\nExecution-agent: t")
        self.commit("sources/data/wave1.csv", "id\n1\n", "Add wave 1\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        unnamed = report["intake"]["unnamed_material"]
        self.assertEqual(unnamed["sources"], {"total": 1, "unnamed": 1, "examples": ["sources/data/wave1.csv"]})
        self.assertEqual((unnamed["incoming"]["path"], unnamed["incoming"]["total"],
                          unnamed["incoming"]["awaiting_decision"]), ("sources/incoming", 1, 1))

    def test_a_commit_naming_a_fixed_ledger_row_is_joined_to_its_recheck(self):
        # LESSON (2026-10-14 review): the per-commit E column counted default
        # adoption only, so a correction of a finding read not applicable
        # whether or not anyone rechecked it.
        (self.root / "ai" / "defects.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"id": "AH-001", "state": "fixed"},
            {"id": "AH-002", "state": "verified",
             "verifications": [{"outcome": "pass", "decision": "verified"}]},
            {"id": "AH-003", "state": "open"},
            {"id": "AH-004", "state": "fixed", "objects": ["t1.md", "t2.md"],
             "verifications": [{"outcome": "pass", "decision": "recorded", "covers": "t1.md"}]},
            {"id": "AH-005", "state": "unresolved",
             "verifications": [{"outcome": "fail", "decision": "unresolved"}]}]))
        unchecked = self.commit("scripts/a.py", "1", "Fix AH-001\n\nExecution-agent: t")
        rechecked = self.commit("scripts/b.py", "2", "Correct the count\n\nCloses AH-002.\n\nExecution-agent: t")
        opened = self.commit("scripts/c.py", "3", "Open AH-003\n\nExecution-agent: t")
        partly = self.commit("scripts/d.py", "4", "Fix AH-004 and AH-002\n\nExecution-agent: t")
        failed = self.commit("scripts/e.py", "5", "Fix AH-005 (AH-0050 is not a row)\n\nExecution-agent: t")
        rules = self.commit("AGENTS.md", "rule", "Revise the rule behind AH-002\n\nFixes AH-002.\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        self.assertEqual(by[unchecked]["states"]["E"], "missing")
        self.assertIn("no recheck recorded for AH-001", by[unchecked]["why"]["E"])
        self.assertEqual(by[rechecked]["states"]["E"], "recorded")
        self.assertIn("AH-002 (verified)", by[rechecked]["why"]["E"])
        self.assertEqual(by[opened]["states"]["E"], "not-applicable", "an open row has no correction yet")
        self.assertEqual(by[partly]["states"]["E"], "missing", "one of two objects rechecked")
        self.assertIn("no recheck recorded for AH-004; AH-002 rechecked", by[partly]["why"]["E"])
        self.assertEqual(by[failed]["states"]["E"], "recorded", "a failed recheck is a recheck recorded")
        self.assertNotIn("AH-0050", by[failed]["why"]["E"])
        self.assertEqual(by[rules]["states"]["E"], "missing", "the instruction change has no adoption row")
        self.assertIn("no default-adoption row", by[rules]["why"]["E"])
        self.assertIn("each has a recorded recheck", by[rules]["why"]["E"])
        self.assertEqual(report["totals"]["E"], {"recorded": 2, "missing": 3, "not-applicable": 1, "unknown": 0})
        self.assertIn("claims a fix (Fixes <id>) of a ledger row claimed fixed", report["applicability"]["E"])

    def test_only_a_fix_claim_joins_a_commit_to_a_row_and_a_sweep_closure_is_no_correction(self):
        # LESSON (rc22 release review, 96c230d F1 and F2): any commit whose
        # message mentioned a ledger id was read as that row's correction, so a
        # commit citing ids as examples read E missing; and a row a later sweep
        # closed was read as a correction waiting for a recheck that the queue
        # and `check` never ask of it. Only a fix claim before the id joins a
        # commit to a row now, and a row closed by a sweep is never joined.
        (self.root / "ai" / "defects.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"id": "AH-001", "state": "fixed"}, {"id": "AH-002", "state": "fixed"},
            {"id": "AH-003", "state": "fixed"},
            {"id": "AH-004", "state": "closed", "runs": ["m", "deep-1:absent"]},
            {"id": "AH-005", "state": "closed", "runs": ["m", "manual:Jane Roe:closed"]},
            {"id": "AH-006", "state": "verified", "verifications": [{"outcome": "pass", "decision": "verified"}]}]))
        cited = self.commit("scripts/a.py", "1", "Explain the ledger\n\nAH-001 and AH-002 are examples of "
                            "rows; see AH-003.\n\nExecution-agent: t")
        listed = self.commit("scripts/b.py", "2", "Correct the denominators\n\nFixes AH-001, AH-002 and "
                             "AH-003.\n\nExecution-agent: t")
        lower = self.commit("scripts/c.py", "3", "Tidy the parser\n\nresolved: AH-006\n\nExecution-agent: t")
        prefix = self.commit("scripts/d.py", "4", "Prefix AH-001 with its file\n\nExecution-agent: t")
        swept = self.commit("scripts/e.py", "5", "Fix AH-004\n\nExecution-agent: t")
        hand = self.commit("scripts/f.py", "6", "Shorten the label\n\nCloses AH-005\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        self.assertEqual(by[cited]["states"]["E"], "not-applicable", "ids cited as examples correct nothing")
        self.assertEqual(by[prefix]["states"]["E"], "not-applicable", "a verb inside a word is no fix claim")
        self.assertEqual(by[listed]["states"]["E"], "missing")
        self.assertIn("correction of AH-001 (fixed), AH-002 (fixed), AH-003 (fixed)", by[listed]["why"]["E"])
        self.assertEqual(by[lower]["states"]["E"], "recorded")
        self.assertEqual(by[swept]["states"]["E"], "not-applicable")
        self.assertIn("AH-004 was closed by a sweep", by[swept]["why"]["E"])
        self.assertEqual(by[hand]["states"]["E"], "missing", "a closure by hand waits on a recheck")
        self.assertIn("no recheck recorded for AH-005", by[hand]["why"]["E"])

    def test_an_empty_intake_registry_says_none_rather_than_a_dangling_clause(self):
        # LESSON (rc22 release review, cf4950e F2): with no intake rows the
        # decisions clause had nothing to list and the line ended "; decisions: ",
        # the first report a fresh install prints. It reads none, as the
        # transitions' decisions line does; a report written before rc22 has no
        # decisions and gets no clause.
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        line = next(ln for ln in tr.render(report).splitlines() if ln.startswith("  intake:"))
        self.assertEqual(line, "  intake: 0 row(s), 0 with nothing checked; decisions: none")
        earlier = dict(report, intake={k: v for k, v in report["intake"].items() if k != "decisions"})
        line = next(ln for ln in tr.render(earlier).splitlines() if ln.startswith("  intake:"))
        self.assertEqual(line, "  intake: 0 row(s), 0 with nothing checked")

    def test_the_text_report_prints_each_principle_by_its_name(self):
        # LESSON (rc22 release review, 0d3a7e8 F2): the principle names changed
        # on 12 October 2026 and only the trace_stats table pinned one of them;
        # no test named a label in the report's own table.
        text = tr.render(tr.build_report(ledger=self.root / "ai" / "defects.jsonl"))
        for letter, name in (("T", "track records"), ("R", "review against sources"),
                             ("A", "accept independently"), ("C", "control transitions"),
                             ("E", "evaluate corrections")):
            self.assertRegex(text, rf"(?m)^  {letter}  {name} +\d+ +\d+ +\d+ +\d+$")

    def test_the_ledger_summary_counts_partly_rechecked_and_hand_closed_rows(self):
        (self.root / "ai" / "defects.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"id": "AH-001", "state": "fixed", "objects": ["t1.md", "t2.md"],
             "verifications": [{"outcome": "pass", "decision": "recorded", "covers": "t1.md"}]},
            {"id": "AH-002", "state": "closed", "runs": ["m", "manual:Jane Roe:closed"]},
            {"id": "AH-003", "state": "closed", "runs": ["m", "deep-1:absent"]}]))
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        self.assertEqual(report["ledger"]["fixed_without_recheck"], 1)
        self.assertEqual(report["ledger"]["closed_without_verification"], 2)
        self.assertEqual(report["ledger"]["closed_other_than_by_a_sweep_without_recheck"], 1)
        self.assertIn("2 closed without verification (1 of them not by a sweep)", tr.render(report))

    def test_merge_resolution_is_a_governed_change_and_each_commit_counts_once(self):
        self.commit("paper/result.tex", "base\n", "Base result\n\nExecution-agent: t")
        main = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "topic")
        topic = self.commit("paper/result.tex", "topic\n", "Topic result\n\nExecution-agent: t")
        self.git("checkout", "-q", main)
        self.commit("paper/result.tex", "main\n", "Main result\n\nExecution-agent: t")
        result = subprocess.run(["git", "merge", "--no-ff", "--no-commit", "topic"],
                                cwd=self.root, env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 1, "fixture must produce a real conflict")
        merge = self.commit("paper/result.tex", "new resolution\n",
                            "Resolve the result\n\nExecution-agent: t")
        rows = tr.commits_since_baseline()
        self.assertEqual(len(rows), len({r['commit'] for r in rows}))
        self.assertIn(topic, {r['commit'] for r in rows}, "topic history stays in scope")
        merged = next(r for r in rows if r['commit'] == merge)
        self.assertIn("paper/result.tex", merged['files'])
        report = tr.build_report()
        states = next(r['states'] for r in report['commits'] if r['commit'] == merge)
        self.assertEqual(states['R'], 'unknown', "the merge has no check record")
        self.assertEqual(states['A'], 'missing')
        self.assertEqual(states['C'], 'missing')

    def test_an_invalid_acceptance_cannot_be_reported_as_recorded(self):
        sha = self.commit("paper/result.tex", "result", "Result\n\nExecution-agent: t")
        hr.ACCEPTANCES.write_text(json.dumps({'kind': 'delegated', 'reviewer': 'reviewer',
            'covers': [sha], 'count': 1, 'at': 'now', 'decision': 'accept'}) + '\n')
        with self.assertRaisesRegex(ValueError, 'authorizing researcher'):
            tr.build_report()

    def test_a_record_named_by_its_bare_id_or_absolute_path_is_the_same_record(self):
        # LESSON (AH-136): two commits here say `Record: 20260922-trace-review-agentic`,
        # and the report joined the text as a path from the root, so T read
        # "names an absent record" for a record on disk.
        rec = self.record("s3", [{"kind": "code-test", "runner": "unittest", "method": "m",
                                  "result": "pass", "artifact": "README.md"}])
        bare = self.commit("docs/a.md", "x", "Note\n\nRecord: s3\n\nExecution-agent: t")
        absolute = self.commit("docs/b.md", "y", f"Note\n\nRecord: {self.root.resolve()}/{rec}\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        by = {c["commit"]: c for c in report["commits"]}
        for sha in (bare, absolute):
            self.assertEqual(by[sha]["record"], rec)
            self.assertEqual(by[sha]["states"]["T"], "recorded")

    def test_a_refused_adoption_is_not_an_adoption_awaiting_a_later_run(self):
        # LESSON (AH-139): the report took a refused adoption for an adoption,
        # so E read "adoption recorded, no later-run check", which no later
        # run could clear: `later-run` refuses a row that adopted nothing.
        rules = self.commit("AGENTS.md", "new rule", "Change a rule\n\nExecution-agent: t")
        hr.TRANSITIONS.write_text(json.dumps({"id": "TR-001", "kind": "default-adoption",
            "object": "AGENTS.md", "by": "t", "authority": "T2", "checks": "c",
            "decision": "refuse", "commits": [rules], "at": "t", "later_run": "nothing stated"}) + "\n")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        e = next(c for c in report["commits"] if c["commit"] == rules)
        self.assertEqual(e["states"]["E"], "missing", "an instruction change nobody adopted")
        self.assertNotIn("no later-run check", e["why"]["E"])
        self.assertIn("TR-001", e["why"]["E"])
        self.assertIn("refuse", e["why"]["E"])


class AdoptionObservations(unittest.TestCase):
    """LESSON (2026-09-22): the report read an adoption as E `recorded` from a
    later_run string alone. It now also says who observed each adoption's later
    run, so a link nobody looked at is counted as stated without an observation,
    not as observed. LESSON (2026-09-23): the per-commit state still read that
    bare link as `recorded`, beside the line counting it as unobserved and the
    queue saying E waits on it; in the workshop template the one adoption read
    both ways at once. Per commit, E is now recorded only from an observed run.
    """

    def _adoption_state(self, row: dict) -> dict:
        covered = {"c0": [dict({"id": "TR-001", "kind": "default-adoption", "decision": "adopt"}, **row)]}
        commit = {"commit": "c0", "subject": "rule", "files": ["AGENTS.md"], "record": None, "machine": True}
        return tr.assess(commit, {}, covered, ("AGENTS.md",), ("scripts/",))["E"]

    def test_a_later_run_nobody_observed_reads_missing_per_commit(self):
        bare = self._adoption_state({"later_run": "ai/records/later"})
        self.assertEqual(bare["state"], "missing")
        self.assertIn("nobody observed it", bare["why"])
        none = self._adoption_state({"later_run": "nothing stated"})
        self.assertEqual((none["state"], none["why"]), ("missing", "adoption recorded, no later-run check"))
        for kind in ("human", "agent", "delegated"):
            seen = self._adoption_state({"later_run": "ai/records/later", "later_run_kind": kind,
                                         "later_run_observed": "it held", "later_run_by": "p"})
            self.assertEqual(seen["state"], "recorded", kind)
            self.assertIn("observed later run: TR-001", seen["why"])

    def test_adoptions_are_counted_by_observer_kind(self):
        rows = [
            {"kind": "default-adoption", "decision": "adopt", "later_run": "nothing stated"},
            {"kind": "default-adoption", "decision": "adopt", "later_run": "ai/records/a"},
            {"kind": "default-adoption", "decision": "adopt", "later_run": "ai/records/b",
             "later_run_kind": "human", "later_run_observed": "it held", "later_run_by": "p"},
            {"kind": "default-adoption", "decision": "adopt", "later_run": "ai/records/c",
             "later_run_kind": "agent", "later_run_observed": "it held", "later_run_by": "m"},
            {"kind": "default-adoption", "decision": "return", "later_run": "nothing stated"},
            {"kind": "promotion", "decision": "promote"},
        ]
        self.assertEqual(tr.adoption_summary(rows), {
            "observed_by_a_person": 1, "observed_by_a_model": 1, "observed_by_a_delegated_actor": 0,
            "stated_without_an_observation": 1, "awaiting": 1})

    def test_a_recheck_with_no_stated_outcome_is_still_fixed_without_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "defects.jsonl"
            ledger.write_text("".join(json.dumps(r) + "\n" for r in [
                {"id": "AH-001", "state": "fixed", "verifications": [{"outcome": "not stated", "decision": "recorded"}]},
                {"id": "AH-002", "state": "fixed", "verifications": [{"outcome": "pass", "decision": "recorded"}]},
                {"id": "AH-003", "state": "fixed"}]))
            self.assertEqual(tr.ledger_summary(ledger)["fixed_without_recheck"], 2)

    def test_the_rendered_report_carries_the_line(self):
        report = {"commits_since_baseline": 0, "baseline": "0" * 40,
                  "totals": {p: {s: 0 for s in tr.STATES} for p in tr.PRINCIPLES},
                  "ledger": {"states": {s: 0 for s in tr.LEDGER_STATES}, "closed_without_verification": 0,
                             "fixed_without_recheck": 0, "rows_naming_a_faulty_default": 0},
                  "intake": {"rows": 0, "checked_nothing_stated": 0},
                  "transitions": {"rows": 1, "by_kind": {"intake": 0, "promotion": 0, "default-adoption": 1},
                                  "adoptions": tr.adoption_summary([{"kind": "default-adoption", "decision": "adopt",
                                                                     "later_run": "nothing stated"}])},
                  "applicability": {p: "" for p in tr.PRINCIPLES}, "meaning": "m"}
        self.assertIn("adoptions: later run 0 observed by a person, 0 observed by a model, "
                      "0 observed by a delegated actor, 0 stated without an observation, 1 awaiting",
                      tr.render(report))


class WhatARecordedReviewRestsOn(unittest.TestCase):
    """LESSON (rc22): R read recorded for any check row whose artifact resolved, so a
    "tests pass" row made a commit to sources/ read as reviewed against sources.
    The state stays recorded, and the report says what the rows name."""

    setUp = GoverningActions.setUp
    git = GoverningActions.git
    write = GoverningActions.write
    commit = GoverningActions.commit
    record = GoverningActions.record

    def basis_of(self, rid, checks):
        rec = self.record(rid, checks)
        sha = self.commit(f"sources/{rid}.md", "x", f"Work\n\nRecord: {rec}\n\nExecution-agent: t")
        report = tr.build_report(ledger=self.root / "ai" / "defects.jsonl")
        row = {c["commit"]: c for c in report["commits"]}[sha]
        return row, report

    def test_a_row_naming_its_sources_a_criterion_alone_and_an_artifact_alone_are_told_apart(self):
        base = {"kind": "k", "runner": "r", "method": "m", "result": "pass",
                "artifact": "log", "reference": "declared"}
        sourced, _ = self.basis_of("a", [dict(base, criterion="matches the data",
                                              sources=[{"path": "data.csv", "sha256": "ab" * 32}])])
        criterion, _ = self.basis_of("b", [dict(base, criterion="tests pass")])
        bare, report = self.basis_of("c", [dict(base, criterion="not stated")])
        for row in (sourced, criterion, bare):
            self.assertEqual(row["states"]["R"], "recorded")
        self.assertIn("1 name a source, 0 only a criterion", sourced["why"]["R"])
        self.assertIn("0 name a source, 1 only a criterion", criterion["why"]["R"])
        self.assertIn("0 name a source, 0 only a criterion", bare["why"]["R"])
        self.assertEqual(report["review_basis"], {"names_a_source": 1, "names_a_criterion_only": 1,
                                                  "resolves_an_artifact_only": 1})
        self.assertIn("R recorded: 1 with a row naming its sources, 1 with a stated criterion "
                      "and no source, 1 on a resolved artifact alone", tr.render(report))

    def test_a_row_written_before_sources_existed_counts_as_it_did(self):
        row, report = self.basis_of("old", [{"kind": "k", "runner": "r", "method": "m",
                                             "result": "pass", "artifact": "log",
                                             "reference": "declared"}])
        self.assertEqual(row["states"]["R"], "recorded")
        self.assertEqual(report["totals"]["R"]["recorded"], 1)
        self.assertEqual(report["review_basis"]["resolves_an_artifact_only"], 1)


if __name__ == "__main__":
    unittest.main()
