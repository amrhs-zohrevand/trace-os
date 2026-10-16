#!/usr/bin/env python3
"""The skill-handoff report must say missing where a skill does not state its
handoff, and must never fail the run for saying so.

Added 2026-09-21. A skill without a handoff step runs, produces its artifact,
and leaves a session in which nothing was checked. Before 0.1.0-rc5 that was
found by a person reading the two shipped example methods, and nothing
mechanical reported it. These tests name the ways the report could go wrong:
reading a signal that is not there, missing a layout a project actually uses,
crediting a line that names some other skill's id, and failing a run because a
project's own method is unfinished.

Run:  python3 -X utf8 -I -S -m unittest discover -s tests
stdlib only; no pytest needed.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import check_skill_handoff as ch  # noqa: E402
import check_workflow as cw  # noqa: E402

# The ids the kit ships: six governance entry points and four example methods.
SHIPPED = ("claim-evidence-audit", "literature-processing", "meeting-and-idea-intake",
           "trace-acceptance", "trace-check", "trace-ctransition", "trace-evaluate",
           "trace-review", "trace-state", "zotero-knowledge-base")

HEAD = """---
name: example-method
---

# An example method

Read AGENTS.md and `ai/SKILL-RUN-CONTRACT.md`.
Do the work the method describes.
"""
HANDOFF = ("Record the run with `procedure` set to `example-method`, "
           "`procedure_version` set to the digest\n"
           "`trace_record.py procedure-version example-method` reads off this file, "
           "and every field the contract requires.\n")
COMPLETE = HEAD + HANDOFF


def skills_tree(directory: pathlib.Path, files: dict) -> pathlib.Path:
    """A `.skills/` tree written from {published path: text}, both layouts."""
    skills = directory / ".skills"
    for name, text in files.items():
        path = skills / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    skills.mkdir(parents=True, exist_ok=True)
    return skills


def state_of(result: dict, skill_id: str) -> dict:
    return next(s for s in result["states"] if s["skill"] == skill_id)


def shipped_states(tree: pathlib.Path) -> dict:
    """The kit's own `<id>/SKILL.md` copy of each shipped id, keyed by id.

    A project may keep its own flat `<id>.md` beside the kit's directory copy.
    That file is the project's text and the report reads it as such; it never
    stands in for the copy the kit shipped.
    """
    return {s["skill"]: s for s in ch.report(tree)["states"]
            if s["skill"] in SHIPPED and s["path"].endswith(f"/{s['skill']}/SKILL.md")}


class OneRuleThreeSignals(unittest.TestCase):
    """All three signals present is the only way to `handoff stated`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_a_skill_carrying_all_three_signals_states_its_handoff(self):
        skills = skills_tree(self.dir, {"example-method/SKILL.md": COMPLETE})
        result = ch.report(skills)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["handoff_stated"], 1)
        self.assertEqual(state_of(result, "example-method"),
                         {"skill": "example-method",
                          "path": ".skills/example-method/SKILL.md",
                          "state": ch.STATED, "absent": []})

    def swap(self, text: str, before: str, after: str) -> str:
        """Edit a fixture, and fail when the text this test edits is not there."""
        self.assertIn(before, text, "this fixture no longer carries the edited text")
        return text.replace(before, after)

    def test_a_skill_that_never_names_the_contract_is_missing_that_signal(self):
        text = self.swap(COMPLETE, "AGENTS.md and `ai/SKILL-RUN-CONTRACT.md`",
                         "AGENTS.md")
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        state = state_of(ch.report(skills), "example-method")
        self.assertEqual(state["state"], ch.NOT_STATED)
        self.assertEqual(state["absent"], [ch.CONTRACT_NAMED])

    def test_a_skill_that_names_no_record_mechanism_is_missing_that_signal(self):
        # LESSON (rc5 review): naming the contract while never saying where the
        # row goes reproduces the failure the contract was written to prevent.
        text = HEAD + "Record the run with `procedure` set to `example-method`.\n"
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        state = state_of(ch.report(skills), "example-method")
        self.assertEqual(state["state"], ch.NOT_STATED)
        self.assertEqual(state["absent"], [ch.RECORD_NAMED])

    def test_a_skill_that_sets_procedure_to_nothing_is_missing_that_signal(self):
        # LESSON: a template-shaped line ("set `procedure` to this skill's id")
        # carries no id, so it cannot be read as a skill naming itself.
        text = self.swap(COMPLETE, "`procedure` set to `example-method`",
                         "`procedure` set to this skill's id")
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        state = state_of(ch.report(skills), "example-method")
        self.assertEqual(state["state"], ch.NOT_STATED)
        self.assertEqual(state["absent"], [ch.PROCEDURE_NAMED])

    def test_a_line_naming_another_skills_id_is_not_this_skill_naming_itself(self):
        text = HEAD + ("Record the run with `procedure` set to `trace-review`, and the "
                       "digest `trace_record.py` reads.\n")
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        state = state_of(ch.report(skills), "example-method")
        self.assertEqual(state["absent"], [ch.PROCEDURE_NAMED])

    def test_the_id_and_the_word_must_share_one_line(self):
        text = HEAD + ("Record the run with `procedure` set\nas follows, for "
                       "`example-method`, with the digest `trace_record.py` reads.\n")
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        self.assertEqual(state_of(ch.report(skills), "example-method")["absent"],
                         [ch.PROCEDURE_NAMED])

    def test_a_skill_missing_everything_names_all_three(self):
        skills = skills_tree(self.dir, {"bare/SKILL.md": "# Bare\n\nDo the work.\n"})
        state = state_of(ch.report(skills), "bare")
        self.assertEqual(state["absent"], list(ch.SIGNALS))

    def test_a_bare_contract_stem_counts_and_a_full_path_is_not_required(self):
        # LESSON (GLM 5.3 review, 2026-09-21): matching a full path would report
        # a relative or bare reference as absent, which is a style finding
        # dressed up as a missing handoff.
        text = self.swap(COMPLETE, "`ai/SKILL-RUN-CONTRACT.md`",
                         "`../SKILL-RUN-CONTRACT.md`")
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        self.assertEqual(state_of(ch.report(skills), "example-method")["state"],
                         ch.STATED)

    def test_checks_jsonl_alone_satisfies_the_record_signal(self):
        text = HEAD + ("Write one check row into checks.jsonl with `procedure` set to "
                       "`example-method`.\n")
        skills = skills_tree(self.dir, {"example-method/SKILL.md": text})
        self.assertEqual(state_of(ch.report(skills), "example-method")["state"],
                         ch.STATED)


class BothLayouts(unittest.TestCase):
    """A project keeping single files is not a project with no skills."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_a_single_file_skill_is_found_and_its_stem_is_its_id(self):
        # LESSON: this repository keeps its own project skills as
        # .skills/<id>.md, so a check that globbed only */SKILL.md would have
        # reported a clean tree over ten unread files.
        skills = skills_tree(self.dir, {
            "single-file-method.md": COMPLETE.replace("example-method",
                                                      "single-file-method")})
        result = ch.report(skills)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(state_of(result, "single-file-method"),
                         {"skill": "single-file-method",
                          "path": ".skills/single-file-method.md",
                          "state": ch.STATED, "absent": []})

    def test_both_layouts_are_read_in_one_run(self):
        skills = skills_tree(self.dir, {
            "example-method/SKILL.md": COMPLETE,
            "bare.md": "# Bare\n",
        })
        result = ch.report(skills)
        self.assertEqual((result["checked"], result["handoff_stated"],
                          result["handoff_not_stated"]), (2, 1, 1))

    def test_a_reference_file_beside_a_skill_is_not_a_skill(self):
        skills = skills_tree(self.dir, {
            "example-method/SKILL.md": COMPLETE,
            "example-method/references/checklist.md": "# Not a skill\n",
        })
        self.assertEqual([s["skill"] for s in ch.report(skills)["states"]],
                         ["example-method"])


class InformationalByConstruction(unittest.TestCase):
    """Exit 0 for both states; 2 only when the tree cannot be read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def run_main(self, skills):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ch.main(["--skills", str(skills)])
        return code, out.getvalue()

    def test_a_missing_handoff_still_exits_zero(self):
        # LESSON: nothing in this kit refuses work. A missing handoff is a state
        # to report, and an exit code that failed on it would make a project's
        # own unfinished method an error.
        skills = skills_tree(self.dir, {"bare/SKILL.md": "# Bare\n"})
        code, text = self.run_main(skills)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["handoff_not_stated"], 1)

    def test_a_clean_tree_exits_zero_too(self):
        skills = skills_tree(self.dir, {"example-method/SKILL.md": COMPLETE})
        code, text = self.run_main(skills)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["handoff_not_stated"], 0)

    def test_an_absent_skills_directory_is_not_applicable_with_a_reason(self):
        code, text = self.run_main(self.dir / "nowhere")
        result = json.loads(text)
        self.assertEqual(code, 0)
        self.assertFalse(result["applicable"])
        self.assertEqual(result["checked"], 0)
        self.assertTrue(result["skip_reasons"][0])

    def test_an_unreadable_skill_file_is_exit_two_and_not_a_clean_report(self):
        skills = skills_tree(self.dir, {"broken/SKILL.md": "x"})
        (skills / "broken" / "SKILL.md").write_bytes(b"\xff\xfe\x00 not utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ch.main(["--skills", str(skills)])
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().strip())

    def test_the_stated_limit_travels_with_the_result(self):
        skills = skills_tree(self.dir, {"example-method/SKILL.md": COMPLETE})
        result = ch.report(skills)
        self.assertIn("procedure_version", result["limit"])
        self.assertIn("reads the skill text", result["limit"])

    def test_no_absolute_path_is_printed(self):
        skills = skills_tree(self.dir, {"example-method/SKILL.md": COMPLETE})
        _, text = self.run_main(skills)
        self.assertNotIn(str(self.dir), text)


class InsideTheComposedCheck(unittest.TestCase):
    """check_workflow must register it as informational and never fail on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_the_check_is_informational_and_carries_the_per_skill_states(self):
        skills = skills_tree(self.dir, {"example-method/SKILL.md": COMPLETE,
                                        "bare/SKILL.md": "# Bare\n"})
        check = cw.check_skill_handoff(skills)
        self.assertEqual((check["kind"], check["status"]), ("informational", "info"))
        self.assertEqual(check["outcome"]["checked"], 2)
        self.assertEqual({s["skill"]: s["state"] for s in check["outcome"]["states"]},
                         {"example-method": ch.STATED, "bare": ch.NOT_STATED})
        self.assertIn("procedure_version", check["note"])

    def test_a_missing_handoff_does_not_fail_the_run(self):
        skills = skills_tree(self.dir, {"bare/SKILL.md": "# Bare\n"})
        check = cw.check_skill_handoff(skills)
        summary = cw.summarize([check])
        self.assertEqual(summary["mechanical_failures"], [])
        self.assertEqual(summary["execution_errors"], [])
        self.assertEqual(summary["informational"]["warnings"], [])
        self.assertEqual(summary["informational"]["skill_handoff"],
                         {"checked": 1, "handoff_stated": 0, "handoff_not_stated": 1})
        self.assertEqual(cw.verdict(summary)[0], 0)

    def test_an_absent_tree_is_a_skip_that_names_what_was_skipped(self):
        check = cw.check_skill_handoff(self.dir / "nowhere")
        summary = cw.summarize([check])
        self.assertEqual(check["status"], "info")
        self.assertEqual(summary["skipped"][0]["check"], "skill handoff")
        self.assertIn("skill-handoff", summary["skipped"][0]["what"])
        self.assertEqual(cw.verdict(summary)[0], 0)

    def test_the_skills_tree_counts_as_an_input_the_checks_read(self):
        # LESSON: the fingerprint block is a claim about which bytes decided the
        # result. The allowlist covered the checks' own code, so a dirty skill
        # file would have gone unnamed in a report whose states it produced.
        self.assertTrue(cw.is_read_input(".skills/example-method/SKILL.md"))
        self.assertTrue(cw.is_read_input(".skills/single-file-method.md"))

    def test_the_composed_check_registers_it(self):
        report = cw.build_report(
            out=self.dir / "report.json", shared=False, log_dir=self.dir / "logs",
            records_dir=self.dir / "records", ledger=self.dir / "defects.jsonl",
            tests_dir=self.suite(), acceptances=self.dir / "acceptances.jsonl",
            intake=self.dir / "intake.jsonl", transitions=self.dir / "transitions.jsonl",
            skills_dir=skills_tree(self.dir, {"bare/SKILL.md": "# Bare\n"}))
        check = next(c for c in report["checks"] if c["name"] == "skill handoff")
        self.assertEqual((check["kind"], check["status"]), ("informational", "info"))
        self.assertEqual(check["outcome"]["handoff_not_stated"], 1)
        self.assertNotIn("skill handoff",
                         [e["check"] for e in report["summary"]["mechanical_failures"]
                          + report["summary"]["execution_errors"]])
        self.assertIn("skill_handoff", report["summary"]["informational"])

    def suite(self) -> pathlib.Path:
        """A one-file suite, so this test does not run the whole suite again."""
        where = self.dir / "suite"
        where.mkdir(parents=True, exist_ok=True)
        (where / "test_tiny.py").write_text(
            "import unittest\n\n\nclass Tiny(unittest.TestCase):\n"
            "    def test_passes(self):\n        self.assertTrue(True)\n")
        return where


class TheShippedSkills(unittest.TestCase):
    """Every skill the kit ships must state its handoff, or the examples teach
    the failure they exist to prevent."""

    def shipped_tree(self):
        starter = REPO / "starter" / "files" / ".skills"
        if starter.is_dir():
            return starter
        installed = REPO / ".skills"
        if any(installed.glob("*/SKILL.md")):
            return installed
        return None

    def test_every_shipped_skill_present_here_states_its_handoff(self):
        tree = self.shipped_tree()
        if tree is None:
            self.skipTest("no .skills/<id>/SKILL.md tree in this project")
        states = shipped_states(tree)
        if not states:
            self.skipTest("this project keeps none of the shipped skills")
        for skill_id, state in sorted(states.items()):
            with self.subTest(skill=skill_id):
                self.assertEqual(state["state"], ch.STATED,
                                 f"{skill_id} is missing {state['absent']}")

    def test_a_projects_flat_file_does_not_stand_in_for_the_shipped_copy(self):
        # LESSON (AH-197): the shipped states were keyed by id, so a project's own
        # flat `.skills/claim-evidence-audit.md` overwrote the kit's
        # `.skills/claim-evidence-audit/SKILL.md` and the shipped copy was judged
        # by the project's text, failing the check right after install.
        with tempfile.TemporaryDirectory() as tmp:
            kit_copy = COMPLETE.replace("example-method", "claim-evidence-audit")
            tree = skills_tree(pathlib.Path(tmp), {"claim-evidence-audit/SKILL.md": kit_copy,
                                                  "claim-evidence-audit.md": HEAD})
            states = shipped_states(tree)
        self.assertEqual(states["claim-evidence-audit"]["path"],
                         ".skills/claim-evidence-audit/SKILL.md")
        self.assertEqual(states["claim-evidence-audit"]["state"], ch.STATED)

    def test_every_skill_the_kit_ships_states_it(self):
        starter = REPO / "starter" / "files" / ".skills"
        if not starter.is_dir():
            self.skipTest("not the kit development tree")
        result = ch.report(starter)
        self.assertEqual(sorted(s["skill"] for s in result["states"]), sorted(SHIPPED))
        self.assertEqual((result["checked"], result["handoff_stated"]), (len(SHIPPED), len(SHIPPED)))


if __name__ == "__main__":
    unittest.main()
