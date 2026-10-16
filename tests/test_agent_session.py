"""`trace_record.py session` and `extract current`: the running agent's own
transcript and model id, read off the CLI's files the same way for Claude Code
and Codex rather than left to each model to find or report.

LESSON: in the agent-parity matrix of 2026-10-15 a Codex agent found its own
rollout through its environment and recorded the session, while Claude agents
given the same instructions recorded none. The difference was what each model
happened to know about where its runtime writes, not what the kit asked.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import trace_record as hr  # noqa: E402


def write_jsonl(path: pathlib.Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class CurrentSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-session-")
        self.home = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **values):
        return {"HOME": str(self.home), **values}

    def test_claude_code_session_is_found_and_its_latest_model_read(self):
        sid = "11111111-2222-3333-4444-555555555555"
        write_jsonl(self.home / ".claude/projects/-work-proj" / f"{sid}.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": "2026-10-15T09:00:00Z",
             "message": {"model": "claude-sonnet-5-5", "content": []}},
            {"type": "assistant", "timestamp": "2026-10-15T09:05:00Z",
             "message": {"model": "<synthetic>", "content": []}},
            {"type": "assistant", "timestamp": "2026-10-15T09:10:00Z",
             "message": {"model": "claude-opus-5-5", "content": []}},
        ])
        with mock.patch.object(pathlib.Path, "home", return_value=self.home):
            found = hr.current_session(self.env(CLAUDE_CODE_SESSION_ID=sid), chain=[])
        self.assertEqual(found["runner"], "claude-code")
        self.assertTrue(found["transcript"].endswith(f"{sid}.jsonl"))
        # The model running now, after a switch, not the session's first one.
        self.assertEqual(found["model"], "claude-opus-5-5")

    def test_codex_rollout_is_found_under_codex_home(self):
        tid = "01a13edb-613d-71d1-b4c7-7a89a61aa6c9"
        codex_home = self.home / "custom-codex"
        write_jsonl(codex_home / "sessions/2026/10/15" / f"rollout-2026-10-15T09-18-47-{tid}.jsonl", [
            {"type": "session_meta", "payload": {"id": tid}},
            {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
            {"type": "response_item", "timestamp": "2026-10-15T09:19:00Z",
             "payload": {"type": "message", "role": "assistant", "content": []}},
        ])
        found = hr.current_session(self.env(CODEX_THREAD_ID=tid, CODEX_HOME=str(codex_home)), chain=[])
        self.assertEqual(found["runner"], "codex")
        self.assertEqual(found["model"], "gpt-6-astra")

    def test_no_session_is_said_and_not_guessed(self):
        found = hr.current_session(self.env(), chain=[])
        self.assertIsNone(found["runner"])
        self.assertIsNone(found["model"])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True), \
                contextlib.redirect_stdout(out):
            code = hr.main(["session"])
        self.assertEqual(code, 1)
        self.assertIn("self-reported", out.getvalue())

    def test_an_agent_started_from_another_agents_shell_is_itself(self):
        # LESSON: a Codex run started from a Claude Code shell inherited
        # CLAUDE_CODE_SESSION_ID and signed its commit with the outer Claude model
        # (agent-parity matrix, 2026-10-15). agentic_review.py nests agents the
        # same way, so the nearest agent program decides.
        tid = "01a13efb-21b5-7401-9acf-6d30d949b222"
        codex_home = self.home / ".codex"
        write_jsonl(codex_home / "sessions/2026/10/15" / f"rollout-x-{tid}.jsonl", [
            {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
            {"type": "response_item", "timestamp": "2026-10-15T09:19:00Z",
             "payload": {"type": "message", "role": "assistant", "content": []}},
        ])
        env = self.env(CLAUDE_CODE_SESSION_ID="outer", CODEX_THREAD_ID=tid)
        chain = [["python3", "scripts/trace_record.py", "session"], ["/bin/bash", "-lc", "x"],
                 ["/opt/codex/bin/codex-linux-sandbox", "--sandbox-policy-cwd", "/p"],
                 ["node", "/usr/lib/node_modules/.bin/codex", "exec"],
                 ["/usr/local/bin/claude", "-p"]]
        with mock.patch.object(pathlib.Path, "home", return_value=self.home):
            found = hr.current_session(env, chain=chain)
        self.assertEqual((found["runner"], found["model"]), ("codex", "gpt-5.5"))

    def test_two_sessions_and_no_process_tree_is_ambiguous_not_a_guess(self):
        found = hr.current_session(self.env(CLAUDE_CODE_SESSION_ID="a", CODEX_THREAD_ID="b"), chain=[])
        self.assertIsNone(found["runner"])
        self.assertEqual(found["ambiguous"], ["claude-code", "codex"])

    def test_an_inherited_variable_under_another_program_is_not_taken(self):
        # Only the outer agent's variable is set, but the program running this
        # command is a different agent CLI: its session cannot be the outer one.
        found = hr.current_session(self.env(CLAUDE_CODE_SESSION_ID="outer"),
                                   chain=[["node", "/x/bin/codex", "exec"], ["claude"]])
        self.assertIsNone(found["runner"])
        self.assertEqual(found["ambiguous"], ["claude-code"])

    def test_a_directory_named_claude_does_not_make_a_program_claude(self):
        self.assertIsNone(hr._agent_cli(["python3", "/tmp/claude-0/run.py"]))
        self.assertEqual(hr._agent_cli(["node", "/n/@anthropic-ai/claude-code/cli.js"]), "claude-code")

    def test_extract_current_reads_the_running_session(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        write_jsonl(self.home / ".claude/projects/-p" / f"{sid}.jsonl", [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "please start the notes"}},
            {"type": "assistant", "timestamp": "2026-10-15T09:00:00Z",
             "message": {"model": "claude-opus-5-5", "content": []}},
        ])
        work = self.home / "proj"
        (work / "workspace").mkdir(parents=True)
        (work / ".gitignore").write_text("workspace/record-staging/\n", encoding="utf-8")
        import subprocess
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        saved = os.getcwd()
        os.chdir(work)
        try:
            with mock.patch.object(pathlib.Path, "home", return_value=self.home), \
                    mock.patch.object(hr, "nearest_agent_cli", return_value="claude-code"), \
                    mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}), \
                    contextlib.redirect_stdout(io.StringIO()):
                hr.main(["extract", "current", "--out", "workspace/record-staging/r1",
                         "--record-id", "r1"])
            evidence = json.loads((work / "workspace/record-staging/r1/evidence.json").read_text())
        finally:
            os.chdir(saved)
        self.assertEqual([t["text"] for t in evidence["turns"]], ["please start the notes"])


class ProcessTree(unittest.TestCase):
    """`_process_chain`, the reader the tests above replace with written chains,
    run on its own two sources: a Linux /proc tree, and `ps` where there is none.

    /proc is a folder of this test's, reached by replacing the module's `pathlib`
    for the call, and `ps` is a replaced `subprocess.run`; the walk is the
    function's own.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="process-tree-")
        self.proc = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, pid: int, argv: list[str], stat: str) -> None:
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (folder / "stat").write_text(stat, encoding="utf-8")

    def proc_only(self):
        """The module's pathlib, with /proc/<pid> answered from this test's folder;
        PurePath stays the real one, which `_agent_cli` reads names with."""
        def path(name):
            self.assertTrue(str(name).startswith("/proc/"), name)
            return self.proc / str(name)[len("/proc/"):]
        return mock.patch.object(hr, "pathlib", types.SimpleNamespace(Path=path, PurePath=pathlib.PurePath))

    @staticmethod
    def ps(table, calls=None, fail=None):
        """A `subprocess.run` that answers `ps -o ppid= -o command= -p <pid>` from a table."""
        def run(argv, **kwargs):
            if calls is not None:
                calls.append(argv)
            pid = int(argv[-1])
            if fail and pid in fail:
                raise fail[pid]
            return subprocess.CompletedProcess(argv, 0, stdout=table.get(pid, "") + "\n", stderr="")
        return mock.patch.object(hr.subprocess, "run", side_effect=run)

    def test_linux_walk_reads_the_sandbox_as_process_1_and_ends_there(self):
        # Inside Codex's sandbox the command runs in its own process namespace,
        # where the sandbox program is process 1 and its parent reads as 0.
        # The parenthesis inside the second name is why stat is split at the last one.
        self.add(300, ["/bin/bash", "-lc", "python3 scripts/trace_record.py session"],
                 "300 (ba) sh) S 1 300 300 0 -1 4194560")
        self.add(1, ["/opt/codex/bin/codex-linux-sandbox", "--sandbox-policy-cwd", "/p"],
                 "1 (codex-linux-sa) S 0 1 1 0 -1 4194560")
        with self.proc_only(), mock.patch.object(hr.subprocess, "run",
                                                 side_effect=AssertionError("ps run beside /proc")):
            chain = hr._process_chain(300)
            with mock.patch.object(hr.os, "getppid", return_value=300):
                nearest = hr.nearest_agent_cli()
        self.assertEqual(chain, [["/bin/bash", "-lc", "python3 scripts/trace_record.py session"],
                                 ["/opt/codex/bin/codex-linux-sandbox", "--sandbox-policy-cwd", "/p"]])
        self.assertEqual(nearest, "codex")

    def test_ps_walk_where_there_is_no_proc(self):
        table = {400: "  300 /bin/zsh -c python3 scripts/trace_record.py session",
                 300: "  200 node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js -p",
                 200: "    1 /usr/sbin/sshd -D",
                 1: "    0 /sbin/launchd"}
        calls = []
        with self.proc_only(), self.ps(table, calls):
            chain = hr._process_chain(400)
            with mock.patch.object(hr.os, "getppid", return_value=400):
                nearest = hr.nearest_agent_cli()
        self.assertEqual(calls[0], ["ps", "-o", "ppid=", "-o", "command=", "-p", "400"])
        self.assertEqual([argv[-1] for argv in calls[:4]], ["400", "300", "200", "1"])
        self.assertEqual(chain, [
            ["/bin/zsh", "-c", "python3", "scripts/trace_record.py", "session"],
            ["node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-p"],
            ["/usr/sbin/sshd", "-D"], ["/sbin/launchd"]])
        self.assertEqual(nearest, "claude-code")

    def test_a_parent_that_cannot_be_read_ends_the_walk_with_what_was_read(self):
        # On Linux: the parent's folder is there, its command line is not.
        self.add(300, ["/bin/bash", "-lc", "x"], "300 (bash) S 250 300 300 0 -1 0")
        (self.proc / "250").mkdir()
        with self.proc_only(), mock.patch.object(hr.subprocess, "run",
                                                 side_effect=AssertionError("ps run beside /proc")):
            self.assertEqual(hr._process_chain(300), [["/bin/bash", "-lc", "x"]])
        # Through ps, for pids /proc does not hold: the parent has exited, ps
        # timed out, or its answer is not a pid.
        first = {600: "  500 /bin/zsh -c x"}
        for name, table, fail in (
                ("exited", first, None),
                ("timed out", first, {500: subprocess.TimeoutExpired(["ps"], 5)}),
                ("unreadable", {**first, 500: "pid? /usr/bin/x"}, None)):
            with self.subTest(name), self.proc_only(), self.ps(table, fail=fail):
                self.assertEqual(hr._process_chain(600), [["/bin/zsh", "-c", "x"]])


if __name__ == "__main__":
    unittest.main()
