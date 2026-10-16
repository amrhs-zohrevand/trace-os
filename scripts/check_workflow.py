#!/usr/bin/env python3
"""Run this repository's own engineering checks once and write down what they found.

The checks already existed and were run by hand, one at a time, which is how a
result gets read as better than it was.
This runs them together, records the command, exit code and outcome of each, and
writes a compact JSON report at a path the caller names.

    python3 scripts/check_workflow.py --out /tmp/workflow-check.json
    python3 scripts/check_workflow.py --out report.json --shared

What it runs, and what each result is allowed to mean:

  portable test suite      python3 -X utf8 -I -S -m unittest discover -s tests -v;
                           where .trace-os/install.json exists, only the kit's test
                           modules it lists, naming the project's other test files
                           under tests/ as not run and a listed module that is
                           absent (informational)
  installed skill parity   scripts/check_skills.py when .trace-os/install.json exists
  skill handoff            whether each skill under .skills/ states its handoff to
                           the record (scripts/check_skill_handoff.py)
  trace records            every directory under ai/records/, discovered
  registries               acceptance, intake and transition rows, validated on read
  review status            counts of accepted, returned, model-reviewed, unreviewed
  defect ledger            states as stored, plus defect_ledger.py check
  governing actions        per commit and principle: recorded, missing,
                           not applicable or unknown, with the decision each
                           transition row records and the data files,
                           shared-inventory files and snapshots no intake row
                           names (scripts/trace_report.py; informational)
  build configuration      scripts/check_build_config.py

Four kinds of result are kept apart, because collapsing them is how a report
starts flattering the thing it measures:

  mechanical failure   a check ran and said no. This fails the command.
  execution error      a check could not run, or its output could not be read.
                       This also fails the command, because an unreadable result
                       is not a passing one.
  skipped              a check that had no input, with the reason kept.
  informational        review, ledger and skill-handoff states, and a project's
                       own test files the suite did not run, reported and never
                       failed.

Open defects and work no person has accepted are normal, publishable states in
this project, so they are informational and never fail the command.
A recurrence reported by the ledger is a warning carrying that command's own
exit code: this script did not re-diagnose the manuscript, it read what the
ledger already stored.
A ledger the checker could not read is recorded as an execution error rather than as that permitted warning.
A check that raises is recorded as an execution error too, and the checks that do not depend on it still run, because the run that most needs a written record is the one where something broke.

What this command is not.
It performs no intake, no acceptance, no correction, no sync, and no model
review, and it writes nothing into the tracked records.
Checking the build configuration is not building the PDF.
A clean report says the mechanical checks passed, and says nothing about whether
the work is any good.

stdlib only; no install step.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import trace_record as hr  # noqa: E402
import trace_report  # noqa: E402

SCHEMA_VERSION = 2
HOME = str(pathlib.Path.home())
# The installation state: .trace-os/install.json, or the same file in an earlier folder
# (.traceware/ in 0.1.0-rc19, .trace-kit/ before) that no update has moved yet, newest first.
INSTALL_STATE = ".trace-os/install.json"
STATE_DIRS = (".trace-os", ".traceware", ".trace-kit")


def install_state():
    """This project's install.json, read in .trace-os/ first and then in each earlier folder; None without one."""
    for folder in STATE_DIRS:
        path = ROOT / folder / "install.json"
        if path.is_file():
            return path
    return None


# The variables' names, newest first: TRACE_OS_*, then TRACEWARE_* (0.1.0-rc19), then TRACE_KIT_* (before).
SHARED_ROOT_NAMES = ("TRACE_OS_SHARED_ROOT", "TRACEWARE_SHARED_ROOT", "TRACE_KIT_SHARED_ROOT")
REQUIRE_SHARED_NAMES = ("TRACE_OS_REQUIRE_SHARED", "TRACEWARE_REQUIRE_SHARED", "TRACE_KIT_REQUIRE_SHARED")


def shared_root():
    """(variable, value) for the shared archive root: TRACE_OS_SHARED_ROOT, else an earlier
    name a setup used (TRACEWARE_SHARED_ROOT in 0.1.0-rc19, TRACE_KIT_SHARED_ROOT before);
    an empty value when none is set."""
    for name in SHARED_ROOT_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    return SHARED_ROOT_NAMES[0], ""

# Dirty files the checks actually read, for the fingerprint block. A report over
# a dirty tree names what it read, so a later reader can tell which bytes were
# checked without trusting that the tree still looks the same.
#
# This list covers the checks' own code. What the suite reads beyond it is
# read from literal path expressions by `suite_referenced_paths`.
# This is static coverage of the current suite, not runtime dependency discovery.
# Computed paths need explicit coverage when introduced. The list was
# derived by running the suite once with `open` traced and `sys.modules` read
# afterwards: every repository file the suite opened or imported was either under
# one of these prefixes, under `sources/coding/recode-part1/`, or the workspace
# module `workspace/sandbox/appendix-tasks/scripts/project_paths.py`, and the
# last two are exactly what the test sources name.
READ_PREFIXES = ("scripts/", "tests/", "ai/", ".skills/", ".vscode/")
READ_EXACT = ("paper/orgsci.bst",)
BUILD_PREFIXES = ("paper/", "presentations/", "workspace/sandbox/")
BUILD_SUFFIXES = (".tex", ".latexmkrc")

TOP_LEVEL = ("sources/", "workspace/", "scripts/", "tests/", "ai/", "paper/",
             "presentations/")

# unittest's own summary, which is the only trustworthy statement of what ran.
RAN_RE = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s", re.M)
RESULT_RE = re.compile(r"^(OK|FAILED|NO TESTS RAN)(?:\s*\((.*)\))?\s*$", re.M)
COUNT_RE = re.compile(r"([A-Za-z][A-Za-z ]*?)\s*=\s*(\d+)")
SKIP_RE = re.compile(r"\.\.\.\s+skipped\s+(.+?)\s*$", re.M)


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrub(text: str) -> str:
    """Take this machine's own paths out of anything the report will carry.

    The interpreter is written back as `python3` so that two reports of the same
    run compare, rather than differing by where Python happens to be installed.
    """
    return (str(text).replace(str(ROOT), ".").replace(sys.executable, "python3")
            .replace(HOME, "~"))


def rel(path: pathlib.Path) -> str:
    """A repository-relative path where possible, so reports compare across machines."""
    try:
        return str(pathlib.Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return scrub(str(path))


@contextlib.contextmanager
def working_directory(path: pathlib.Path):
    """Run inside the repository root, which several tracked paths assume."""
    previous = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(previous)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str, cwd=None) -> str:
    """Git's output with the surrounding whitespace removed.

    Only for output that carries no significant whitespace, such as a sha.
    Porcelain status output is read by `git_raw`, because stripping it removes the leading column of the first line and with it the first character of that path.
    """
    out = subprocess.run(["git", *args], cwd=str(cwd or ROOT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout.strip()


def git_raw(*args: str, cwd=None) -> str:
    """Git's output byte for byte, for output whose whitespace means something."""
    out = subprocess.run(["git", *args], cwd=str(cwd or ROOT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout


def run_command(command, cwd=None, env=None, log_dir=None, log_name=None) -> dict:
    """Run one check and return what it did, with the detail written to a log file.

    The report keeps the command, the exit code and the duration; the output goes
    to the run directory, so the JSON stays small enough to read.
    """
    started = time.monotonic()
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    # LESSON (AH-192): strict decoding raised on one stray byte before the log
    # was written, so a passing run became an execution error with no output
    # kept. A byte that is not UTF-8 is shown as U+FFFD and the run is scored.
    proc = subprocess.run([str(c) for c in command], cwd=str(cwd or ROOT), env=child_env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    duration = round(time.monotonic() - started, 3)
    log_path = None
    if log_dir is not None and log_name:
        log_dir = pathlib.Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / log_name
        log_path.write_text(
            f"$ {' '.join(str(c) for c in command)}\n"
            f"cwd: {rel(pathlib.Path(cwd or ROOT))}\n"
            f"exit: {proc.returncode}  duration: {duration}s\n\n"
            f"[stdout]\n{proc.stdout}\n[stderr]\n{proc.stderr}\n", encoding="utf-8")
    return {
        "command": scrub(" ".join(str(c) for c in command)),
        "exit_code": proc.returncode,
        "duration_s": duration,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "log": rel(log_path) if log_path else None,
    }


def parse_unittest(text: str) -> dict:
    """Read unittest's verbose output, or refuse to guess.

    An exit code alone cannot say how many tests ran or which were skipped, and a
    report that silently counted zero tests as a pass would be worse than no
    report. Anything unreadable raises, and the caller records an execution error.

    The summary is the final `Ran` line and the result line after it.
    A test can print either line, or a failure message can quote them, and
    those always come before the runner's own summary.
    """
    rans = list(RAN_RE.finditer(text))
    ran = rans[-1] if rans else None
    result = RESULT_RE.search(text, ran.end()) if ran else None
    if not ran or not result:
        raise ValueError("unittest output carries no 'Ran N tests' line and result "
                         "line; the run cannot be scored")
    counts = {"failures": 0, "errors": 0, "skipped": 0,
              "expected failures": 0, "unexpected successes": 0}
    for name, value in COUNT_RE.findall(result.group(2) or ""):
        counts[name.strip()] = int(value)
    reasons = []
    for raw in SKIP_RE.findall(text):
        try:
            reasons.append(str(ast.literal_eval(raw)))
        except (ValueError, SyntaxError):
            reasons.append(raw)
    return {
        "run": int(ran.group(1)),
        "status": "ok" if result.group(1) == "OK" else "failed",
        "failures": counts["failures"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
        "expected_failures": counts["expected failures"],
        "unexpected_successes": counts["unexpected successes"],
        "skip_reasons": [scrub(r) for r in reasons],
    }


def check_tests(tests_dir: pathlib.Path, shared: bool, log_dir) -> dict:
    """The portable suite, run the way a clean checkout runs it.

    `--shared` is the explicit archive mode: it requires the declared root, tells
    the suite that a missing archive input is a failure rather than a skip, and
    fails here if the variable naming the archive is absent.
    """
    check = {"name": "portable test suite", "kind": "mechanical",
             "note": "counts and skip reasons are read from the suite's own output"}
    env = dict(os.environ)
    if shared:
        name, root = shared_root()
        if not root:
            check.update(status="fail", command=None, exit_code=None, duration_s=0.0,
                         outcome={"reason": "--shared was requested but TRACE_OS_SHARED_ROOT "
                                            "(TRACEWARE_SHARED_ROOT in 0.1.0-rc19, TRACE_KIT_SHARED_ROOT "
                                            "before) is not set"})
            return check
        if not pathlib.Path(root).exists():
            check.update(status="fail", command=None, exit_code=None, duration_s=0.0,
                         outcome={"reason": f"{name} does not exist",
                                  "shared_root": scrub(root)})
            return check
        # The suite is told under every name, so a test that reads any one of them sees the archive mode.
        for variable in SHARED_ROOT_NAMES:
            env[variable] = root
        for variable in REQUIRE_SHARED_NAMES:
            env[variable] = "1"
        check["shared_root"] = scrub(root)

    where = rel(tests_dir) if str(tests_dir).startswith(str(ROOT)) else str(tests_dir)
    # LESSON (AH-198): in an installed project, discovery also ran the project's
    # own test files, and one that could not import failed the kit's check.
    selection = kit_test_selection(tests_dir)
    if selection is not None and not selection["modules"]:
        # Discovery here would run the project's own tests again, so nothing runs.
        selection.pop("modules")
        state = selection.get("installation_state", INSTALL_STATE)
        reason = (f"every kit test module listed in {state} is absent"
                  if selection.get("kit_tests_absent")
                  else f"{state} lists no kit test module")
        check.update(status="error", command=None, exit_code=None, duration_s=0.0, log=None,
                     outcome=dict(selection, reason=reason + "; nothing was run"))
        return check
    if selection is None:
        command = [sys.executable, "-X", "utf8", "-I", "-S", "-m", "unittest", "discover",
                   "-s", where, "-v"]
    else:
        command = [sys.executable, "-X", "utf8", "-I", "-S", "-c", KIT_SUITE_RUNNER, where,
                   *[pathlib.PurePosixPath(name).stem for name in selection.pop("modules")]]
    run = run_command(command, env=env, log_dir=log_dir, log_name="tests.log")
    check.update(command=run["command"], exit_code=run["exit_code"],
                 duration_s=run["duration_s"], log=run["log"])
    scored = score_suite(check, run, where)
    if selection:
        scored["outcome"].update(selection)
    return scored


# The kit's own test modules, loaded by name the way discovery loads them: the
# tests directory first on the path, and nothing else from the environment.
# A module that fails to load for any reason, a SyntaxError from a merge marker
# as much as an ImportError, is that module's error, and a module that skips
# itself is a skip, as discovery reports them, rather than the end of the run
# and of every count (AH-198).
KIT_SUITE_RUNNER = """\
import os, sys, traceback, unittest
sys.path.insert(0, os.path.abspath(sys.argv[1]))


class Loader(unittest.TestLoader):
    def loadTestsFromName(self, name, module=None):
        try:
            return super().loadTestsFromName(name, module)
        except unittest.SkipTest as exc:
            error = unittest.SkipTest(str(exc))
        except Exception:
            error = ImportError("Failed to import test module: %s\\n%s" % (name, traceback.format_exc()))

        def test(self):
            raise error
        return self.suiteClass([type("_FailedTest", (unittest.TestCase,),
                                     {"__module__": "unittest.loader", name: test})(name)])


unittest.main(module=None, argv=["unittest", "-v", *sys.argv[2:]], testLoader=Loader())
"""
KIT_TEST_RE = re.compile(r"tests/test[^/]*\.py")
# unittest's own test for a file it would import as a module.
MODULE_NAME_RE = re.compile(r"[_a-z]\w*\.py$", re.I)


def discoverable(directory: pathlib.Path) -> list:
    """The test files `unittest discover` would load: `test*.py` files that are
    module names, in the directory and in the packages below it. A folder with
    no `__init__.py` is never entered, so a fixture there is not a test file."""
    found = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and fnmatch.fnmatch(path.name, "test*.py") and MODULE_NAME_RE.match(path.name):
            found.append(path)
        elif path.is_dir() and (path / "__init__.py").is_file():
            found += discoverable(path)
    return found


def kit_test_selection(tests_dir: pathlib.Path) -> dict | None:
    """In an installed project, the kit's test modules and the test files left alone.

    A project keeps its own tests beside the kit's, run however the project runs
    them; some import packages this isolated run cannot load, and a failure there
    is not the kit's. So where `.trace-os/install.json` exists (or the same file in
    an earlier folder, `.traceware/` or `.trace-kit/`), the suite is the test modules it lists,
    and the other test files are named, never failed.
    None means discovery, as in this repository or for another --tests-dir.
    """
    state = install_state()
    if state is None or pathlib.Path(tests_dir).resolve() != (ROOT / "tests").resolve():
        return None
    name = rel(state)
    files = json.loads(state.read_text(encoding="utf-8")).get("files")
    if not isinstance(files, dict):
        raise ValueError(f"{name} carries no files map")
    listed = sorted(name for name in files if KIT_TEST_RE.fullmatch(name))
    # LESSON (AH-198): with every listed module absent, and then with none
    # listed at all, this fell back to discovery, which runs the project's own
    # tests; in both cases the caller runs nothing.
    present = [name for name in listed if (ROOT / name).is_file()]
    others = sorted(path.relative_to(ROOT).as_posix()
                    for path in discoverable(ROOT / "tests")
                    if path.relative_to(ROOT).as_posix() not in listed)
    selection = {"modules": present, "installation_state": name,
                 "selected": f"the kit's test modules listed in {name}"}
    if others:
        selection["not_run"] = {
            "count": len(others), "files": others,
            "reason": f"the project's own test files, not listed in {name}; "
                      "the project runs them itself"}
    absent = [name for name in listed if name not in present]
    if absent:
        selection["kit_tests_absent"] = absent
    return selection


def score_suite(check: dict, run: dict, where: str) -> dict:
    """Score a suite run from unittest's own summary, or record why it cannot be."""
    try:
        # LESSON (AH-180): stdout came first in the text searched, so a test
        # that printed `FAILED` was read as the run's result. unittest writes
        # its verbose lines and its summary to stderr, so that is what is read.
        parsed = parse_unittest(run["stderr"])
    except ValueError as exc:
        check.update(status="error",
                     outcome={"reason": str(exc),
                              "tail": scrub((run["stdout"] + run["stderr"])[-400:])})
        return check
    if (run["exit_code"] == 0) != (parsed["status"] == "ok"):
        check.update(status="error",
                     outcome=dict(parsed, reason="exit code and reported result "
                                                 "disagree; the run cannot be scored"))
        return check
    if parsed["run"] == 0:
        # LESSON: an empty suite reports OK. Reading that as a pass would let a
        # mistyped --tests-dir, or a suite that failed to import, be recorded as
        # a clean run of nothing.
        check.update(status="error",
                     outcome=dict(parsed, reason="the suite discovered zero tests; "
                                                 "an empty run is a configuration "
                                                 "error, not a pass",
                                  tests_dir=where))
        return check
    check.update(status="pass" if parsed["status"] == "ok" else "fail", outcome=parsed)
    return check


def check_skill_parity(log_dir=None) -> dict:
    """Run installed-copy parity when this is an initialized starter project."""
    check = {"name": "installed skill parity", "kind": "mechanical",
             "note": "byte parity only; discovery and global overrides remain outside this check"}
    state = install_state()
    if state is None:
        check.update(status="pass", command=None, exit_code=None, duration_s=0.0,
                     outcome={"applicable": False, "checked": 0, "mismatched": [],
                              "skip_reasons": ["no .trace-os/install.json; installed-skill parity is not applicable"]})
        return check
    script = ROOT / "scripts" / "check_skills.py"
    if not script.is_file():
        raise FileNotFoundError("install state exists but scripts/check_skills.py is absent")
    run = run_command([sys.executable, str(script)], log_dir=log_dir,
                      log_name="installed-skill-parity.log")
    try:
        outcome = json.loads(run["stdout"])
    except json.JSONDecodeError as exc:
        raise ValueError("check_skills.py returned unreadable JSON") from exc
    if not isinstance(outcome, dict) or type(outcome.get("checked")) is not int \
            or not isinstance(outcome.get("mismatched"), list):
        raise ValueError("check_skills.py returned an invalid result")
    outcome["applicable"] = True
    if run["exit_code"] == 0:
        status = "pass"
    elif run["exit_code"] == 1:
        status = "fail"
        outcome["reason"] = f"{len(outcome['mismatched'])} installed skill mismatch(es)"
    else:
        status = "error"
        outcome["reason"] = scrub(run["stderr"].strip() or
                                   f"check_skills.py exited {run['exit_code']}")
    check.update(status=status, command=run["command"], exit_code=run["exit_code"],
                 duration_s=run["duration_s"], log=run["log"], outcome=outcome)
    return check


def check_skill_handoff(skills_dir: pathlib.Path, log_dir=None) -> dict:
    """Whether each project skill states its handoff to the record. Never fails.

    A skill without a handoff step runs, produces its artifact, and leaves a
    session in which nothing was checked. Before 0.1.0-rc5 that was found by a
    person reading the two shipped example methods; nothing mechanical reported
    it. This reports it, and reports only it: a handoff not stated is a state,
    and failing the run on it would make a project's own unfinished method an
    error.
    """
    check = {"name": "skill handoff", "kind": "informational",
             "note": "the skill text only; it does not establish that a stated "
                     "handoff was followed, which the check row's "
                     "procedure_version digest establishes per run"}
    script = ROOT / "scripts" / "check_skill_handoff.py"
    if not script.is_file():
        raise FileNotFoundError("scripts/check_skill_handoff.py is absent")
    run = run_command([sys.executable, str(script), "--skills", str(skills_dir)],
                      log_dir=log_dir, log_name="skill-handoff.log")
    try:
        outcome = json.loads(run["stdout"])
    except json.JSONDecodeError as exc:
        raise ValueError("check_skill_handoff.py returned unreadable JSON") from exc
    if not isinstance(outcome, dict) or type(outcome.get("checked")) is not int \
            or not isinstance(outcome.get("states"), list):
        raise ValueError("check_skill_handoff.py returned an invalid result")
    if run["exit_code"] != 0:
        raise ValueError(scrub(run["stderr"].strip() or
                               f"check_skill_handoff.py exited {run['exit_code']}"))
    check.update(status="info", command=run["command"], exit_code=run["exit_code"],
                 duration_s=run["duration_s"], log=run["log"], outcome=outcome)
    return check


def check_records(records_dir: pathlib.Path) -> dict:
    """Every trace record under ai/records/, found rather than assumed.

    There are seven today. A hard-coded seven would keep passing after the eighth
    was added and never checked, so the directories are discovered each run.
    """
    check = {"name": "trace records", "kind": "mechanical",
             "command": "trace_record.evaluate_record over each ai/records/*/",
             "exit_code": None, "duration_s": 0.0,
             "note": "mechanical integrity only: coverage, schema and consent, not "
                     "whether the record is complete or the checks adequate"}
    started = time.monotonic()
    records_dir = pathlib.Path(records_dir)
    if not records_dir.is_dir():
        check.update(status="error", duration_s=0.0,
                     outcome={"reason": f"no such directory: {rel(records_dir)}"})
        return check

    evaluated = []
    for path in sorted(p for p in records_dir.iterdir() if p.is_dir()):
        try:
            result = hr.evaluate_record(path)
            problems = result["problems"]
        except SystemExit as exc:
            # `load_evidence` exits when the primary artefact is missing, which is
            # a broken record rather than a broken checker.
            problems = [str(exc)]
        except Exception as exc:  # noqa: BLE001
            problems = [f"{type(exc).__name__}: {exc}"]
        evaluated.append({"record": rel(path), "ok": not problems,
                          "problems": [scrub(p) for p in problems]})

    bad = [r for r in evaluated if not r["ok"]]
    check.update(status="fail" if bad else "pass",
                 duration_s=round(time.monotonic() - started, 3),
                 outcome={"records_dir": rel(records_dir), "evaluated": len(evaluated),
                          "with_problems": [r["record"] for r in bad],
                          "records": evaluated})
    return check


def check_registries(acceptances: pathlib.Path, intake: pathlib.Path,
                     transitions: pathlib.Path) -> dict:
    """The three appended registries, validated on read.

    Until 2026-09-15 a row was validated only by the command that wrote it, and
    the reporting code then trusted whatever kind or decision it found. A row
    with an undeclared kind or an unknown decision now fails this check, where
    it can be fixed, instead of being classified by assumption.
    """
    started = time.monotonic()
    with working_directory(ROOT):
        result = hr.validate_registries(acceptances=pathlib.Path(acceptances),
                                        intake=pathlib.Path(intake),
                                        transitions=pathlib.Path(transitions))
    outcome = {"rows": result["rows"], "notes": [scrub(n) for n in result["notes"]],
               "problems": [scrub(p) for p in result["problems"]]}
    if result["problems"]:
        outcome["reason"] = f"{len(result['problems'])} malformed registry row(s)"
    return {"name": "registries", "kind": "mechanical",
            "status": "pass" if result["ok"] else "fail",
            "command": "trace_record.validate_registries", "exit_code": None,
            "duration_s": round(time.monotonic() - started, 3), "outcome": outcome,
            "note": "row shape and vocabulary only: a valid row does not establish "
                    "that the actor was independent or the check adequate"}


def check_governing_actions(ledger: pathlib.Path) -> dict:
    """Which of the five actions each trace record has a record of. Never fails.

    Four states per commit and principle, applicability declared rather than
    inferred. Reported, because unperformed work is a normal, publishable
    state; the requirement is that it reads as such.
    """
    started = time.monotonic()
    with working_directory(ROOT):
        report = trace_report.build_report(ledger=pathlib.Path(ledger))
    missing = [c for c in report["commits"] if "missing" in c["states"].values()]
    outcome = {k: report[k] for k in ("baseline", "commits_since_baseline", "applicability",
                                      "totals", "review_basis", "intake", "transitions", "ledger",
                                      "meaning")}
    outcome["commits_with_a_missing_record"] = len(missing)
    outcome["missing_examples"] = [
        {"commit": c["commit"], "subject": scrub(c["subject"]),
         "missing": [p for p in trace_report.PRINCIPLES if c["states"][p] == "missing"]}
        for c in missing[:50]]
    return {"name": "governing actions", "kind": "informational", "status": "info",
            "command": "trace_report.build_report", "exit_code": None,
            "duration_s": round(time.monotonic() - started, 3), "outcome": outcome,
            "note": "recorded means a joined record exists; it does not establish adequacy, "
                    "independence or authorization"}


def check_review_status() -> dict:
    """Who has reviewed what. Reported, never failed.

    Acceptance is a recorded act by an authorized actor independent of
    production, a person or a delegated agent or process, and happens in batches
    after publishing, so a count of unaccepted commits is a state of the project
    rather than a defect in it. This check exists to keep that count visible,
    not to gate on it.
    """
    started = time.monotonic()
    states = {key: 0 for key in hr.STATE_LABELS}
    with working_directory(ROOT):
        index = hr.review_index()
        for sha in hr.reviewable_commits():
            states[hr.commit_state_detail(sha, index)] += 1
    return {
        "name": "review status", "kind": "informational", "status": "info",
        "command": "trace_record.commit_state over reviewable commits",
        "exit_code": None, "duration_s": round(time.monotonic() - started, 3),
        "outcome": {"commits_since_epoch": sum(states.values()),
                    "accepted_by_a_person": states["accepted-by-person"],
                    "accepted_by_a_delegated_actor": states["accepted-delegated"],
                    "returned": states["returned"],
                    "refused": states["refused"],
                    "reviewed_by_a_model": states["model-reviewed"],
                    "reviewed_by_an_undeclared_actor": states["undeclared-actor"],
                    "unreviewed": states["unreviewed"]},
        "note": "registry coverage, not a count of every reading performed; work "
                "no person has accepted is a normal state and does not fail this",
    }


def check_shared_folder() -> dict:
    """Whether the project's shared folder is connected on this machine. Reported, never failed.

    The state is of this machine, not of the project: each coauthor connects their own copy.
    The folder's path stays out of the report, which may be shared, since it names a person's
    home folder; the state, the sync-client hint and the next step say what the agent needs.
    """
    started = time.monotonic()
    script = ROOT / "scripts" / "shared_folder.py"
    base = {"name": "shared folder", "kind": "informational", "status": "info",
            "command": "shared_folder.status", "exit_code": None}
    if not script.is_file():
        return {**base, "duration_s": 0.0,
                "outcome": {"applicable": False,
                            "reason": "scripts/shared_folder.py is not in this project"}}
    import importlib.util
    spec = importlib.util.spec_from_file_location("trace_shared_folder", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = module.status(ROOT)
    outcome = {"applicable": True}
    for key in ("state", "shared_path", "ignored_by_git", "target_synced_hint",
                "project_synced_hint", "next"):
        if key in found:
            outcome[key] = scrub(found[key]) if isinstance(found[key], str) else found[key]
    return {**base, "duration_s": round(time.monotonic() - started, 3), "outcome": outcome,
            "note": "this machine's link to the team's shared folder; not connected is a "
                    "state to act on, not a failure"}


def read_ledger_rows(ledger: pathlib.Path) -> tuple:
    """The states the ledger stores, and every row that is not a ledger row.

    A ledger row is a JSON object with a string `id` and a string `state`.
    Read the states here; the ledger command validates the fields it uses and returns its completed result separately.
    """
    counts: dict = {}
    problems = []
    if not ledger.exists():
        return counts, problems
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError as exc:
        return counts, [f"the ledger could not be read: {exc.strerror or exc}"]
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not JSON ({exc.msg})")
            continue
        if not isinstance(row, dict):
            problems.append(f"line {number}: not a JSON object")
            continue
        missing = [f for f in ("id", "state") if not isinstance(row.get(f), str)]
        if missing:
            problems.append(f"line {number}: no string {' or '.join(missing)}")
            continue
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts, problems


def check_ledger(ledger: pathlib.Path, log_dir) -> dict:
    """Defect states as the ledger stores them, plus the ledger's own check.

    `defect_ledger.py check` exits 1 on a recurrence, which means a correction did not hold.
    That is worth surfacing and is not this command's finding: nothing here re-read the manuscript.
    It is carried as a warning with the ledger's own exit code, and it does not change the mechanical result.

    Its JSON result and exit code must agree, with no unexpected stderr.
    Partial output or a subsequent exception is an execution error, even if the checker printed a recurrence first.
    """
    check = {"name": "defect ledger", "kind": "informational",
             "note": "states as stored by the ledger; this command did not "
                     "re-diagnose the manuscript"}
    selected = pathlib.Path(ledger).expanduser()
    with contextlib.suppress(OSError):
        selected = selected.resolve()
    counts, problems = read_ledger_rows(selected)
    outcome = {"ledger": rel(selected), "present": selected.exists(),
               "states": counts, "unreadable_lines": len(problems),
               "check_exit_code": None}
    if problems:
        outcome["row_problems"] = [scrub(p) for p in problems[:10]]
        outcome["reason"] = (f"{len(problems)} row(s) in the selected ledger are not "
                             f"ledger rows: {scrub('; '.join(problems[:3]))}")
        check.update(command=None, exit_code=None, duration_s=0.0, log=None,
                     status="error", outcome=outcome)
        return check

    # The path is passed explicitly. Without it the command resolves its own
    # default ledger from the working directory, so a --ledger given here was
    # counted above while a different file was the one actually checked.
    run = run_command([sys.executable, str(ROOT / "scripts" / "defect_ledger.py"),
                       "check", "--json", "--ledger", str(selected)],
                      cwd=ROOT, log_dir=log_dir, log_name="defect-ledger.log")
    check.update(command=run["command"], exit_code=run["exit_code"],
                 duration_s=run["duration_s"], log=run["log"])
    outcome["check_exit_code"] = run["exit_code"]
    try:
        result = json.loads(run["stdout"])
    except (ValueError, TypeError):
        result = None
    if not isinstance(result, dict):
        result = {}
    recurred = result.get("recurred")
    unresolved = result.get("unresolved", [])

    def rows_ok(rows) -> bool:
        return isinstance(rows, list) and all(
            isinstance(row, dict) and all(isinstance(row.get(field), str)
                                         for field in ("id", "severity", "label"))
            for row in rows)

    valid_rows = rows_ok(recurred) and rows_ok(unresolved)
    completed = valid_rows and not run["stderr"].strip()
    # LESSON (AH-159): a verified row whose faulty default no later run
    # rechecked is reported by the ledger as information; it was dropped here.
    uncovered = result.get("verified_without_later_run")
    if completed and isinstance(uncovered, list) and uncovered:
        outcome["verified_without_later_run"] = [
            scrub(f"NOT COVERED {r.get('id')}: verified, and no later run rechecked its faulty "
                  f"default {r.get('faulty_default')} ({r.get('label')})")
            for r in uncovered if isinstance(r, dict)]
    if completed and run["exit_code"] == 0 and result.get("status") == "ok" and not recurred and not unresolved:
        check.update(status="info", outcome=outcome)
    elif (completed and run["exit_code"] == 1
          and ((result.get("status") == "recurred" and recurred)
               or (result.get("status") == "unresolved" and unresolved and not recurred))):
        # A ledger failure is the ledger's finding, carried here as a warning:
        # this command re-read nothing. Recurred and unresolved are kept apart
        # because they are different claims about a correction.
        if recurred:
            outcome["recurred"] = [scrub(f"RECURRED {r['id']} ({r['severity']}): {r['label']}")
                                   for r in recurred]
        if unresolved:
            outcome["unresolved"] = [scrub(f"UNRESOLVED {r['id']} ({r['severity']}): {r['label']}")
                                     for r in unresolved]
        parts = []
        if recurred:
            parts.append("a recurrence: a correction did not hold")
        if unresolved:
            parts.append("an unresolved defect: a targeted recheck failed")
        outcome["warning"] = "defect_ledger.py check reports " + " and ".join(parts)
        check.update(status="warning", outcome=outcome)
    else:
        detail = run["stderr"].strip() or result.get("reason") or "incomplete or inconsistent JSON result"
        outcome["reason"] = "defect_ledger.py check did not complete: " + scrub(str(detail)[-400:])
        check.update(status="error", outcome=outcome)
    return check


BST_FLAG = "--no-bst"


def checker_compares_bst(script: pathlib.Path) -> bool:
    """Whether a build checker has the bibliography-style comparison at all.

    The comparison and its `--no-bst` switch come together: this repository's
    checker has both, and the checker the starter ships has neither and ignores
    its arguments. A checker that does not know the switch has nothing to skip.
    """
    try:
        return BST_FLAG in script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def check_build(log_dir) -> dict:
    """The build configuration, and an honest account of the part TeX is needed for.

    Only the bibliography-staleness comparison needs an installed TeX. Where there
    is none, that part is reported as unavailable and the rest still runs, because
    calling an unavailable check a failure says something false about the
    configuration. Where the checker has no such comparison, as in an installed
    project, it is reported as not checked, whatever TeX is installed.
    A checker that crashes, or exits non-zero without a report, did not complete,
    which is an execution error rather than a configuration that failed.
    Configuration checking is not a PDF build either way.
    """
    with working_directory(ROOT):
        config = hr.project_config()
    custom_bibliography = (hr.CUSTOM_BIBLIOGRAPHY if config is None
                           else config.get("custom_bibliography", hr.CUSTOM_BIBLIOGRAPHY))
    if type(custom_bibliography) is not bool:
        raise ValueError("custom_bibliography must be true or false")
    kpsewhich = shutil.which("kpsewhich")
    command = [sys.executable, "scripts/check_build_config.py"]
    if not kpsewhich:
        command.append(BST_FLAG)
    run = run_command(command, cwd=ROOT, log_dir=log_dir, log_name="build-config.log")
    # LESSON (AH-190): `checked` was inferred from kpsewhich being installed, and
    # an installed project's checker has no style comparison to run.
    if not custom_bibliography:
        bst = {"status": "not_applicable", "reason": "project declares no custom bibliography style"}
    elif not checker_compares_bst(ROOT / "scripts" / "check_build_config.py"):
        bst = {"status": "not_checked",
               "reason": "scripts/check_build_config.py has no bibliography-style comparison"}
    elif kpsewhich:
        bst = {"status": "checked"}
    else:
        bst = {"status": "unavailable", "reason": "TeX not installed",
               "detail": "the staleness comparison regenerates paper/orgsci.bst "
                         "from the installed abbrvnat.bst, which kpsewhich locates"}
    outcome = {"bst_staleness": bst, "report": scrub(run["stdout"].strip()[-600:])}
    stderr = run["stderr"].strip()
    if stderr:
        outcome["stderr"] = scrub(stderr[-600:])
    # LESSON (AH-191): a traceback was recorded as a configuration that failed,
    # with an empty report and the traceback dropped.
    if run["exit_code"] == 0:
        status = "pass"
    elif "Traceback (most recent call last)" in stderr or not outcome["report"]:
        status = "error"
        last = stderr.splitlines()[-1] if stderr else f"exit code {run['exit_code']} and no report"
        outcome["reason"] = "the build checker did not complete: " + scrub(last)
    else:
        status = "fail"
    return {
        "name": "build configuration", "kind": "mechanical",
        "command": run["command"], "exit_code": run["exit_code"],
        "duration_s": run["duration_s"], "log": run["log"],
        "status": status,
        "outcome": outcome,
        "note": "configuration checking is not a PDF build; it checks that a plain "
                "latexmk would use the tracked engine and output directory",
    }


def parse_porcelain_z(raw: str) -> list:
    """Read `git status --porcelain=v1 -z` without touching the path bytes.

    The NUL-delimited form is used because it is the only one that leaves a path alone: no quoting, no escaping, and no line to split on.
    Each record is two status columns, a space, then the path, so a name that begins or ends with a space survives.
    A rename carries its source path as the next record, which is consumed here and dropped, because the target is the file that now holds the bytes.
    """
    paths = []
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if "R" in status or "C" in status:
            index += 1  # the source path of a rename or copy, not what to read
        if path:
            paths.append(path)
    return sorted(set(paths))


def dirty_state(root=None) -> list:
    """Paths git reports as changed or untracked, with renames read as their target.

    `--untracked-files=all` asks git to name the files inside an untracked directory rather than the directory, so the report lists what it fingerprints and git, not this script, decides what is ignored.
    """
    return parse_porcelain_z(git_raw("status", "--porcelain=v1", "-z",
                                     "--untracked-files=all", cwd=root))


def suite_referenced_paths(tests_dir=None, not_run=()) -> tuple:
    """Existing paths named by literal `/` expressions in the test sources.

    Python's parser handles either quote style, parentheses and comments without changing the referenced path.
    Only complete chains count, so a reference into one workspace directory does not include every workspace file.
    This covers literal paths, not arbitrary computations or transitive imports.
    A test file the suite did not run (`not_run`, paths from the root) names nothing the checks read.
    """
    tests_dir = pathlib.Path(tests_dir) if tests_dir else ROOT / "tests"
    found = set()
    if not tests_dir.is_dir():
        return ()
    for module in sorted(tests_dir.rglob("test_*.py")):
        with contextlib.suppress(ValueError):
            if module.resolve().relative_to(ROOT.resolve()).as_posix() in not_run:
                continue
        try:
            tree = ast.parse(module.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) and parent.left is node:
                continue
            parts = []
            while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if not isinstance(node.right, ast.Constant) or not isinstance(node.right.value, str):
                    break
                parts.append(node.right.value)
                node = node.left
            candidate = "/".join(reversed(parts)).strip("/")
            if not candidate.startswith(TOP_LEVEL):
                continue
            full = ROOT / candidate
            if full.is_dir():
                found.add(candidate.rstrip("/") + "/")
            elif full.is_file():
                found.add(candidate)
    return tuple(sorted(found))


def is_read_input(path: str, referenced=None, not_run=()) -> bool:
    """True where a dirty path is something these checks actually read.

    A project test file the suite did not run is not one (AH-198).
    """
    if path in not_run:
        return False
    if path.startswith(READ_PREFIXES) or path in READ_EXACT:
        return True
    if path.startswith(BUILD_PREFIXES) and path.endswith(BUILD_SUFFIXES):
        return True
    referenced = suite_referenced_paths() if referenced is None else referenced
    return any(path == entry or (entry.endswith("/") and path.startswith(entry))
               for entry in referenced)


def fingerprints(paths, exclude, tests_dir=None, not_run=()) -> dict:
    """sha256 of each dirty file the checks read, so the report names its inputs."""
    out = {}
    excluded = {p.resolve() for p in exclude}
    referenced = suite_referenced_paths(tests_dir, not_run)
    for path in paths:
        if not is_read_input(path, referenced, not_run):
            continue
        full = (ROOT / path)
        if any(full.resolve() == e or e in full.resolve().parents for e in excluded):
            continue
        if full.is_dir():
            for child in sorted(full.rglob("*")):
                if child.is_file() and not any(child == e or e in child.parents
                                               for e in excluded):
                    out[rel(child)] = digest_or_reason(child)
        elif full.is_file():
            out[path] = digest_or_reason(full)
        else:
            out[path] = "absent"
    return out


def digest_or_reason(path: pathlib.Path) -> str:
    """The file's sha256, or why there is none. A missing digest says which."""
    try:
        return sha256_file(path)
    except OSError as exc:
        return f"unreadable: {exc.strerror or type(exc).__name__}"


def summarize(checks) -> dict:
    """Four groups, kept apart. A report that mixes them can only flatter itself."""
    summary = {"mechanical_failures": [], "execution_errors": [], "skipped": [],
               "informational": {"warnings": []}}
    for check in checks:
        name = check["name"]
        outcome = check.get("outcome") or {}
        if check["status"] == "fail":
            summary["mechanical_failures"].append(
                {"check": name, "detail": outcome.get("reason")
                 or ", ".join(outcome.get("with_problems", []))
                 or f"exit code {check.get('exit_code')}"})
        elif check["status"] == "error":
            summary["execution_errors"].append(
                {"check": name, "detail": outcome.get("reason", "unreadable result")})
        elif check["status"] == "warning":
            summary["informational"]["warnings"].append(
                {"check": name, "warning": outcome.get("warning", "reported a warning"),
                 "exit_code": check.get("exit_code")})
        for reason in outcome.get("skip_reasons", []):
            what = {"installed skill parity": "installed-skill parity",
                    "skill handoff": "the skill-handoff report"}.get(
                        name, "a test in the suite")
            summary["skipped"].append({"check": name, "what": what,
                                       "reason": reason})
        bst = outcome.get("bst_staleness", {})
        if bst.get("status") == "unavailable":
            summary["skipped"].append({"check": name, "what": "paper/orgsci.bst staleness",
                                       "reason": bst["reason"]})
        elif bst.get("status") == "not_checked":
            summary["skipped"].append({"check": name, "what": "bibliography-style staleness",
                                       "reason": bst["reason"]})
        if name == "portable test suite" and outcome.get("not_run"):
            summary["informational"]["tests_not_run"] = outcome["not_run"]
        if name == "portable test suite" and outcome.get("kit_tests_absent"):
            summary["informational"]["kit_tests_absent"] = outcome["kit_tests_absent"]
            summary["informational"]["installation_state"] = outcome.get("installation_state", INSTALL_STATE)
        if name == "review status":
            summary["informational"]["review_status"] = outcome
        if name == "defect ledger":
            summary["informational"]["defect_ledger"] = {
                "states": outcome.get("states", {}),
                "check_exit_code": outcome.get("check_exit_code")}
            if outcome.get("verified_without_later_run"):
                summary["informational"]["defect_ledger"]["verified_without_later_run"] = \
                    outcome["verified_without_later_run"]
        if name == "skill handoff":
            summary["informational"]["skill_handoff"] = {
                "checked": outcome.get("checked"),
                "handoff_stated": outcome.get("handoff_stated"),
                "handoff_not_stated": outcome.get("handoff_not_stated")}
        if name == "shared folder" and outcome.get("applicable"):
            summary["informational"]["shared_folder"] = {
                "state": outcome.get("state"), "next": outcome.get("next")}
        if name == "governing actions":
            summary["informational"]["governing_actions"] = {
                "commits_since_baseline": outcome.get("commits_since_baseline"),
                "totals": outcome.get("totals", {}),
                "commits_with_a_missing_record": outcome.get("commits_with_a_missing_record")}
    return summary


def verdict(summary) -> tuple:
    """The exit code and the closing line, from the four groups.

    A run with a skip or a warning in it never gets an unqualified all-clear,
    because "everything passed" and "everything that ran passed" are different
    statements and only one of them is true.
    """
    failures = summary["mechanical_failures"]
    errors = summary["execution_errors"]
    skipped = summary["skipped"]
    warnings = summary["informational"]["warnings"]
    if failures or errors:
        return 1, (f"{len(failures)} mechanical failure(s), {len(errors)} execution "
                   f"error(s). The report was written anyway.")
    if skipped or warnings:
        return 0, (f"mechanical checks passed; {len(skipped)} skipped, "
                   f"{len(warnings)} warning(s)")
    return 0, "mechanical checks passed with no skips or warnings"


def guarded(name: str, kind: str, run) -> dict:
    """Run one check so that its own failure is written down rather than thrown.

    A check that raised used to end the command before the report was written, so the run that most needed a record produced none, and the checks after it never ran.
    The exception becomes this check's execution error, which fails the command, and the independent checks continue.
    """
    try:
        return run()
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "kind": kind, "status": "error", "command": None,
                "exit_code": None, "duration_s": 0.0,
                "outcome": {"reason": scrub(f"{type(exc).__name__}: {exc}"),
                            "traceback_tail": scrub(traceback.format_exc()[-600:])},
                "note": "this check raised; the error is the checker's, and says "
                        "nothing about what it would have found"}


def build_report(out: pathlib.Path, shared: bool, log_dir: pathlib.Path,
                 records_dir: pathlib.Path, ledger: pathlib.Path,
                 tests_dir: pathlib.Path, acceptances: pathlib.Path | None = None,
                 intake: pathlib.Path | None = None,
                 transitions: pathlib.Path | None = None,
                 skills_dir: pathlib.Path | None = None) -> dict:
    acceptances = acceptances or ROOT / "ai" / "records" / "acceptances.jsonl"
    intake = intake or ROOT / "sources" / "intake.jsonl"
    transitions = transitions or ROOT / "sources" / "transitions.jsonl"
    skills_dir = skills_dir or ROOT / ".skills"
    checks = [
        guarded("portable test suite", "mechanical",
                lambda: check_tests(tests_dir, shared, log_dir)),
        guarded("installed skill parity", "mechanical",
                lambda: check_skill_parity(log_dir)),
        guarded("skill handoff", "informational",
                lambda: check_skill_handoff(skills_dir, log_dir)),
        guarded("trace records", "mechanical",
                lambda: check_records(records_dir)),
        guarded("registries", "mechanical",
                lambda: check_registries(acceptances, intake, transitions)),
        guarded("review status", "informational", check_review_status),
        guarded("defect ledger", "informational",
                lambda: check_ledger(ledger, log_dir)),
        guarded("governing actions", "informational",
                lambda: check_governing_actions(ledger)),
        guarded("build configuration", "mechanical", lambda: check_build(log_dir)),
        guarded("shared folder", "informational", check_shared_folder),
    ]
    for check in checks:
        check.pop("stdout", None)
        check.pop("stderr", None)
    paths = dirty_state()
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_utc(),
        "evaluated_commit": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(paths),
        "dirty_paths": paths,
        "shared_mode": shared,
        "log_dir": rel(log_dir),
        "checks": checks,
    }
    if paths:
        suite = next((c.get("outcome") or {} for c in checks if c["name"] == "portable test suite"), {})
        suite = suite.get("not_run") or {}
        report["fingerprint_algorithm"] = "sha256"
        report["input_fingerprints"] = fingerprints(paths, exclude=[out, log_dir], tests_dir=tests_dir,
                                                    not_run=tuple(suite.get("files", ())))
    report["summary"] = summarize(checks)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True,
                        help="where to write the JSON report (named explicitly, so "
                             "no run silently overwrites another)")
    parser.add_argument("--shared", action="store_true",
                        help="require the declared shared archive and fail on a "
                             "missing archive input instead of skipping it")
    parser.add_argument("--log-dir",
                        help="directory for the detailed logs "
                             "(default: .workflow-checks/<timestamp>/, ignored by git)")
    parser.add_argument("--records-dir", default=str(ROOT / "ai" / "records"),
                        help="where trace records live (for tests)")
    parser.add_argument("--ledger", default=str(ROOT / "ai" / "defects.jsonl"),
                        help="the defect ledger to read (for tests)")
    parser.add_argument("--tests-dir", default=str(ROOT / "tests"),
                        help="the suite to run (for tests)")
    parser.add_argument("--acceptances", default=str(ROOT / "ai" / "records" / "acceptances.jsonl"),
                        help="the acceptance registry to validate (for tests)")
    parser.add_argument("--intake", default=str(ROOT / "sources" / "intake.jsonl"),
                        help="the intake registry to validate (for tests)")
    parser.add_argument("--transitions", default=str(ROOT / "sources" / "transitions.jsonl"),
                        help="the transition registry to validate (for tests)")
    parser.add_argument("--skills-dir", default=str(ROOT / ".skills"),
                        help="the skills directory to read handoff states from (for tests)")
    args = parser.parse_args(argv)

    out = pathlib.Path(args.out).expanduser().resolve()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = pathlib.Path(args.log_dir).expanduser().resolve() if args.log_dir \
        else ROOT / ".workflow-checks" / stamp
    log_dir.mkdir(parents=True, exist_ok=True)

    report = build_report(out=out, shared=args.shared, log_dir=log_dir,
                          records_dir=pathlib.Path(args.records_dir).expanduser(),
                          ledger=pathlib.Path(args.ledger).expanduser(),
                          tests_dir=pathlib.Path(args.tests_dir).expanduser(),
                          acceptances=pathlib.Path(args.acceptances).expanduser(),
                          intake=pathlib.Path(args.intake).expanduser(),
                          transitions=pathlib.Path(args.transitions).expanduser(),
                          skills_dir=pathlib.Path(args.skills_dir).expanduser())

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, separators=(",", ":"), sort_keys=False) + "\n", encoding="utf-8")

    for check in report["checks"]:
        print(f"  {check['status']:<11} {check['name']}")
    summary = report["summary"]
    failures = summary["mechanical_failures"]
    errors = summary["execution_errors"]
    skipped = summary["skipped"]
    warnings = summary["informational"]["warnings"]
    for entry in failures:
        print(f"FAIL  {entry['check']}: {entry['detail']}")
    for entry in errors:
        print(f"ERROR {entry['check']}: {entry['detail']}")
    for entry in warnings:
        print(f"WARN  {entry['check']}: {entry['warning']} "
              f"(exit {entry['exit_code']}, informational)")
    for entry in skipped:
        print(f"SKIP  {entry['check']}: {entry['what']}, {entry['reason']}")
    not_run = summary["informational"].get("tests_not_run")
    if not_run:
        print(f"NOTE  portable test suite: {not_run['count']} test file(s) under tests/ "
              f"not run, {not_run['reason']} (informational)")
    for line in (summary["informational"].get("defect_ledger") or {}).get("verified_without_later_run", []):
        print(f"NOTE  defect ledger: {line} (informational)")
    absent = summary["informational"].get("kit_tests_absent")
    if absent:
        print(f"NOTE  portable test suite: {len(absent)} kit test module(s) listed in "
              f"{summary['informational'].get('installation_state', INSTALL_STATE)} are absent: "
              f"{', '.join(absent)} (informational)")
    print(f"\nreport: {rel(out)}   logs: {rel(log_dir)}")

    code, line = verdict(summary)
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
