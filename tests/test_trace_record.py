#!/usr/bin/env python3
"""Regression tests: every lesson this project paid for, locked in.

Each test names the failure that taught it. Software engineering's answer to a
bug is a test that fails if the bug returns, and that is what makes a lesson
part of the process instead of a paragraph someone might read. If a test here
fails, a mistake we already made is being made again.

Run:  python3 tests/test_trace_record.py
stdlib only; no pytest needed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import trace_record as hr  # noqa: E402

# The real ledger, fingerprinted before a single test runs. Every acceptance
# test writes to a temporary file instead, and `tearDownModule` proves it: a
# test suite that records an acceptance would be forging the one act in this
# repository that only a person may perform.
REAL_LEDGER = REPO / "ai" / "records" / "acceptances.jsonl"


def ledger_fingerprint():
    if not REAL_LEDGER.exists():
        return "absent"
    return hashlib.sha256(REAL_LEDGER.read_bytes()).hexdigest()


LEDGER_AT_IMPORT = ledger_fingerprint()


def tearDownModule():
    assert ledger_fingerprint() == LEDGER_AT_IMPORT, (
        f"{REAL_LEDGER} changed while the tests ran; a test wrote a real "
        "review record instead of using a temporary one"
    )


def user_row(text):
    return {"type": "user", "message": {"content": text}}


class ExtractionLessons(unittest.TestCase):
    """Found by running the extractor on a real session, 2026-08-03.

    The transcript's `user` role is not the human. The first version of the
    extractor was wrong in three separate ways and reported success anyway.
    """

    def test_wrapper_prepended_to_a_real_prompt_does_not_drop_it(self):
        # LESSON: the editor's open-file notice rides along with what the person
        # typed. Dropping the whole row lost the session's opening prompt.
        row = user_row("<ide_opened_file>The user opened /tmp/x.md</ide_opened_file>"
                       "i think we have an issue in many of the repos")
        text = hr.human_text(row)
        self.assertIsNotNone(text, "a wrapper must not delete the human's words")
        self.assertIn("issue in many of the repos", text)
        self.assertNotIn("ide_opened_file", text)

    def test_utf8_human_text_survives_extract_promote_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            transcript = root / "session.jsonl"
            prompt = "研究計画を確認してください"
            transcript.write_bytes((json.dumps(user_row(prompt), ensure_ascii=False) + "\n").encode())
            staging, promoted = root / "staging", root / "promoted"
            args = type("A", (), {"session": str(transcript), "out": str(staging),
                                    "record_id": "utf8", "speaker": "Researcher"})()
            real_open = io.open
            def ascii_default(file, mode="r", buffering=-1, encoding=None, errors=None,
                              newline=None, closefd=True, opener=None):
                if "b" not in mode and encoding in (None, "locale"):
                    encoding = "ascii"
                return real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)
            # Newer pathlib resolves a missing encoding before calling io.open.
            # Keep this simulated locale independent of the runner's UTF-8 mode.
            with mock.patch("io.text_encoding", create=True,
                            side_effect=lambda encoding, stacklevel=2: "locale" if encoding is None else encoding), \
                    mock.patch("io.open", side_effect=ascii_default):
                with contextlib.redirect_stdout(io.StringIO()):
                    hr.cmd_extract(args)
                evidence_path = staging / "evidence.json"
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                self.assertEqual(evidence["turns"][0]["text"], prompt)
                evidence["consent"]["third_party_review"] = "cleared"
                evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                (staging / "summary.md").write_text("The researcher requested review (T1).", encoding="utf-8")
                (staging / "checks.jsonl").write_text(json.dumps({
                    "kind": "code-test", "runner": "unittest", "method": "UTF-8 round trip",
                    "result": "pass", "artifact": "evidence.json",
                    "candidate": "evidence.json", "criterion": "the prompt survives unchanged",
                    "limits": "synthetic Claude transcript fixture only"}) + "\n", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    hr.cmd_cover(type("A", (), {"record": str(staging)})())
                    import record_privacy
                    record_privacy.cmd_publication(hr, type("A", (), {"record": str(staging), "by": "test checker", "scope": "synthetic fixture"})())
                    hr.cmd_promote(type("A", (), {"staging": str(staging), "dest": str(promoted),
                                                    "force": False})())
                result = hr.evaluate_record(promoted)
                self.assertTrue(result["ok"], result["problems"])
                self.assertIn(prompt, (promoted / "evidence.json").read_text(encoding="utf-8"))

    def test_omitted_speaker_remains_unknown_and_does_not_create_an_owner(self):
        # LESSON (SKR-01): a distributed extractor carried one maintainer's
        # name as its CLI default, assigning every adopter's prompt to them.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps(user_row("please inspect this")) + "\n",
                                  encoding="utf-8")
            staging = root / "staging"
            with contextlib.redirect_stdout(io.StringIO()):
                code = hr.main(["extract", str(transcript), "--out", str(staging),
                                "--record-id", "unknown-speaker"])
            self.assertEqual(code, 0)
            evidence = json.loads((staging / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["record_schema_version"], 2)
            self.assertEqual(evidence["turns"][0]["speaker"], "unknown")
            self.assertEqual(evidence["consent"]["own_turns_by"], "unknown")
            import trace_story
            self.assertEqual(trace_story.owner_names(evidence["turns"], ["T1"]), [])

    def test_a_repeated_row_is_not_a_further_turn(self):
        # LESSON (AH-229): a Claude Code transcript can carry the same user rows
        # twice, uuids included, after a compaction. The second copies were
        # numbered as further turns and had to be excluded by hand.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = dict(user_row("please inspect this"), uuid="u-1")
            second = dict(user_row("now fix it"), uuid="u-2")
            transcript = root / "session.jsonl"
            transcript.write_text("".join(json.dumps(r) + "\n" for r in
                                          (first, second, first, second)), encoding="utf-8")
            staging = root / "staging"
            with contextlib.redirect_stdout(io.StringIO()):
                code = hr.main(["extract", str(transcript), "--out", str(staging),
                                "--record-id", "repeats", "--speaker", "R"])
            self.assertEqual(code, 0)
            turns = json.loads((staging / "evidence.json").read_text(encoding="utf-8"))["turns"]
            self.assertEqual([(t["id"], t["row"]) for t in turns], [("T1", 0), ("T2", 1)])

    def test_a_record_holding_a_repeat_from_before_the_fix_still_refreshes(self):
        # A record extracted before AH-229 kept the repeat as a turn. Refreshing
        # it keeps that turn and its id instead of refusing as if it were lost.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            row = dict(user_row("please inspect this"), uuid="u-1")
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
            staging = root / "staging"
            with contextlib.redirect_stdout(io.StringIO()):
                hr.main(["extract", str(transcript), "--out", str(staging), "--record-id", "old"])
            path = staging / "evidence.json"
            evidence = json.loads(path.read_text(encoding="utf-8"))
            evidence["turns"].append(dict(evidence["turns"][0], id="T2", row=1))
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with open(transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(user_row("and then this"), uuid="u-3")) + "\n")
            with contextlib.redirect_stdout(io.StringIO()):
                code = hr.main(["extract", str(transcript), "--out", str(staging), "--record-id", "old"])
            self.assertEqual(code, 0)
            turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
            self.assertEqual([(t["id"], t["row"]) for t in turns], [("T1", 0), ("T2", 1), ("T3", 2)])

    def test_loaded_skill_definition_is_not_a_human_turn(self):
        # LESSON: the harness loads a skill file into the user role. It was
        # counted as a 1901-word human turn, the longest in the record.
        row = user_row("Base directory for this skill: /Users/x/.claude/skills/foo\n\n"
                       "# Some Skill\n\nUse this skill to do things.")
        self.assertIsNone(hr.human_text(row))

    def test_messages_sent_while_the_agent_works_are_captured(self):
        # LESSON: mid-turn messages are not user rows at all. They are
        # `queued_command` attachments. Missing them lost 4 of 13 turns while
        # the extractor reported success.
        row = {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "origin": {"kind": "human"},
                "prompt": [{"type": "text", "text": "also my idea of seeing what human prompted"}],
            },
        }
        text = hr.human_text(row)
        self.assertIsNotNone(text, "mid-turn messages are human input")
        self.assertIn("what human prompted", text)

    def test_tool_results_are_never_human_turns(self):
        row = {"type": "user", "toolUseResult": {"stdout": "ok"},
               "message": {"content": "command output"}}
        self.assertIsNone(hr.human_text(row))

    def test_a_row_that_is_only_a_wrapper_yields_nothing(self):
        self.assertIsNone(hr.human_text(user_row("<system-reminder>be good</system-reminder>")))


class SecondRunExtractionLessons(unittest.TestCase):
    """Found by running the extractor on four more sessions, 2026-09-01.

    The same shape of mistake as the first run: rows that wear the user role
    and are not the person. Each was counted as a human turn and the extractor
    reported success.
    """

    def test_a_background_task_reporting_back_is_not_a_human_turn(self):
        # LESSON: a finished subagent posts a notification in the user role. In
        # one 112-turn extraction, 50 turns were these.
        row = user_row("<task-notification>\n<task-id>a74c86</task-id>\n"
                       "<status>completed</status>\n</task-notification>")
        self.assertIsNone(hr.human_text(row))

    def test_a_queued_message_gets_the_same_filters_as_a_typed_one(self):
        # LESSON: queued messages bypassed the wrapper filter, so an editor
        # notice was kept verbatim and a bare `/compact` became human input.
        def queued(text):
            return {"type": "attachment", "attachment": {
                "type": "queued_command", "origin": {"kind": "human"},
                "prompt": [{"type": "text", "text": text}]}}
        text = hr.human_text(queued("<ide_opened_file>The user opened /x/BRIEF.md"
                                    "</ide_opened_file> comit this, and promote it"))
        self.assertIsNotNone(text)
        self.assertNotIn("ide_opened_file", text)
        self.assertIn("comit this", text)
        self.assertIsNone(hr.human_text(queued("/compact")))
        self.assertIsNotNone(hr.human_text(queued("/compact keep the ledger plan")),
                             "a command that carries words keeps the words")

    def test_a_compaction_summary_is_the_models_prose_not_the_humans(self):
        # LESSON: after a context reset the harness injects the model's own
        # summary as a user row, flagged `isCompactSummary`.
        row = user_row("This session is being continued from a previous conversation "
                       "that ran out of context. The summary below covers...")
        row["isCompactSummary"] = True
        self.assertIsNone(hr.human_text(row))
        self.assertIsNone(hr.human_text(user_row(
            "This session is being continued from a previous conversation.")),
            "the text alone is enough; the flag is belt and braces")

    def test_codex_rollout_keeps_the_person_and_drops_the_injections(self):
        # LESSON: the abstraction-transfer documents were produced in Codex,
        # which the extractor could not read at all, so their prompts had no
        # record. Codex wraps the person's message in a different file shape
        # and posts two machine rows in the user role.
        def codex_user(text):
            return {"type": "response_item", "timestamp": "2026-08-26T09:47:59Z",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": text}]}}
        self.assertIsNone(hr.codex_human_text(codex_user(
            "<recommended_plugins>\nHere is a list of plugins...\n</recommended_plugins>")))
        self.assertIsNone(hr.codex_human_text(codex_user(
            "# AGENTS.md instructions for /Users/x/repo\n\n<INSTRUCTIONS>...</INSTRUCTIONS>")))
        # LESSON (AH-199): the header also comes bare; a person opening with
        # the same words is still the person.
        self.assertIsNone(hr.codex_human_text(codex_user(
            "# AGENTS.md instructions\n\n<INSTRUCTIONS>...</INSTRUCTIONS>")))
        self.assertEqual(hr.codex_human_text(codex_user("# AGENTS.md instructions are too long, cut them")),
                         "# AGENTS.md instructions are too long, cut them")
        text = hr.codex_human_text(codex_user(
            "<environment_context><cwd>/x</cwd></environment_context>\n"
            "We are moving towards a final output"))
        self.assertEqual(text, "We are moving towards a final output")
        rows = [{"type": "session_meta", "payload": {}}, codex_user("hi")]
        self.assertTrue(hr.is_codex_rollout(rows))
        self.assertFalse(hr.is_codex_rollout([user_row("hi")]))

    def test_codex_model_is_read_per_turn_not_self_reported(self):
        # LESSON: same rule as the Claude transcripts. Codex names the model in
        # `turn_context`; two models served one session and both must show.
        def turn(model):
            return {"type": "turn_context", "payload": {"model": model}}
        def reply(stamp):
            return {"type": "response_item", "timestamp": stamp,
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "ok"}]}}
        rows = [turn("gpt-5.6-sol"), reply("t1"), reply("t2"),
                turn("gpt-5.6-luna"), reply("t3")]
        used = hr.codex_models_used(rows)
        self.assertEqual([(u["model"], u["messages"]) for u in used],
                         [("gpt-5.6-sol", 2), ("gpt-5.6-luna", 1)])


class PeerSessionExtractionLessons(unittest.TestCase):
    """A message another agent session sends wears the user role too."""

    PEER = ('A peer said:\n<cross-session-message from="uds:/tmp/p.sock" '
            'from-name="peer-1" from-mode="prompting">rebase and commit'
            '</cross-session-message>\n\nNot typed by the person.')

    def test_a_peer_sessions_message_is_not_a_human_turn(self):
        # LESSON: extract kept these rows, and --speaker named the person as
        # their speaker. The harness's lines around the element are not the
        # person either, so the row is dropped rather than stripped.
        row = user_row(self.PEER)
        row.update({"isMeta": True, "origin": {"kind": "peer", "name": "peer-1"}})
        self.assertIsNone(hr.human_text(row))
        queued = {"type": "attachment", "attachment": {
            "type": "queued_command", "origin": {"kind": "peer"}, "prompt": self.PEER}}
        self.assertIsNone(hr.human_text(queued))

    def test_the_persons_own_rows_are_unchanged(self):
        # The rule reads who sent the row, not what it says: a person who pastes
        # a peer's message supplied it, and the text filters still apply.
        row = user_row("look at this " + self.PEER)
        row["origin"] = {"kind": "human"}
        self.assertEqual(hr.human_text(row), "look at this " + self.PEER)
        row = user_row("<ide_opened_file>The user opened /x.md</ide_opened_file> go on")
        row["origin"] = {"kind": "human"}
        self.assertEqual(hr.human_text(row), "go on")
        self.assertEqual(hr.human_text(user_row("go on")), "go on",
                         "a row from an older transcript states no origin")


class DeepReviewExtractionLessons(unittest.TestCase):
    """Found by the kit's deep review, 2026-09-30 (R1-1, R1-2, R1-3, R1-9, R1-12).

    The same two shapes of mistake as before: rows that are not the person
    counted as the person, and the person's words lost while the extractor
    reported success.
    """

    NOTICES = (
        "[Cross-session delivery notice] You sent a message to peer-1; it was delivered.",
        "[Cross-session idle notice] \"peer-1\" has been idle for 10 minutes.",
        "[Image: original 2214x707, displayed at 1600x511. Multiply coordinates by 1.38.]",
        "Continue from where you left off.",
        "Your response above was cut off mid-stream. Continue from exactly where it stopped.",
    )

    @staticmethod
    def queued(prompt, **attachment):
        return {"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": prompt, **attachment}}

    @staticmethod
    def codex_user(text):
        return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": text}]}}

    def test_a_harness_notice_marked_is_meta_is_not_a_human_turn(self):
        # LESSON (AH-124): Claude Code writes its own notices as user rows
        # flagged `isMeta`, with no origin. 119 of them sat in 25 tracked
        # records under the person's name, and 19 were summarized as the
        # person's direction ("Continue (T53).").
        for notice in self.NOTICES:
            for content in (notice, [{"type": "text", "text": notice}]):
                row = {"type": "user", "isMeta": True, "message": {"content": content}}
                self.assertIsNone(hr.human_text(row), notice)
            # The rule reads the harness's flag, not the words: a person who
            # types the same sentence is still the person.
            self.assertEqual(hr.human_text(user_row(notice)), notice)

    def test_queued_messages_in_every_recorded_shape_are_captured(self):
        # LESSON (AH-125): Claude Code 2.1.181 to 2.1.201 wrote a queued
        # message with no `origin`, and 2.1.222 wrote its `prompt` as a plain
        # string. Both were dropped without notice; an explicit null origin
        # crashed the extractor.
        blocks = [{"type": "text", "text": "note that I purged the storage"}]
        self.assertEqual(hr.human_text(self.queued(blocks)), "note that I purged the storage")
        self.assertEqual(hr.human_text(self.queued("Do that a/b thing too", origin={"kind": "human"})),
                         "Do that a/b thing too")
        self.assertEqual(hr.human_text(self.queued(blocks, origin=None)), "note that I purged the storage")
        # A sender the harness names as not the person stays out, in either shape.
        for kind in ("peer", "task-notification", "coordinator"):
            self.assertIsNone(hr.human_text(self.queued("rebase and commit", origin={"kind": kind})), kind)
            self.assertIsNone(hr.human_text(self.queued(blocks, origin={"kind": kind})), kind)
        self.assertIsNone(hr.human_text(self.queued("rebase and commit", isMeta=True)))
        # An origin-less notification is still dropped by the text filter.
        self.assertIsNone(hr.human_text(self.queued(
            "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n</task-notification>",
            commandMode="task-notification")))

    def test_a_synthetic_model_is_not_an_execution_agent(self):
        # LESSON (AH-126): harness-made assistant rows carry the model
        # `<synthetic>`, and `draft-message` wrote `Execution-agent: <synthetic>`.
        rows = [{"type": "assistant", "timestamp": "T1", "message": {"model": "claude-opus-5"}},
                {"type": "assistant", "timestamp": "T2", "message": {"model": "<synthetic>"}}]
        self.assertEqual([e["model"] for e in hr.models_used(rows)], ["claude-opus-5"])
        self.assertFalse(hr.is_model_id("<synthetic>"))
        self.assertTrue(hr.is_model_id("claude-opus-5"))

    def test_codex_machine_payloads_are_not_the_person(self):
        # LESSON (AH-130): Codex posts subagent reports, turn aborts, automation
        # heartbeats and edit notices in the user role. Each became a turn
        # under the person's name.
        payloads = (
            '<subagent_notification>{"agent_id": "a1", "status": "done"}</subagent_notification>',
            "<turn_aborted>The user interrupted the previous turn on purpose.</turn_aborted>",
            "<heartbeat><automation_id>nightly-check</automation_id>\nRun the checks.</heartbeat>",
            "<external_codex_apps_writing_block_edits>edits</external_codex_apps_writing_block_edits>",
        )
        for payload in payloads:
            self.assertIsNone(hr.codex_human_text(self.codex_user(payload)), payload)
            self.assertEqual(hr.codex_human_text(self.codex_user(payload + "\nkeep going with the table")),
                             "keep going with the table", "the person's words beside a payload stay")
        quoted = "Why did <turn_aborted>The user interrupted</turn_aborted> appear in the log?"
        self.assertEqual(hr.codex_human_text(self.codex_user(quoted)), quoted,
                         "a person quoting a payload is not the payload")

    def test_a_person_quoting_a_codex_wrapper_keeps_every_word(self):
        # LESSON (AH-206): the environment and plugin wrappers were stripped
        # anywhere in a Codex row, so a person who quoted one lost the words
        # inside it. Only a leading run of wrappers and payloads is stripped.
        for text in ("Please preserve <environment_context>quoted words</environment_context> literally.",
                     "Why is <recommended_plugins>a list</recommended_plugins> in my prompt?"):
            self.assertEqual(hr.codex_human_text(self.codex_user(text)), text)
        wrapped = ("<environment_context><cwd>/x</cwd></environment_context>\n"
                   "<subagent_notification>{}</subagent_notification>\n"
                   "<recommended_plugins>list</recommended_plugins>\n"
                   "Keep <environment_context>this</environment_context> as typed.")
        self.assertEqual(hr.codex_human_text(self.codex_user(wrapped)),
                         "Keep <environment_context>this</environment_context> as typed.",
                         "a leading run in any order goes; the person's quote stays")

    def test_a_person_quoting_harness_text_keeps_every_word(self):
        # LESSON (AH-133): the filters matched harness text anywhere in a row,
        # so a person who quoted a tag or a marker lost the rest of the sentence
        # or the whole turn. Only a harness payload at the row's start is
        # stripped; the prepended editor notice still goes.
        for text in ("Please preserve <system-reminder>quoted words</system-reminder> literally.",
                     "Why does the <system-reminder> tag keep repeating? It clutters every turn.",
                     "The log shows [Request interrupted by user] after my edit, why?",
                     "Is 'Base directory for this skill:' printed by the harness?"):
            self.assertEqual(hr.human_text(user_row(text)), text)
        self.assertEqual(hr.human_text(user_row(
            "<ide_opened_file>The user opened /x.md</ide_opened_file>"
            "<system-reminder>be good</system-reminder>Keep <system-reminder> as written")),
            "Keep <system-reminder> as written")
        self.assertIsNone(hr.human_text(user_row("[Request interrupted by user for tool use]")))
        self.assertIsNone(hr.human_text(user_row(
            "<ide_opened_file>The user opened /x.md</ide_opened_file>"
            "Base directory for this skill: /x/skills/foo\n\n# Foo")))


class QuestionDialogExtractionLessons(unittest.TestCase):
    """Found while recording the instruction audit, 2026-10-04 (AH-274).

    When an agent asks through Claude Code's question dialog, the person's
    answers come back as the tool's result, and every row carrying a tool's
    result was dropped. Four selections that directed that work were missing
    from its record, and 13 answers in 12 sessions of this project had reached
    no trace record at all.
    """

    QUESTION = "Send the invitations now?"
    OPTIONS = ("Not yet", "Yes, send now")

    @staticmethod
    def question(text, labels, multi=False):
        return {"question": text, "header": "Q", "multiSelect": multi,
                "options": [{"label": label, "description": "the agent's gloss"} for label in labels]}

    @classmethod
    def dialog_row(cls, answers, questions=None, uuid="u-dialog", tool_use_id="toolu_1", **extra):
        questions = questions or [cls.question(cls.QUESTION, cls.OPTIONS)]
        result = {"questions": questions, "answers": answers}
        result.update(extra.pop("result", {}))
        content = ", ".join(f'"{q}"="{a}"' for q, a in answers.items())
        row = {"type": "user", "uuid": uuid, "timestamp": "2026-10-04T09:00:00Z",
               "toolUseResult": result,
               "message": {"role": "user", "content": [{
                   "type": "tool_result", "tool_use_id": tool_use_id,
                   "content": f"Your questions have been answered: {content}."}]}}
        row.update(extra)
        return row

    @staticmethod
    def asking_row(tool_use_id="toolu_1", model="claude-opus-5-5"):
        return {"type": "assistant", "timestamp": "2026-10-04T08:59:00Z",
                "message": {"model": model, "content": [{
                    "type": "tool_use", "id": tool_use_id, "name": "AskUserQuestion",
                    "input": {"questions": []}}]}}

    def extract(self, root, rows, record_id="dialog"):
        transcript = root / "session.jsonl"
        transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        staging = root / "staging"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = hr.main(["extract", str(transcript), "--out", str(staging),
                            "--record-id", record_id, "--speaker", "R"])
        self.assertEqual(code, 0)
        turns = json.loads((staging / "evidence.json").read_text(encoding="utf-8"))["turns"]
        return turns, out.getvalue(), staging

    def test_answers_given_through_the_question_dialog_are_a_human_turn(self):
        # LESSON: the answer arrives as a tool result, and the extractor dropped
        # every row with a `toolUseResult`, so the person's choice was lost
        # while the extractor reported success.
        with tempfile.TemporaryDirectory() as directory:
            rows = [dict(user_row("prepare the invitations"), uuid="u-1"),
                    self.asking_row(),
                    self.dialog_row({self.QUESTION: "Yes, send now"}),
                    dict(user_row("thanks"), uuid="u-3")]
            turns, printed, _ = self.extract(pathlib.Path(directory), rows)
        self.assertEqual([(t["id"], t["row"], t["source"]["kind"]) for t in turns],
                         [("T1", 0, "chat"), ("T2", 2, "question-dialog"), ("T3", 3, "chat")])
        turn = turns[1]
        self.assertEqual((turn["speaker"], turn["speaker_kind"], turn["asked_by"]),
                         ("R", "human", "claude-opus-5-5"))
        self.assertEqual(turn["text"], "[selected from the agent's options] Yes, send now "
                                       "[to the question] Send the invitations now?")
        self.assertEqual(turn["dialog"], [{"question": self.QUESTION, "options": list(self.OPTIONS),
                                           "answer": "Yes, send now", "answer_kind": "selected"}])
        self.assertEqual(turn["source"]["text_sha256"], hr.sha256_text(turn["text"]))
        self.assertIn("1 of them from the question dialog", printed)

    def test_a_typed_answer_is_told_apart_from_an_offered_option(self):
        # LESSON: the option wording is the agent's and only the choice is the
        # person's. A turn that printed an agent's label as the person's words,
        # or the person's words as a label they picked, would misstate both.
        typed = self.dialog_row({self.QUESTION: "Not yet, ask Oveis first "})
        self.assertEqual(hr.dialog_entries(typed)[0]["answer_kind"], "typed",
                         "words that open with a label are not that label")
        self.assertEqual(hr.dialog_text(hr.dialog_entries(typed)),
                         "[typed in answer] Not yet, ask Oveis first [to the question] Send the invitations now?")
        # Labels can hold ", " themselves, so a multi-select answer is read by
        # matching labels whole, never by splitting it at its commas.
        labels = ("Merge, then tag", "Tag only", "Publish")
        multi = [self.question("Which steps?", labels, multi=True)]
        picked = hr.dialog_entries(self.dialog_row({"Which steps?": "Merge, then tag, Publish"}, multi))
        self.assertEqual(picked[0]["answer_kind"], "selected")
        self.assertEqual(hr.dialog_text(picked),
                         "[selected from the agent's options] Merge, then tag, Publish [to the question] Which steps?")
        split = hr.dialog_entries(self.dialog_row({"Which steps?": "Merge, then publish"}, multi))
        self.assertEqual(split[0]["answer_kind"], "typed", "a fragment of a label is not the label")
        single = [self.question("Which step?", labels)]
        self.assertEqual(hr.dialog_entries(self.dialog_row({"Which step?": "Merge, then tag, Publish"}, single))
                         [0]["answer_kind"], "typed", "a single-select answer is one label or typed")
        noted = hr.dialog_entries(self.dialog_row({self.QUESTION: "Not yet"},
                                                  result={"annotations": {self.QUESTION: {"notes": "after Monday"}}}))
        self.assertEqual(noted[0]["note"], "after Monday")
        self.assertTrue(hr.dialog_text(noted).endswith("[to the question] Send the invitations now? [note] after Monday"))

    def test_options_picked_and_words_typed_in_one_answer_are_kept_apart(self):
        # LESSON: in a multi-select, the person can pick options and type words
        # in the same answer; one real answer opened with two proposals and then
        # asked to see any change to the workshop text first. Read as typed,
        # the labels became the person's words; read as selected, the person's
        # condition became an agent's option.
        labels = ("Accepted copy reads A recorded", "Queue shows a return", "Closing step uses the queue")
        multi = [self.question("Which proposals go in?", labels, multi=True)]
        answer = "Queue shows a return, Closing step uses the queue, show me any text change first, then I decide"
        entry = hr.dialog_entries(self.dialog_row({"Which proposals go in?": answer}, multi))[0]
        self.assertEqual((entry["answer_kind"], entry["options_matched"], entry["typed"], entry["answer"]),
                         ("options-and-typed", ["Queue shows a return", "Closing step uses the queue"],
                          "show me any text change first, then I decide", answer))
        self.assertNotIn("selected", entry)
        self.assertEqual(hr.dialog_text([entry]),
                         "[opens with the agent's options, picked or typed] Queue shows a return, "
                         "Closing step uses the queue "
                         "[and typed] show me any text change first, then I decide "
                         "[to the question] Which proposals go in?")

    def test_an_answer_opening_with_a_label_is_not_recorded_as_a_selection(self):
        # LESSON (AH-279, Codex review of 6f94865): the dialog stores one string
        # per question and records no click, so "Publish, but only after my
        # review" may be a pick followed by words or a sentence typed whole. It
        # was recorded as `selected: ["Publish"]` and rendered as a selection,
        # an inference shown as an observation. Only the words after the label
        # are certainly the person's, and the record says no more than that.
        multi = [self.question("Which actions?", ("Publish", "Wait"), multi=True)]
        entries = hr.dialog_entries(self.dialog_row({"Which actions?": "Publish, but only after my review"}, multi))
        self.assertEqual((entries[0]["answer_kind"], entries[0]["options_matched"], entries[0]["typed"]),
                         ("options-and-typed", ["Publish"], "but only after my review"))
        text = hr.dialog_text(entries)
        self.assertNotIn("[selected from the agent's options]", text)
        self.assertEqual(text, "[opens with the agent's options, picked or typed] Publish "
                               "[and typed] but only after my review [to the question] Which actions?")
        exact = hr.dialog_entries(self.dialog_row({"Which actions?": "Publish, Wait"}, multi))
        self.assertEqual(exact[0]["answer_kind"], "selected",
                         "an answer equal to offered labels is still a match of the stored string")

    def dialog_record(self, root, question, answer):
        """A records folder holding one record whose only turn answers the dialog."""
        records = root / "records"
        record = records / "sample"
        record.mkdir(parents=True)
        entries = hr.dialog_entries(self.dialog_row({question: answer}, [self.question(question, ("Wait", "Go"))]))
        turn = {"id": "T1", "speaker": "R", "speaker_kind": "human", "text": hr.dialog_text(entries),
                "source": {"kind": "question-dialog"}, "dialog": entries}
        (record / "evidence.json").write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
        return records

    @staticmethod
    def said(words):
        return [{"kind": "human", "recorded_by": "claude-opus-5-5", "words": words}]

    def test_the_agents_question_is_never_the_persons_words(self):
        # LESSON (AH-280, Codex review of 6f94865): a dialog turn's text carries
        # the agent's question, and the words lookup searched the whole text,
        # so "I accept the changes" was reported as the person's words when the
        # person had answered "Wait" to "Do you say I accept the changes?".
        with tempfile.TemporaryDirectory() as directory:
            records = self.dialog_record(pathlib.Path(directory), "Do you say I accept the changes?", "Wait")
            notes = hr.words_in_human_turns(self.said("I accept the changes"), records)
        self.assertEqual(notes, ["acceptances row 1: the person's words occur in no captured human turn; "
                                 "the session they were said in may not be captured yet, or the words redacted"])

    def test_the_persons_dialog_answer_is_found_and_said_to_be_one(self):
        # The person's answer is searched, and a match there names the dialog
        # rather than calling it a captured human turn.
        with tempfile.TemporaryDirectory() as directory:
            records = self.dialog_record(pathlib.Path(directory), "Do you accept the changes?",
                                         "Yes, I accept the changes after the build passes")
            notes = hr.words_in_human_turns(self.said("I accept the changes"), records)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("acceptances row 1: the person's words occur in an answer "
                                            "given through the question dialog, "), notes[0])
        self.assertTrue(notes[0].endswith("sample T1"), notes[0])

    def test_another_tools_result_is_still_not_a_human_turn(self):
        # The dialog is recognized by its shape; any other tool's result, and a
        # dialog nobody answered, stays out as before.
        for result in ({"stdout": "ok"}, "User rejected tool use",
                       {"questions": [self.question(self.QUESTION, self.OPTIONS)], "answers": {}},
                       {"answers": {self.QUESTION: "Not yet"}}):
            row = {"type": "user", "uuid": "u-t", "toolUseResult": result,
                   "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}
            self.assertIsNone(hr.dialog_entries(row), result)
            self.assertIsNone(hr.human_text(row), result)
        with tempfile.TemporaryDirectory() as directory:
            rows = [dict(user_row("run the tests"), uuid="u-1"),
                    {"type": "user", "uuid": "u-2", "toolUseResult": {"stdout": "ok"},
                     "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}]
            turns, printed, _ = self.extract(pathlib.Path(directory), rows)
        self.assertEqual([t["id"] for t in turns], ["T1"])
        self.assertNotIn("question dialog", printed)

    def test_a_dialog_row_with_a_non_human_origin_or_marked_is_meta_is_dropped(self):
        # The exclusions that apply to a typed row apply to a dialog row too.
        answers = {self.QUESTION: "Not yet"}
        self.assertIsNone(hr.dialog_entries(self.dialog_row(answers, origin={"kind": "peer"})))
        self.assertIsNone(hr.dialog_entries(self.dialog_row(answers, isMeta=True)))
        self.assertIsNotNone(hr.dialog_entries(self.dialog_row(answers, origin={"kind": "human"})))
        self.assertIsNotNone(hr.dialog_entries(self.dialog_row(answers)), "an older row states no origin")

    def test_a_dialog_row_written_twice_is_one_turn(self):
        # The same-uuid repeat rule (AH-229) holds for a dialog row as well.
        with tempfile.TemporaryDirectory() as directory:
            row = self.dialog_row({self.QUESTION: "Not yet"})
            turns, _, _ = self.extract(pathlib.Path(directory), [row, row])
        self.assertEqual([(t["id"], t["row"]) for t in turns], [("T1", 0)])

    def test_refreshing_a_record_made_before_the_fix_adds_the_answers_without_renumbering(self):
        # A record extracted before AH-274 numbered only the typed turns. A
        # refresh keeps every id it gave and appends the dialog answers after
        # them, so turn ids cited in a summary or a decision still mean what
        # they meant.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            rows = [dict(user_row("prepare the invitations"), uuid="u-1"),
                    self.asking_row(),
                    self.dialog_row({self.QUESTION: "Not yet"}),
                    dict(user_row("then draft the email"), uuid="u-3")]
            with mock.patch.object(hr, "dialog_entries", return_value=None):
                before, _, _ = self.extract(root, rows)
            self.assertEqual([(t["id"], t["row"]) for t in before], [("T1", 0), ("T2", 3)])
            after, printed, _ = self.extract(root, rows)
        self.assertEqual([(t["id"], t["row"], t["source"]["kind"]) for t in after],
                         [("T1", 0, "chat"), ("T2", 3, "chat"), ("T3", 2, "question-dialog")])
        self.assertEqual(after[:2], before)
        self.assertIn("1 of them from the question dialog", printed)

    def test_a_dialog_turn_reads_truthfully_wherever_a_turn_is_shown(self):
        # Every reader prints a turn's text as the person's words. The brackets
        # travel with the text, so coverage, the privacy screen, verification,
        # the brief and the queue's origin cell need no change to stay true.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            rows = [self.asking_row(), self.dialog_row({self.QUESTION: "Yes, send now"})]
            _, _, staging = self.extract(root, rows, record_id="staging")
            evidence_path = staging / "evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["consent"]["third_party_review"] = "cleared"
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            (staging / "summary.md").write_text("R chose to send the invitations now (T1).", encoding="utf-8")
            (staging / "checks.jsonl").write_text(json.dumps({
                "kind": "code-test", "runner": "unittest", "method": "dialog turn through the readers",
                "result": "pass", "artifact": "evidence.json", "candidate": "evidence.json",
                "criterion": "the turn's text says which words are the agent's",
                "limits": "synthetic transcript fixture only"}) + "\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                hr.cmd_cover(type("A", (), {"record": str(staging)})())
            result = hr.evaluate_record(staging)
            self.assertTrue(result["ok"], result["problems"])
            import record_privacy
            self.assertEqual(record_privacy.scan_record(staging), [])
            turns = hr.record_turns(staging)
            self.assertIn("[selected from the agent's options] Yes, send now",
                          hr.origin_sentence(turns, [], True))
            # The brief's commits come from the project's git history, which a
            # fresh install has not set up; the turns are what is under test.
            with mock.patch.object(hr, "record_commits", return_value=[]):
                brief = "\n".join(hr.brief_lines(str(staging)))
            self.assertIn('T1 R: "[selected from the agent\'s options] Yes, send now', brief)
            records = root / "records"
            records.mkdir()
            staging.rename(records / "staging")
            notes = hr.words_in_human_turns([{"kind": "human", "recorded_by": "claude-opus-5-5",
                                              "words": "Yes, send now"}], records)
            self.assertIn("occur in an answer given through the question dialog", notes[0])


class RecordIntegrityLessons(unittest.TestCase):
    """Found by external review (Codex), two loops of three rounds each."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = pathlib.Path(self.tmp.name)
        (self.record / "evidence.json").write_text(json.dumps({
            "record_id": "test",
            "consent": {"third_party_review": "cleared"},
            "turns": [{"id": "T1", "text": "first"}, {"id": "T2", "text": "second"}],
        }))
        (self.record / "summary.md").write_text("The author set the direction (T1).")
        (self.record / "checks.jsonl").write_text(json.dumps({
            "kind": "build", "runner": "latexmk", "method": "main.tex",
            "result": "exit=0", "artifact": "build.log"}) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _cover(self):
        hr.cmd_cover(type("A", (), {"record": str(self.record)})())
        return json.loads((self.record / "coverage.json").read_text())

    def test_a_turn_the_summary_ignores_is_flagged_not_hidden(self):
        # LESSON (Codex loop 2, round 3): a summary can be true in everything it
        # says and still mislead by omission. Backlinks prove inclusions and say
        # nothing about what was left out.
        cov = self._cover()
        status = {e["turn"]: e["status"] for e in cov["turns"]}
        self.assertEqual(status["T1"], "summarized")
        self.assertEqual(status["T2"], "UNCOVERED",
                         "an unsummarized turn must be visible, not absent")

    def test_an_exclusion_must_carry_a_reason_and_then_survives(self):
        # LESSON (AH-132): this test only checked that a supplied reason
        # survived, so it still passed with the check it is named for deleted.
        self._cover()
        cov_path = self.record / "coverage.json"
        cov = json.loads(cov_path.read_text())
        for entry in cov["turns"]:
            if entry["turn"] == "T2":
                entry.update(status="excluded")
                entry.pop("reason", None)
        cov_path.write_text(json.dumps(cov))
        self.assertIn("exclusion needs a reason: T2", self._verify_problems(),
                      "an exclusion without a reason is just an omission")
        for entry in cov["turns"]:
            if entry["turn"] == "T2":
                entry.update(status="excluded", reason="control instruction, no content")
        cov_path.write_text(json.dumps(cov))
        again = self._cover()
        t2 = next(e for e in again["turns"] if e["turn"] == "T2")
        self.assertEqual(t2["status"], "excluded")
        self.assertIn("reason", t2, "an exclusion without a reason is just an omission")
        self.assertFalse(any("exclusion needs a reason" in p for p in self._verify_problems()))

    def test_a_summary_naming_an_excluded_turn_does_not_undo_the_exclusion(self):
        # LESSON (AH-224): a summary names a turn to say why it is excluded
        # ("T2 is the harness's notice"); re-covering read the citation as
        # coverage and erased the exclusion and its reason.
        self._cover()
        cov_path = self.record / "coverage.json"
        cov = json.loads(cov_path.read_text())
        for entry in cov["turns"]:
            if entry["turn"] == "T2":
                entry.update(status="excluded", reason="a harness notice, not the person's words")
        cov_path.write_text(json.dumps(cov))
        with (self.record / "summary.md").open("a", encoding="utf-8") as fh:
            fh.write("\nT2 is a harness notice, excluded.\n")
        again = self._cover()
        t2 = next(e for e in again["turns"] if e["turn"] == "T2")
        self.assertEqual((t2["status"], t2.get("reason")), ("excluded", "a harness notice, not the person's words"))

    def test_editing_the_summary_after_coverage_is_detected(self):
        # LESSON: a coverage manifest that does not pin the summary it covers
        # can be built once and then quietly diverge from what it describes.
        self._cover()
        (self.record / "summary.md").write_text("Something else entirely.")
        problems = self._verify_problems()
        self.assertTrue(any("changed since coverage" in p for p in problems))

    def test_promotion_refuses_while_consent_is_pending(self):
        # LESSON (Codex loop 2, round 3): scope is not consent. A person can
        # paste a colleague's words into their own prompt, so promotion needs a
        # human third-party check, not an ownership rule.
        staging = pathlib.Path(self.tmp.name) / "staging"
        staging.mkdir()
        (staging / "evidence.json").write_text(json.dumps({
            "record_id": "t", "consent": {"third_party_review": "PENDING"}, "turns": []}))
        args = type("A", (), {"staging": str(staging), "dest": str(self.record / "out"),
                              "force": False})()
        with self.assertRaises(SystemExit) as ctx:
            hr.cmd_promote(args)
        self.assertIn("cleared", str(ctx.exception))

    def _verify_problems(self):
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {"record": str(self.record), "range": None})()
        with contextlib.redirect_stdout(buf):
            hr.cmd_verify(args)
        return [ln.strip()[6:] for ln in buf.getvalue().splitlines()
                if ln.strip().startswith("FAIL")]

    def test_a_check_line_missing_a_field_is_flagged(self):
        # LESSON: "it was checked" must name runner, method, result, and
        # artifact, or it is an assertion rather than evidence.
        (self.record / "checks.jsonl").write_text(json.dumps({"kind": "build"}) + "\n")
        self._cover()
        problems = self._verify_problems()
        self.assertTrue(any("missing" in p for p in problems))

    def test_version_two_requires_candidate_criterion_and_limits(self):
        evidence_path = self.record / "evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["record_schema_version"] = 2
        evidence_path.write_text(json.dumps(evidence))
        (self.record / "summary.md").write_text("The author set the direction (T1, T2).")
        self._cover()
        problems = self._verify_problems()
        self.assertTrue(any("candidate" in p and "criterion" in p and "limits" in p
                            for p in problems), problems)

    def test_version_two_accepts_procedure_and_stable_defect_links(self):
        # The ledger is redirected because this test is about the schema
        # accepting the optional handoff fields, not about which defects the
        # surrounding project happens to have recorded. Without that, it passed
        # in the kit's development repository, whose ledger holds AH-031, and failed in an exported
        # project, whose seeded ledger is empty (found by the 2026-09-19
        # literature-processing fresh-install forward test).
        ledger = self.record / "defects.jsonl"
        ledger.write_text(json.dumps({"id": "AH-031", "state": "open", "label": "fixture",
                                      "severity": "low"}) + "\n")
        saved = os.environ.get("TRACE_OS_DEFECT_LEDGER")
        os.environ["TRACE_OS_DEFECT_LEDGER"] = str(ledger)
        self.addCleanup(lambda: os.environ.__setitem__("TRACE_OS_DEFECT_LEDGER", saved)
                        if saved is not None else os.environ.pop("TRACE_OS_DEFECT_LEDGER", None))
        evidence_path = self.record / "evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["record_schema_version"] = 2
        evidence_path.write_text(json.dumps(evidence))
        (self.record / "summary.md").write_text("The author set the direction (T1, T2).")
        (self.record / "checks.jsonl").write_text(json.dumps({
            "kind": "source-review", "runner": "reviewer", "method": "compare",
            "result": "pass", "artifact": "review.md", "reference": "declared",
            "candidate": "draft.tex@sha256:abc", "criterion": "claims match sources",
            "limits": "one draft version", "procedure": "trace-review",
            "procedure_version": "1", "defect_ids": ["AH-031"],
            "recheck_evidence": ["ai/records/run/checks.jsonl#check-1"]
        }) + "\n")
        self._cover()
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("1 of 1 checks name a procedure and version", result["notes"])
        self.assertIn("1 defect links and 1 recheck evidence links recorded", result["notes"])
        # A hand-typed "1" stays readable and is counted as declared, not derived.
        self.assertTrue(any("1 declared without a derivable version" in note
                            for note in result["notes"]), result["notes"])
        self.assertIn("defect links resolved against the project ledger", result["notes"])


class AuthorshipLessons(unittest.TestCase):
    """The vendor default that asserted what every journal policy forbids."""

    def test_ai_coauthorship_trailer_is_rejected(self):
        # LESSON (HN-035): 51 of 74 commits asserted AI co-authorship by default.
        # Authorship carries accountability a model cannot hold.
        message = "Do a thing\n\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>\n"
        self.assertTrue(hr.commit_message_problems(message),
                        "an AI co-authorship claim must be refused")

    def test_a_commit_with_no_trailer_is_not_a_problem(self):
        # LESSON (2026-09-19): a hook that rejected the missing trailer stopped
        # or turned CI red on six commits (Overleaf-editor edits, a hand edit an
        # agent committed for the person with a body saying so, and a merge the
        # sync script made) and caught zero agent omissions in about 540 agent
        # commits. A person is asked to type nothing; absence reads as a
        # person's commit.
        self.assertEqual(hr.commit_message_problems("Do a thing\n\nno trailer here\n"), [])

    def test_a_correctly_marked_commit_passes(self):
        message = "Do a thing\n\nExecution-agent: claude-fable-5\n"
        self.assertEqual(hr.commit_message_problems(message), [])

    def test_the_verb_is_execution_not_authorship_or_making(self):
        # LESSON: "Made-by" still overclaims. A model executes instructions; the
        # results are actions. A pan does not make the food.
        problems = hr.commit_message_problems("Do a thing\n\nMade-by: agent:x\n")
        self.assertTrue(problems, "`Made-by` must not satisfy the rule")

    def test_old_coauthor_trailer_is_read_as_evidence_not_discarded(self):
        # LESSON: pre-rule history is not simply out of scope. The old trailer is
        # a wrong authorship claim and a usable signal about who executed.
        body = "Old work\n\nCo-Authored-By: Claude Fable 5 <x@y>\n"
        status, agent = hr.classify(body, exempt=True)
        self.assertEqual(status, "inferred")
        self.assertEqual(agent, "claude-fable-5",
                         "read the full model name the old trailer names, not just the vendor")

    def test_inferred_is_never_reported_as_declared(self):
        # LESSON: a guess dressed as a declaration is the false trail this whole
        # design exists to prevent.
        declared, _ = hr.classify("x\n\nExecution-agent: agent:claude-fable-5\n", exempt=True)
        inferred, _ = hr.classify("x\n\nCo-Authored-By: Claude\n", exempt=True)
        self.assertEqual(declared, "declared")
        self.assertNotEqual(inferred, "declared")

    def test_no_signal_yields_unknown_rather_than_a_guess(self):
        status, agent = hr.classify("A commit with no trailers\n", exempt=True)
        self.assertEqual(status, "unknown")
        self.assertEqual(agent, "", "absence of evidence must not become an attribution")

    def test_a_post_rule_commit_with_no_trailer_is_none_declared(self):
        # Under the rule a machine declares itself and a person types nothing,
        # so a post-rule commit with no trailer reads as a person's. The state
        # says "no machine declared", not "a person declared".
        status, agent = hr.classify("Tighten the opening frames by hand\n", exempt=False)
        self.assertEqual(status, "none-declared")
        self.assertEqual(agent, "")
        self.assertNotEqual(status, "declared")

    def test_a_redundant_agent_prefix_is_rejected(self):
        # LESSON: two concurrent sessions in one clone drifted to different
        # trailer values within an hour ("agent:claude-opus-5" vs
        # "claude-opus-5") because the check accepted any non-empty value. An
        # unpinned format forks as soon as more than one writer exists.
        problems = hr.commit_message_problems("x\n\nExecution-agent: agent:claude-opus-5\n")
        self.assertTrue(problems, "a redundant prefix must be rejected")
        self.assertIn("claude-opus-5", problems[0], "say what to write instead")
        self.assertEqual(hr.commit_message_problems("x\n\nExecution-agent: claude-opus-5\n"), [])

    def test_backfill_normalises_a_legacy_prefix_without_rewriting_history(self):
        # Existing commits keep their text; the tracked classification beside
        # them is written correctly.
        status, agent = hr.classify("x\n\nExecution-agent: agent:claude-opus-5\n", exempt=False)
        self.assertEqual(status, "declared")
        self.assertEqual(agent, "claude-opus-5")

    def test_the_executing_model_is_read_from_evidence_not_self_report(self):
        # LESSON: an agent asked which model it is answers from its own system
        # prompt, which names the model the session STARTED with and survives a
        # mid-session switch. Here the agent declared claude-fable-5 while
        # claude-opus-5 had executed 339 of 349 messages.
        rows = [{"type": "assistant", "timestamp": "T0", "message": {"model": "claude-fable-5"}}]
        rows += [{"type": "assistant", "timestamp": f"T{i}", "message": {"model": "claude-opus-5"}}
                 for i in range(1, 20)]
        used = hr.models_used(rows)
        self.assertEqual(used[0]["model"], "claude-opus-5",
                         "the dominant executor must be the one that actually ran")
        self.assertEqual(len(used), 2, "a model switch must not erase the earlier model")
        self.assertEqual(sum(e["messages"] for e in used), 20)

    def test_a_human_authored_commit_needs_no_producer_marker(self):
        # A person typing their own commit is the normal case and must not be
        # forced to claim a machine made it, and must not be asked to type a
        # flag or a field to say so either. The check has no human switch
        # because it does not need one: absence is the person's case.
        self.assertEqual(hr.commit_message_problems("Fix a typo\n"), [])

    def test_a_wrapped_body_line_is_not_the_trailer(self):
        # LESSON (AH-123): a port of the backfill recorded a commit's agent as
        # <model-id>, read off a line of the explanation that wrapped to start
        # with `Execution-agent:`. Trailers are read from the message's end.
        body = ("Read trailers from the end\n\n"
                "The kit writes a line of the form\n"
                "Execution-agent: <model-id> at the end of the message, and\n"
                "Co-Authored-By: Claude is the vendor default it replaced.\n\n"
                "Record: ai/records/x\n\n"
                "Execution-agent: claude-opus-5-5\n")
        self.assertEqual(hr.classify(body, exempt=False), ("declared", "claude-opus-5-5"))
        self.assertEqual(hr.commit_message_problems(body), [])
        prose = "Explain the rule\n\nThe trailer reads\nExecution-agent: <model-id>\nin every agent commit.\n\nNo trailer.\n"
        self.assertEqual(hr.classify(prose, exempt=False), ("none-declared", ""))

    def test_a_trailer_beside_other_lines_is_still_read(self):
        # Git reads trailers only from a last paragraph made of trailers alone.
        # Ten commits in this history end in a paragraph that also holds
        # `Human inputs: T2` or a sentence, or put the trailer under the
        # subject; they declared an agent and must still read as declared.
        mixed = "Do a thing\n\nRecord: ai/records/x\nHuman inputs: T2 and T5\nExecution-agent: gpt-6-astra\n"
        self.assertEqual(hr.classify(mixed, exempt=False), ("declared", "gpt-6-astra"))
        self.assertEqual(hr.classify("abstract edit\nExecution-agent: Opus\n", exempt=False),
                         ("declared", "Opus"))
        two = "Do a thing\n\nBody.\n\nExecution-agent: claude-fable-5-1\n\nExecution-agent: gpt-6-astra\n"
        self.assertEqual(hr.classify(two, exempt=False), ("declared", "claude-fable-5-1"),
                         "trailer-only paragraphs above the last belong to the block")

    def test_execution_agent_none_declares_no_machine(self):
        # LESSON (AH-137): six hand-typed commits here say `Execution-agent:
        # none` (or `None`, or `Execution-agent:none`), and the value was read
        # as a model id, so the report asked for a trace record of a person's
        # own edit. `none` says no machine executed the commit.
        for line in ("Execution-agent: none", "Execution-agent: None", "Execution-agent:none"):
            body = f"Changed slide 8 by hand\n\n{line}\n"
            self.assertEqual(hr.classify(body, exempt=False), ("none-declared", ""), line)
            self.assertEqual(hr.declared_agents(body), [], line)
        # It never hides a real declaration in the same block.
        mixed = "Do a thing\n\nExecution-agent: none\nExecution-agent: claude-opus-5\n"
        self.assertEqual(hr.classify(mixed, exempt=False), ("declared", "claude-opus-5"))
        self.assertEqual(hr.declared_agents(mixed), ["claude-opus-5"])
        self.assertEqual(hr.classify("Old\n\nExecution-agent: none\nCo-Authored-By: Claude Fable 5 <x@y>\n",
                                     exempt=True), ("inferred", "claude-fable-5"),
                         "pre-rule, the old trailer is still read as evidence")

    def test_a_post_rule_co_author_trailer_reads_as_backfill_reads_it(self):
        # LESSON (AH-127): a post-rule commit with only the vendor's
        # Co-Authored-By trailer was a person's commit to backfill and
        # verify-range and a machine's to the report. The documented default
        # stands: absence of an execution trailer reads as a person's commit,
        # and the co-authorship claim is still reported.
        body = "Agent work\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
        self.assertEqual(hr.classify(body, exempt=False), ("none-declared", ""))
        self.assertTrue(any("co-authorship" in p for p in hr.commit_message_problems(body)))


class ReviewStatusLessons(unittest.TestCase):
    """Found while building the control the manuscript describes, 2026-08-21.

    The project claimed independent accountable acceptance and did not perform
    it: work published with nobody recorded as having accepted it. Two attempts
    to fix that overcorrected. The first refused to publish an unaccepted state;
    the second refused to record a reviewer whose name read like a model. Both
    treated the principle as a bouncer.

    It is not. An agent may commit, an agent may review, and work may publish
    unreviewed. The control is that the record says which of those happened, so
    that unreviewed work is never mistaken for accepted work.
    """

    def test_a_model_reviewer_is_recorded_not_refused(self):
        # LESSON: refusing a model reviewer threw away real information. Model
        # review happens and is worth having; it is simply not acceptance.
        self.assertEqual(hr.reviewer_kind("claude-fable-5"), "agent")
        self.assertEqual(hr.reviewer_kind("GPT-5 Sol"), "agent")
        # LESSON (2026-09-15): a name that does not read like a model used to be
        # classified as a person, so an adopter's model called "deepseek" would
        # have "accepted by a person". Nothing infers a person any more.
        self.assertEqual(hr.reviewer_kind("Alex Researcher"), "unknown")
        self.assertEqual(hr.reviewer_kind("deepseek/deepseek-v4-flash"), "agent")

    def test_a_declared_kind_beats_the_guess(self):
        # LESSON: name-sniffing is a heuristic. A person may be called anything,
        # so an explicit declaration has to win.
        self.assertEqual(hr.reviewer_kind("Ada Bot", declared="human"), "human")
        self.assertEqual(hr.reviewer_kind("Alex Researcher", declared="agent"), "agent")

    def test_model_review_does_not_make_a_commit_accepted(self):
        # LESSON: this is the distinction the whole record exists to preserve. A
        # model raising objections is not a person taking responsibility.
        sha = "a" * 40
        model = {sha: [{"kind": "agent", "reviewer": "claude-fable-5"}]}
        human = {sha: [{"kind": "agent", "reviewer": "claude-fable-5"},
                       {"kind": "human", "reviewer": "Alex Researcher"}]}
        self.assertEqual(hr.commit_state(sha, model), "model-reviewed")
        self.assertEqual(hr.commit_state(sha, human), "accepted")
        self.assertEqual(hr.commit_state(sha, {}), "unreviewed")

    def test_a_review_covers_only_the_commits_it_lists(self):
        # LESSON: the record stores the commits covered rather than a high-water
        # mark, so reviewing a batch cannot silently bless what comes after it.
        rows = [{"kind": "human", "reviewer": "Alex Researcher",
                 "covers": ["a" * 40, "b" * 40]}]
        original = hr.load_acceptances
        hr.load_acceptances = lambda: rows
        try:
            index = hr.review_index()
            self.assertEqual(hr.commit_state("a" * 40, index), "accepted")
            self.assertEqual(hr.commit_state("c" * 40, index), "unreviewed")
        finally:
            hr.load_acceptances = original

    def test_reporting_review_status_never_blocks(self):
        # LESSON: blocking at publish time was the wrong control. This reports,
        # so it must always succeed whatever it finds; a reporting check that can
        # fail becomes a check people route around.
        orig_reviewable, orig_load = hr.reviewable_commits, hr.load_acceptances
        hr.reviewable_commits = lambda: ["d" * 40]
        hr.load_acceptances = lambda: []
        try:
            self.assertEqual(hr.cmd_review_status(argparse.Namespace(limit=5)), 0,
                             "review status reports, it does not refuse")
        finally:
            hr.reviewable_commits, hr.load_acceptances = orig_reviewable, orig_load

    def test_every_command_the_module_defines_is_one_a_person_can_run(self):
        # LESSON (AH-146): `cmd_check_accepted`, the publish refusal this class
        # retired, stayed behind unwired and calling a function that no longer
        # existed. A command nobody can run is dead code that reads as policy.
        import re
        source = pathlib.Path(hr.__file__).read_text(encoding="utf-8")
        for name in re.findall(r"^def (cmd_\w+)\(", source, re.MULTILINE):
            self.assertRegex(source, rf"set_defaults\(func={name}\)", name)


OLDEST, MIDDLE, NEWEST = "a" * 40, "b" * 40, "c" * 40


class FakeRepo:
    """Stands in for git and for the recorded reviews, so no test needs either."""

    def __init__(self, commits, events=None, producers=None, ranges=None):
        self.commits = list(commits)      # newest first, the order rev-list gives
        self.events = events or {}        # sha -> review rows already recorded
        self.producers = producers or {}  # sha -> the model named in its trailer
        self.ranges = ranges or {}        # "base..head" -> shas, newest first

    def git(self, *args):
        if args[0] == "rev-list":
            return "\n".join(self.ranges.get(args[-1], []))
        if args[:3] == ("log", "-1", "--format=%B"):
            agent = self.producers.get(args[3])
            trailer = f"\n\nExecution-agent: {agent}" if agent else ""
            return f"a subject for {args[3][:4]}{trailer}"
        if args[:3] == ("log", "-1", "--format=%s"):
            return f"a subject for {args[3][:4]}"
        return ""


# The words of a delegation, which `accept --authorized-by` records with
# `--authority`; without them the row reads "authority not stated" (2026-10-14).
AUTHORITY = "T4: the release check may accept this batch against the test suite."


def model_review(reviewer="claude-fable-5"):
    return {"kind": "agent", "reviewer": reviewer}


def human_acceptance(reviewer="Alex Researcher"):
    return {"kind": "human", "reviewer": reviewer}


@contextlib.contextmanager
def acceptance_sandbox(repo):
    """Point the ledger at a temporary file and git at the fake repository."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger = pathlib.Path(tmp) / "acceptances.jsonl"
        saved = (hr.git, hr.reviewable_commits, hr.review_index, hr.ACCEPTANCES)
        hr.git = repo.git
        hr.reviewable_commits = lambda: list(repo.commits)
        hr.review_index = lambda: {k: list(v) for k, v in repo.events.items()}
        hr.ACCEPTANCES = ledger
        # A return opens a defect-ledger row, so that ledger is pointed at the
        # sandbox too; the real ai/defects.jsonl must never see a test's row.
        defects = mock.patch.dict(os.environ, {"TRACE_OS_DEFECT_LEDGER": str(pathlib.Path(tmp) / "defects.jsonl")})
        defects.start()
        try:
            yield ledger
        finally:
            defects.stop()
            hr.git, hr.reviewable_commits, hr.review_index, hr.ACCEPTANCES = saved


def defect_rows() -> list[dict]:
    path = pathlib.Path(os.environ["TRACE_OS_DEFECT_LEDGER"])
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def run_accept(by, agent=False, rev_range="", scope="", authorized_by="",
               criteria="", evidence="", objections="", person=False,
               decision="accept", independence="", recorded_by="", words="",
               authority=""):
    args = argparse.Namespace(by=by, agent=agent, scope=scope, range=rev_range,
                              authorized_by=authorized_by, criteria=criteria,
                              evidence=evidence, objections=objections,
                              person=person, decision=decision,
                              independence=independence, recorded_by=recorded_by,
                              words=words, authority=authority)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = hr.cmd_accept(args)
    return code, out.getvalue()


def recorded(ledger):
    if not ledger.exists():
        return []
    return [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]


class AcceptanceSelectionLessons(unittest.TestCase):
    """Found reading the accept path, 2026-09-07.

    The default batch was `unreviewed` for every reviewer. A model review
    therefore moved a commit out of the set a person would be offered next,
    although human acceptance is exactly what a model review is waiting for. A
    person running `accept --by "Full Name"` after a model review silently
    accepted less than they had read.

    The repair changes selection only. Acceptance policy is untouched: a model
    review is still not acceptance, an agent must still not record a human
    acceptance, and classifying a reviewer by declaration or name authenticates
    no one and shows no independence.
    """

    def test_a_person_default_includes_model_reviewed_work(self):
        # LESSON: the batch a person is offered must not shrink because a model
        # looked first. Model-reviewed work is the work most ready to accept.
        repo = FakeRepo(
            commits=[NEWEST, MIDDLE, OLDEST],
            events={NEWEST: [human_acceptance()], MIDDLE: [model_review()]},
        )
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("Alex Researcher", person=True)
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "human")
            self.assertEqual(row["covers"], [OLDEST, MIDDLE],
                             "unreviewed and model-reviewed work, oldest first")
            self.assertEqual(row["count"], 2)
            self.assertFalse(row["self_review"])
            self.assertIn("accepted 2 commit(s)", out)
        self.assertEqual(ledger_fingerprint(), LEDGER_AT_IMPORT,
                         "the real ledger must never be written by a test")

    def test_a_wholly_model_reviewed_batch_still_awaits_a_person(self):
        # LESSON: the two defaults search different sets, so each empty message
        # has to say which set it searched.
        events = {MIDDLE: [model_review()], OLDEST: [model_review()]}
        repo = FakeRepo(commits=[MIDDLE, OLDEST], events=events)

        with acceptance_sandbox(repo) as ledger:
            code, _ = run_accept("Alex Researcher", person=True)
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["covers"], [OLDEST, MIDDLE])

        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-fable-5", agent=True)
            self.assertEqual(code, 0, "an empty batch reports, it does not fail")
            self.assertEqual(recorded(ledger), [], "nothing to review, nothing written")
            self.assertIn("nothing unreviewed", out)
            self.assertNotIn("awaiting a person", out)

    def test_a_person_can_accept_without_any_model_review_first(self):
        # LESSON: model review is optional. Acceptance is a person's own act and
        # never requires a model to have gone first.
        repo = FakeRepo(commits=[MIDDLE, OLDEST])
        with acceptance_sandbox(repo) as ledger:
            self.assertEqual(run_accept("Alex Researcher", person=True)[0], 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "human")
            self.assertEqual(row["covers"], [OLDEST, MIDDLE])
            self.assertEqual(row["count"], 2)

    def test_work_a_person_accepted_is_not_offered_again(self):
        # LESSON: re-accepting settled work by default would make the count
        # meaningless. An explicit range is how you review something twice.
        events = {MIDDLE: [human_acceptance()], OLDEST: [human_acceptance()]}
        repo = FakeRepo(commits=[MIDDLE, OLDEST], events=events)
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("Alex Researcher", person=True)
            self.assertEqual(code, 0)
            self.assertEqual(recorded(ledger), [])
            self.assertIn("nothing awaiting a person's acceptance", out)

    def test_an_explicit_range_is_taken_exactly_as_given(self):
        # LESSON: naming a range is a deliberate act, including when it covers
        # work already reviewed. Neither default may override it.
        rng = "aaaaaaa..ccccccc"
        events = {sha: [human_acceptance(), model_review()]
                  for sha in (NEWEST, MIDDLE, OLDEST)}
        repo = FakeRepo(commits=[NEWEST, MIDDLE, OLDEST], events=events,
                        ranges={rng: [NEWEST, MIDDLE, OLDEST]})

        with acceptance_sandbox(repo) as ledger:
            self.assertEqual(run_accept("Alex Researcher", person=True, rev_range=rng)[0], 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "human")
            self.assertEqual(row["covers"], [OLDEST, MIDDLE, NEWEST])
            self.assertEqual(row["count"], 3)

        with acceptance_sandbox(repo) as ledger:
            self.assertEqual(run_accept("claude-fable-5", agent=True, rev_range=rng)[0], 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertEqual(row["covers"], [OLDEST, MIDDLE, NEWEST])

    def test_a_model_reviewing_its_own_output_is_still_flagged(self):
        # LESSON: the weakest reading of all is a model reviewing what it wrote.
        # Changing selection must not lose the flag that says so.
        repo = FakeRepo(commits=[MIDDLE, OLDEST],
                        producers={OLDEST: "claude-opus-5"})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-opus-5", agent=True)
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertIn("claude-opus-5", row["producer_agents"])
            self.assertTrue(row["self_review"])
            self.assertIn("Flagged as self-review", out)

        with acceptance_sandbox(repo) as ledger:
            self.assertEqual(run_accept("Alex Researcher", person=True)[0], 0)
            row, = recorded(ledger)
            self.assertFalse(row["self_review"], "a person did not produce it")


class DelegatedAcceptanceLessons(unittest.TestCase):
    """Added 2026-09-13, when the repository followed the paper's Section 4.

    Until then the record allowed acceptance by a person only. The paper's move
    from coding to AI-assisted work generalized the accepting actor to a person,
    agent or process independent of production, with a named researcher
    accountable for the delegation. Miros's direction, verbatim: "this new
    reading is more in line with moving from coding to ai-assisted research and
    taking a higher level of abstraction."

    The conditions come from the paper: the actor must be independent of
    production, must be able to examine the evidence and reject, and must record
    what it examined; a favorable critique is not a decision.
    """

    def test_a_delegated_actor_accepts_under_a_named_researcher(self):
        # LESSON: acceptance is an act of an authorized actor, not of a species.
        repo = FakeRepo(commits=[MIDDLE, OLDEST], events={MIDDLE: [model_review()]})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-opus-5", authorized_by="Alex Researcher",
                                   criteria="builds, tests, citation check",
                                   evidence="ai/records/x/checks.jsonl", authority=AUTHORITY)
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "delegated")
            self.assertEqual(row["authorized_by"], "Alex Researcher")
            self.assertEqual(row["authority"], AUTHORITY)
            self.assertIn(f"Authority, verbatim: {AUTHORITY}", out)
            self.assertEqual(row["decision"], "accept")
            self.assertEqual(row["covers"], [OLDEST, MIDDLE],
                             "the acceptance default: everything not yet accepted")
            self.assertFalse(row["self_review"])
            self.assertIn("under Alex Researcher's authority", out)
            self.assertIn("remains accountable", out)

    def test_delegated_acceptance_counts_as_accepted_and_stays_distinguishable(self):
        # LESSON: accepted is accepted, and the record still says by whom.
        sha = "a" * 40
        index = {sha: [{"kind": "delegated", "reviewer": "claude-opus-5",
                        "authorized_by": "Alex Researcher"}]}
        self.assertEqual(hr.commit_state(sha, index), "accepted")
        self.assertEqual(hr.commit_state_detail(sha, index), "accepted-delegated")
        person = {sha: [{"kind": "human", "reviewer": "Alex Researcher"}]}
        self.assertEqual(hr.commit_state_detail(sha, person), "accepted-by-person")

    def test_an_actor_that_executed_the_work_cannot_accept_it(self):
        # LESSON: independence is definitional. The attempt is recorded, as
        # self-review, so it is never mistaken for acceptance.
        repo = FakeRepo(commits=[MIDDLE, OLDEST], producers={OLDEST: "claude-opus-5"})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-opus-5", authorized_by="Alex Researcher",
                                   evidence="checks.jsonl")
            self.assertEqual(code, 0, "recorded, not refused")
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertTrue(row["self_review"])
            self.assertEqual(row["decision"], "self-review, not acceptance")
            self.assertIn("NOT independent", out)
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state(OLDEST, index), "model-reviewed")

    def test_a_person_declared_under_a_producers_name_is_self_review_not_acceptance(self):
        # LESSON (AH-134): the independence check ran for a delegated actor
        # only, so `accept --person --by gpt-9` over work gpt-9 executed was
        # written as a person's acceptance, with gpt-9 listed as the producer
        # in the same row. The match is on the execution trailers, never on
        # whether the name reads like a model.
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "gpt-9"})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("gpt-9", person=True)
            self.assertEqual(code, 0, "recorded, not refused")
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertEqual(row["declared_kind"], "human")
            self.assertTrue(row["self_review"])
            self.assertEqual(row["decision"], hr.SELF_REVIEW_DECISION)
            self.assertIn("NOT independent", out)
            self.assertNotIn("Recorded as a person's decision", out)
            self.assertEqual(hr.validate_acceptances([row])[0], [])
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state_detail(OLDEST, index), "model-reviewed")
        # Said to an agent: the row stays valid, and who typed it and the
        # words are kept, but it is no person's decision.
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("gpt-9", person=True, recorded_by="claude-opus-5",
                                   words="yes, accept it")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertNotIn("recorded_by", row)
            self.assertEqual(row["recorded_by_stated"], "claude-opus-5")
            self.assertEqual(row["words"], "yes, accept it")
            self.assertEqual(hr.validate_acceptances([row])[0], [])
        # A person whose name merely reads like a model is still a person.
        with acceptance_sandbox(repo) as ledger:
            run_accept("Ada Bot", person=True)
            row, = recorded(ledger)
            self.assertEqual((row["kind"], row["decision"]), ("human", "accept"))
            self.assertFalse(row["self_review"])
        # And a row flagged as self-review is never read as acceptance,
        # whatever kind it was written with.
        flagged = {OLDEST: [{"kind": "human", "reviewer": "gpt-9", "decision": "accept",
                             "self_review": True}]}
        self.assertNotEqual(hr.commit_state(OLDEST, flagged), "accepted")

    def test_a_delegated_decision_prints_its_decision_word(self):
        # LESSON (AH-144): the verb was cut back to a stem and printed
        # "Recorded as delegated accepte." and "returne".
        for decision in hr.DECISIONS:
            repo = FakeRepo(commits=[OLDEST])
            with self.subTest(decision=decision), acceptance_sandbox(repo):
                code, out = run_accept("release-check", authorized_by="Alex Researcher",
                                       decision=decision, objections="section 2",
                                       authority=AUTHORITY)
                self.assertEqual(code, 0)
                self.assertIn(f"Recorded as a delegated decision: {decision}.", out)
                self.assertNotIn("accepte.", out)
                self.assertNotIn("returne.", out)

    def test_accept_reads_producers_as_the_backfill_does(self):
        # LESSON (AH-123, found again by the 2026-09-30 review): `accept` kept
        # its own reading of the message, so a legacy `agent:` prefix let the
        # producer accept its own work, and a model named in the explanation
        # was taken for a producer.
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "agent:claude-opus-5"})
        with acceptance_sandbox(repo) as ledger:
            run_accept("claude-opus-5", authorized_by="Alex Researcher", evidence="checks.jsonl")
            row, = recorded(ledger)
            self.assertTrue(row["self_review"])
            self.assertEqual(row["producer_agents"], ["claude-opus-5"])

        class ProseRepo(FakeRepo):
            def git(self, *args):
                if args[:3] == ("log", "-1", "--format=%B"):
                    return ("a subject\n\nThe first pass was\nExecution-agent: gpt-6-astra and this was not.\n\n"
                            "Execution-Agent: claude-opus-5\n")
                return super().git(*args)

        with acceptance_sandbox(ProseRepo(commits=[OLDEST])) as ledger:
            run_accept("gpt-6-astra", authorized_by="Alex Researcher", evidence="checks.jsonl")
            row, = recorded(ledger)
            self.assertEqual(row["producer_agents"], ["claude-opus-5"])
            self.assertFalse(row["self_review"])

    def test_unstated_evidence_is_recorded_as_such_not_refused(self):
        # LESSON: the same rule as intake. A gap in the record can be acted on;
        # an entry that was refused leaves no gap to act on.
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("release-check", authorized_by="Alex Researcher",
                                   authority=AUTHORITY)
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "delegated")
            self.assertEqual(row["evidence"], "nothing stated")
            self.assertEqual(row["criteria"], "nothing stated")
            self.assertIn("No evidence stated", out)

    def test_advisory_review_and_delegated_acceptance_are_not_the_same_command(self):
        # LESSON: a favorable critique is not a decision, so the two must not be
        # recordable in one breath.
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, _ = run_accept("claude-opus-5", agent=True, authorized_by="Alex Researcher")
            self.assertEqual(code, 1)
            self.assertEqual(recorded(ledger), [])

    def test_a_delegation_without_its_words_is_recorded_as_advisory_and_says_so(self):
        # LESSON (2026-10-14 review, A): `accept --authorized-by` stored the
        # researcher's name and no authorization, so the limits of the
        # delegation were nowhere, while agentic_review.py already required the
        # words (AH-154). The row is written, reads "authority not stated", and
        # is advisory, as such a review run is; nothing is refused.
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("release-check", authorized_by="Alex Researcher",
                                   decision="return", objections="section 2 is thin")
            self.assertEqual(code, 0, "recorded, not refused")
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertEqual(row["declared_kind"], "delegated")
            self.assertEqual(row["authorized_by"], "Alex Researcher")
            self.assertEqual(row["authority"], "not stated")
            self.assertEqual(row["decision"], hr.NO_AUTHORITY_DECISION)
            self.assertEqual(row["decision_stated"], "return")
            self.assertEqual(row["objections"], "section 2 is thin")
            self.assertFalse(row["self_review"])
            self.assertNotIn("defect", row)
            self.assertEqual(defect_rows(), [], "an advisory return opens no ledger row")
            self.assertIn("Authority not stated", out)
            self.assertIn("--authority", out)
            problems, notes = hr.validate_acceptances([row])
            self.assertEqual(problems, [])
            self.assertTrue(any("authority not stated" in n for n in notes), notes)
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state_detail(OLDEST, index), "model-reviewed")

    def test_a_delegation_with_its_words_is_a_delegated_decision(self):
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("release-check", authorized_by="Alex Researcher",
                                   authority=AUTHORITY, evidence="checks.jsonl")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual((row["kind"], row["decision"], row["authority"]),
                             ("delegated", "accept", AUTHORITY))
            self.assertNotIn("declared_kind", row)
            self.assertEqual(hr.validate_acceptances([row])[0], [])
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state_detail(OLDEST, index), "accepted-delegated")

    def test_authority_without_a_researcher_is_a_usage_error_and_writes_nothing(self):
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run_accept("release-check", authority=AUTHORITY)
            self.assertEqual(code, 1)
            self.assertIn("--authorized-by", err.getvalue())
            self.assertEqual(recorded(ledger), [])

    def test_a_delegated_row_written_before_authority_existed_reads_as_before(self):
        # Back-compatibility: no `authority` field is not "not stated"; the row
        # keeps the meaning it was written with.
        old = {"kind": "delegated", "kind_basis": "declared", "reviewer": "release-check",
               "at": "2026-09-15T00:00:00Z", "covers": [OLDEST], "count": 1,
               "authorized_by": "A", "decision": "accept", "self_review": False}
        problems, notes = hr.validate_acceptances([old])
        self.assertEqual(problems, [])
        self.assertFalse(any("authority" in n for n in notes), notes)
        self.assertEqual(hr.commit_state_detail(OLDEST, {OLDEST: [old]}), "accepted-delegated")
        # A malformed new field is reported where it can be fixed.
        self.assertTrue(hr.validate_acceptances([dict(old, authority="")])[0])
        bare = dict(old, authority="not stated"); del bare["authorized_by"]
        self.assertTrue(any("authority without" in p for p in hr.validate_acceptances([bare])[0]))


class VerificationInterfaceLessons(unittest.TestCase):
    """Found reading the CI job, 2026-09-07.

    `verify` printed FAIL lines and returned zero, so the workflow step that ran
    it over every tracked record passed on a broken one.
    A check that cannot fail records nothing, which is the defect this project
    diagnoses elsewhere, so the findings became a structure a caller can act on.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = pathlib.Path(self.tmp.name)
        (self.record / "evidence.json").write_text(json.dumps({
            "record_id": "test",
            "consent": {"third_party_review": "cleared"},
            "turns": [{"id": "T1", "text": "first"}],
        }))
        (self.record / "summary.md").write_text("The author set the direction (T1).")
        (self.record / "checks.jsonl").write_text(json.dumps({
            "kind": "build", "runner": "latexmk", "method": "main.tex",
            "result": "exit=0", "artifact": "build.log"}) + "\n")
        hr.cmd_cover(argparse.Namespace(record=str(self.record)))

    def tearDown(self):
        self.tmp.cleanup()

    def _break_it(self):
        # A check line that names no runner, method, result, or artifact is an
        # assertion rather than evidence, and the record must say so.
        (self.record / "checks.jsonl").write_text(json.dumps({"kind": "build"}) + "\n")

    def _verify(self, **kwargs):
        args = argparse.Namespace(record=str(self.record), range=None,
                                  json=False, strict=False)
        for name, value in kwargs.items():
            setattr(args, name, value)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = hr.cmd_verify(args)
        return code, out.getvalue()

    def test_a_sound_record_evaluates_as_ok(self):
        result = hr.evaluate_record(self.record)
        self.assertTrue(result["ok"])
        self.assertEqual(result["problems"], [])
        self.assertTrue(result["notes"])

    def test_a_broken_record_carries_its_problems_in_the_structure(self):
        # LESSON: the caller needs the finding, not a line of text to grep.
        self._break_it()
        result = hr.evaluate_record(self.record)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing" in p for p in result["problems"]))
        self.assertEqual(result["record"], str(self.record))

    def test_the_default_report_is_unchanged_and_still_exits_zero(self):
        # LESSON: humans read this over every record at once, so the default must
        # keep going past a broken one. The exit code is not the report.
        self._break_it()
        code, text = self._verify()
        self.assertEqual(code, 0)
        self.assertIn("FAIL", text)
        self.assertIn("Report-only: nothing was blocked.", text)

    def test_strict_fails_on_the_same_record(self):
        # LESSON: this is for a caller that must not proceed on a broken record.
        # Opt-in, so no human use changed.
        self._break_it()
        code, text = self._verify(strict=True)
        self.assertEqual(code, 1)
        self.assertIn("Strict mode", text)

    def test_strict_passes_a_sound_record(self):
        code, _ = self._verify(strict=True)
        self.assertEqual(code, 0)

    def test_a_malformed_coverage_entry_is_reported_not_raised(self):
        # LESSON (AH-145): an entry with no status was reported as unknown and
        # then indexed by the next line, so `verify` ended in a KeyError; an
        # entry with no turn failed the same way one line earlier.
        path = self.record / "coverage.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        cases = (([{"turn": "T1"}], "unknown coverage status for T1"),
                 ([{"status": "summarized"}], "coverage entry 1 names no turn"),
                 (["T1"], "coverage entry 1 names no turn"),
                 (None, "coverage.json holds no list of turns"))
        for turns, problem in cases:
            with self.subTest(turns=turns):
                path.write_text(json.dumps(dict(manifest, turns=turns)), encoding="utf-8")
                result = hr.evaluate_record(self.record)
                self.assertFalse(result["ok"])
                self.assertIn(problem, result["problems"])

    def test_json_emits_the_same_findings_as_the_evaluator(self):
        self._break_it()
        code, text = self._verify(json=True)
        self.assertEqual(code, 0, "json alone stays report-only")
        self.assertEqual(json.loads(text), hr.evaluate_record(self.record))
        strict_code, strict_text = self._verify(json=True, strict=True)
        self.assertEqual(strict_code, 1)
        self.assertFalse(json.loads(strict_text)["ok"])


class IntakeLessons(unittest.TestCase):
    """Found in the same review, 2026-08-21.

    Controlled boundary transitions had only its promotion half. Outside
    material entered with no record of what was admitted or what was checked, so
    the entry boundary existed in the prose and nowhere else.
    """

    def test_an_unchecked_entry_is_recorded_rather_than_refused(self):
        # LESSON: refusing an entry that names no check does not produce a check.
        # It produces an unrecorded entry, which is strictly worse. Record the
        # gap so someone can close it.
        import tempfile, pathlib as pl
        with tempfile.TemporaryDirectory() as tmp:
            original = (hr.INTAKE, hr.git)
            hr.INTAKE = pl.Path(tmp) / "intake.jsonl"
            # No git user either: since rc22 --by defaults to it, and with
            # none the actor still reads "not stated" rather than a guess.
            hr.git = lambda *a, **k: ""
            try:
                args = argparse.Namespace(what="a dataset", origin="", by="",
                                          checked="", used_for="")
                self.assertEqual(hr.cmd_intake(args), 0)
                row = json.loads(hr.INTAKE.read_text().strip())
                self.assertEqual(row["checked"], "nothing stated",
                                 "the gap must be in the record, not hidden")
                self.assertEqual(row["admitted_by"], "not stated")
            finally:
                hr.INTAKE, hr.git = original


class DeclaredActorLessons(unittest.TestCase):
    """Added 2026-09-15, after the kit assessment and its three model reviews.

    An unrecognized reviewer name used to be classified as a person, so a
    delegated acceptance by an adopter's local model would have read as
    "accepted by a person". A GLM 5.3 round and a Codex round both reproduced
    it. Nothing infers a person now: a person declares, a delegated actor is
    authorized, a model is flagged, and anything else is recorded as an actor
    whose kind was not declared, which is never acceptance.
    """

    def test_an_undeclared_actor_is_recorded_and_is_never_acceptance(self):
        repo = FakeRepo(commits=[MIDDLE, OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("release-check-of-unknown-kind")
            self.assertEqual(code, 0, "recorded, not refused")
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "unknown")
            self.assertEqual(row["kind_basis"], "not declared")
            self.assertIn("NOT DECLARED", out)
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state_detail(OLDEST, index), "undeclared-actor")
            self.assertNotEqual(hr.commit_state(OLDEST, index), "accepted")

    def test_a_person_declares_and_the_basis_is_recorded(self):
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            self.assertEqual(run_accept("Alex Researcher", person=True)[0], 0)
            row, = recorded(ledger)
            self.assertEqual((row["kind"], row["kind_basis"]), ("human", "declared"))
            self.assertEqual(row["decision"], "accept")
            self.assertEqual(row["independence"]["declared"], "nothing stated")

    def test_person_agent_and_delegated_are_one_choice(self):
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, _ = run_accept("Alex Researcher", person=True, agent=True)
            self.assertEqual(code, 1)
            self.assertEqual(recorded(ledger), [])

    def test_a_return_or_refusal_is_a_recorded_decision_that_leaves_work_unaccepted(self):
        repo = FakeRepo(commits=[MIDDLE, OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("Alex Researcher", person=True, decision="return",
                                   objections="table 2 does not match the code")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["decision"], "return")
            self.assertIn("stays unaccepted", out)
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state_detail(OLDEST, index), "returned")
            self.assertNotEqual(hr.commit_state(OLDEST, index), "accepted")
            # The default batch offers returned work again: it is not accepted.
            self.assertEqual(hr.default_batch("human", index), [MIDDLE, OLDEST])

    def test_a_persons_return_opens_a_ledger_row_from_the_objection(self):
        # LESSON (2026-09-22): a return is a finding. Before this, a return was
        # an acceptance row and nothing else, so the fix and its recheck, the
        # whole of E, had no row to land on unless somebody typed `open`.
        repo = FakeRepo(commits=[MIDDLE, OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("Alex Researcher", person=True, decision="return",
                                   objections="the pooled share is the mean of the yearly percentages")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            finding, = defect_rows()
            self.assertEqual(row["defect"], finding["id"])
            self.assertEqual(finding["label"], "the pooled share is the mean of the yearly percentages")
            self.assertEqual(finding["opened_by"], "Alex Researcher")
            self.assertEqual(finding["state"], "open")
            self.assertEqual(finding["severity"], "medium")
            self.assertIn("severity medium by default", finding["note"])
            self.assertEqual(finding["returned"]["covers"], row["covers"])
            self.assertIn(f"Opened ledger row {finding['id']} from the objection", out)

    def test_a_ledger_that_cannot_be_read_never_loses_the_persons_return(self):
        repo = FakeRepo(commits=[MIDDLE, OLDEST])
        with acceptance_sandbox(repo) as ledger:
            pathlib.Path(os.environ["TRACE_OS_DEFECT_LEDGER"]).write_text("{not json\n")
            code, out = run_accept("Alex Researcher", person=True, decision="return",
                                   objections="the table does not match the code")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["decision"], "return")
            self.assertNotIn("defect", row)
            self.assertIn("the ledger row could not be opened", out)

    def test_a_refusal_and_a_model_review_open_no_ledger_row(self):
        repo = FakeRepo(commits=[MIDDLE, OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, _ = run_accept("Alex Researcher", person=True, decision="refuse",
                                 objections="not to be used")
            self.assertEqual(code, 0)
            code, _ = run_accept("claude-fable-5", agent=True)
            self.assertEqual(code, 0)
            self.assertEqual(len(recorded(ledger)), 2)
            self.assertEqual(defect_rows(), [])
            self.assertNotIn("defect", recorded(ledger)[0])

    def test_the_latest_decision_by_an_accepting_actor_wins(self):
        sha = OLDEST
        returned_then_accepted = {sha: [
            {"kind": "human", "reviewer": "A", "decision": "return"},
            {"kind": "delegated", "reviewer": "release-check", "authorized_by": "A", "decision": "accept"}]}
        accepted_then_refused = {sha: [
            {"kind": "human", "reviewer": "A", "decision": "accept"},
            {"kind": "human", "reviewer": "A", "decision": "refuse"}]}
        self.assertEqual(hr.commit_state_detail(sha, returned_then_accepted), "accepted-delegated")
        self.assertEqual(hr.commit_state_detail(sha, accepted_then_refused), "refused")

    def test_the_decision_that_stands_is_the_latest_in_time_not_the_last_in_the_file(self):
        # LESSON (AH-135): acceptances.jsonl is union-merged, so the order of
        # its rows is whoever merged last, not when each decision was made.
        # Bob returned at :34 and Ann accepted at :36; read by file position,
        # the same history said accepted in one clone and returned in another.
        sha = OLDEST
        bob = {"kind": "human", "reviewer": "Bob", "decision": "return", "at": "2026-09-30T07:14:34Z",
               "defect": "AH-900"}
        ann = {"kind": "human", "reviewer": "Ann", "decision": "accept", "at": "2026-09-30T07:14:36Z",
               "recorded_by": "some-model", "words": "yes"}
        for events in ([bob, ann], [ann, bob]):
            index = {sha: list(events)}
            self.assertEqual(hr.commit_state_detail(sha, index), "accepted-by-person")
            self.assertTrue(hr.recorded_from_words(sha, index))
            with mock.patch.object(hr, "review_index", lambda: index):
                self.assertEqual(hr.standing_returns([{"commit": sha}]), [])
        later_bob = dict(bob, at="2026-09-30T07:14:40Z")
        for events in ([later_bob, ann], [ann, later_bob]):
            index = {sha: list(events)}
            self.assertEqual(hr.commit_state_detail(sha, index), "returned")
            self.assertFalse(hr.recorded_from_words(sha, index))
            with mock.patch.object(hr, "review_index", lambda: index):
                self.assertEqual(hr.standing_returns([{"commit": sha}]),
                                 ["returned by Bob, ledger row AH-900 opened from the objection"])

    def test_decisions_in_one_second_resolve_the_same_way_in_every_clone(self):
        # LESSON (AH-135): a timestamp has one-second resolution, so two
        # decisions can share it, and a tie broken by file position is the
        # merge-order bug again. The stricter decision stands, refuse over
        # return over accept, because acceptance is never inferred from an
        # ambiguity; review-status says when that happened.
        import itertools
        sha, at = OLDEST, "2026-09-30T07:14:34Z"
        accept = {"kind": "human", "reviewer": "Ann", "decision": "accept", "at": at}
        back = {"kind": "delegated", "reviewer": "release-check", "authorized_by": "Ann",
                "decision": "return", "at": at}
        refuse = {"kind": "human", "reviewer": "Bob", "decision": "refuse", "at": at}
        for events in itertools.permutations([accept, back]):
            self.assertEqual(hr.commit_state_detail(sha, {sha: list(events)}), "returned")
            self.assertTrue(hr.standing_decision(list(events))[1], "a same-second conflict")
        for events in itertools.permutations([accept, back, refuse]):
            self.assertEqual(hr.commit_state_detail(sha, {sha: list(events)}), "refused")
        agreeing = [accept, dict(accept, reviewer="Bob")]
        self.assertFalse(hr.standing_decision(agreeing)[1], "two acceptances do not conflict")

    def test_a_row_with_no_readable_time_is_read_before_the_timed_rows(self):
        # Rows written without a parseable `at` keep the order they were
        # written in, as the test above this one pins, and come before rows
        # that carry a time.
        sha = OLDEST
        legacy = {"kind": "human", "reviewer": "A", "decision": "return"}
        timed = {"kind": "human", "reviewer": "B", "decision": "accept", "at": "2026-09-30T07:14:34Z"}
        for events in ([legacy, timed], [timed, legacy]):
            self.assertEqual(hr.commit_state_detail(sha, {sha: list(events)}), "accepted-by-person")

    def test_a_model_review_records_no_decision(self):
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, _ = run_accept("claude-fable-5", agent=True, decision="refuse")
            self.assertEqual(code, 1)
            self.assertEqual(recorded(ledger), [])

    def test_what_a_non_accepting_actor_states_is_kept_and_is_not_a_decision(self):
        # LESSON (AH-138): a return and its objection, typed without --person,
        # left a row with neither, no ledger row and no word about it, so the
        # objection was lost. The words are kept as what was stated; the row
        # carries no decision, opens no ledger row, and says why.
        repo = FakeRepo(commits=[OLDEST])
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("Jane Doe", decision="return",
                                   objections="section 2 misquotes the source")
            self.assertEqual(code, 0, "recorded, not refused")
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "unknown")
            self.assertNotIn("decision", row)
            self.assertEqual(row["decision_stated"], "return")
            self.assertEqual(row["objections"], "section 2 misquotes the source")
            self.assertEqual(defect_rows(), [])
            self.assertIn("not recorded as a decision", out)
            self.assertEqual(hr.validate_acceptances([row])[0], [])
            index = {sha: [row] for sha in row["covers"]}
            self.assertEqual(hr.commit_state_detail(OLDEST, index), "undeclared-actor")
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("gpt-9", decision="refuse", objections="not usable")
            row, = recorded(ledger)
            self.assertEqual((row["kind"], row["kind_basis"]), ("agent", "inferred from the name"))
            self.assertNotIn("decision", row)
            self.assertEqual(row["decision_stated"], "refuse")
            self.assertEqual(hr.validate_acceptances([row])[0], [])
            self.assertIn("not recorded as a decision", out)
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-fable-5", agent=True, objections="table 2 is off",
                                   evidence="checks.jsonl", criteria="the tests")
            row, = recorded(ledger)
            self.assertNotIn("decision", row)
            self.assertNotIn("decision_stated", row)
            self.assertEqual(row["objections"], "table 2 is off")
            self.assertEqual(row["evidence"], "checks.jsonl")
            self.assertEqual(row["criteria"], "the tests")
            self.assertIn("advisory", out)
            self.assertEqual(hr.validate_acceptances([row])[0], [])

    def test_producer_matching_ignores_case(self):
        # LESSON: `GPT-6-ASTRA` accepting work executed by `gpt-6-astra` was
        # recorded as delegated acceptance because the comparison was exact.
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "gpt-6-astra"})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("GPT-6-ASTRA", authorized_by="Alex Researcher")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertTrue(row["self_review"])
            self.assertIn("NOT independent", out)

    def test_producer_matching_ignores_a_bracketed_suffix_and_a_date(self):
        # LESSON: a CLI states `claude-opus-5-5[1m]` or `claude-haiku-4-5-20251001`
        # where the trailer states the short id, and each is the model that
        # executed the work. The row keeps the ids exactly as given.
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "claude-opus-5-5"})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-opus-5-5[1m]", agent=True)
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertTrue(row["self_review"])
            self.assertEqual(row["reviewer"], "claude-opus-5-5[1m]")
            self.assertEqual(row["producer_agents"], ["claude-opus-5-5"])
            self.assertIn("Flagged as self-review", out)
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "claude-haiku-4-5"})
        with acceptance_sandbox(repo) as ledger:
            code, out = run_accept("claude-haiku-4-5-20251001", authorized_by="Alex Researcher")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["kind"], "agent")
            self.assertTrue(row["self_review"])
            self.assertEqual(row["decision"], "self-review, not acceptance")
            self.assertIn("NOT independent", out)

    def test_a_different_model_stays_independent(self):
        # Only case, a bracketed suffix and a date come off: no prefix or
        # lineage matching makes another model the producer.
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "claude-opus-5-5"})
        for reviewer in ("claude-opus-5[1m]", "claude-opus-5-5-1", "claude-fable-5-20260101"):
            with self.subTest(reviewer=reviewer), acceptance_sandbox(repo) as ledger:
                code, out = run_accept(reviewer, authorized_by="Alex Researcher",
                                       authority=AUTHORITY)
                self.assertEqual(code, 0)
                row, = recorded(ledger)
                self.assertEqual(row["kind"], "delegated")
                self.assertFalse(row["self_review"])
                self.assertNotIn("NOT independent", out)

    def test_an_independence_declaration_is_recorded_beside_the_trailer_evidence(self):
        repo = FakeRepo(commits=[OLDEST], producers={OLDEST: "claude-opus-5"})
        with acceptance_sandbox(repo) as ledger:
            code, _ = run_accept("gemini-3.7", authorized_by="Alex Researcher",
                                 independence="separate run; did not propose or select the candidate")
            self.assertEqual(code, 0)
            row, = recorded(ledger)
            self.assertEqual(row["independence"]["execution_producers"], ["claude-opus-5"])
            self.assertIn("did not propose", row["independence"]["declared"])


class RegistryValidationLessons(unittest.TestCase):
    """Added 2026-09-15. The appended registries were never read back critically.

    A row with an undeclared kind, an unknown decision or a verified state with
    no recheck event was read as whatever the reporting code assumed.
    """

    def good_acceptance(self, **over):
        row = {"kind": "delegated", "kind_basis": "declared", "reviewer": "x",
               "at": "2026-09-15T00:00:00Z", "covers": [OLDEST], "count": 1,
               "authorized_by": "A", "decision": "accept", "self_review": False}
        row.update(over)
        return row

    def test_a_sound_registry_has_no_problems(self):
        problems, _ = hr.validate_acceptances([self.good_acceptance()])
        self.assertEqual(problems, [])

    def test_bad_kind_decision_and_covers_are_reported(self):
        problems, _ = hr.validate_acceptances([
            self.good_acceptance(kind="person"),
            self.good_acceptance(decision="maybe"),
            self.good_acceptance(covers=[], count=0),
            self.good_acceptance(authorized_by=""),
        ])
        self.assertEqual(len(problems), 4, problems)

    def test_a_model_review_with_a_decision_is_flagged(self):
        problems, _ = hr.validate_acceptances([
            {"kind": "agent", "reviewer": "m", "at": "t", "covers": [OLDEST], "count": 1,
             "self_review": False, "decision": "accept"}])
        self.assertTrue(any("carries no decision" in p for p in problems))

    def test_legacy_rows_are_read_with_a_note_not_a_failure(self):
        row = self.good_acceptance(); del row["kind_basis"]
        problems, notes = hr.validate_acceptances([row])
        self.assertEqual(problems, [])
        self.assertTrue(any("predate declared actor kinds" in n for n in notes))

    def test_status_readers_validate_before_classifying_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'acceptances.jsonl'
            row = self.good_acceptance()
            del row['authorized_by']
            path.write_text(json.dumps(row) + '\n')
            with mock.patch.object(hr, 'ACCEPTANCES', path):
                with self.assertRaisesRegex(ValueError, 'authorizing researcher'):
                    hr.review_index()
                result = hr.validate_registries(acceptances=path)
                self.assertFalse(result['ok'])
            row['authorized_by'] = 'A'
            del row['kind_basis']
            path.write_text(json.dumps(row) + '\n')
            with mock.patch.object(hr, 'ACCEPTANCES', path):
                self.assertEqual(hr.commit_state_detail(OLDEST, hr.review_index()), 'accepted-delegated')

    def test_intake_and_transition_rows_are_validated(self):
        problems, _ = hr.validate_intake([{"what": "x", "admitted_by": "y", "at": "t", "checked": ""}])
        self.assertTrue(any("no checked" in p for p in problems))
        problems, _ = hr.validate_transitions([
            {"id": "TR-001", "kind": "promotion", "object": "paper/x.tex", "by": "a",
             "authority": "T3", "checks": "c", "decision": "adopt", "at": "t", "commits": []}])
        self.assertTrue(any("decision" in p for p in problems))

    def test_the_real_registries_of_this_repository_validate(self):
        import os
        before = os.getcwd(); os.chdir(REPO)
        try:
            result = hr.validate_registries()
        finally:
            os.chdir(before)
        self.assertTrue(result["ok"], result["problems"])

    def test_an_id_two_rows_share_is_a_note_not_a_problem(self):
        row = {"id": "TR-022", "kind": "promotion", "object": "workspace/sandbox/x.md", "by": "a",
               "authority": "T1", "checks": "c", "decision": "promote", "at": "t", "commits": [OLDEST]}
        problems, notes = hr.validate_transitions([row, dict(row, by="b")])
        self.assertEqual(problems, [])
        self.assertTrue(any("TR-022 is on rows 1, 2" in n for n in notes), notes)
        self.assertIs(hr.row_by_id([row], "TR-022", "transitions"), row)
        self.assertIsNone(hr.row_by_id([row], "TR-023", "transitions"))
        with self.assertRaises(ValueError) as caught:
            hr.row_by_id([row, dict(row, by="b")], "TR-022", "transitions")
        self.assertIn("transitions id TR-022 is on rows 1, 2", str(caught.exception))
        self.assertIn('row 2: by "b"', str(caught.exception))
        self.assertNotIn("object", str(caught.exception), "only the fields that tell the rows apart")
        _, notes = hr.validate_intake([{"id": "IN-001", "what": "x", "admitted_by": "y", "at": "t", "checked": "z"},
                                       {"id": "IN-001", "what": "w", "admitted_by": "v", "at": "u", "checked": "z"}])
        self.assertTrue(any("IN-001 is on rows 1, 2" in n for n in notes), notes)

    def test_two_versions_of_one_row_are_a_problem(self):
        row = {"id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "by": "a", "authority": "T1",
               "checks": "c", "decision": "adopt", "at": "t", "commits": [OLDEST], "later_run": "nothing stated"}
        updated = dict(row, later_run="ai/records/x", later_run_observed="o", later_run_by="p",
                       later_run_kind="human", later_run_at="t2")
        problems, _ = hr.validate_transitions([updated, row])
        self.assertTrue(any("versions of one row TR-001" in p for p in problems), problems)


class RegistryMergeLessons(unittest.TestCase):
    """Added 2026-09-23. Two partners who decided at the same moment stopped the second Sync.

    Both appended a row to the one acceptances file and pushed; the second
    pull --rebase stopped on a conflict in it, and the workshop guide had
    asked the pair to take turns to avoid that. The union merge in
    .gitattributes keeps both rows instead.
    """

    REGISTRIES = ("ai/records/acceptances.jsonl", "sources/intake.jsonl",
                  "sources/transitions.jsonl", "ai/workflow-log.md")

    def git(self, cwd, *args, check=True):
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.invalid")
        return subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, check=check)

    def test_rows_two_partners_append_at_once_both_survive_the_second_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            seed = tmp / "seed"; seed.mkdir()
            self.git(seed, "init", "-q")
            (seed / ".gitattributes").write_bytes((REPO / ".gitattributes").read_bytes())
            for path in self.REGISTRIES + ("ai/defects.jsonl",):
                (seed / path).parent.mkdir(parents=True, exist_ok=True)
                (seed / path).write_text('{"row": 0}\n')
            self.git(seed, "add", "-A"); self.git(seed, "commit", "-qm", "seed")
            self.git(tmp, "clone", "-q", "--bare", "seed", "origin.git")
            for who in ("a", "b"):
                self.git(tmp, "clone", "-q", "origin.git", who)
                for path in self.REGISTRIES:
                    with open(tmp / who / path, "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps({"row": who}) + "\n")
                self.git(tmp / who, "commit", "-qam", f"{who} decides")
            self.git(tmp / "a", "push", "-q", "origin", "HEAD")
            self.assertNotEqual(self.git(tmp / "b", "push", "-q", "origin", "HEAD", check=False).returncode, 0)
            sync = self.git(tmp / "b", "pull", "--rebase", "-q", check=False)
            self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
            for path in self.REGISTRIES:
                text = (tmp / "b" / path).read_text(encoding="utf-8")
                self.assertEqual(text.splitlines(), ['{"row": 0}', '{"row": "a"}', '{"row": "b"}'], path)
            merge = self.git(tmp / "b", "check-attr", "merge", "--", "ai/defects.jsonl").stdout
            self.assertIn("unspecified", merge, "a union merge would keep a stale ledger row beside its update")

    def test_a_row_rewritten_in_place_beside_an_append_is_reported(self):
        """Found 2026-09-23 by the codex reviewer: later-run rewrote the last
        transition row while a partner appended, and the union kept both versions."""
        row = {"id": "TR-001", "kind": "default-adoption", "object": "AGENTS.md", "by": "a", "authority": "T1",
               "checks": "c", "decision": "adopt", "at": "t", "commits": [OLDEST], "later_run": "nothing stated"}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            seed = tmp / "seed"; (seed / "sources").mkdir(parents=True)
            self.git(seed, "init", "-q")
            (seed / ".gitattributes").write_bytes((REPO / ".gitattributes").read_bytes())
            (seed / "sources/transitions.jsonl").write_text(json.dumps(row) + "\n")
            self.git(seed, "add", "-A"); self.git(seed, "commit", "-qm", "seed")
            self.git(tmp, "clone", "-q", "--bare", "seed", "origin.git")
            for who in ("a", "b"):
                self.git(tmp, "clone", "-q", "origin.git", who)
            later = dict(row, later_run="ai/records/x", later_run_observed="o", later_run_by="p",
                         later_run_kind="human", later_run_at="t2")
            (tmp / "a/sources/transitions.jsonl").write_text(json.dumps(later) + "\n")
            self.git(tmp / "a", "commit", "-qam", "later run"); self.git(tmp / "a", "push", "-q", "origin", "HEAD")
            with open(tmp / "b/sources/transitions.jsonl", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(dict(row, id="TR-002", object="paper/x.md", kind="promotion",
                                         decision="promote", at="t3")) + "\n")
            self.git(tmp / "b", "commit", "-qam", "promotion")
            self.assertEqual(self.git(tmp / "b", "pull", "--rebase", "-q", check=False).returncode, 0)
            rows = hr.load_jsonl(tmp / "b/sources/transitions.jsonl")
            self.assertEqual([r["id"] for r in rows], ["TR-001", "TR-001", "TR-002"])
            problems, _ = hr.validate_transitions(rows)
            self.assertTrue(any("versions of one row TR-001" in p for p in problems), problems)

    def test_a_promotion_row_naming_a_commit_a_sync_rewrote_is_noted(self):
        """Both partners promote and name their copy before pushing it; the
        second Sync rebases that copy, so its row names a sha the branch no
        longer holds. registries says so, in both clones, and a shallow clone
        does not mistake missing history for a stale reference."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            seed = tmp / "seed"; (seed / "sources").mkdir(parents=True)
            self.git(seed, "init", "-q")
            (seed / ".gitattributes").write_bytes((REPO / ".gitattributes").read_bytes())
            (seed / "sources/transitions.jsonl").write_text("")
            self.git(seed, "add", "-A"); self.git(seed, "commit", "-qm", "seed")
            self.git(tmp, "clone", "-q", "--bare", "seed", "origin.git")
            copies = {}
            for who in ("a", "b"):
                self.git(tmp, "clone", "-q", "origin.git", who)
                (tmp / who / "paper").mkdir()
                (tmp / who / "paper" / f"{who}.tex").write_text(who + "\n")
                self.git(tmp / who, "add", "-A"); self.git(tmp / who, "commit", "-qm", f"{who} copies")
                copies[who] = self.git(tmp / who, "rev-parse", "HEAD").stdout.strip()
                with open(tmp / who / "sources/transitions.jsonl", "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps({"id": f"TR-00{1 if who == 'a' else 2}", "kind": "promotion",
                                         "object": f"paper/{who}.tex", "by": who, "authority": "T1",
                                         "checks": "c", "decision": "promote", "at": "t",
                                         "commits": [copies[who]]}) + "\n")
                self.git(tmp / who, "commit", "-qam", f"{who} records the promotion")
            self.git(tmp / "a", "push", "-q", "origin", "HEAD")
            self.assertEqual(self.git(tmp / "b", "pull", "--rebase", "-q", check=False).returncode, 0)
            self.git(tmp / "b", "push", "-q", "origin", "HEAD")
            self.git(tmp / "a", "pull", "--rebase", "-q")
            self.git(tmp, "clone", "-q", "--depth", "1", (tmp / "origin.git").as_uri(), "c")

            def notes(clone):
                before = os.getcwd(); os.chdir(tmp / clone)
                try:
                    result = hr.validate_registries(transitions=pathlib.Path("sources/transitions.jsonl"))
                finally:
                    os.chdir(before)
                self.assertTrue(result["ok"], result["problems"])
                return [n for n in result["notes"] if n.startswith("transitions row") or "shallow" in n]

            stale = copies["b"][:12]
            self.assertEqual(notes("b"), [f"transitions row 2 (TR-002) names {stale}, which this repository "
                                          "holds outside the checked-out history: rewritten by a rebase, "
                                          "or on another branch"])
            self.assertEqual(notes("a"), [f"transitions row 2 (TR-002) names {stale}, which is not in this "
                                          "repository: rewritten by a rebase in another clone, or never pushed"])
            unchecked = ["2 transition commit reference(s) not checked: this clone is shallow, "
                         "and a commit outside the history it fetched may still be in the branch"]
            self.assertEqual(notes("c"), unchecked)
            # A shallow view that still holds an ancestor beyond its boundary
            # cannot tell a rewritten commit from an older one either.
            boundary = tmp / "shallow"
            boundary.write_text(self.git(tmp / "a", "rev-parse", "HEAD").stdout)
            with mock.patch.dict(os.environ, {"GIT_SHALLOW_FILE": str(boundary)}):
                self.assertEqual(notes("a"), unchecked)

    def test_a_malformed_commits_field_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "transitions.jsonl"
            path.write_text(json.dumps({"id": "TR-001", "kind": "promotion", "object": "paper/a.tex",
                                        "by": "a", "authority": "T1", "checks": "c",
                                        "decision": "promote", "at": "t", "commits": 7}) + "\n")
            before = os.getcwd(); os.chdir(tmp)
            try:
                result = hr.validate_registries(transitions=path)
            finally:
                os.chdir(before)
            self.assertFalse(result["ok"])
            self.assertTrue(any("commits must be a list" in p for p in result["problems"]), result["problems"])


class TransitionLessons(unittest.TestCase):
    """Added 2026-09-15. Intake rows had no identifier, digest, authority or join
    to the work they authorized; promotion and default adoption had no row at all.
    """

    def test_a_transition_row_joins_object_authority_checks_and_decision(self):
        import tempfile, pathlib as pl, os
        with tempfile.TemporaryDirectory() as tmp:
            root = pl.Path(tmp)
            (root / "note.md").write_text("a note", encoding="utf-8")
            saved = (hr.TRANSITIONS, hr.git, os.getcwd())
            hr.TRANSITIONS = root / "transitions.jsonl"
            hr.git = lambda *a: ""
            os.chdir(root)
            try:
                args = argparse.Namespace(kind="promotion", object="note.md", digest="",
                                          checks="checks.jsonl#1", by="claude-opus-5",
                                          authority="T4 of ai/records/x", decision="",
                                          destination="paper/sections/02.tex", commits=[],
                                          later_run="")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(hr.cmd_transition(args), 0)
                row = json.loads(hr.TRANSITIONS.read_text().strip())
                self.assertEqual(row["id"], "TR-001")
                self.assertEqual(row["decision"], "promote")
                self.assertEqual(row["object_sha256"], hr.sha256_text("a note"))
                self.assertEqual(row["authority"], "T4 of ai/records/x")
                self.assertEqual(hr.validate_transitions([row])[0], [])
                bad = argparse.Namespace(**{**vars(args), "decision": "adopt"})
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(hr.cmd_transition(bad), 1)
            finally:
                hr.TRANSITIONS, hr.git, cwd = saved
                os.chdir(cwd)

    def test_an_intake_row_now_carries_an_id_and_authority(self):
        import tempfile, pathlib as pl
        with tempfile.TemporaryDirectory() as tmp:
            original = hr.INTAKE
            hr.INTAKE = pl.Path(tmp) / "intake.jsonl"
            try:
                args = argparse.Namespace(what="a dataset", origin="", by="", checked="",
                                          used_for="", path="", authority="T2 of ai/records/y")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(hr.cmd_intake(args), 0)
                row = json.loads(hr.INTAKE.read_text().strip())
                self.assertEqual(row["id"], "IN-001")
                self.assertEqual(row["authority"], "T2 of ai/records/y")
                self.assertEqual(row["decision"], "admit")
                self.assertEqual(hr.validate_intake([row])[0], [])
            finally:
                hr.INTAKE = original

    def _intake(self, root: pathlib.Path, **fields) -> tuple[dict, str]:
        args = argparse.Namespace(**{"what": "the Smith paper", "origin": "", "by": "", "checked": "",
                                     "used_for": "", "path": "", "authority": "", **fields})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(hr.cmd_intake(args), 0, "an intake is never refused")
        return json.loads(hr.INTAKE.read_text(encoding="utf-8").splitlines()[-1]), out.getvalue()

    def test_an_explicit_intake_path_is_kept_as_given_and_a_missing_file_is_said(self):
        # LESSON (AH-140): a path with a space, an absolute path and a mistyped
        # one all left a row with no object and printed nothing, because the
        # explicit --path went through the heuristic for telling a path from a
        # description.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "Smith 2020.pdf").write_text("pdf bytes", encoding="utf-8")
            saved = (hr.INTAKE, os.getcwd())
            hr.INTAKE = root / "intake.jsonl"
            os.chdir(root)
            try:
                digest = hr.sha256_text("pdf bytes")
                for given in ("Smith 2020.pdf", str(root / "Smith 2020.pdf")):
                    row, out = self._intake(root, path=given)
                    self.assertEqual((row["object"], row["object_sha256"]), (given, digest))
                row, out = self._intake(root, path="papers/Smith 2021.pdf")
                self.assertEqual(row["object"], "papers/Smith 2021.pdf")
                self.assertNotIn("object_sha256", row)
                self.assertIn("no file at papers/Smith 2021.pdf", out)
                rows = [json.loads(ln) for ln in hr.INTAKE.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(hr.validate_intake(rows)[0], [])
            finally:
                hr.INTAKE, cwd = saved
                os.chdir(cwd)

    def test_an_intake_with_no_name_writes_a_row_its_validator_reads(self):
        # LESSON (AH-141): `intake --what ""` wrote `"what": ""`, which the
        # registry validator rejects, so `registries` and the report failed on
        # a row the command had just written. Transition already said
        # "(unnamed)" for an unnamed object.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            original = hr.INTAKE
            hr.INTAKE = root / "intake.jsonl"
            try:
                row, out = self._intake(root, what="   ")
                self.assertEqual(row["what"], "(unnamed)")
                self.assertEqual(hr.validate_intake([row])[0], [])
                self.assertIn("admitted: (unnamed)", out)
            finally:
                hr.INTAKE = original

    def test_an_intake_cites_a_record_by_its_whole_id_only(self):
        # LESSON (AH-142): a substring match counted a row citing
        # 20260922-accept-from-words-postcheck as citing 20260922-accept-from-words.
        with tempfile.TemporaryDirectory() as tmp:
            original = hr.INTAKE
            hr.INTAKE = pathlib.Path(tmp) / "intake.jsonl"
            try:
                hr.INTAKE.write_text("".join(json.dumps(r) + "\n" for r in [
                    {"what": "a", "authority": "T2 of ai/records/20260922-accept-from-words-postcheck"},
                    {"what": "b", "authority": "T1 of ai/records/20260922-accept.",
                     "used_for": "20260922-accept, again"}]), encoding="utf-8")
                self.assertEqual(hr.intake_citations("20260922-accept-from-words"), 0)
                self.assertEqual(hr.intake_citations("20260922-accept-from-words-postcheck"), 1)
                self.assertEqual(hr.intake_citations("20260922-accept"), 1)
            finally:
                hr.INTAKE = original

    def test_a_transition_object_with_a_space_or_an_absolute_path_keeps_its_digest(self):
        # LESSON (AH-140): the transition shares the digest helper, so a file
        # it could read went without a digest for its name alone; a named
        # file that is not there is said, and a description is not a file.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / "my note.md").write_text("a note", encoding="utf-8")
            saved = (hr.TRANSITIONS, hr.git, os.getcwd())
            hr.TRANSITIONS = root / "transitions.jsonl"
            hr.git = lambda *a, **k: ""
            os.chdir(root)
            try:
                for obj, digest in (("my note.md", hr.sha256_text("a note")),
                                    (str(root / "my note.md"), hr.sha256_text("a note")),
                                    ("paper/sections/gone.tex", None),
                                    ("the rule on citations", None)):
                    args = argparse.Namespace(kind="promotion", object=obj, digest="", checks="c",
                                              by="b", authority="T1", decision="", destination="paper/",
                                              commits=[], later_run="")
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(hr.cmd_transition(args), 0)
                    row = json.loads(hr.TRANSITIONS.read_text().splitlines()[-1])
                    self.assertEqual((row["object"], row["object_sha256"]), (obj, digest))
                    if obj == "paper/sections/gone.tex":
                        self.assertIn("no file at paper/sections/gone.tex", out.getvalue())
                    if obj == "the rule on citations":
                        self.assertNotIn("no file at", out.getvalue())
            finally:
                hr.TRANSITIONS, hr.git, cwd = saved
                os.chdir(cwd)

    def test_a_refused_adoption_is_not_told_to_add_a_later_run(self):
        # LESSON (AH-139): a refused or returned adoption was told to add a
        # later run, which `later-run` then refuses: nothing was adopted.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            saved = (hr.TRANSITIONS, hr.git)
            hr.TRANSITIONS = root / "transitions.jsonl"
            hr.git = lambda *a, **k: ""
            try:
                for decision, told in (("refuse", False), ("return", False), ("adopt", True)):
                    args = argparse.Namespace(kind="default-adoption", object="AGENTS.md", digest="",
                                              checks="c", by="b", authority="T1", decision=decision,
                                              destination="", commits=[], later_run="")
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(hr.cmd_transition(args), 0)
                    self.assertEqual("later-run TR-" in out.getvalue(), told, decision)
            finally:
                hr.TRANSITIONS, hr.git = saved


class IntakeDecisionLessons(unittest.TestCase):
    """Added 2026-10-14 (rc22), from the review of TRACE-OS against the frozen principles.

    `intake` always wrote "admit" and recorded who admitted as "not stated"
    unless --by was typed, so the entry boundary held no refusals and, by
    default, no actor; the material's version was kept only as a digest.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name).resolve()
        saved = (hr.INTAKE, hr.git, os.getcwd())
        self.addCleanup(lambda: (setattr(hr, "INTAKE", saved[0]), setattr(hr, "git", saved[1]),
                                 os.chdir(saved[2])))
        hr.INTAKE = self.root / "intake.jsonl"
        hr.git = lambda *a, **k: "Ann Author" if a[:2] == ("config", "user.name") else ""
        os.chdir(self.root)

    def intake(self, code=0, **fields) -> tuple[dict | None, str]:
        args = argparse.Namespace(**{"what": "survey wave 2", "origin": "the panel team", "by": "",
                                     "checked": "row counts against the codebook", "used_for": "",
                                     "path": "", "authority": "T1 of ai/records/x", **fields})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(hr.cmd_intake(args), code, err.getvalue())
        if code:
            return None, err.getvalue()
        return json.loads(hr.INTAKE.read_text(encoding="utf-8").splitlines()[-1]), out.getvalue()

    def rows(self) -> list[dict]:
        return [json.loads(ln) for ln in hr.INTAKE.read_text(encoding="utf-8").splitlines()]

    def test_who_decided_defaults_to_the_git_user_and_the_kind_is_declared_never_read_off_the_name(self):
        row, out = self.intake()
        self.assertEqual((row["admitted_by"], row["actor_name_basis"]), ("Ann Author", "git config user.name"))
        self.assertEqual((row["actor_kind"], row["actor_kind_basis"]), ("unknown", "not declared"),
                         "a defaulted name is not a declared person")
        self.assertIn("by Ann Author (kind not declared, name from git config user.name)", out)
        self.assertIn("--person, --agent or --authorized-by", out)
        row, out = self.intake(person=True)
        self.assertEqual((row["admitted_by"], row["actor_kind"], row["actor_kind_basis"]),
                         ("Ann Author", "human", "declared"))
        row, _ = self.intake(by="claude-opus-5-5", agent=True)
        self.assertEqual((row["admitted_by"], row["actor_name_basis"], row["actor_kind"]),
                         ("claude-opus-5-5", "stated", "agent"))
        # A model named nobody: the git user is the person, not the model.
        row, _ = self.intake(agent=True)
        self.assertEqual((row["admitted_by"], row["actor_kind"]), ("not stated", "agent"))
        row, _ = self.intake(by="claude-opus-5-5")
        self.assertEqual((row["actor_kind"], row["actor_kind_basis"]), ("agent", "inferred from the name"))
        row, _ = self.intake(by="release-check", authorized_by="Ann Author")
        self.assertEqual((row["actor_kind"], row["authorized_by"]), ("delegated", "Ann Author"))
        _, err = self.intake(code=1, person=True, agent=True)
        self.assertIn("Choose one", err)
        self.assertEqual(hr.validate_intake(self.rows())[0], [])

    def test_a_refusal_at_entry_is_recorded_in_the_intake_registry(self):
        row, out = self.intake(decision="refuse", person=True, checked="the licence forbids redistribution")
        self.assertEqual(row["decision"], "refuse")
        self.assertTrue(out.startswith("refused: survey wave 2"), out)
        self.assertIn("nothing was admitted under this row", out)
        row, out = self.intake(decision="return")
        self.assertEqual(row["decision"], "return")
        problems, notes = hr.validate_intake(self.rows())
        self.assertEqual(problems, [])
        self.assertIn("2 intake row(s) record material returned or refused at entry", notes)
        _, err = self.intake(code=1, decision="promote")
        self.assertIn("admit, return, refuse", err)
        # The command line takes the flags.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hr.main(["intake", "--what", "a corpus", "--decision", "refuse", "--person",
                                      "--version", "2026-10"]), 0)
        self.assertEqual({k: self.rows()[-1][k] for k in ("decision", "actor_kind", "version")},
                         {"decision": "refuse", "actor_kind": "human", "version": "2026-10"})

    def test_material_admitted_from_the_holding_folder_is_told_where_it_goes(self):
        # LESSON (rc22, Stage 2): nothing said where material waits before a
        # decision. It waits in incoming/ inside the shared folder; admitting it
        # is the step after which it moves into the data. Nothing is moved here.
        held = self.root / "workspace" / "shared" / "incoming" / "panel_v4.csv"
        held.parent.mkdir(parents=True)
        held.write_text("id\n1\n", encoding="utf-8")
        self.assertEqual(hr.incoming_folder(), "workspace/shared/incoming")
        row, out = self.intake(path="workspace/shared/incoming/panel_v4.csv", person=True)
        self.assertIn("Held in workspace/shared/incoming/: move it into the project's data", out)
        self.assertTrue(held.is_file(), "intake moves nothing")
        _, out = self.intake(path="workspace/shared/incoming/panel_v4.csv", decision="return")
        self.assertNotIn("Held in", out)
        _, out = self.intake(path="sources/data/wave.csv", person=True)
        self.assertNotIn("Held in", out)

    def test_the_version_is_recorded_beside_the_digest(self):
        (self.root / "wave2.csv").write_text("id,x\n1,2\n", encoding="utf-8")
        row, out = self.intake(path="wave2.csv", version="v2.1")
        self.assertEqual((row["version"], row["object_sha256"]), ("v2.1", hr.sha256_text("id,x\n1,2\n")))
        self.assertIn("version: v2.1, digest", out)
        row, _ = self.intake()
        self.assertEqual(row["version"], "not stated")

    def test_historical_rows_stay_valid_and_new_fields_are_checked(self):
        legacy = [{"what": "x", "admitted_by": "y", "at": "t", "checked": "z"},
                  {"id": "IN-002", "what": "x", "admitted_by": "not stated", "at": "t", "checked": "z",
                   "authority": "nothing stated", "decision": "admit"}]
        problems, notes = hr.validate_intake(legacy)
        self.assertEqual(problems, [])
        self.assertIn("2 intake row(s) predate the recorded actor kind and are read as written", notes)
        new = {"id": "IN-003", "what": "x", "admitted_by": "y", "at": "t", "checked": "z", "decision": "admit",
               "actor_kind": "human", "actor_kind_basis": "declared", "version": "not stated"}
        self.assertEqual(hr.validate_intake([new])[0], [])
        for change, problem in (({"actor_kind": "robot"}, "actor_kind 'robot'"),
                                ({"actor_kind": "delegated"}, "names no authorizing researcher"),
                                ({"version": ""}, "version is empty")):
            with self.subTest(change=change):
                problems, _ = hr.validate_intake([{**new, **change}])
                self.assertTrue(any(problem in p for p in problems), problems)


class ProvenanceAndLinkLessons(unittest.TestCase):
    """Added 2026-09-15. The record did not say which standing instructions
    governed a run, and no check counted commits that name no record.
    """

    def test_instruction_provenance_digests_the_files_in_force_and_says_what_it_is(self):
        import tempfile, pathlib as pl
        with tempfile.TemporaryDirectory() as tmp:
            root = pl.Path(tmp)
            (root / "AGENTS.md").write_text("rules", encoding="utf-8")
            (root / ".skills" / "x").mkdir(parents=True)
            (root / ".skills" / "x" / "SKILL.md").write_text("skill", encoding="utf-8")
            prov = hr.instruction_provenance(root)
            self.assertEqual(prov["files"]["AGENTS.md"], hr.sha256_text("rules"))
            self.assertEqual(prov["files"][".skills/x/SKILL.md"], hr.sha256_text("skill"))
            self.assertIn("not proof of what the agent", prov["basis"])
            self.assertIsNone(prov["base_commit"], "no repository, no commit")

    def test_the_review_prose_rules_are_an_instruction_file_in_force(self):
        # LESSON (rc22 review, f1ca7a5 F004): ai/review-prose-rules.md shapes
        # the reviewer's prompt and is protected as an instruction path, yet
        # a record's instruction digests left it out.
        import tempfile, pathlib as pl
        with tempfile.TemporaryDirectory() as tmp:
            root = pl.Path(tmp)
            (root / "ai").mkdir()
            (root / "ai" / "review-prose-rules.md").write_text("no em dash", encoding="utf-8")
            prov = hr.instruction_provenance(root)
            self.assertEqual(prov["files"]["ai/review-prose-rules.md"], hr.sha256_text("no em dash"))
            # A tree without the file names nothing for it, as before.
            (root / "ai" / "review-prose-rules.md").unlink()
            self.assertNotIn("ai/review-prose-rules.md", hr.instruction_provenance(root)["files"])

    def test_artifact_references_are_classified_not_trusted(self):
        import tempfile, pathlib as pl
        with tempfile.TemporaryDirectory() as tmp:
            record = pl.Path(tmp)
            (record / "checks.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(hr.resolve_artifact("checks.jsonl", None, record), "resolved")
            self.assertEqual(hr.resolve_artifact("scripts/trace_record.py"), "resolved")
            self.assertEqual(hr.resolve_artifact("https://example.invalid/run/1"), "declared")
            self.assertEqual(hr.resolve_artifact("synthetic fixture"), "declared")
            self.assertEqual(hr.resolve_artifact("stdout"), "declared")
            self.assertEqual(hr.resolve_artifact("prose", "declared"), "declared")
            self.assertEqual(hr.resolve_artifact("workspace/shared/archive/x.json"), "unavailable")
            self.assertEqual(hr.resolve_artifact("scripts/no-such-file.py"), "nonexistent")

    def test_promotion_refuses_a_check_that_names_an_absent_path(self):
        # LESSON: a check row pointing at nothing looked exactly like a check
        # row pointing at evidence. At promotion the difference is enforced.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps(user_row("please check")) + "\n", encoding="utf-8")
            staging = root / "staging"
            with contextlib.redirect_stdout(io.StringIO()):
                hr.cmd_extract(type("A", (), {"session": str(transcript), "out": str(staging),
                                              "record_id": "x", "speaker": "R"})())
            evidence_path = staging / "evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["consent"]["third_party_review"] = "cleared"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            (staging / "summary.md").write_text("The researcher asked for a check (T1).", encoding="utf-8")
            (staging / "checks.jsonl").write_text(json.dumps({
                "kind": "code-test", "runner": "unittest", "method": "ran", "result": "pass",
                "artifact": "scripts/absent/output.json", "candidate": "scripts/x.py@sha256:abc",
                "criterion": "all tests pass", "limits": "synthetic absent path"}) + "\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                hr.cmd_cover(type("A", (), {"record": str(staging)})())
                import record_privacy
                record_privacy.cmd_publication(hr, type("A", (), {"record": str(staging), "by": "t", "scope": "s"})())
            result = hr.evaluate_record(staging)
            self.assertTrue(result["ok"], "report-only verification counts, it does not fail")
            self.assertIn("1 of 1 checks bind a candidate and a criterion", result["notes"])
            self.assertTrue(any("1 nonexistent" in n for n in result["notes"]))
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(io.StringIO()):
                    hr.cmd_promote(type("A", (), {"staging": str(staging), "dest": str(root / "p"),
                                                  "force": False})())

    def test_explicit_file_references_keep_the_whole_path_and_do_not_become_labels(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before = pathlib.Path.cwd()
            os.chdir(root)
            try:
                for path in ('missing.json', 'checks with spaces/missing.json'):
                    self.assertEqual(hr.resolve_artifact(path, 'path'), 'nonexistent')
                (root / 'checks with spaces').mkdir()
                (root / 'checks with spaces/result.json').write_text('{}')
                self.assertEqual(hr.resolve_artifact('checks with spaces/result.json', 'path'), 'resolved')
                record = root / 'record'; record.mkdir()
                (record / 'evidence.json').write_text('{}')
                self.assertEqual(hr.resolve_artifact('evidence.json', 'path', record), 'resolved')
                self.assertEqual(hr.resolve_artifact('session.log'), 'declared', 'legacy labels remain readable')
                self.assertEqual(hr.resolve_artifact('missing.json', 'declared'), 'declared')
            finally:
                os.chdir(before)

    def test_promotion_rejects_an_explicit_missing_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            staging = root / 'staging'; staging.mkdir()
            (staging / 'evidence.json').write_text(json.dumps({'record_id': 'test',
                'consent': {'third_party_review': 'cleared'}, 'turns': [{'id': 'T1', 'text': 'check'}]}))
            (staging / 'summary.md').write_text('T1 requested a check.')
            (staging / 'checks.jsonl').write_text(json.dumps({'kind': 'code-test', 'runner': 'test',
                'method': 'fixture', 'result': 'pass', 'artifact': 'missing.json', 'reference': 'path'}) + '\n')
            with contextlib.redirect_stdout(io.StringIO()):
                hr.cmd_cover(argparse.Namespace(record=str(staging)))
                import record_privacy
                record_privacy.cmd_publication(hr, argparse.Namespace(record=str(staging), by='tester', scope='fixture'))
                with self.assertRaisesRegex(SystemExit, 'does not exist'):
                    hr.cmd_promote(argparse.Namespace(staging=str(staging), dest=str(root / 'promoted'), force=False))
            self.assertFalse((root / 'promoted').exists())


class CommitRangeExecutionLessons(unittest.TestCase):
    def test_invalid_ranges_fail_but_a_valid_empty_range_succeeds(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = dict(os.environ, GIT_AUTHOR_NAME='Test Researcher', GIT_AUTHOR_EMAIL='t@example.invalid',
                       GIT_COMMITTER_NAME='Test Researcher', GIT_COMMITTER_EMAIL='t@example.invalid')
            subprocess.run(['git', 'init', '-q'], cwd=root, env=env, check=True)
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', 'baseline'],
                           cwd=root, env=env, check=True)
            command = [sys.executable, str(REPO / 'scripts/trace_record.py'), 'verify-range']
            bad = subprocess.run(command + ['missing-base..HEAD'], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertNotIn('no commits', bad.stdout)
            self.assertNotIn('Traceback', bad.stderr)
            empty = subprocess.run(command + ['HEAD..HEAD'], cwd=root, capture_output=True, text=True)
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertIn('no commits', empty.stdout)

    def test_a_range_of_person_typed_commits_is_reported_and_never_fails(self):
        # The report counts commits with no trailer; it does not fail on them,
        # because failing turned CI red on the person's own hand-typed commits.
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = dict(os.environ, GIT_AUTHOR_NAME='Test Researcher', GIT_AUTHOR_EMAIL='t@example.invalid',
                       GIT_COMMITTER_NAME='Test Researcher', GIT_COMMITTER_EMAIL='t@example.invalid')
            subprocess.run(['git', 'init', '-q'], cwd=root, env=env, check=True)
            for subject in ('baseline', 'Tighten the opening frames by hand', 'Update on Overleaf.'):
                subprocess.run(['git', 'commit', '--allow-empty', '-qm', subject], cwd=root, env=env, check=True)
            # An exported kit has no built-in epoch; a project supplies its own,
            # as setup writes it, so the report knows the rule was in force.
            baseline = subprocess.run(['git', 'rev-parse', 'HEAD~2'], cwd=root, env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()
            (root / 'ai').mkdir()
            (root / 'ai' / 'project.json').write_text(json.dumps(
                {'schema_version': 1, 'policy_epoch': baseline, 'accept_epoch': baseline}))
            command = [sys.executable, str(REPO / 'scripts/trace_record.py'), 'verify-range', 'HEAD~2..HEAD']
            done = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn('2 carry no execution trailer', done.stdout)
            self.assertIn('typed by a person', done.stdout)
            self.assertNotIn('problem', done.stdout.lower().replace('a count, not a problem', ''))
            # LESSON (AH-137): a person who wrote `Execution-agent: none` said
            # no machine executed it, and is counted with the other hand commits.
            subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'Changed slide 8 by hand\n\nExecution-agent: None'],
                           cwd=root, env=env, check=True)
            done = subprocess.run(command[:-1] + ['HEAD~3..HEAD'], cwd=root, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn('3 carry no execution trailer', done.stdout)

    def test_a_range_holding_a_coauthorship_claim_names_it_and_blocks_nothing(self):
        # Carried over from the tests of the removed CI range script (2026-09-30):
        # the report run by hand must reach an earlier commit in the range, name
        # it, and exit 0, because it prints a false claim so it is on the record.
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = dict(os.environ, GIT_AUTHOR_NAME='Test Researcher', GIT_AUTHOR_EMAIL='t@example.invalid',
                       GIT_COMMITTER_NAME='Test Researcher', GIT_COMMITTER_EMAIL='t@example.invalid')
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            git('commit', '--allow-empty', '-qm', 'baseline')
            baseline = git('rev-parse', 'HEAD')
            (root / 'ai').mkdir()
            (root / 'ai' / 'project.json').write_text(json.dumps(
                {'schema_version': 1, 'policy_epoch': baseline, 'accept_epoch': baseline}))
            git('commit', '--allow-empty', '-qm',
                'claims a co-author\n\nCo-Authored-By: Claude <noreply@example.invalid>')
            reported = git('rev-parse', 'HEAD')
            git('commit', '--allow-empty', '-qm', 'valid tip\n\nExecution-agent: fixture-model')
            done = subprocess.run([sys.executable, str(REPO / 'scripts/trace_record.py'), 'verify-range',
                                   f'{baseline}..HEAD'], cwd=root, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn(reported[:8], done.stdout)
            self.assertIn('Nothing is blocked', done.stdout)

    def test_backfill_over_an_invalid_range_is_an_error_and_keeps_the_file(self):
        # LESSON (AH-129): `backfill --range mian..HEAD` exited 0, said "0
        # commits classified" and emptied the tracked attribution file. An
        # unreadable range is an execution error, and the file is written
        # through one rename, so it is either the old file or the new one.
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = dict(os.environ, GIT_AUTHOR_NAME='Test Researcher', GIT_AUTHOR_EMAIL='t@example.invalid',
                       GIT_COMMITTER_NAME='Test Researcher', GIT_COMMITTER_EMAIL='t@example.invalid')
            subprocess.run(['git', 'init', '-q'], cwd=root, env=env, check=True)
            for subject in ('baseline', 'Tighten the opening by hand'):
                subprocess.run(['git', 'commit', '--allow-empty', '-qm', subject], cwd=root, env=env, check=True)
            baseline = subprocess.run(['git', 'rev-parse', 'HEAD~1'], cwd=root, env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()
            (root / 'ai' / 'records').mkdir(parents=True)
            (root / 'ai' / 'project.json').write_text(json.dumps(
                {'schema_version': 1, 'policy_epoch': baseline, 'accept_epoch': baseline}))
            out = root / 'ai' / 'records' / 'attribution-backfill.jsonl'
            before = '{"commit": "a"}\n{"commit": "b"}\n{"commit": "c"}\n'
            out.write_text(before, encoding='utf-8')
            command = [sys.executable, str(REPO / 'scripts/trace_record.py'), 'backfill', '--out', str(out)]
            bad = subprocess.run(command + ['--range', 'mian..HEAD'], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0, bad.stdout)
            self.assertIn('ERROR', bad.stderr)
            self.assertNotIn('Traceback', bad.stderr)
            self.assertNotIn('classified', bad.stdout)
            self.assertEqual(out.read_text(encoding='utf-8'), before, 'a failed run must leave the file as it was')
            good = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            rows = [json.loads(line) for line in out.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([row['basis'] for row in rows], ['none-declared', 'unknown'])
            self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ['attribution-backfill.jsonl'])


class RecordLineLessons(unittest.TestCase):
    """LESSON (2026-09-19): a list on the Record: line made a present record read as absent.

    Two commits named the origin record and the session record on one line,
    separated by a comma. The pattern captured `\\S+`, so the first path kept
    its trailing comma, the directory check failed, and the governing-actions
    report said T was missing for a commit whose record is in the tree. The
    line carries one path; the pattern now stops at the separator instead of
    reading the punctuation as part of it.
    """

    def test_a_comma_separated_record_line_resolves_its_first_path(self):
        body = "Subject\n\nRecord: ai/records/20260918-example, ai/records/20260919-example\n\nExecution-agent: m\n"
        match = hr.RECORD_LINE_RE.search(body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "ai/records/20260918-example")

    def test_a_single_record_line_is_unchanged(self):
        body = "Subject\n\nRecord: ai/records/20260919-example\n\nExecution-agent: m\n"
        self.assertEqual(hr.RECORD_LINE_RE.search(body).group(1), "ai/records/20260919-example")


class ProcedureVersionLessons(unittest.TestCase):
    """LESSON (2026-09-19): a procedure version nobody can check is a number somebody typed.

    The three trace-* skills hard-typed `procedure_version: 1`, and the
    validator only counted the field, so the record said which procedure ran
    without saying which text of it ran. The version is now the digest of the
    skill file, checked against the digest the same record kept for that file at
    extraction time. The working tree is deliberately not the comparand: a skill
    edited after a run must not retroactively contradict the record of that run.
    """

    DIGEST = "a" * 64

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.record = self.root / "record"
        self.record.mkdir()
        (self.record / "summary.md").write_text("The author set the direction (T1).")
        self._evidence({".skills/trace-review/SKILL.md": self.DIGEST})

    def tearDown(self):
        self.tmp.cleanup()

    def _evidence(self, files):
        (self.record / "evidence.json").write_text(json.dumps({
            "record_id": "test", "record_schema_version": 2,
            "consent": {"third_party_review": "cleared"},
            "instructions": {"files": files, "basis": "file state at extraction time"},
            "turns": [{"id": "T1", "text": "first"}]}))

    def _row(self, version, procedure="trace-review"):
        (self.record / "checks.jsonl").write_text(json.dumps({
            "kind": "source-review", "runner": "reviewer", "method": "compare",
            "result": "pass", "artifact": "review.md", "reference": "declared",
            "candidate": "draft.tex@sha256:abc", "criterion": "claims match sources",
            "limits": "one draft version", "procedure": procedure,
            "procedure_version": version}) + "\n")
        hr.cmd_cover(type("A", (), {"record": str(self.record)})())
        return hr.evaluate_record(self.record)

    def test_the_version_is_read_off_a_skill_file_in_either_shape(self):
        skills = self.root / ".skills"
        (skills / "trace-review").mkdir(parents=True)
        (skills / "trace-review" / "SKILL.md").write_text("procedure\n", encoding="utf-8")
        (skills / "flat-skill.md").write_text("procedure\n", encoding="utf-8")
        expected = "sha256:" + hr.sha256_file(skills / "trace-review" / "SKILL.md")
        self.assertRegex(expected, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(hr.procedure_text_version("trace-review", self.root), expected)
        self.assertEqual(hr.procedure_text_version("flat-skill", self.root), expected)
        self.assertIsNone(hr.procedure_text_version("absent", self.root))
        self.assertIsNone(hr.procedure_text_version("../escape", self.root))

    def test_a_version_matching_the_records_own_digest_is_reported_as_matching(self):
        result = self._row("sha256:" + self.DIGEST)
        self.assertTrue(result["ok"], result["problems"])
        self.assertTrue(any("1 matching this record's own digest" in note
                            for note in result["notes"]), result["notes"])

    def test_a_twelve_character_prefix_is_accepted_as_a_prefix(self):
        result = self._row("sha256:" + self.DIGEST[:12])
        self.assertTrue(result["ok"], result["problems"])
        self.assertTrue(any("1 matching this record's own digest" in note
                            for note in result["notes"]), result["notes"])

    def test_a_version_the_records_own_provenance_contradicts_is_a_problem(self):
        result = self._row("sha256:" + "b" * 64)
        self.assertFalse(result["ok"])
        self.assertTrue(any("instruction provenance digests" in p and "trace-review" in p
                            for p in result["problems"]), result["problems"])

    def test_a_declared_label_stays_readable_and_is_counted_as_declared(self):
        # Historical rows wrote "1". They are not retroactively wrong; they are
        # reported as declared, because a label is not a derivation.
        result = self._row("1")
        self.assertTrue(result["ok"], result["problems"])
        self.assertTrue(any("1 declared without a derivable version" in note
                            for note in result["notes"]), result["notes"])

    def test_a_procedure_the_record_never_digested_is_not_resolvable_not_wrong(self):
        result = self._row("sha256:" + self.DIGEST, procedure="trace-elsewhere")
        self.assertTrue(result["ok"], result["problems"])
        self.assertTrue(any("1 not resolvable to a skill file this record digested" in note
                            for note in result["notes"]), result["notes"])

    def test_a_later_edit_to_the_skill_does_not_contradict_an_earlier_record(self):
        # The comparand is the record's captured digest, not today's file, so a
        # revised skill leaves every record made under the old text intact.
        (self.root / ".skills" / "trace-review").mkdir(parents=True)
        (self.root / ".skills" / "trace-review" / "SKILL.md").write_text("revised\n", encoding="utf-8")
        self.assertNotEqual(hr.procedure_text_version("trace-review", self.root),
                            "sha256:" + self.DIGEST)
        result = self._row("sha256:" + self.DIGEST)
        self.assertTrue(result["ok"], result["problems"])

class DefectLinkLessons(unittest.TestCase):
    """LESSON (2026-09-19): a defect id with the right shape can still name nothing.

    `STABLE_ID_RE` accepts `CLM-001` as readily as `AH-031`, and
    claim-evidence-audit issues `CLM-` ids for the claims inside one audit. The
    shipped contract says "Stable defect ids use the project's ledger ids", and
    until now nothing checked it, so a `defect_ids` list could point at an
    audit's own numbering and pass the schema.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.record = self.root / "record"
        self.record.mkdir()
        (self.record / "evidence.json").write_text(json.dumps({
            "record_id": "test", "record_schema_version": 2,
            "consent": {"third_party_review": "cleared"},
            "turns": [{"id": "T1", "text": "first"}]}))
        (self.record / "summary.md").write_text("The author set the direction (T1).")
        self.ledger = self.root / "defects.jsonl"
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.saved = os.environ.get("TRACE_OS_DEFECT_LEDGER")
        os.environ["TRACE_OS_DEFECT_LEDGER"] = str(self.ledger)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("TRACE_OS_DEFECT_LEDGER", None)
        else:
            os.environ["TRACE_OS_DEFECT_LEDGER"] = self.saved
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def _row(self, defect_ids):
        (self.record / "checks.jsonl").write_text(json.dumps({
            "kind": "citation-check", "runner": "auditor", "method": "compare",
            "result": "pass", "artifact": "audit.md", "reference": "declared",
            "candidate": "draft.tex@sha256:abc", "criterion": "claims match sources",
            "limits": "one draft version", "defect_ids": defect_ids}) + "\n")
        hr.cmd_cover(type("A", (), {"record": str(self.record)})())
        return hr.evaluate_record(self.record)

    def test_an_id_the_ledger_holds_resolves(self):
        self.ledger.write_text(json.dumps({"id": "AH-031", "state": "open", "label": "x",
                                           "severity": "low"}) + "\n")
        result = self._row(["AH-031"])
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("defect links resolved against the project ledger", result["notes"])

    def test_an_audits_own_claim_id_is_reported_not_accepted(self):
        self.ledger.write_text(json.dumps({"id": "AH-031", "state": "open", "label": "x",
                                           "severity": "low"}) + "\n")
        result = self._row(["CLM-001"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("CLM-001" in p and "defect ledger does not hold" in p
                            for p in result["problems"]), result["problems"])

    def test_without_a_ledger_the_link_is_not_resolvable_rather_than_wrong(self):
        result = self._row(["AH-031"])
        self.assertTrue(result["ok"], result["problems"])
        self.assertTrue(any("not resolvable" in note for note in result["notes"]),
                        result["notes"])


class APersonsCheckLessons(unittest.TestCase):
    """LESSON (rc22): R had no command for a person's check.

    A and E already recorded a person's words through the agent (`accept --words`,
    `defect_ledger.py draft-recheck --said`); a check a person said they made
    left only free text in `runner`. `check` drafts the row from the checker's
    own words, prints it back, and appends it only with --confirm. A check row
    may also name the sources it compared, each with its digest.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.record = self.root / "record"
        self.record.mkdir()
        (self.record / "evidence.json").write_text(json.dumps({
            "record_id": "test", "record_schema_version": 2,
            "consent": {"third_party_review": "cleared"},
            "turns": [{"id": "T1", "text": "first"}]}))
        (self.record / "summary.md").write_text("The author set the direction (T1).")
        self.cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def check(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hr.main(["check", *argv])
        return code, out.getvalue(), err.getvalue()

    def evaluate(self, *rows) -> dict:
        (self.record / "checks.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            hr.cmd_cover(type("A", (), {"record": str(self.record)})())
        return hr.evaluate_record(self.record)

    def test_the_draft_is_printed_field_by_field_and_nothing_is_written(self):
        target = self.record / "checks.jsonl"
        code, out, _ = self.check("--person", "--by", "Jane Roe",
                                  "--said", "I compared table 2 with the survey file; it matches",
                                  "--candidate", "paper/tables/t2.tex",
                                  "--checks-jsonl", str(target))
        self.assertEqual(code, 0)
        self.assertFalse(target.exists())
        self.assertIn("draft check row, to confirm before it is recorded", out)
        self.assertIn("candidate : paper/tables/t2.tex", out)
        # What the checker did not say reads `not stated`; nothing is inferred.
        for label in ("criterion", "examined", "result", "limits"):
            self.assertIn(f"{label:<10}: not stated", out)
        self.assertIn("checker   : Jane Roe (human)", out)
        self.assertIn("said      : I compared table 2 with the survey file; it matches", out)
        self.assertIn("--confirm", out)

    def test_confirmed_the_row_is_appended_in_the_persons_name_with_their_words(self):
        target = self.record / "checks.jsonl"
        data = self.root / "survey.csv"
        data.write_text("firm,n\na,1\n", encoding="utf-8")
        code, out, _ = self.check("--person", "--by", "Jane Roe", "--recorded-by", "some-model-1",
                                  "--said", "it matches the survey",
                                  "--candidate", "paper/tables/t2.tex",
                                  "--criterion", "every cell matches the data",
                                  "--examined", "each cell against the csv",
                                  "--result", "all cells match", "--limits", "the totals row only",
                                  "--source", "survey.csv", "--source", "Smith 2020, p. 4",
                                  "--checks-jsonl", str(target), "--confirm")
        self.assertEqual(code, 0, out)
        row = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["kind"], "person-check")
        self.assertEqual(row["checker"], "Jane Roe")
        self.assertEqual(row["checker_kind"], "human")
        self.assertEqual(row["recorded_by"], "some-model-1")
        self.assertIn("recorded by some-model-1", row["runner"])
        self.assertEqual(row["words"]["checker_said"], "it matches the survey")
        self.assertEqual(row["method"], "each cell against the csv")
        self.assertEqual(row["sources"], [
            {"path": "survey.csv", "sha256": hashlib.sha256(data.read_bytes()).hexdigest()},
            {"path": "Smith 2020, p. 4"}])
        self.assertEqual(row["reference"], "declared")
        result = self.evaluate(row)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("1 of 1 checks bind a candidate and a criterion", result["notes"])
        self.assertIn("1 of 1 checks name the sources they compared", result["notes"])

    def test_the_checkers_kind_is_declared_and_an_agents_check_is_advisory(self):
        code, _, err = self.check("--by", "Jane Roe")
        self.assertEqual(code, 2)
        self.assertIn("--person or --agent", err)
        code, _, err = self.check("--person", "--agent", "--by", "Jane Roe")
        self.assertEqual(code, 2)
        target = self.record / "checks.jsonl"
        code, out, _ = self.check("--agent", "--by", "some-model-1", "--result", "ok",
                                  "--checks-jsonl", str(target), "--confirm")
        self.assertEqual(code, 0)
        row = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((row["kind"], row["checker_kind"]), ("agent-check", "model"))
        self.assertIn("advisory", row["runner"])
        self.assertIn("advisory", out)

    def test_confirm_without_a_record_file_writes_nothing(self):
        code, _, err = self.check("--person", "--by", "Jane Roe", "--confirm")
        self.assertEqual(code, 2)
        self.assertIn("--checks-jsonl", err)

    def test_a_row_of_not_stated_fields_validates_and_binds_nothing(self):
        target = self.record / "checks.jsonl"
        self.check("--person", "--by", "Jane Roe", "--checks-jsonl", str(target), "--confirm")
        row = json.loads(target.read_text(encoding="utf-8"))
        result = self.evaluate(row)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("0 of 1 checks bind a candidate and a criterion", result["notes"])

    def test_a_row_written_before_sources_existed_keeps_its_meaning(self):
        result = self.evaluate({"kind": "code-test", "runner": "unittest", "method": "ran",
                                "result": "pass", "artifact": "log", "reference": "declared",
                                "candidate": "scripts/x.py", "criterion": "tests pass",
                                "limits": "one run"})
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("1 of 1 checks bind a candidate and a criterion", result["notes"])
        self.assertIn("0 of 1 checks name the sources they compared", result["notes"])

    def test_malformed_sources_are_reported(self):
        base = {"kind": "x", "runner": "r", "method": "m", "result": "r", "artifact": "a",
                "reference": "declared", "candidate": "c", "criterion": "k", "limits": "l"}
        for sources, phrase in (([], "non-empty list"), ([{"sha256": "0" * 64}], "with a path"),
                                ([{"path": "a.csv", "sha256": "abc"}], "not a sha256")):
            with self.subTest(sources=sources):
                result = self.evaluate(dict(base, sources=sources))
                self.assertFalse(result["ok"])
                self.assertTrue(any(phrase in p for p in result["problems"]), result["problems"])

    def snapshot(self) -> dict:
        """A verified snapshot of one file under workspace/snapshots/survey."""
        module = hr.starter_script("shared_snapshot")
        if module is None:
            self.skipTest("scripts/shared_snapshot.py is not in this project")
        source = self.root / "outside" / "data"
        source.mkdir(parents=True)
        (source / "survey.csv").write_text("firm,n\na,1\n", encoding="utf-8")
        return module.materialize(source, module.create_manifest(source, "survey"),
                                  self.root / "workspace" / "snapshots" / "survey")

    def test_a_source_in_a_snapshot_is_verified_against_its_manifest_and_named(self):
        # LESSON (rc22, Stage 2): `check` hashed a file in a snapshot and neither
        # verified the snapshot nor named it, while `agentic_review.py --source`
        # did both. They share the logic now, so both say the same of one snapshot.
        made = self.snapshot()
        inside = f"workspace/snapshots/survey/snapshots/{made['snapshot']}/data/survey.csv"
        target = self.record / "checks.jsonl"
        code, out, _ = self.check("--person", "--by", "Jane Roe", "--source", "workspace/snapshots/survey",
                                  "--source", inside, "--checks-jsonl", str(target), "--confirm")
        self.assertEqual(code, 0, out)
        row = json.loads(target.read_text(encoding="utf-8"))
        store, data = row["sources"]
        self.assertEqual(store, {"path": "workspace/snapshots/survey", "snapshot": made["snapshot"]})
        self.assertEqual((data["path"], data["snapshot"]), (inside, made["snapshot"]))
        self.assertEqual(data["sha256"], hashlib.sha256(b"firm,n\na,1\n").hexdigest())
        self.assertIn(f"verified snapshot {made['snapshot'][:12]}", out)
        result = self.evaluate(row)
        self.assertTrue(result["ok"], result["problems"])

    def test_a_snapshot_that_does_not_verify_is_recorded_with_the_reason(self):
        # Nothing forced: the person's check is recorded, and the row says the
        # snapshot it names did not match its manifest when it was recorded.
        made = self.snapshot()
        (pathlib.Path(made["data"]) / "survey.csv").write_text("tampered\n", encoding="utf-8")
        target = self.record / "checks.jsonl"
        code, out, _ = self.check("--person", "--by", "Jane Roe", "--source", "workspace/snapshots/survey",
                                  "--checks-jsonl", str(target), "--confirm")
        self.assertEqual(code, 0, out)
        entry = json.loads(target.read_text(encoding="utf-8"))["sources"][0]
        self.assertNotIn("snapshot", entry)
        self.assertIn("does not verify", entry["snapshot_unverified"])
        self.assertIn("did not verify against its manifest", out)
        self.assertTrue(self.evaluate(json.loads(target.read_text(encoding="utf-8")))["ok"])

    def test_the_review_and_the_check_share_one_snapshot_logic(self):
        sys.path.insert(0, str(pathlib.Path(hr.__file__).resolve().parent))
        import agentic_review as ar
        self.assertIs(ar.verify_snapshot, hr.verify_snapshot)
        self.assertIs(ar.snapshot_parts, hr.snapshot_parts)

    def test_a_snapshots_own_folder_and_its_data_folder_are_that_snapshot(self):
        # LESSON (rc22 review, f1ca7a5 F001): only a file below a snapshot's
        # data folder was read as the snapshot, so a check naming the
        # snapshot's folder or its data folder named no snapshot and verified
        # nothing.
        made = self.snapshot()
        folder = f"workspace/snapshots/survey/snapshots/{made['snapshot']}"
        target = self.record / "checks.jsonl"
        code, out, _ = self.check("--person", "--by", "Jane Roe", "--source", folder,
                                  "--source", f"{folder}/data", "--checks-jsonl", str(target),
                                  "--confirm")
        self.assertEqual(code, 0, out)
        named = json.loads(target.read_text(encoding="utf-8"))["sources"]
        self.assertEqual(named, [{"path": folder, "snapshot": made["snapshot"]},
                                 {"path": f"{folder}/data", "snapshot": made["snapshot"]}])
        for rel in (folder, folder + "/", f"{folder}/data", f"{folder}/manifest.json"):
            self.assertEqual(hr.snapshot_parts(rel), ("workspace/snapshots/survey", made["snapshot"]))
        self.assertEqual(hr.snapshot_parts("workspace/snapshots/survey/"),
                         ("workspace/snapshots/survey", None))
        self.assertIsNone(hr.snapshot_parts("workspace/snapshots/survey/snapshots"))
        self.assertIsNone(hr.snapshot_parts("workspace/snapshots/survey/current.json"))


class ConsentOrderLessons(unittest.TestCase):
    """LESSON (rc22, Stage 2): the convention said to clear consent by hand and
    then run cover, and an agent that cleared it after cover found promote
    refusing with only "rerun cover". The order is stated, and the refusal
    gives the way back."""

    def test_an_edit_after_cover_refuses_with_the_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = pathlib.Path(tmp) / "staging"
            staging.mkdir()
            evidence = {"record_id": "r", "record_schema_version": 2,
                        "consent": {"third_party_review": "PENDING"},
                        "turns": [{"id": "T1", "text": "do it"}]}
            (staging / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            (staging / "summary.md").write_text("Asked (T1).", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                hr.cmd_cover(type("A", (), {"record": str(staging)})())
            evidence["consent"]["third_party_review"] = "cleared"
            (staging / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            args = type("A", (), {"staging": str(staging), "dest": str(pathlib.Path(tmp) / "out"),
                                  "force": False})()
            with self.assertRaises(SystemExit) as caught:
                hr.cmd_promote(args)
            message = str(caught.exception.code)
            self.assertIn("evidence.json changed since coverage was built", message)
            self.assertIn("To recover: run cover again, then privacy-check and publication-review", message)

    def test_the_conventions_state_the_order(self):
        # The kit's own repository only: an installed project's convention is a
        # seed its team may have edited.
        root = pathlib.Path(hr.__file__).resolve().parents[1]
        if not (root / "starter" / "files" / "ai" / "RECORD-CONVENTION.md").is_file():
            self.skipTest("not the kit's repository")
        for name, phrase in (("ai/RECORD-CONVENTION.md", "BEFORE cover"),
                             ("starter/files/ai/RECORD-CONVENTION.md", "Clear consent before cover")):
            with self.subTest(name=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertIn(phrase, text)
                self.assertIn("run cover again", text.replace("`", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
