"""The everyday workflow check must include installed skill-copy parity."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if not (REPO / "scripts" / "check_workflow.py").is_file():
    REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import check_workflow as cw  # noqa: E402


class InstalledSkillParity(unittest.TestCase):
    def test_noninstalled_project_is_explicitly_not_applicable(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(cw, "ROOT", Path(directory)):
            check = cw.check_skill_parity()
        self.assertEqual(check["status"], "pass")
        self.assertFalse(check["outcome"]["applicable"])
        self.assertIn("not applicable", check["outcome"]["skip_reasons"][0])

    def test_mismatch_fails_the_check(self):
        # .trace-os/, and an earlier folder in a project installed before 0.1.0-rc20 that no update has moved.
        for folder in (".trace-os", ".traceware", ".trace-kit"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / folder).mkdir()
                (root / folder / "install.json").write_text("{}")
                (root / "scripts").mkdir()
                (root / "scripts" / "check_skills.py").write_text("# fixture\n")
                run = {"command": "python3 scripts/check_skills.py", "exit_code": 1,
                       "duration_s": 0.01,
                       "stdout": json.dumps({"checked": 6, "mismatched": [".agents/x/SKILL.md"]}),
                       "stderr": "", "log": None}
                with mock.patch.object(cw, "ROOT", root), mock.patch.object(
                        cw, "run_command", return_value=run):
                    check = cw.check_skill_parity()
                self.assertEqual(check["status"], "fail")
                self.assertEqual(check["outcome"]["mismatched"], [".agents/x/SKILL.md"])

    def test_the_kit_test_selection_reads_either_state_folder(self):
        # The suite a project installed before 0.1.0-rc20 runs is still the one its earlier folder lists.
        for folder in (".trace-os", ".traceware", ".trace-kit"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / folder).mkdir()
                (root / folder / "install.json").write_text(json.dumps(
                    {"files": {"tests/test_kit.py": {}}}))
                (root / "tests").mkdir()
                for name in ("test_kit.py", "test_own.py"):
                    (root / "tests" / name).write_text("")
                with mock.patch.object(cw, "ROOT", root):
                    selection = cw.kit_test_selection(root / "tests")
                self.assertEqual(selection["modules"], ["tests/test_kit.py"])
                self.assertEqual(selection["installation_state"], folder + "/install.json")
                self.assertEqual(selection["not_run"]["files"], ["tests/test_own.py"])

    def test_the_shared_root_reads_the_newest_variable_first_and_the_earlier_ones_after(self):
        cases = [({"TRACE_OS_SHARED_ROOT": "/new", "TRACEWARE_SHARED_ROOT": "/mid", "TRACE_KIT_SHARED_ROOT": "/old"},
                  ("TRACE_OS_SHARED_ROOT", "/new")),
                 ({"TRACEWARE_SHARED_ROOT": "/mid", "TRACE_KIT_SHARED_ROOT": "/old"},
                  ("TRACEWARE_SHARED_ROOT", "/mid")),
                 ({"TRACE_KIT_SHARED_ROOT": "/old"}, ("TRACE_KIT_SHARED_ROOT", "/old")),
                 ({}, ("TRACE_OS_SHARED_ROOT", ""))]
        for environment, expected in cases:
            with self.subTest(environment=environment), mock.patch.dict(
                    "os.environ", environment, clear=True):
                self.assertEqual(cw.shared_root(), expected)


if __name__ == "__main__":
    unittest.main()
