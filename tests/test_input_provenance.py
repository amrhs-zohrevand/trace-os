"""Synthetic input/metadata disclosure regressions; no private data or network."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import trace_record as hr
import trace_story as story
import input_provenance as ip
import record_privacy as privacy


class InputProvenance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.record = self.root / "staging"
        self.record.mkdir()
        self.words = 'From Smith: "Selection matters." My idea: compare two groups. Add that comparison.'
        self.evidence = {"record_id": "synthetic", "consent": {"third_party_review": "cleared"},
                         "execution_agents": [{"model": "synthetic-agent"}],
                         "turns": [{"id": "T1", "speaker": "Rae", "text": self.words}]}
        self.write_evidence()
        (self.record / "summary.md").write_text("T1 supplied Smith's excerpt and a comparison idea.\n", encoding="utf-8")
        (self.record / "checks.jsonl").write_text(json.dumps({"kind": "self-test", "runner": "checker", "method": "compare sources", "result": "pass", "artifact": "synthetic-note"}) + "\n", encoding="utf-8")
        self.cover()

    def tearDown(self):
        self.temp.cleanup()

    def write_evidence(self):
        story.write_json(self.record / "evidence.json", self.evidence)

    def call(self, func, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return func(hr, argparse.Namespace(**kwargs))

    def cover(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_cover(argparse.Namespace(record=str(self.record)))

    def review(self):
        self.call(privacy.cmd_publication, record=str(self.record), by="synthetic-checker", scope="synthetic test record")

    def annotate(self, segments=None):
        if segments is None:
            segments = [
                {"quote": "Selection matters.", "role": "material", "origin": "external", "basis": "explicit", "basis_refs": ["T1"], "source": "Smith"},
                {"quote": "compare two groups", "role": "idea", "origin": "speaker", "basis": "explicit", "basis_refs": ["T1"]},
                {"quote": "Add that comparison.", "role": "instruction"}]
        packet = {"turn": "T1", "text_sha256": ip.text_hash(self.evidence["turns"][0]["text"]), "segments": segments}
        path = self.root / "packet.json"
        story.write_json(path, packet)
        self.call(ip.cmd_annotate, record=str(self.record), input=str(path), by="synthetic-annotator")
        return story.read_json(self.record / "evidence.json")

    def test_mixed_input_preserves_words_speaker_and_separates_origin_from_role(self):
        e = self.annotate()
        t = e["turns"][0]
        self.assertEqual(t["text"], self.words)
        self.assertEqual(t["speaker"], "Rae")
        self.assertEqual([s["role"] for s in t["annotation"]["segments"]], ["material", "idea", "instruction"])
        self.assertEqual([s["origin"] for s in t["annotation"]["segments"]], ["external", "speaker", "unknown"])
        self.assertFalse(any("quote" in s for s in t["annotation"]["segments"]))
        self.assertEqual(e["consent"]["third_party_review"], "PENDING")
        self.assertEqual(story.owner_names(e["turns"], ["T1"]), ["Rae"])

    def test_style_or_pasting_alone_cannot_establish_speaker_origin(self):
        with self.assertRaisesRegex(ValueError, "explicit self-attribution"):
            self.annotate([{"quote": "Selection matters.", "role": "idea", "origin": "speaker", "basis": "inferred", "basis_refs": ["T1"]}])
        e = self.annotate([{"quote": "Selection matters.", "role": "material"}])
        self.assertEqual(e["turns"][0]["annotation"]["segments"][0]["origin"], "unknown")

    def test_ambiguous_unicode_quote_needs_offsets_and_offsets_use_codepoints(self):
        self.evidence["turns"][0]["text"] = "😀 café; café"
        self.write_evidence()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.annotate([{"quote": "café", "role": "material"}])
        e = self.annotate([{"quote": "café", "start": 8, "end": 12, "role": "material"}])
        s = e["turns"][0]["annotation"]["segments"][0]
        self.assertEqual(e["turns"][0]["text"][s["start"]:s["end"]], "café")

    def test_overlap_absent_evidence_and_unsupported_labels_fail_before_write(self):
        before = (self.record / "evidence.json").read_bytes()
        cases = [
            [{"quote": self.words, "role": "material"}, {"quote": "Selection matters.", "role": "idea"}],
            [{"quote": "Selection matters.", "role": "material", "origin": "external", "basis": "explicit", "basis_refs": ["T9"]}],
            [{"quote": "Selection matters.", "role": "original_author"}]]
        for segments in cases:
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                self.annotate(segments)
            self.assertEqual(before, (self.record / "evidence.json").read_bytes())

    def test_redaction_invalidates_span_annotation_even_after_new_coverage(self):
        e = self.annotate()
        e["turns"][0]["text"] = "[REDACTED: private source]"
        e["consent"]["third_party_review"] = "cleared"
        story.write_json(self.record / "evidence.json", e)
        self.cover()
        self.assertTrue(any("re-annotate" in p for p in hr.evaluate_record(self.record)["problems"]))

    def test_refresh_preserves_existing_annotations_and_unclear_history_stays_unknown(self):
        self.assertEqual(ip.input_account(self.evidence)["unannotated_turns"], 1)
        session = self.root / "session.jsonl"
        session.write_text(json.dumps({"type": "user", "message": {"content": self.words}}) + "\n", encoding="utf-8")
        self.record = self.root / "refresh"
        args = argparse.Namespace(session=str(session), out=str(self.record), record_id="synthetic", speaker="Rae")
        with contextlib.redirect_stdout(io.StringIO()): hr.cmd_extract(args)
        e = self.annotate()
        with contextlib.redirect_stdout(io.StringIO()): hr.cmd_extract(args)
        self.assertEqual(e["turns"], story.read_json(self.record / "evidence.json")["turns"])

    def test_draft_and_overview_include_passage_account_without_copying_passages(self):
        e = self.annotate()
        e["consent"]["third_party_review"] = "cleared"
        story.write_json(self.record / "evidence.json", e)
        self.cover()
        self.review()
        output = self.root / "message.txt"
        self.call(story.cmd_message, record=str(self.record), subject="Compare groups", out=str(output), execution_agent=None)
        text = output.read_text(encoding="utf-8")
        for item in ("material; origin external", "idea; origin speaker", "instruction; origin unknown"):
            self.assertIn(item, text)
        self.assertNotIn("Selection matters.", text)
        self.call(story.cmd_view, records=[str(self.record)], out=str(self.root / "overview.json"), json=True)
        d = story.read_json(self.root / "overview.json")
        self.assertEqual(len(d["accounts"][0]["input_passages"]["annotated_turns"]), 1)
        self.assertNotIn(self.words, json.dumps(d))

    def test_changed_check_or_annotation_invalidates_publication_and_draft(self):
        self.review()
        p = self.record / "checks.jsonl"
        check = json.loads(p.read_text()); check["result"] = "different result"
        p.write_text(json.dumps(check) + "\n")
        self.assertFalse(hr.evaluate_record(self.record)["ok"])
        self.cover()
        self.assertTrue(privacy.publication_problems(self.record))
        with self.assertRaises(ValueError):
            self.call(story.cmd_message, record=str(self.record), subject="Update", out=str(self.root / "message"), execution_agent=None)
        self.review()
        self.assertTrue(hr.evaluate_record(self.record)["ok"])

    def test_receipt_survives_crlf_and_promotes_the_exact_reviewed_members(self):
        self.review()
        for p in self.record.iterdir():
            p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))  # CRLF whatever the platform wrote
        dest = self.root / "promoted"
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_promote(argparse.Namespace(staging=str(self.record), dest=str(dest), force=False))
        self.assertTrue(hr.evaluate_record(dest)["ok"])
        self.assertEqual(privacy.file_hashes(self.record), privacy.file_hashes(dest))

    def test_comment_metadata_is_allowlisted_and_a_redacted_text_cannot_hide_known_secret(self):
        token = "ghp_" + "Q" * 36
        path = self.root / "comments.json"
        story.write_json(path, {"items": [{"who": "Rae", "text": "[REDACTED: token]", "source": "PR #1", "source_id": "c1", "raw_line": token, "extra_context": "private context"}]})
        self.call(story.cmd_comments, record=str(self.record), input=str(path), speaker=None)
        e = story.read_json(self.record / "evidence.json")
        self.assertNotIn("extra_context", e["turns"][-1]["source"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = privacy.cmd_privacy(hr, argparse.Namespace(record=str(self.record)))
        self.assertEqual(status, 1)
        self.assertNotIn(token, output.getvalue())
        self.assertIn("provider-token", output.getvalue())

    def test_escaped_secret_in_metadata_is_detected_without_printing_it(self):
        token = "ghp_" + "Z" * 36
        self.evidence["turns"][0]["source"] = {"url": token}
        self.write_evidence()
        p = self.record / "evidence.json"
        p.write_text(p.read_text().replace("ghp_", "\\u0067hp_"))
        with self.assertRaises(ValueError) as error:
            privacy.require_screened(self.record)
        self.assertNotIn(token, str(error.exception))

    def test_promotion_without_content_review_receipt_does_not_create_destination(self):
        dest = self.root / "promoted"
        with self.assertRaisesRegex(SystemExit, "publication review"):
            hr.cmd_promote(argparse.Namespace(staging=str(self.record), dest=str(dest), force=False))
        self.assertFalse(dest.exists())

    def test_capture_inside_git_requires_ignored_and_untracked_destination(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("private/\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ignored, untracked"):
            privacy.private_capture_target(self.root / "ai/records/new")
        privacy.private_capture_target(self.root / "private/record")
        tracked = self.root / "private/tracked/evidence.json"
        tracked.parent.mkdir(); tracked.write_text("{}")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", str(tracked)], check=True)
        with self.assertRaises(ValueError): privacy.private_capture_target(tracked.parent)

    def test_record_symlink_cannot_read_or_overwrite_an_unrelated_file(self):
        p = self.record / "summary.md"
        p.unlink()
        unrelated = self.root / "private.txt"
        unrelated.write_text("private test material")
        try: p.symlink_to(unrelated)
        except OSError: self.skipTest("symlink creation unavailable on this runner")
        with self.assertRaises(ValueError): privacy.scan_record(self.record)
        self.assertEqual(unrelated.read_text(), "private test material")

    def test_known_sensitive_payload_is_rejected_before_external_transport(self):
        try: import review_panel
        except ImportError: self.skipTest("research-only external review client not exported")
        value = "https://example.invalid/?access_token=" + "synthetic-private-value"
        with mock.patch.object(review_panel.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "sensitive content"):
                review_panel.ask({"model": "synthetic"}, value, "", 1.0, "unused")
            network.assert_not_called()

    def test_comment_capture_keeps_the_pr_commit_anchor(self):
        path = self.root / "comments.json"
        story.write_json(path, {"items": [{"who": "Sam", "text": "Check the comparison", "source": "PR #1", "source_id": "repo:comment:1", "commit_id": "a" * 40}]})
        self.call(story.cmd_comments, record=str(self.record), input=str(path), speaker=None)
        self.assertEqual(story.read_json(self.record / "evidence.json")["turns"][-1]["source"]["commit_id"], "a" * 40)

    def test_concurrent_source_edit_cannot_enter_after_promotion_screening(self):
        self.review()
        before = (self.record / "summary.md").read_text()
        screen = privacy.require_screened
        def change_source_after_check(snapshot):
            screen(snapshot)
            (self.record / "summary.md").write_text("ghp_" + "P" * 36)
        dest = self.root / "promoted"
        with mock.patch.object(privacy, "require_screened", side_effect=change_source_after_check), contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_promote(argparse.Namespace(staging=str(self.record), dest=str(dest), force=False))
        self.assertEqual((dest / "summary.md").read_text(), before)
        self.assertTrue(hr.evaluate_record(dest)["ok"])

    def test_dubious_git_ownership_is_not_treated_as_outside_git(self):
        # Git refuses a repository owned by another user with exit 128, the same
        # code it returns outside any repository. A real refusal cannot be forced
        # portably: GIT_TEST_ASSUME_DIFFERENT_OWNER is ignored wherever
        # safe.directory is set globally, as on GitHub-hosted runners, so the
        # refusal is produced here and the .git directory is real.
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        real_run = privacy.subprocess.run
        def refuse_rev_parse(args, **kwargs):
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership in repository\n")
            return real_run(args, **kwargs)
        with mock.patch.object(privacy.subprocess, "run", side_effect=refuse_rev_parse):
            with self.assertRaisesRegex(ValueError, "Git state"):
                privacy.private_capture_target(self.root / "ai/records/unsafe")

    def test_annotation_cannot_write_into_a_tracked_record(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "staging"], check=True)
        before = (self.record / "evidence.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "ignored, untracked"):
            self.annotate()
        self.assertEqual((self.record / "evidence.json").read_bytes(), before)

    def test_no_passage_boilerplate_in_an_unannotated_commit(self):
        out = self.root / "message"
        self.call(story.cmd_message, record=str(self.record), subject="Ordinary edit", out=str(out), execution_agent=None)
        self.assertNotIn("Input passages", out.read_text())

    def test_missing_speaker_kind_is_not_invented_by_the_annotation_renderer(self):
        e = self.annotate()
        self.assertEqual(ip.input_account(e)["annotated_turns"][0]["speaker_kind"], "unknown")

    def test_legacy_ownership_convention_is_separate_from_origin_and_explicit_kind(self):
        e = self.annotate()
        self.assertEqual(story.owner_names(e["turns"], ["T1"]), ["Rae"])
        for kind in ("agent", "unknown"):
            e["turns"][0]["speaker_kind"] = kind
            self.assertEqual(story.owner_names(e["turns"], ["T1"]), [])

    def test_concurrent_check_edit_cannot_enter_the_commit_after_inspection(self):
        self.review()
        screen = privacy.require_screened
        def mutate_after_screen(snapshot):
            screen(snapshot)
            p = self.record / "checks.jsonl"
            d = json.loads(p.read_text());d["result"] = "UNREVIEWED_PRIVATE_CONTEXT"
            p.write_text(json.dumps(d) + "\n")
        out = self.root / "message"
        with mock.patch.object(privacy, "require_screened", side_effect=mutate_after_screen):
            self.call(story.cmd_message, record=str(self.record), subject="Ordinary edit", out=str(out), execution_agent=None)
        self.assertNotIn("UNREVIEWED_PRIVATE_CONTEXT", out.read_text())

    def test_clustering_and_ledger_matching_screen_before_transport(self):
        try:
            import deep_review
            import defect_ledger
        except ImportError: self.skipTest("research-only external clients not exported")
        token = "ghp_" + "V" * 36
        with mock.patch.object(deep_review.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "sensitive content"):
                deep_review.cluster([{"n": 0, "claim": token}], "unused")
            with self.assertRaisesRegex(ValueError, "sensitive content"):
                defect_ledger.match([{"id": "D1", "label": token}], [{"label": "new finding"}], "unused")
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
