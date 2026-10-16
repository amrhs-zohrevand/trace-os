"""`trace_record.py keep`: the record routine in one command, before each commit.

LESSON (rc23, 15 October 2026): an agent session that commits work keeps a
trace record, and the rule holds for every agent. The routine was seven
commands, and in the agent-parity matrix of that day the agents that skipped it
were the ones left to find their transcript and run the steps by hand. `keep` runs them for the running session
and stops only where the agent's judgment is needed: a turn the summary does
not account for, a missing check, and the disclosure read, confirmed by the
digest it printed. These tests run it in temporary repositories against
synthetic transcripts, and never touch this repository's records.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import trace_record as hr  # noqa: E402
import trace_story as story  # noqa: E402

# This repository's records, ledger and session pointers, fingerprinted before
# a single test runs. Every test works in a temporary repository, and
# `tearDownModule` proves it: a test that promoted into the real ai/records
# would be writing history in the person's name.
GUARDED = ("ai/records", "ai/defects.jsonl", "workspace/record-staging/.sessions")


def fingerprint() -> str:
    digest = hashlib.sha256()
    for name in GUARDED:
        path = REPO / name
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()) \
            if path.is_dir() else []
        for item in files:
            stat = item.stat()
            digest.update(f"{item.relative_to(REPO)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


AT_IMPORT = fingerprint()


def tearDownModule():
    assert fingerprint() == AT_IMPORT, (
        "this repository's records, ledger or session pointers changed while the keep tests ran; "
        "a test wrote outside its temporary repository")


def inside(root: pathlib.Path, target) -> pathlib.Path:
    """`target`, resolved, after failing the test unless it lies inside `root`.

    AH-353: a deletion by relative path acts on whatever folder the test runs
    in, and this module ships to installed projects, where that folder can be
    the project itself. Every deletion or overwrite here takes an absolute path
    and passes through this check first.
    """
    target = pathlib.Path(target)
    if not target.is_absolute():
        raise AssertionError(f"{target} is a relative path; a destructive operation here takes an absolute one")
    root, resolved = pathlib.Path(root).resolve(), target.resolve()
    if root not in resolved.parents:
        raise AssertionError(f"{resolved} is not inside the temporary repository {root}")
    return resolved


SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "CLAUDE_CONFIG_DIR", "CODEX_HOME")


class KeepCase(unittest.TestCase):
    """A temporary repository and a Claude Code session whose transcript the test writes."""

    SID = "5e5510e0-aaaa-bbbb-cccc-0123456789ab"

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.tmp = tempfile.TemporaryDirectory(prefix="record-keep-")
        base = pathlib.Path(self.tmp.name).resolve()
        self.home, self.repo = base / "home", base / "repo"
        self.repo.mkdir()
        self.saved = os.getcwd()
        os.chdir(self.repo)
        for args in (["init", "-q", "-b", "master"], ["config", "user.name", "Rae Researcher"],
                     ["config", "user.email", "rae@example.test"], ["config", "commit.gpgsign", "false"]):
            self.git(*args)
        self.at(".gitignore").write_text("workspace/record-staging/\n", encoding="utf-8")
        self.at("notes").mkdir()
        self.at("notes/methods.md").write_text("The method.\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Start the project")
        self.transcript = self.home / ".claude/projects/-repo" / f"{self.SID}.jsonl"
        self.rows: list[dict] = []
        self.say("Please write the methods note.")
        self.answer("claude-opus-5-5")

    def tearDown(self):
        os.chdir(self.saved)
        self.tmp.cleanup()

    # --- the session --------------------------------------------------------

    def git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def at(self, relative) -> pathlib.Path:
        """A path in the temporary repository, absolute and checked to lie inside it."""
        return inside(self.repo, self.repo / relative)

    def remove(self, relative) -> None:
        """Delete a file or folder of the temporary repository, never anything outside it."""
        target = self.at(relative)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()

    def write(self):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8")

    def say(self, text: str):
        n = len(self.rows)
        self.rows.append({"type": "user", "uuid": f"u{n}", "timestamp": f"2026-10-15T09:{n:02d}:00Z",
                          "message": {"role": "user", "content": text}})
        self.write()

    def answer(self, model: str):
        n = len(self.rows)
        self.rows.append({"type": "assistant", "timestamp": f"2026-10-15T09:{n:02d}:30Z",
                          "message": {"model": model, "content": []}})
        self.write()

    def environment(self, **values) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
        env["HOME"] = str(self.home)
        env.update(values)
        return env

    def keep(self, *args, env=None, nearest="claude-code") -> tuple[int, str]:
        env = self.environment(CLAUDE_CODE_SESSION_ID=self.SID) if env is None else env
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pathlib.Path, "home", return_value=self.home), \
                mock.patch.object(hr, "nearest_agent_cli", return_value=nearest), \
                contextlib.redirect_stdout(out):
            code = hr.main(["keep", *args])
        return code, out.getvalue()

    # --- the record's own parts, which keep never writes --------------------

    def staging(self, rid="r1") -> pathlib.Path:
        return self.at(pathlib.Path("workspace/record-staging") / rid)

    def summary(self, text: str, rid="r1"):
        self.staging(rid).mkdir(parents=True, exist_ok=True)
        (self.staging(rid) / "summary.md").write_text(f"# Human direction\n\n{text}\n", encoding="utf-8")

    def check(self, rid="r1", method="read the note", artifact="notes/methods.md", reference="path"):
        self.staging(rid).mkdir(parents=True, exist_ok=True)
        row = {"kind": "agent-check", "runner": "claude-opus-5-5 (model, advisory)", "method": method,
               "result": "as asked", "artifact": artifact, "reference": reference,
               "candidate": "notes/methods.md", "criterion": "the note says what was asked",
               "limits": "one synthetic note"}
        with (self.staging(rid) / "checks.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def decision(self, rid="r1", **changes):
        entry = json.loads((REPO / "scripts" / "decision.example.json").read_text(encoding="utf-8"))
        entry.update(changes)
        path = pathlib.Path(self.tmp.name) / "decision.json"
        path.write_text(json.dumps(entry), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            story.cmd_decision(hr, argparse.Namespace(record=str(self.staging(rid)), input=str(path), scope=None))

    @staticmethod
    def digest(out: str) -> str:
        found = re.search(r"^digest:\s+([0-9a-f]{16})", out, re.MULTILINE)
        assert found, out
        return found.group(1)

    def evidence(self, where: pathlib.Path) -> dict:
        return json.loads((where / "evidence.json").read_text(encoding="utf-8"))

    def coverage(self, where: pathlib.Path) -> dict:
        return {e["turn"]: e for e in json.loads((where / "coverage.json").read_text(encoding="utf-8"))["turns"]}

    def kept(self, *args, rid="r1") -> str:
        """A whole keep: the first run prints the digest, the second clears and promotes."""
        code, out = self.keep("--record-id", rid, *args)
        self.assertEqual(code, 1, out)
        code, out = self.keep("--record-id", rid, "--cleared", self.digest(out), *args)
        self.assertEqual(code, 0, out)
        return out


class OneCommand(KeepCase):

    def test_one_command_keeps_a_record_of_the_running_session(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        code, out = self.keep("--record-id", "r1", "--subject", "Write the methods note")
        self.assertEqual(code, 1, out)
        self.assertIn("nothing was promoted", out)
        self.assertIn("Nothing here stops a commit", out)
        self.assertFalse(pathlib.Path("ai/records/r1").exists())
        code, out = self.keep("--record-id", "r1", "--subject", "Write the methods note",
                              "--cleared", self.digest(out))
        self.assertEqual(code, 0, out)
        record = pathlib.Path("ai/records/r1")
        result = hr.evaluate_record(record)
        self.assertTrue(result["ok"], result["problems"])
        evidence = self.evidence(record)
        self.assertEqual(evidence["consent"]["third_party_review"], "cleared")
        self.assertEqual(evidence["consent"]["own_turns_by"], "Rae Researcher")
        self.assertIn("git config user.name", evidence["consent"]["own_turns_basis"])
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertTrue(message.startswith("Write the methods note\n"))
        self.assertIn("Rae asked for the methods note (T1).", message)
        self.assertIn("\nRecord: ai/records/r1\n", message)
        self.assertIn("Execution-agent: claude-opus-5-5", message)
        self.assertEqual(hr.commit_message_problems(message), [])
        self.assertIn("git commit --file workspace/record-staging/r1/commit-message.txt", out)

    def test_consent_is_never_cleared_without_the_declaration(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        for _ in range(2):
            code, out = self.keep("--record-id", "r1")
            self.assertEqual(code, 1, out)
            self.assertEqual(self.evidence(self.staging())["consent"]["third_party_review"], "PENDING")
        code, out = self.keep("--record-id", "r1", "--cleared", "0" * 16)
        self.assertEqual(code, 1, out)
        self.assertIn(f"the digest passed, {'0' * 16}, is not the digest of the turns now", out)
        self.assertEqual(self.evidence(self.staging())["consent"]["third_party_review"], "PENDING")
        self.assertFalse(pathlib.Path("ai/records/r1").exists())

    def test_a_turn_added_after_the_digest_keeps_consent_pending_and_is_named(self):
        self.summary("Rae asked for the methods note (T1) and the sampling paragraph (T2).")
        self.check()
        code, out = self.keep("--record-id", "r1")
        read = self.digest(out)
        self.say("Also add the sampling paragraph.")
        code, out = self.keep("--record-id", "r1", "--cleared", read)
        self.assertEqual(code, 1, out)
        self.assertIn(f"read the turns added or changed since digest {read} (T2)", out)
        self.assertEqual(self.evidence(self.staging())["consent"]["third_party_review"], "PENDING")
        self.assertFalse(pathlib.Path("ai/records/r1").exists())

    def test_a_screen_match_blocks_clearance_and_names_only_the_category(self):
        token = "ghp_" + "Q" * 36
        self.say(f"The deploy key is {token}, use it.")
        self.summary("Rae asked for the methods note (T1) and gave a key (T2).")
        self.check()
        code, first = self.keep("--record-id", "r1")
        code, out = self.keep("--record-id", "r1", "--cleared", self.digest(first))
        self.assertEqual(code, 1, out)
        self.assertIn("provider-token", out)
        self.assertNotIn(token, first + out)
        self.assertEqual(self.evidence(self.staging())["consent"]["third_party_review"], "PENDING")
        self.assertFalse(pathlib.Path("ai/records/r1").exists())


class Coverage(KeepCase):

    def test_an_uncited_turn_is_listed_and_never_excluded(self):
        self.say("thanks")
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        code, out = self.keep("--record-id", "r1")
        code, out = self.keep("--record-id", "r1", "--cleared", self.digest(out))
        self.assertEqual(code, 1, out)
        self.assertIn("turns neither cited in summary.md nor excluded: T2.", out)
        self.assertEqual(self.coverage(self.staging())["T2"]["status"], "UNCOVERED")
        self.assertFalse(pathlib.Path("ai/records/r1").exists())

    def test_an_exclusion_needs_a_reason_and_survives_reruns(self):
        self.say("thanks")
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        code, out = self.keep("--record-id", "r1", "--exclude", "T2", " ")
        self.assertEqual(code, 2, out)
        self.assertIn("needs a reason", out)
        self.assertFalse((self.staging() / "evidence.json").exists())
        code, out = self.keep("--record-id", "r1", "--exclude", "T9", "absent")
        self.assertEqual(code, 2, out)
        self.assertIn("--exclude names T9", out)
        code, out = self.keep("--record-id", "r1", "--exclude", "T2", "a thank-you, no direction")
        self.assertEqual(self.coverage(self.staging())["T2"],
                         {"turn": "T2", "status": "excluded", "reason": "a thank-you, no direction"})
        code, out = self.keep("--record-id", "r1", "--cleared", self.digest(out))
        self.assertEqual(code, 0, out)
        self.assertIn("excluded  T2: a thank-you, no direction", out)
        self.assertEqual(self.coverage(pathlib.Path("ai/records/r1"))["T2"]["status"], "excluded")

    def plant(self, relative: str, turn: dict, status: str):
        """Another record holding the same transcript row under its own id, T5."""
        where = self.at(relative)
        where.mkdir(parents=True)
        copy = {**turn, "id": "T5"}
        (where / "evidence.json").write_text(json.dumps({"record_id": where.name, "turns": [copy]}),
                                             encoding="utf-8")
        (where / "summary.md").write_text("Rae asked for the sampling paragraph (T5).\n", encoding="utf-8")
        entry = {"turn": "T5", "status": status}
        if status == "excluded":
            entry["reason"] = "kept elsewhere"
        (where / "coverage.json").write_text(json.dumps({"turns": [entry]}), encoding="utf-8")

    def test_a_turn_summarized_in_another_promoted_record_is_excluded_with_a_checkable_reason(self):
        self.say("Also add the sampling paragraph.")
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.keep("--record-id", "r1")
        sampling = self.evidence(self.staging())["turns"][1]
        self.plant("ai/records/other", sampling, "summarized")
        code, out = self.keep("--record-id", "r1")
        reason = self.coverage(self.staging())["T2"]
        self.assertEqual(reason["status"], "excluded")
        self.assertEqual(reason["reason"], "summarized in ai/records/other as T5 (the same transcript "
                                           f"row {sampling['row']} with the same captured-text digest)")
        self.assertIn("excluded  T2: summarized in ai/records/other as T5", out)
        # Not when that record excludes the turn too, and never a record only
        # in staging: each would let the turn fall out of every summary.
        self.remove("ai/records/other")
        self.plant("ai/records/other", sampling, "excluded")
        self.plant("workspace/record-staging/draft", sampling, "summarized")
        self.summary("Rae asked for the methods note (T1).", rid="r2")
        code, out = self.keep("--record-id", "r2")
        self.assertEqual(self.coverage(self.staging("r2"))["T2"]["status"], "UNCOVERED")
        self.assertIn("turns neither cited in summary.md nor excluded: T2.", out)

    def test_a_turn_cited_only_by_a_decision_is_named(self):
        self.say("Use the short form.")
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.keep("--record-id", "r1")
        self.decision(human_inputs=["T2"], human_direction="Rae asked for the short form.")
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 1, out)
        self.assertIn("T2 (a human input of D1)", out)


class LaterCommits(KeepCase):

    def commit(self, message_file="workspace/record-staging/r1/commit-message.txt"):
        self.git("add", "ai/records", "notes")
        self.git("commit", "-q", "--file", str(self.at(message_file)))

    def test_a_second_commit_updates_the_record_without_renumbering_and_drafts_only_what_is_new(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check(method="read the first note")
        self.kept("--subject", "Write the methods note")
        first = self.evidence(pathlib.Path("ai/records/r1"))["turns"]
        self.commit()
        self.say("Now add the sampling paragraph, keep it short.")
        self.answer("claude-opus-5-5")
        self.at("notes/methods.md").write_text("The method.\nThe sample.\n", encoding="utf-8")
        with (self.staging() / "summary.md").open("a", encoding="utf-8") as handle:
            handle.write("Rae asked for a short sampling paragraph (T2).\n")
        # The refresh that adds T2, so that the decision it directed can cite it.
        code, out = self.keep("--record-id", "r1")
        self.assertIn("T2 added", out)
        self.decision(human_inputs=["T2"], human_direction="Rae asked for a short paragraph.",
                      artifacts=["notes/methods.md"])
        self.check(method="counted the paragraph's lines")
        self.kept("--subject", "Add the sampling paragraph")
        record = pathlib.Path("ai/records/r1")
        turns = self.evidence(record)["turns"]
        self.assertEqual([t["id"] for t in turns], ["T1", "T2"])
        self.assertEqual(turns[0], first[0])
        self.assertTrue(hr.evaluate_record(record)["ok"])
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertIn("Human direction account, new since the record's last commit:", message)
        self.assertIn("Rae asked for a short sampling paragraph (T2).", message)
        self.assertNotIn("Rae asked for the methods note (T1).", message)
        self.assertIn("D1:", message)
        self.assertIn("counted the paragraph's lines", message)
        self.assertNotIn("read the first note", message)
        self.assertIn("Earlier: 1 commit(s) name this record", message)
        self.assertIn("this body adds 1 turn(s), 1 decision(s) and 1 check(s)", message)
        self.assertIn("Record: ai/records/r1", message)

    def test_the_sessions_record_is_found_without_an_id(self):
        code, out = self.keep("--subject", "Write the methods note")
        self.assertEqual(code, 1, out)
        named = re.search(r"^record:\s+(\S+)", out, re.MULTILINE).group(1)
        self.assertRegex(named, r"^\d{8}-write-the-methods-note$")
        code, out = self.keep()
        self.assertIn(f"record:     {named} (the record this session kept last", out)
        self.remove("workspace/record-staging/.sessions")
        code, out = self.keep()
        self.assertIn(f"record:     {named} (the one record holding turns of this session's transcript)", out)

    def test_several_records_from_one_transcript_without_a_pointer_are_ambiguous(self):
        self.keep("--record-id", "a")
        self.keep("--record-id", "b")
        self.remove("workspace/record-staging/.sessions")
        before = sorted(str(p) for p in pathlib.Path("workspace").rglob("*"))
        code, out = self.keep()
        self.assertEqual(code, 2, out)
        self.assertIn("several records hold turns of this session's transcript (a, b)", out)
        self.assertIn("Pass --record-id", out)
        self.assertEqual(sorted(str(p) for p in pathlib.Path("workspace").rglob("*")), before)

    def test_a_promoted_record_edited_after_promotion_is_not_overwritten(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.kept()
        edited = self.at("ai/records/r1/summary.md")
        edited.write_text("Edited by hand after promotion (T1).\n", encoding="utf-8")
        self.say("One more thing.")
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 2, out)
        self.assertIn("ai/records/r1 changed after its promotion (summary.md), compared with the file "
                      "digests of its disclosure receipt", out)
        self.assertIn("--reseed", out)
        self.assertEqual(edited.read_text(encoding="utf-8"), "Edited by hand after promotion (T1).\n")
        self.assertEqual(len(self.evidence(self.staging())["turns"]), 1, "staging was not refreshed")

    def test_a_hand_edit_committed_after_promotion_is_seen_without_a_receipt(self):
        # AH-352: a record promoted before receipts existed was compared with
        # `git status`, which sees no edit once it is committed, and staging's
        # summary.md would then have overwritten the person's edit.
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.kept("--subject", "Write the methods note")
        self.remove("ai/records/r1/publication.json")
        self.commit()
        record = pathlib.Path("ai/records/r1")
        changed, compared = hr.changed_since_promotion(record)
        self.assertEqual(changed, [])
        self.assertRegex(compared, r"^the record's last commit, [0-9a-f]{8}$")
        edited = self.at("ai/records/r1/summary.md")
        edited.write_text("Edited by hand after promotion (T1).\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "Edit the summary by hand")
        self.assertEqual(self.git("status", "--porcelain", "--", "ai/records/r1"), "")
        self.say("One more thing.")
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 2, out)
        self.assertIn("ai/records/r1 changed after its promotion (summary.md), compared with the record's "
                      "last commit", out)
        self.assertEqual(edited.read_text(encoding="utf-8"), "Edited by hand after promotion (T1).\n")
        self.assertEqual(len(self.evidence(self.staging())["turns"]), 1, "staging was not refreshed")

    def test_a_held_lock_refuses_a_second_run_and_writes_nothing_to_staging(self):
        lock = self.at("workspace/record-staging/.sessions/r1.lock")
        lock.parent.mkdir(parents=True)
        lock.write_text("4242 another keep run\n", encoding="utf-8")
        before = sorted(str(p) for p in self.at("workspace").rglob("*"))
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 2, out)
        self.assertIn("another keep run holds workspace/record-staging/.sessions/r1.lock", out)
        self.assertIn("Nothing here stops a commit", out)
        self.assertFalse(self.staging().exists(), "a refused run wrote to staging")
        self.assertEqual(sorted(str(p) for p in self.at("workspace").rglob("*")), before)
        self.assertEqual(lock.read_text(encoding="utf-8"), "4242 another keep run\n",
                         "the refused run removed the lock it did not hold")

    def test_reseed_starts_staging_again_from_the_promoted_record_and_keeps_it(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.kept()
        promoted = self.at("ai/records/r1/summary.md")
        by_hand = "# Human direction\n\nRae asked for the methods note, said again by hand (T1).\n"
        promoted.write_text(by_hand, encoding="utf-8")
        self.summary("Staging's own draft, which the reseed replaces (T1).")
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 2, out)
        code, out = self.keep("--record-id", "r1", "--reseed")
        self.assertEqual(code, 0, out)
        self.assertIn("staging:    started from ai/records/r1 (--reseed)", out)
        self.assertEqual((self.staging() / "summary.md").read_text(encoding="utf-8"), by_hand)
        self.assertEqual(promoted.read_text(encoding="utf-8"), by_hand)
        record = pathlib.Path("ai/records/r1")
        self.assertTrue(hr.evaluate_record(record)["ok"], hr.evaluate_record(record)["problems"])
        self.assertEqual(hr.changed_since_promotion(record)[0], [], "the receipt was not renewed")
        self.assertIn("review:     disclosure review recorded by claude-opus-5-5 (executing agent)", out)

    def test_a_staging_folder_of_another_record_or_session_is_refused(self):
        self.keep("--record-id", "other")
        code, out = self.keep("--record-id", "r1", "--staging", "workspace/record-staging/other")
        self.assertEqual(code, 2, out)
        self.assertIn("workspace/record-staging/other holds the record other, not r1", out)
        elsewhere = self.home / "elsewhere.jsonl"
        elsewhere.write_text(json.dumps({"type": "user", "uuid": "x", "message": {
            "role": "user", "content": "Another session's request."}}) + "\n", encoding="utf-8")
        self.keep("--record-id", "r2", "--transcript", str(elsewhere))
        held = self.evidence(self.staging("r2"))
        code, out = self.keep("--record-id", "r2")
        self.assertEqual(code, 2, out)
        self.assertIn("holds turns of another session's transcript (elsewhere.jsonl), not of this one "
                      f"({self.SID}.jsonl)", out)
        self.assertEqual(self.evidence(self.staging("r2")), held)

    def test_a_missing_staging_folder_is_seeded_from_the_promoted_record(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.kept()
        promoted = {name: (pathlib.Path("ai/records/r1") / name).read_text(encoding="utf-8")
                    for name in ("summary.md", "checks.jsonl")}
        self.remove(self.staging())
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 0, out)
        self.assertIn("staging:    started from ai/records/r1, as staging held no record", out)
        for name, text in promoted.items():
            self.assertEqual((self.staging() / name).read_text(encoding="utf-8"), text)
        self.assertTrue(hr.evaluate_record(pathlib.Path("ai/records/r1"))["ok"])

    def test_unchanged_turns_keep_their_clearance_and_say_whose(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        self.kept("--by", "Rae Researcher")
        code, out = self.keep("--record-id", "r1")
        self.assertEqual(code, 0, out)
        self.assertIn("consent:    carries over: the captured turns are the ones Rae Researcher cleared at", out)
        consent = self.evidence(pathlib.Path("ai/records/r1"))["consent"]
        self.assertEqual((consent["third_party_review"], consent["cleared_by"]), ("cleared", "Rae Researcher"))

    def test_a_check_naming_a_missing_path_stops_before_promotion(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check(artifact="results/missing.json")
        code, out = self.keep("--record-id", "r1")
        code, out = self.keep("--record-id", "r1", "--cleared", self.digest(out))
        self.assertEqual(code, 1, out)
        self.assertIn("names an artifact that does not exist: results/missing.json", out)
        self.assertFalse(pathlib.Path("ai/records/r1").exists())


class NoSession(KeepCase):

    def written(self) -> list[str]:
        return sorted(str(p) for p in pathlib.Path(".").rglob("*") if ".git" not in p.parts)

    def test_no_transcript_writes_nothing_and_says_how_to_commit(self):
        before = self.written()
        for env in (self.environment(),
                    self.environment(CLAUDE_CODE_SESSION_ID="no-such-session")):
            code, out = self.keep("--subject", "Write the methods note", env=env)
            self.assertEqual(code, 2, out)
            self.assertIn("no trace record was kept and why, with no Record: line", out)
            self.assertIn("Execution-agent:", out)
            self.assertNotIn("Record: none", out)
            self.assertEqual(self.written(), before)

    def test_two_sessions_without_a_process_tree_write_nothing(self):
        before = self.written()
        env = self.environment(CLAUDE_CODE_SESSION_ID=self.SID, CODEX_THREAD_ID="b")
        code, out = self.keep("--subject", "Write the methods note", env=env, nearest=None)
        self.assertEqual(code, 2, out)
        self.assertIn("ambiguous", out)
        self.assertIn("--transcript", out)
        self.assertEqual(self.written(), before)


class Boundaries(KeepCase):

    def test_keep_never_stages_or_commits(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        head = self.git("rev-parse", "HEAD")
        real, calls = subprocess.run, []

        def spy(argv, *args, **kwargs):
            calls.append([str(a) for a in argv])
            return real(argv, *args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=spy):
            self.kept("--subject", "Write the methods note")
            # A record promoted before receipts existed, which no commit names,
            # is compared with the working tree, through the one status read
            # keep makes.
            legacy = pathlib.Path("ai/records/legacy")
            shutil.copytree(self.at("ai/records/r1"), self.at(legacy))
            self.remove(legacy / "publication.json")
            changed, compared = hr.changed_since_promotion(legacy)
            self.assertTrue(changed)
            self.assertIn("the working tree only", compared)
        git_calls = [argv[1:] for argv in calls if argv and argv[0] == "git"]
        self.assertTrue(git_calls)
        for argv in git_calls:
            words = [a for a in argv if not a.startswith("-")]
            self.assertFalse({"add", "commit", "stash", "reset", "checkout", "rm"} & set(words), argv)
            if "status" in argv:
                self.assertLess(argv.index("--no-optional-locks"), argv.index("status"), argv)
        self.assertTrue(any("status" in argv for argv in git_calls))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        # The refusal says how little that comparison sees.
        code, out = self.keep("--record-id", "legacy")
        self.assertEqual(code, 2, out)
        self.assertIn("compared with the working tree only, since it has no disclosure receipt and no "
                      "commit names it, so a change committed since its promotion is not seen", out)

    def test_the_trailer_names_the_model_running_now(self):
        self.rows = []
        self.say("Please write the methods note.")
        self.answer("claude-sonnet-5-5")
        self.say("Switching models; go on.")
        self.answer("claude-opus-5-5")
        self.summary("Rae asked for the methods note (T1) and to go on (T2).")
        self.check()
        self.kept("--subject", "Write the methods note")
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertIn("Execution-agent: claude-opus-5-5", message)
        self.assertNotIn("Execution-agent: claude-sonnet-5-5", message)
        executed = [e["model"] for e in self.evidence(pathlib.Path("ai/records/r1"))["execution_agents"]]
        self.assertEqual(sorted(executed), ["claude-opus-5-5", "claude-sonnet-5-5"])

    def receipt_by(self) -> str:
        return json.loads(self.at("ai/records/r1/publication.json").read_text(encoding="utf-8"))["by"]

    def test_a_self_reported_execution_agent_is_marked_wherever_keep_writes_it(self):
        # AH-349: an id passed with --execution-agent that the transcript does
        # not name as its latest model was presented as the model running now,
        # recorded as the clearer and the disclosure reviewer, and signed.
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        out = self.kept("--subject", "Write the methods note", "--execution-agent", "claude-fable-5-1")
        self.assertIn("model:      claude-fable-5-1 (self-reported with --execution-agent, and recorded as "
                      "self-reported; this session's transcript names claude-opus-5-5 as its latest model)", out)
        record = pathlib.Path("ai/records/r1")
        self.assertEqual(self.evidence(record)["consent"]["cleared_by"],
                         "claude-fable-5-1 (executing agent, self-reported)")
        self.assertEqual(self.receipt_by(), "claude-fable-5-1 (executing agent, self-reported)")
        self.assertTrue(hr.evaluate_record(record)["ok"], hr.evaluate_record(record)["problems"])
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertIn("\nThe Execution-agent id claude-fable-5-1 is self-reported: passed with --execution-agent, "
                      "while this session's transcript names claude-opus-5-5 as its latest model.\n", message)
        self.assertEqual(hr.EXECUTION_AGENT_RE.findall(hr.trailer_block(message)), ["claude-fable-5-1"])
        self.assertEqual(hr.EXECUTION_AGENT_RE.findall(message), ["claude-fable-5-1"])
        self.assertEqual(hr.commit_message_problems(message), [])

    def test_an_execution_agent_equal_to_the_model_read_changes_nothing(self):
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        out = self.kept("--subject", "Write the methods note", "--execution-agent", "claude-opus-5-5")
        self.assertIn("model:      claude-opus-5-5 (the latest model in this session's transcript", out)
        self.assertNotIn("self-reported", out)
        self.assertEqual(self.evidence(pathlib.Path("ai/records/r1"))["consent"]["cleared_by"],
                         "claude-opus-5-5 (executing agent)")
        self.assertEqual(self.receipt_by(), "claude-opus-5-5 (executing agent)")
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertNotIn("self-reported", message)
        self.assertTrue(message.endswith("\nExecution-agent: claude-opus-5-5\n"), message)

    def test_an_execution_agent_over_a_transcript_naming_no_model_is_self_reported(self):
        self.rows = []
        self.say("Please write the methods note.")
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        code, out = self.keep("--record-id", "r1", "--subject", "Write the methods note")
        self.assertEqual(code, 1, out)
        self.assertIn("model:      not stated in the transcript; pass --execution-agent with your model id, "
                      "which keep records as self-reported", out)
        out = self.kept("--subject", "Write the methods note", "--execution-agent", "claude-fable-5-1")
        self.assertIn("this session's transcript names no model)", out)
        self.assertEqual(self.receipt_by(), "claude-fable-5-1 (executing agent, self-reported)")
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertIn("The Execution-agent id claude-fable-5-1 is self-reported: passed with --execution-agent, "
                      "while this session's transcript names no model.", message)

    def test_the_help_and_the_model_line_say_a_subagent_passes_its_own_model(self):
        # AH-350: a subagent runs under its parent's session, so the latest
        # model in that transcript is the parent's; keep cannot tell them apart.
        said = "a subagent shares its parent's session, so a subagent passes --execution-agent with its own model id"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            hr.main(["keep", "--help"])
        flat = re.sub(r"\s+", "", out.getvalue())
        self.assertIn(re.sub(r"\s+", "", said), flat)
        self.assertIn(re.sub(r"\s+", "", "is recorded as self-reported"), flat)
        code, out = self.keep("--record-id", "r1")
        self.assertIn("model:      claude-opus-5-5 (the latest model in this session's transcript, for the "
                      f"Execution-agent: trailer; {said})", out)

    def test_a_codex_rollout_is_kept_the_same_way(self):
        tid = "01a13edb-0000-71d1-b4c7-7a89a61aa6c9"
        codex_home = self.home / "codex-home"
        rollout = codex_home / "sessions/2026/10/15" / f"rollout-2026-10-15T09-18-47-{tid}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("".join(json.dumps(r) + "\n" for r in (
            {"type": "session_meta", "payload": {"id": tid}},
            {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
            {"type": "response_item", "timestamp": "2026-10-15T09:19:00Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Please write the methods note."}]}},
            {"type": "response_item", "timestamp": "2026-10-15T09:19:30Z",
             "payload": {"type": "message", "role": "assistant", "content": []}})), encoding="utf-8")
        env = self.environment(CODEX_THREAD_ID=tid, CODEX_HOME=str(codex_home))
        self.summary("Rae asked for the methods note (T1).")
        self.check()
        code, out = self.keep("--record-id", "r1", env=env, nearest="codex")
        self.assertEqual(code, 1, out)
        code, out = self.keep("--record-id", "r1", "--cleared", self.digest(out), "--subject",
                              "Write the methods note", env=env, nearest="codex")
        self.assertEqual(code, 0, out)
        record = pathlib.Path("ai/records/r1")
        self.assertEqual(self.evidence(record)["transcript_format"], "codex-rollout")
        self.assertEqual(self.evidence(record)["turns"][0]["source"]["session"], rollout.name)
        message = (self.staging() / "commit-message.txt").read_text(encoding="utf-8")
        self.assertIn("Execution-agent: gpt-5.5", message)

    def test_a_redaction_replaces_one_exact_passage_and_needs_a_new_clearance(self):
        self.say("Ask Sam Smith at the clinic for the codes.")
        self.summary("Rae asked for the methods note (T1) and where the codes are (T2).")
        self.check()
        code, out = self.keep("--record-id", "r1")
        before = self.digest(out)
        code, out = self.keep("--record-id", "r1", "--redact", "T2", "e", "name")
        self.assertEqual(code, 2, out)
        self.assertIn("occurs", out)
        code, out = self.keep("--record-id", "r1", "--redact", "T2", "Sam Smith", "a third party's name")
        self.assertNotIn("Sam Smith", out)
        text = self.evidence(self.staging())["turns"][1]["text"]
        self.assertEqual(text, "Ask [REDACTED: a third party's name] at the clinic for the codes.")
        self.assertNotEqual(self.digest(out), before)
        code, out = self.keep("--record-id", "r1", "--redact", "T2", "Sam Smith", "a third party's name",
                              "--cleared", self.digest(out))
        self.assertEqual(code, 0, out)
        self.assertIn("already reads [REDACTED: a third party's name]", out)

    def test_a_check_pinned_to_a_skill_edited_mid_session_still_verifies(self):
        # Design section 9: a refresh recomputes the instruction digests, so a
        # check row written under the skill as it stood earlier would no longer
        # match and promotion would refuse it. The earlier digests are kept.
        skill = self.at(".skills/example-method/SKILL.md")
        skill.parent.mkdir(parents=True)
        skill.write_text("Method, first version.\n", encoding="utf-8")
        self.summary("Rae asked for the methods note (T1).")
        self.keep("--record-id", "r1")
        version = "sha256:" + hr.sha256_file(skill)
        self.check()
        rows = [json.loads(line) for line in (self.staging() / "checks.jsonl").read_text().splitlines()]
        rows[0].update(procedure="example-method", procedure_version=version)
        (self.staging() / "checks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        skill.write_text("Method, second version.\n", encoding="utf-8")
        code, out = self.keep("--record-id", "r1")
        evidence = self.evidence(self.staging())
        self.assertEqual(evidence["instructions_earlier"][0]["files"][".skills/example-method/SKILL.md"],
                         version[len("sha256:"):])
        code, out = self.keep("--record-id", "r1", "--cleared", self.digest(out))
        self.assertEqual(code, 0, out)


class Conventions(unittest.TestCase):

    def test_the_conventions_name_keep(self):
        # The kit's own repository only: an installed project's instructions
        # are seeds its team may have edited.
        if not (REPO / "starter" / "files" / "ai" / "RECORD-CONVENTION.md").is_file():
            self.skipTest("not the kit's repository")
        for name, phrases in (
                ("AGENTS.md", ("run `python3 scripts/trace_record.py keep --subject",
                               "A commit a person typed owes none")),
                ("ai/RECORD-CONVENTION.md", ("`trace_record.py keep` runs the routine below",
                                             "--cleared <digest>", "BEFORE cover",
                                             "a subagent shares its parent's session, so a subagent passes "
                                             "`--execution-agent` with its own model id")),
                ("WORKFLOW.md", ("run `trace_record.py keep --subject",)),
                ("starter/files/AGENTS.md", ("python scripts/trace_record.py keep --subject",
                                             "pass --cleared with the digest it printed",
                                             "A commit a person typed owes none")),
                ("starter/files/ai/RECORD-CONVENTION.md", ("`trace_record.py keep` runs this routine",
                                                          "Clear consent before cover",
                                                          "a subagent shares its parent's session, so a "
                                                          "subagent passes `--execution-agent` with its own "
                                                          "model id")),
                ("starter/files/.skills/trace-state/SKILL.md", ("`scripts/trace_record.py keep --subject",
                                                               "`keep --exclude <turn> \"<reason>\"`"))):
            with self.subTest(name=name):
                text = (REPO / name).read_text(encoding="utf-8")
                for phrase in phrases:
                    self.assertIn(phrase, text)
        # Every option the text tells an agent to pass is one keep takes.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            hr.main(["keep", "--help"])
        for option in ("--subject", "--cleared", "--exclude", "--redact", "--record-id", "--transcript"):
            self.assertIn(option, out.getvalue())


if __name__ == "__main__":
    unittest.main()
