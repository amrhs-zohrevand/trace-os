#!/usr/bin/env python3
"""The meeting-and-idea-intake method must bring a meeting's direction into the
record verbatim, attributed and pending consent, and never guess.

The helper, scripts/meeting_quotes.py, turns quotes into a `comments` packet:
a quote found once keeps its line, speaker and the file's digest; a quote not
found, or found twice, is left out and reported. The end-to-end test runs that
packet through trace_record.py comments and decision in a temporary Git
repository, where capture must land in ignored staging, and checks that the
meeting's speakers become the owners while an unattributed line names nobody
and an AI summary line never becomes a human turn. The last test keeps the
method portable: no product-bound acquisition, machine paths or addresses.

Every transcript here is synthetic; the speakers are Speaker A and Speaker B.
Run:  python3 -X utf8 -I -S -m unittest discover -s tests
stdlib only; Git is needed for the end-to-end test only.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import meeting_quotes as mq  # noqa: E402

TEXT = ("Speaker A: We run the pilot on the first cohort.\n"
        "[00:04:12] Speaker B: I will draft the survey this week.\n"
        "and the survey goes out after the pilot\n")
VTT = ("WEBVTT\n\nNOTE synthetic\n\n1\n00:00:01.000 --> 00:00:04.000\n"
       "<v Speaker A>Split the sample\nby year.</v>\n\n"
       "2\n00:00:05.000 --> 00:00:07.000\nSpeaker B: Keep the   older wave too.\n")


class Helper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def run_helper(self, name, text, *quotes):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        argv = [str(path), "--source", "Synthetic meeting, 2026-01-15"]
        for quote in quotes:
            argv += ["--quote", quote]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mq.main(argv)
        return code, json.loads(out.getvalue())["items"], err.getvalue(), path

    def test_a_unique_quote_keeps_its_line_speaker_source_and_digest(self):
        code, items, err, path = self.run_helper("m.txt", TEXT, "the pilot on the first cohort")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["text"], "the pilot on the first cohort")
        self.assertEqual((item["who"], item["line"], item["kind"]), ("Speaker A", 1, "line"))
        self.assertEqual(item["source"], "Synthetic meeting, 2026-01-15")
        self.assertEqual(item["file_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(item["source_id"].endswith(":1"))
        self.assertFalse(pathlib.PurePath(item["file"]).is_absolute())

    def test_a_quote_not_found_verbatim_is_reported_and_left_out(self):
        code, items, err, _ = self.run_helper("m.txt", TEXT, "the pilot on the second cohort",
                                              "We run the pilot")
        self.assertEqual(code, 1)
        self.assertEqual([i["text"] for i in items], ["We run the pilot"])
        self.assertIn("not found verbatim", err)
        # Case is not loosened: a near miss is not a match.
        self.assertEqual(self.run_helper("m.txt", TEXT, "we run the pilot")[1], [])

    def test_a_quote_found_twice_is_reported_with_its_lines_and_left_out(self):
        code, items, err, _ = self.run_helper("m.txt", TEXT, "the survey")
        self.assertEqual((code, items), (1, []))
        self.assertIn("found 2 times, at lines 2, 3", err)

    def test_a_vtt_cue_gives_its_voice_or_label_speaker_and_first_text_line(self):
        code, items, err, _ = self.run_helper("m.vtt", VTT, "Split the sample by year.",
                                              "Keep the older wave too.")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(i["who"], i["line"], i["kind"]) for i in items],
                         [("Speaker A", 7, "cue"), ("Speaker B", 12, "cue")])

    def test_each_voice_of_a_cue_owns_its_own_words(self):
        two = ("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
               "<v Speaker A>Use the first design.</v>\n<v Speaker B>Use the second design.</v>\n")
        code, items, err, _ = self.run_helper("m.vtt", two, "Use the second design.",
                                              "Use the first design. Use the second design.")
        self.assertEqual(code, 1)
        self.assertEqual([(i["who"], i["text"]) for i in items], [("Speaker B", "Use the second design.")])
        self.assertIn("not found verbatim", err)

    def test_each_label_line_of_a_cue_owns_its_own_words(self):
        # LESSON (AH-184): a cue with two `Name:` lines was joined and read as
        # one label, so the second speaker's words went to the first. Each
        # label line now opens its own turn, a line without a label continues
        # the turn above it, and the cue keeps its first-text-line anchor.
        two = ("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
               "Speaker A: Use the first design.\nSpeaker B: Use the second design.\n\n"
               "00:00:06.000 --> 00:00:09.000\nSpeaker A: Keep the older wave\nand drop the pilot.\n"
               "Speaker B: Agreed.\n")
        code, items, err, _ = self.run_helper("m.vtt", two, "Use the second design.", "Use the first design.",
                                              "Keep the older wave and drop the pilot.", "Agreed.")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(i["who"], i["line"]) for i in items],
                         [("Speaker B", 4), ("Speaker A", 4), ("Speaker A", 8), ("Speaker B", 8)])
        code, items, err, _ = self.run_helper("m.vtt", two, "Use the first design. Speaker B: Use the second design.",
                                              "drop the pilot. Speaker B: Agreed.")
        self.assertEqual((code, items), (1, []))
        self.assertEqual(err.count("not found verbatim"), 2)

    def test_a_label_before_a_voice_keeps_its_continuation(self):
        # LESSON (AH-184): splitting at label lines must not cut a labelled
        # turn from the unlabelled words that run into a voice tag.
        cue = ("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
               "Speaker A: we should\nsplit the sample <v Speaker B>No, keep it whole.</v>\n")
        _, items, err, _ = self.run_helper("m.vtt", cue, "we should split the sample", "No, keep it whole.")
        self.assertEqual(err, "")
        self.assertEqual([i["who"] for i in items], ["Speaker A", "Speaker B"])

    def test_line_numbers_count_only_carriage_returns_and_line_feeds(self):
        # LESSON (AH-194): str.splitlines also breaks at a form feed, U+2028 and
        # the like, so every later line number drifted and a continuation lost
        # its speaker. Only CRLF, LF and CR end a line; the others are spaces.
        text = "Speaker A: first part\x0cstill Speaker A and more\nSpeaker B: the quoted words\rSpeaker C: last\n"
        code, items, err, _ = self.run_helper("m.txt", text, "still Speaker A and more", "the quoted words", "last")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(i["who"], i["line"]) for i in items],
                         [("Speaker A", 1), ("Speaker B", 2), ("Speaker C", 3)])
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nSpeaker A: one two\x0bthree\n\n"
               "00:00:03.000 --> 00:00:04.000\r\nSpeaker B: target words\r\n")
        code, items, err, _ = self.run_helper("m.vtt", vtt, "one two three", "target words")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(i["who"], i["line"]) for i in items], [("Speaker A", 4), ("Speaker B", 7)])

    def test_overlapping_occurrences_count_as_more_than_one(self):
        code, items, err, _ = self.run_helper("m.txt", "Speaker A: no no no\n", "no no")
        self.assertEqual((code, items), (1, []))
        self.assertIn("found 2 times", err)

    def test_a_text_line_reads_its_label_after_a_time_stamp_and_none_without_one(self):
        _, items, _, _ = self.run_helper("m.txt", TEXT, "I will draft the survey",
                                         "goes out after the pilot")
        self.assertEqual([(i["who"], i["line"]) for i in items],
                         [("Speaker B", 2), ("unattributed", 3)])

    def test_help_runs_without_site_packages(self):
        r = subprocess.run([sys.executable, "-X", "utf8", "-I", "-S", str(SCRIPTS / "meeting_quotes.py"),
                            "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[:200])
        self.assertIn("usage:", r.stdout)


@unittest.skipUnless(shutil.which("git"), "Git is not available")
class MeetingDirectionIntoTheRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("workspace/record-staging/\n", encoding="utf-8")
        self.staging = self.root / "workspace" / "record-staging"
        self.staging.mkdir(parents=True)

    def call(self, script, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=self.root,
                              capture_output=True, text=True, timeout=120)

    def test_speakers_own_the_decision_and_the_summary_line_is_not_a_human_turn(self):
        (self.staging / "meeting.txt").write_text(TEXT, encoding="utf-8")
        made = self.call("meeting_quotes.py", "workspace/record-staging/meeting.txt",
                        "--source", "Synthetic meeting, 2026-01-15",
                        "--quote", "We run the pilot on the first cohort.",
                        "--quote", "I will draft the survey this week.",
                        "--quote", "the survey goes out after the pilot")
        self.assertEqual(made.returncode, 0, made.stderr)
        packet = json.loads(made.stdout)
        self.assertEqual(packet["items"][0]["file"], "workspace/record-staging/meeting.txt")
        summary = "Summary: the team agreed to run the pilot first."
        packet["items"].append({"text": summary, "who": "meeting assistant", "speaker_kind": "agent",
                                "source": "AI summary of the synthetic meeting", "source_id": "summary:1"})
        (self.staging / "packet.json").write_text(json.dumps(packet), encoding="utf-8")

        record = "workspace/record-staging/session"
        captured = self.call("trace_record.py", "comments", record,
                            "--input", "workspace/record-staging/packet.json")
        self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
        self.assertIn("1 nonhuman comment(s)", captured.stdout)
        evidence = json.loads((self.root / record / "evidence.json").read_text(encoding="utf-8"))
        turns = evidence["turns"]
        self.assertEqual([t["speaker"] for t in turns], ["Speaker A", "Speaker B", "unattributed"])
        self.assertEqual([t["source"]["line"] for t in turns], [1, 2, 3])
        for turn in turns:
            self.assertEqual(turn["speaker_kind"], "human")
            self.assertEqual(turn["source"]["source"], "Synthetic meeting, 2026-01-15")
            self.assertEqual(turn["source"]["file"], "workspace/record-staging/meeting.txt")
            self.assertRegex(turn["source"]["file_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(summary, [t["text"] for t in turns])
        self.assertEqual(evidence["consent"]["third_party_review"], "PENDING")

        decision = {"question": "Which cohort does the pilot use?", "type": "direction",
                    "status": "selected", "human_inputs": ["T1", "T2", "T3"],
                    "human_direction": "Speaker A set the pilot's cohort and Speaker B took the survey.",
                    "proposed_by": {"name": "Speaker A", "kind": "human"},
                    "selected_by": {"name": "Speaker A", "kind": "human"},
                    "choice": "Run the pilot on the first cohort.",
                    "authority": "The meeting's decision, T1 to T3.",
                    "artifacts": ["workspace/sandbox/pilot-plan.md"], "evidence": ["evidence.json#T1"]}
        (self.staging / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
        decided = self.call("trace_record.py", "decision", record,
                           "--input", "workspace/record-staging/decision.json")
        self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
        rows = (self.root / record / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        entry = json.loads(rows[-1])
        self.assertEqual(entry["owners"], ["Speaker A", "Speaker B"])
        self.assertEqual(entry["owner_basis"], "input speakers")


class Portable(unittest.TestCase):
    def test_no_personal_machine_or_product_bound_values_remain(self):
        skill = next((p for p in (REPO / "starter/files/.skills/meeting-and-idea-intake/SKILL.md",
                                  REPO / ".skills/meeting-and-idea-intake/SKILL.md") if p.is_file()), None)
        files = [SCRIPTS / "meeting_quotes.py"] + ([skill] if skill else [])
        banned = ("Zoom", "Apple Mail", "AppleScript", "osascript", "textutil", "UCL", "\u2014")
        patterns = (r"[\w.+-]+@[\w-]+\.[a-z]{2,}", r"/Users/|/home/|[A-Z]:\\Users|~/")
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{path.name} carries {token!r}")
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, text), f"{path.name} matches {pattern!r}")


if __name__ == "__main__":
    unittest.main()
