"""Exercise human input preservation and the trace record's decision account in temp records."""
import argparse
import codecs
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import trace_story as story
import trace_record as hr
import review_inbox as inbox


class ContributionStory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = self.root / "record"
        self.record.mkdir()
        self.evidence = {"record_id": "record", "consent": {"third_party_review": "cleared"},
                         "execution_agents": [{"model": "example-model"}],
                         "turns": [{"id": "T1", "speaker": "Rae", "text": "Keep evidence and interpretation separate."}]}
        story.write_json(self.record / "evidence.json", self.evidence)
        (self.record / "summary.md").write_text("# Human direction\n\n[T1] Rae supplied the evidence/interpretation distinction.\n", encoding="utf-8")
        (self.record / "checks.jsonl").write_text(json.dumps({"kind": "self-test", "runner": "test checker",
            "method": "compare the source and note", "result": "distinction retained", "artifact": "source-note.md"}) + "\n", encoding="utf-8")
        self.cover()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, func, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return func(hr, argparse.Namespace(**kwargs))

    def cover(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_cover(argparse.Namespace(record=str(self.record)))

    def decision(self, **changes):
        entry = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        entry.update(changes)
        path = self.root / "decision.json"
        story.write_json(path, entry)
        self.call(story.cmd_decision, record=str(self.record), input=str(path))
        return story.load_decisions(self.record, story.read_json(self.record / "evidence.json"))[-1]

    def comments(self, text="Use the original argument.\nRetain its qualification.", who="Sam"):
        path = self.root / "comments.json"
        story.write_json(path, {"items": [{"source": "PR #7", "source_id": "test/repo:comment:21",
            "who": who, "text": text, "url": "https://example.invalid/comment/21"}]})
        self.call(story.cmd_comments, record=str(self.record), input=str(path), speaker=None)
        return path

    def test_comment_survives_resolution_with_words_speaker_and_source(self):
        self.comments()
        evidence = story.read_json(self.record / "evidence.json")
        comment = evidence["turns"][-1]
        self.assertEqual(comment["text"], "Use the original argument.\nRetain its qualification.")
        self.assertEqual(comment["speaker"], "Sam")
        self.assertEqual(comment["source"]["url"], "https://example.invalid/comment/21")
        self.assertEqual(evidence["consent"]["third_party_review"], "PENDING")
        self.comments()
        self.assertEqual(len(story.read_json(self.record / "evidence.json")["turns"]), 2)
        self.comments("Keep the updated qualification.")
        self.assertEqual(len(story.read_json(self.record / "evidence.json")["turns"]), 3)

    def test_comment_author_owns_change_even_when_agent_selects_how(self):
        self.comments()
        entry = self.decision(human_inputs=["T2"], human_direction="Sam asked to retain the qualification.")
        self.assertEqual(entry["owners"], ["Sam"])
        self.assertEqual(entry["selected_by"]["kind"], "agent")
        self.assertEqual(entry["owner_basis"], "input speakers")

    def test_mixed_packet_captures_humans_and_reports_nonhuman_entries(self):
        packet = {"items": [
            {"source": "PR #7", "source_id": "bot:1", "who": "review-bot", "speaker_kind": "agent", "text": "Check the citation."},
            {"source": "PR #7", "source_id": "human:2", "who": "Sam", "speaker_kind": "human", "text": "Keep the qualification."}]}
        path = self.root / "mixed.json"
        story.write_json(path, packet)
        original = path.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = story.cmd_comments(hr, argparse.Namespace(record=str(self.record), input=str(path), speaker=None))
        self.assertEqual(code, 0)
        turns = story.read_json(self.record / "evidence.json")["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[-1]["text"], "Keep the qualification.")
        self.assertEqual(turns[-1]["speaker"], "Sam")
        self.assertIn("1 nonhuman comment(s)", output.getvalue())
        self.assertIn("checks.jsonl", output.getvalue())
        self.assertEqual(path.read_bytes(), original)

    def test_multiple_human_inputs_are_not_assigned_to_last_speaker(self):
        self.comments()
        entry = self.decision(human_inputs=["T1", "T2"])
        self.assertEqual(entry["owners"], ["Rae", "Sam"])

    def test_explicit_assignment_needs_evidence_and_keeps_original_speaker(self):
        with self.assertRaisesRegex(ValueError, "owner_authority"):
            self.decision(owners=["Sam"])
        self.assertFalse((self.record / "decisions.jsonl").exists())
        entry = self.decision(owners=["Sam"], owner_authority="Recorded assignment in team-decisions.md#D3")
        self.assertEqual(entry["owners"], ["Sam"])
        self.assertEqual(story.read_json(self.record / "evidence.json")["turns"][0]["speaker"], "Rae")

    def test_unattributed_comment_does_not_invent_an_owner(self):
        self.comments(who="unattributed")
        entry = self.decision(human_inputs=["T2"])
        self.assertEqual(entry["owners"], [])
        self.assertEqual(entry["owner_basis"], "unknown")

    def test_fabricated_turn_reference_and_duplicate_decision_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "existing turn IDs"):
            self.decision(human_inputs=["T9"])
        self.decision(id="D1")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.decision(id="D1")
        self.assertEqual(len((self.record / "decisions.jsonl").read_text().splitlines()), 1)

    def test_commit_draft_includes_human_idea_owner_agent_choice_and_checks(self):
        self.decision()
        self.cover()
        output = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Clarify the source note", out=str(output), execution_agent=None)
        message = output.read_text(encoding="utf-8")
        for text in ("Rae supplied the evidence/interpretation distinction", "Owner(s): Rae", "selected by: example-model (agent)",
                     "compare the source and note", "Execution-agent: example-model", "T1"):
            self.assertIn(text, message)
        self.assertNotIn("Co-Authored-By", message)
        self.assertFalse((self.root / ".git").exists())

    def test_overview_counts_selected_choices_and_discloses_missing_accounts(self):
        self.decision()
        self.decision(status="proposed", selected_by={"name": "", "kind": "unknown"})
        self.cover()
        other = self.root / "legacy"
        other.mkdir()
        for name in ("evidence.json", "summary.md", "checks.jsonl"):
            (other / name).write_bytes((self.record / name).read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_cover(argparse.Namespace(record=str(other)))
        output = self.root / "overview.json"
        code = self.call(story.cmd_view, records=[str(self.root)], out=str(output), json=True)
        data = story.read_json(output)
        self.assertEqual(code, 0)
        self.assertEqual(data["selected_decisions"], 1)
        self.assertEqual(data["selected_by_kind"]["agent"], 1)
        self.assertEqual(data["records_without_decision_accounts"], 1)

    def test_comments_only_draft_requires_actual_execution_agent(self):
        # A record begun from comments is a current-schema record (AH-128), so
        # its check row carries what version 2 asks of every check.
        check = json.loads((self.record / "checks.jsonl").read_text())
        check.update(candidate="source-note.md", criterion="the distinction is retained",
                     limits="one synthetic note")
        checks = json.dumps(check) + "\n"
        self.record = self.root / "comments-only"
        self.comments()
        evidence = story.read_json(self.record / "evidence.json")
        self.assertEqual(evidence["execution_agents"], [])
        evidence["consent"]["third_party_review"] = "cleared"
        story.write_json(self.record / "evidence.json", evidence)
        (self.record / "summary.md").write_text("[T1] Sam requested the original argument and its qualification.\n")
        (self.record / "checks.jsonl").write_text(checks)
        self.cover()
        output = self.root / "message.txt"
        with self.assertRaisesRegex(ValueError, "pass --execution-agent"):
            self.call(story.cmd_message, record=str(self.record), subject="Clarify the source", out=str(output), execution_agent=None)
        self.assertFalse(output.exists())
        self.call(story.cmd_message, record=str(self.record), subject="Clarify the source", out=str(output), execution_agent=["example-model"])
        self.assertIn("Execution-agent: example-model", output.read_text())
        self.assertEqual(hr.commit_message_problems(output.read_text()), [])

    def test_a_record_begun_from_comments_is_checked_under_the_current_schema(self):
        # LESSON (AH-128): `comments` began a record without a schema version,
        # so a new record was read as a legacy one and its check rows escaped
        # the candidate, criterion and limits that version 2 asks for.
        self.record = self.root / "comments-new"
        self.comments()
        evidence = story.read_json(self.record / "evidence.json")
        self.assertEqual(evidence["record_schema_version"], hr.TRACE_RECORD_SCHEMA_VERSION)

    def test_a_synthetic_model_is_never_drafted_as_an_execution_agent(self):
        # LESSON (AH-126): records extracted before the fix list `<synthetic>`,
        # a harness placeholder, and the draft wrote it as a trailer. It is
        # left out, and no other model is put in its place.
        self.evidence["execution_agents"] = [{"model": "example-model"}, {"model": "<synthetic>"}]
        story.write_json(self.record / "evidence.json", self.evidence)
        self.cover()
        output = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Clarify", out=str(output), execution_agent=None)
        message = output.read_text(encoding="utf-8")
        self.assertIn("Execution-agent: example-model", message)
        self.assertNotIn("<synthetic>", message)
        self.evidence["execution_agents"] = [{"model": "<synthetic>"}]
        story.write_json(self.record / "evidence.json", self.evidence)
        self.cover()
        with self.assertRaisesRegex(ValueError, "pass --execution-agent"):
            self.call(story.cmd_message, record=str(self.record), subject="Clarify", out=str(output), execution_agent=None)

    def test_counting_scope_survives_and_cannot_be_silently_redefined(self):
        path = self.root / "decision.json"
        story.write_json(path, json.loads((ROOT / "scripts" / "decision.example.json").read_text()))
        self.call(story.cmd_decision, record=str(self.record), input=str(path), scope="Research method choices")
        self.cover()
        output = self.root / "overview.json"
        self.call(story.cmd_view, records=[str(self.record)], out=str(output), json=True)
        self.assertEqual(story.read_json(output)["accounts"][0]["decision_scope"], "Research method choices")
        with self.assertRaisesRegex(ValueError, "keep the recorded scope"):
            self.call(story.cmd_decision, record=str(self.record), input=str(path), scope="All research choices")
        self.assertEqual(len((self.record / "decisions.jsonl").read_text().splitlines()), 1)

    def test_changed_input_is_not_hidden_behind_old_coverage(self):
        self.evidence["turns"][0]["text"] = "A different idea."
        story.write_json(self.record / "evidence.json", self.evidence)
        self.assertFalse(hr.evaluate_record(self.record)["ok"])
        with self.assertRaisesRegex(ValueError, "evidence.json changed"):
            self.call(story.cmd_message, record=str(self.record), subject="A change", out=str(self.root / "message"), execution_agent=None)

    def test_broken_records_are_reported_and_fail_overview(self):
        (self.record / "summary.md").unlink()
        output = self.root / "overview.json"
        code = self.call(story.cmd_view, records=[str(self.record)], out=str(output), json=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(story.read_json(output)["errors"]), 1)

    def test_parent_discovery_reports_a_record_missing_its_evidence(self):
        (self.record / "evidence.json").unlink()
        output = self.root / "overview.json"
        code = self.call(story.cmd_view, records=[str(self.root)], out=str(output), json=True)
        self.assertEqual(code, 1)
        data = story.read_json(output)
        self.assertEqual(data["records_examined"], 1)
        self.assertEqual(len(data["errors"]), 1)

    def test_the_older_overview_name_still_runs_the_view(self):
        # LESSON: renaming a command silently breaks every instruction that
        # names the old one, and this repository's own records name `overview`.
        # The alias is what makes the rename safe for an installed project whose
        # local documents were written before 0.1.0-rc9.
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd", required=True)
        story.add_commands(sub, hr)
        for name in ("view", "overview"):
            with self.subTest(name=name):
                args = parser.parse_args([name, str(self.record), "--json",
                                          "--out", str(self.root / (name + ".json"))])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 0)
        self.assertEqual(story.read_json(self.root / "view.json")["records_examined"],
                         story.read_json(self.root / "overview.json")["records_examined"])

    def test_generated_outputs_cannot_overwrite_trace_record_inputs(self):
        original = (self.record / "evidence.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "inputs stay intact"):
            self.call(story.cmd_message, record=str(self.record), subject="Change", out=str(self.record / "evidence.json"), execution_agent=None)
        with self.assertRaisesRegex(ValueError, "inputs stay intact"):
            self.call(story.cmd_view, records=[str(self.record)], out=str(self.record / "evidence.json"), json=True)
        self.assertEqual(original, (self.record / "evidence.json").read_bytes())

    def test_refresh_preserves_comment_ids_and_redacted_prompts(self):
        session = self.root / "session.jsonl"
        session.write_text(json.dumps({"type": "user", "message": {"content": "Original private input."}}) + "\n", encoding="utf-8")
        stage = self.root / "stage"
        args = argparse.Namespace(session=str(session), out=str(stage), record_id="stage", speaker="Rae")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_extract(args)
        evidence = story.read_json(stage / "evidence.json")
        evidence["decision_scope"] = "Research method choices"
        evidence["turns"][0]["text"] = "[REDACTED: private input]"
        evidence["turns"].append({"id": "T2", "speaker": "Sam", "text": "A comment", "source": {"capture_id": "comment-1"}})
        story.write_json(stage / "evidence.json", evidence)
        with session.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "A later direction."}}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_extract(args)
        turns = story.read_json(stage / "evidence.json")["turns"]
        self.assertEqual(story.read_json(stage / "evidence.json")["decision_scope"], "Research method choices")
        self.assertEqual([t["id"] for t in turns], ["T1", "T2", "T3"])
        self.assertEqual(turns[0]["text"], "[REDACTED: private input]")
        self.assertEqual(turns[1]["text"], "A comment")

    def test_inline_identity_survives_moved_line_and_raw_marker_is_retained(self):
        source = self.root / "note.md"
        source.write_text("# REVIEW(Rae): Keep the qualification.\n", encoding="utf-8")
        with mock.patch.object(inbox, "tracked_files", return_value=[source]):
            first = inbox.scan_inline(self.root)[0]
            source.write_text("A paragraph.\n" + source.read_text(), encoding="utf-8")
            second = inbox.scan_inline(self.root)[0]
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(second["line"], 2)
        self.assertEqual(second["raw_line"], "# REVIEW(Rae): Keep the qualification.")

    def test_promotion_carries_decisions_and_hashes_survive_crlf(self):
        self.decision()
        self.cover()
        for name in ("evidence.json", "decisions.jsonl"):
            p = self.record / name
            p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))  # CRLF whatever the platform wrote
        self.assertTrue(hr.evaluate_record(self.record)["ok"])
        out = self.root / "promoted"
        import record_privacy
        self.call(record_privacy.cmd_publication, record=str(self.record), by="test checker", scope="synthetic fixture")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_promote(argparse.Namespace(staging=str(self.record), dest=str(out), force=False))
        self.assertTrue(hr.evaluate_record(out)["ok"])
        self.assertEqual(story.load_decisions(out, story.read_json(out / "evidence.json"))[0]["owners"], ["Rae"])

    # LESSON (2026-10-14): the team keeps "the consequential choices with their
    # reasons", yet a decision row had no place for one: a row with a bare
    # choice passed verify --strict, and brief printed the human's direction
    # as "why:". The reason is now a field of its own, reported when absent
    # and never required, so no historical row stops verifying.

    def unreasoned_input(self, **changes):
        entry = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        entry.pop("reason")
        entry.update(changes)
        path = self.root / "decision.json"
        story.write_json(path, entry)
        return path

    def test_a_stated_reason_is_kept_and_carried_into_the_draft_and_the_view(self):
        example = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        self.assertTrue(story.nonempty(example.get("reason")), "the example shows a reason")
        entry = self.decision()
        self.assertEqual(entry["reason"], example["reason"])
        self.cover()
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("0 of 1 decisions state no reason for the choice (reported, never required)", result["notes"])
        output = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Clarify the source note", out=str(output), execution_agent=None)
        self.assertIn(f"Reason: {example['reason']}", output.read_text(encoding="utf-8"))
        view = self.root / "view.json"
        self.call(story.cmd_view, records=[str(self.record)], out=str(view), json=True)
        data = story.read_json(view)
        self.assertEqual((data["decisions_recorded"], data["decisions_without_reason"]), (1, 0))

    def test_a_choice_recorded_without_a_reason_reads_not_stated_and_is_not_refused(self):
        for n, changes in enumerate(({}, {"reason": "   "}, {"reason": None}), 1):
            with self.subTest(changes=changes):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = story.cmd_decision(hr, argparse.Namespace(
                        record=str(self.record), input=str(self.unreasoned_input(**changes))))
                self.assertEqual(code, 0)
                self.assertIn("reason: not stated", output.getvalue())
                row = json.loads((self.record / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual((row["id"], row["reason"]), (f"D{n}", "not stated"))
        self.cover()
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("3 of 3 decisions state no reason for the choice (reported, never required)", result["notes"])

    def test_a_reason_that_is_present_must_say_something(self):
        with self.assertRaisesRegex(ValueError, "reason must contain text"):
            self.call(story.cmd_decision, record=str(self.record), input=str(self.unreasoned_input(reason=["a list"])))
        self.assertFalse((self.record / "decisions.jsonl").exists())
        # A stored row with an empty reason is a malformed row, reported by verify.
        entry = self.decision()
        entry["reason"] = ""
        (self.record / "decisions.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
        self.cover()
        result = hr.evaluate_record(self.record)
        self.assertFalse(result["ok"])
        self.assertTrue(any("reason must contain text" in p for p in result["problems"]), result["problems"])

    def test_the_allowed_decision_types_are_stated_where_a_user_reads(self):
        # LESSON (rc22, Stage 2): an unknown type was refused with "unknown
        # decision type", and the allowed values were written nowhere a user
        # read. The error and `decision --help` list them.
        path = self.root / "typed.json"
        entry = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        story.write_json(path, dict(entry, type="analysis"))
        with self.assertRaises(ValueError) as caught:
            self.call(story.cmd_decision, record=str(self.record), input=str(path))
        listed = ", ".join(story.DECISION_TYPE_ORDER)
        self.assertIn(f"unknown decision type 'analysis': type is one of {listed}", str(caught.exception))
        self.assertEqual(set(story.DECISION_TYPE_ORDER), story.DECISION_TYPES)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            hr.main(["decision", "--help"])
        flat = " ".join(out.getvalue().split())
        self.assertIn("is one of: " + listed, flat)

    def test_a_historical_row_without_a_reason_still_passes_strict_and_is_counted(self):
        # A row as the kit wrote it before 2026-10-14: no reason field at all.
        entry = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        entry.pop("reason")
        entry.update(id="D1", owners=["Rae"], owner_basis="input speakers",
                     recorded_at="2026-09-20T10:00:00+00:00")
        original = json.dumps(entry, ensure_ascii=False) + "\n"
        (self.record / "decisions.jsonl").write_text(original, encoding="utf-8")
        self.cover()
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("1 of 1 decisions state no reason for the choice (reported, never required)", result["notes"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hr.cmd_verify(argparse.Namespace(record=str(self.record), range=None, strict=True, json=False))
        self.assertEqual(code, 0, output.getvalue())
        message = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Clarify", out=str(message), execution_agent=None)
        self.assertIn("Reason: not stated", message.read_text(encoding="utf-8"))
        view = self.root / "view.json"
        self.assertEqual(self.call(story.cmd_view, records=[str(self.record)], out=str(view), json=True), 0)
        self.assertEqual(story.read_json(view)["decisions_without_reason"], 1)
        rendered = self.root / "view.md"
        self.call(story.cmd_view, records=[str(self.record)], out=str(rendered), json=False)
        self.assertIn("1 of 1 recorded decisions state no reason for the choice", rendered.read_text(encoding="utf-8"))
        # Nothing reads the row back with a reason it never had.
        self.assertEqual((self.record / "decisions.jsonl").read_text(encoding="utf-8"), original)

    # LESSON (2026-10-14): of 217 records with decisions, 193 wrote every
    # decision after the last human turn, from the agent's memory of the
    # session, and a reason stated mid-session was lost unless restated. A
    # decision is now recorded when the choice is made, refreshing the
    # extraction first when the directing turn is newer than the record.

    def test_a_decision_recorded_mid_session_cites_a_turn_a_refresh_adds(self):
        session = self.root / "session.jsonl"
        session.write_text(json.dumps({"type": "user", "message": {"content": "Recode income into brackets."}}) + "\n",
                           encoding="utf-8")
        stage = self.root / "stage"
        extract = ["extract", str(session), "--out", str(stage), "--record-id", "stage"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hr.main(extract + ["--speaker", "Rae"]), 0)
        self.record = stage
        first = self.decision(question="Which top bracket?", choice="150000",
                              reason="the smallest cut that keeps every household in the 2019 panel",
                              human_direction="Rae asked for income brackets.")
        account = (stage / "decisions.jsonl").read_text(encoding="utf-8")
        # The person speaks again while the work goes on.
        with session.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "Use 2019 as the base year."}}) + "\n")
        with self.assertRaisesRegex(ValueError, "run trace_record.py extract again into this staging directory"):
            self.decision(human_inputs=["T2"], human_direction="Rae fixed the base year.")
        # The refresh, run without --speaker as an agent would mid-session.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(hr.main(extract), 0)
        self.assertIn("keep the speaker the earlier extraction named: Rae", output.getvalue())
        evidence = story.read_json(stage / "evidence.json")
        self.assertEqual([(t["id"], t["speaker"]) for t in evidence["turns"]], [("T1", "Rae"), ("T2", "Rae")])
        self.assertEqual(evidence["consent"], {"own_turns_by": "Rae", "third_party_review": "PENDING"})
        self.assertEqual((stage / "decisions.jsonl").read_text(encoding="utf-8"), account, "the refresh leaves the account alone")
        second = self.decision(human_inputs=["T2"], human_direction="Rae fixed the base year.",
                               question="Which base year?", choice="2019", reason="Rae named it.")
        self.assertEqual((first["id"], second["id"], second["owners"]), ("D1", "D2", ["Rae"]))
        self.assertLessEqual(first["recorded_at"], second["recorded_at"])
        # The account is completed at the end, and the record verifies.
        evidence["consent"]["third_party_review"] = "cleared"
        story.write_json(stage / "evidence.json", evidence)
        (stage / "summary.md").write_text("[T1] Rae asked for income brackets. [T2] Rae fixed the base year.\n",
                                          encoding="utf-8")
        check = {"kind": "self-test", "runner": "test checker", "method": "read the brackets",
                 "result": "brackets as chosen", "artifact": "workspace/record-staging/brackets.csv",
                 "candidate": "the recode", "criterion": "the brackets match D1", "limits": "synthetic"}
        (stage / "checks.jsonl").write_text(json.dumps(check) + "\n", encoding="utf-8")
        self.cover()
        result = hr.evaluate_record(stage)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("2 evidence-linked decisions recorded", result["notes"])

    def test_a_refresh_from_a_session_whose_speaker_is_unknown_stays_unknown(self):
        # LESSON (SKR-01) still holds: nobody is named as a speaker by default.
        session = self.root / "session.jsonl"
        session.write_text(json.dumps({"type": "user", "message": {"content": "First."}}) + "\n", encoding="utf-8")
        stage = self.root / "stage"
        extract = ["extract", str(session), "--out", str(stage), "--record-id", "stage"]
        with contextlib.redirect_stdout(io.StringIO()):
            hr.main(extract)
        with session.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "Second."}}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.main(extract)
        evidence = story.read_json(stage / "evidence.json")
        self.assertEqual([t["speaker"] for t in evidence["turns"]], ["unknown", "unknown"])
        self.assertEqual(evidence["consent"]["own_turns_by"], "unknown")
        # A speaker named on the refresh is used for the turns it adds.
        with session.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "Third."}}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.main(extract + ["--speaker", "Rae"])
        self.assertEqual([t["speaker"] for t in story.read_json(stage / "evidence.json")["turns"]],
                         ["unknown", "unknown", "Rae"])

    def test_inputs_are_checked_when_present_and_carried_into_the_draft_and_the_view(self):
        example = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        self.assertTrue(example.get("inputs"), "the example names its inputs")
        for bad in ("IN-001", [], ["IN-001", " "], ["IN-001", "IN-001"]):
            with self.subTest(inputs=bad):
                with self.assertRaisesRegex(ValueError, "inputs"):
                    self.decision(inputs=bad)
        self.assertFalse((self.record / "decisions.jsonl").exists())
        entry = self.decision()
        self.assertEqual(entry["inputs"], example["inputs"])
        self.decision()  # the example again, as D2, made a row from before the field below
        rows =[json.loads(line) for line in (self.record / "decisions.jsonl").read_text().splitlines()]
        rows[1].pop("inputs")
        (self.record / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.cover()
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("1 of 2 decisions name their inputs (optional)", result["notes"])
        output = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Clarify", out=str(output), execution_agent=None)
        message = output.read_text(encoding="utf-8")
        self.assertIn("Inputs: " + ", ".join(example["inputs"]), message)
        self.assertIn("Inputs: not stated", message)
        view = self.root / "view.json"
        self.call(story.cmd_view, records=[str(self.record)], out=str(view), json=True)
        self.assertEqual(story.read_json(view)["decisions_naming_inputs"], 1)
        rendered = self.root / "view.md"
        self.call(story.cmd_view, records=[str(self.record)], out=str(rendered), json=False)
        text = rendered.read_text(encoding="utf-8")
        self.assertIn("1 of 2 recorded decisions name the inputs the choice used", text)
        self.assertIn("- Inputs: " + ", ".join(example["inputs"]), text)

    def test_a_historical_row_without_inputs_verifies_and_reads_not_stated(self):
        entry = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        for field in ("reason", "inputs"):
            entry.pop(field)
        entry.update(id="D1", owners=["Rae"], owner_basis="input speakers", recorded_at="2026-09-20T10:00:00+00:00")
        original = json.dumps(entry, ensure_ascii=False) + "\n"
        (self.record / "decisions.jsonl").write_text(original, encoding="utf-8")
        self.cover()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hr.cmd_verify(argparse.Namespace(record=str(self.record), range=None, strict=True, json=False))
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("0 of 1 decisions name their inputs (optional)", output.getvalue())
        message = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Clarify", out=str(message), execution_agent=None)
        self.assertIn("Inputs: not stated", message.read_text(encoding="utf-8"))
        self.assertEqual((self.record / "decisions.jsonl").read_text(encoding="utf-8"), original)


class DecisionObjectsInTheRepository(unittest.TestCase):
    """`verify` says where a decision's inputs and resulting work lead.

    LESSON (2026-10-14): track records keeps "the inputs" and "the resulting
    versions", yet a decision's artifacts were bare paths nobody checked and
    a row could not name what the choice used. Both are now reported, as
    information: a file moved since, and every row written before, verifies.
    """

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.saved = os.getcwd()
        os.chdir(self.root)
        for args in (["init", "-q", "-b", "master"], ["config", "user.name", "Temp Person"],
                     ["config", "user.email", "temp@example.test"], ["config", "commit.gpgsign", "false"]):
            self.git(*args)
        self.record = Path("ai/records/20260101-recode")
        self.record.mkdir(parents=True)
        Path("ai/records/20251201-earlier").mkdir(parents=True)
        Path("sources").mkdir()
        Path("sources/intake.jsonl").write_text(json.dumps({"id": "IN-001", "what": "survey"}) + "\n"
                                                + "not json\n", encoding="utf-8")
        story.write_json(self.record / "evidence.json", {
            "record_id": "20260101-recode", "consent": {"third_party_review": "cleared"},
            "execution_agents": [{"model": "example-model"}],
            "turns": [{"id": "T1", "speaker": "Rae", "text": "Recode income into brackets."}]})
        (self.record / "summary.md").write_text("[T1] Rae asked for income brackets.\n", encoding="utf-8")
        (self.record / "checks.jsonl").write_text(json.dumps({"kind": "self-test", "runner": "test checker",
            "method": "read", "result": "fine", "artifact": "summary.md"}) + "\n", encoding="utf-8")

    def tearDown(self):
        os.chdir(self.saved)
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    def write_decision(self, **changes):
        entry = json.loads((ROOT / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        entry.update(id="D1", owners=["Rae"], owner_basis="input speakers",
                     recorded_at="2026-01-01T10:00:00+00:00", **changes)
        (self.record / "decisions.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_cover(argparse.Namespace(record=str(self.record)))

    def test_artifacts_and_inputs_are_found_where_they_are_and_absence_is_information(self):
        Path("code").mkdir()
        Path("code/recode.py").write_text("print('v1')\n", encoding="utf-8")
        Path("code/brackets.csv").write_text("150000\n", encoding="utf-8")
        Path("data.csv").write_text("1\n", encoding="utf-8")
        self.write_decision(artifacts=["code/recode.py", "code/brackets.csv", "code/never-written.py",
                                       "https://example.invalid/run"],
                            inputs=["IN-001", "IN-009", "ai/records/20251201-earlier", "data.csv",
                                    "workspace/shared/survey.sav", "ai/records/20250101-gone"])
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Recode income\n\nRecord: ai/records/20260101-recode")
        # The bracket table is moved after the record's commit; the version the
        # record produced is still at that commit.
        self.git("rm", "-q", "code/brackets.csv")
        self.git("commit", "-q", "-m", "Remove the table")
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("decision artifacts: 4 named, 1 in the tree, 1 at the record's commits, 1 declared, "
                      "0 unavailable, 1 not found (reported, never required)", result["notes"])
        self.assertIn("decision inputs: 6 named, 1 in the tree, 0 at the record's commits, 1 intake row, "
                      "1 earlier record, 0 declared, 1 unavailable, 2 not found (reported, never required)",
                      result["notes"])
        lost = next(n for n in result["notes"] if n.startswith("decision artifacts or inputs not found"))
        for name in ("code/never-written.py", "IN-009", "ai/records/20250101-gone"):
            self.assertIn(name, lost)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hr.cmd_verify(argparse.Namespace(record=str(self.record), range=None, strict=True, json=False))
        self.assertEqual(code, 0, output.getvalue())

    def test_a_record_no_commit_names_looks_in_the_tree_only(self):
        self.write_decision(artifacts=["code/later.py"])
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("decision artifacts: 1 named, 0 in the tree, 0 at the record's commits, 0 declared, "
                      "0 unavailable, 1 not found (reported, never required)", result["notes"])


class ReviewInboxReadsEveryTrackedMarker(unittest.TestCase):
    """A marker the inbox does not collect is a human instruction nobody reads."""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=self.root, check=True, capture_output=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")

    def track(self):
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)

    def inbox(self, *args, flags=(), env=None):
        return subprocess.run([sys.executable, *flags, str(ROOT / "scripts" / "review_inbox.py"), *args],
                              cwd=self.root, capture_output=True, env=env)

    def test_markers_under_dot_names_build_prefixes_and_non_ascii_names_are_collected(self):
        # LESSON (AH-179): skip directories were matched as string prefixes, so
        # `.git` swallowed `.github/`, `.gitignore` and `.gitattributes` and `build`
        # swallowed `buildnotes.md`; and `git ls-files` quotes a non-ASCII name,
        # so the quoted path did not exist and the file was skipped. `check` then
        # said the inbox was clear.
        self.write(".github/workflows/ci.yml", "# REVIEW(Ana): run this on the release branch too\n")
        self.write(".gitignore", "# REVIEW(Ben): say why scratch is ignored\n*.tmp\n")
        self.write(".gitattributes", "# REVIEW(Cy): is cr-at-eol still needed\n*.tex text\n")
        self.write("buildnotes.md", "<!-- REVIEW(Di): date this note -->\n")
        self.write("sources/memos/résumé-notes.md", "<!-- REVIEW(Eve): check the second date -->\n")
        self.write("notes.md", "<!-- REVIEW(Fay): an ordinary file -->\n")
        # Whole skipped components, which stay skipped.
        self.write("build/out.md", "<!-- REVIEW(Gus): generated -->\n")
        self.write("paper/build/x.md", "<!-- REVIEW(Hal): generated -->\n")
        self.write("workspace/record-staging/packet.md", "<!-- REVIEW(Ida): staging -->\n")
        self.write("node_modules/pkg/x.js", "// REVIEW(Jo): vendored\n")
        self.write("untracked.md", "<!-- REVIEW(Kit): not tracked -->\n")
        self.track()
        subprocess.run(["git", "rm", "-q", "--cached", "untracked.md"], cwd=self.root, check=True)
        found = {(i["file"], i["who"]) for i in inbox.scan_inline(self.root)}
        self.assertEqual(found, {(".github/workflows/ci.yml", "Ana"), (".gitignore", "Ben"),
                                 (".gitattributes", "Cy"), ("buildnotes.md", "Di"),
                                 ("sources/memos/résumé-notes.md", "Eve"), ("notes.md", "Fay")})

    def test_check_json_returns_what_check_returns(self):
        # LESSON (AH-195): `check --json` returned 0 with items open, while
        # `check` returned 1 for the same tree.
        self.write("notes.md", "Nothing to review.\n")
        self.track()
        self.assertEqual(self.inbox("check", "--json").returncode, 0)
        self.write("notes.md", "<!-- REVIEW(Fay): tighten this -->\n")
        self.track()
        self.assertEqual(self.inbox("check").returncode, 1)
        checked = self.inbox("check", "--json")
        self.assertEqual(checked.returncode, 1)
        self.assertEqual(len(json.loads(checked.stdout.decode("utf-8"))["items"]), 1)
        self.assertEqual(self.inbox("collect", "--json").returncode, 0, "collect still succeeds")

    def test_the_inbox_states_the_route_from_a_diagnosed_defect_to_the_ledger(self):
        # LESSON (2026-10-14 review): a REVIEW marker that diagnosed a defect
        # had no stated route to the defect ledger, so its correction was never
        # rechecked against it.
        self.write("notes.md", "<!-- REVIEW(Fay): this table divides by the wrong count -->\n")
        self.track()
        self.assertEqual(self.inbox("collect").returncode, 0)
        written = (self.root / "ai" / "review-inbox.md").read_text(encoding="utf-8")
        self.assertIn("diagnoses a defect", written)
        self.assertIn("scripts/defect_ledger.py open", written)
        self.assertEqual(len(inbox.scan_inline(self.root)), 1, "the stated route is not itself a marker")

    def latin1_environment(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LC_", "PYTHON")) and k != "LANG"}
        env["LC_ALL"] = "en_US.ISO8859-1"
        probe = subprocess.run([sys.executable, "-X", "utf8=0", "-c",
                                "import locale; print(locale.getpreferredencoding(False))"],
                               env=env, capture_output=True, text=True)
        try:
            name = codecs.lookup(probe.stdout.strip()).name
        except LookupError:
            name = "utf-8"
        if name == "utf-8":
            self.skipTest("no locale with a non-UTF-8 encoding is available here")
        return env

    def test_words_are_read_as_utf8_whatever_the_locale(self):
        # LESSON (AH-193): files and git output were decoded with the locale's
        # encoding, so under Latin-1 the packet `trace_record.py comments`
        # captures as verbatim carried altered words.
        env = self.latin1_environment()
        self.write("notes.md", "<!-- REVIEW(Zoë): cite Müller (2011) here, voilà -->\n")
        self.track()
        packet = self.inbox("collect", "--json", flags=("-X", "utf8=0"), env=env)
        self.assertEqual(packet.returncode, 0, packet.stderr)
        item = json.loads(packet.stdout.decode("utf-8"))["items"][0]
        self.assertEqual((item["who"], item["text"]), ("Zoë", "cite Müller (2011) here, voilà"))
        self.assertEqual(item["source_id"], inbox.scan_inline(self.root)[0]["source_id"])
        self.assertEqual(self.inbox("collect", flags=("-X", "utf8=0"), env=env).returncode, 0)
        written = (self.root / "ai" / "review-inbox.md").read_bytes().decode("utf-8")
        self.assertIn("cite Müller (2011) here, voilà", written)

    def test_a_file_that_is_not_utf8_is_named_not_passed_off_as_words(self):
        # LESSON (AH-193): an undecodable file was read with replacement
        # characters and its marker reported as the reviewer's verbatim words.
        self.write("latin.md", "<!-- REVIEW(Zoë): cite Müller here -->\n".encode("latin-1"))
        self.write("blob.dat", b"\x89\xff\xfe\x00 no marker here\n")
        self.track()
        packet = json.loads(self.inbox("collect", "--json").stdout.decode("utf-8"))
        self.assertEqual(packet["items"], [])
        self.assertEqual([u["file"] for u in packet["unreadable"]], ["latin.md"])
        checked = self.inbox("check")
        self.assertEqual(checked.returncode, 1, "an unread marker is not a clear inbox")
        self.assertIn("latin.md", checked.stdout.decode("utf-8"))
        self.assertEqual(self.inbox("collect").returncode, 0)
        self.assertIn("latin.md", (self.root / "ai" / "review-inbox.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
