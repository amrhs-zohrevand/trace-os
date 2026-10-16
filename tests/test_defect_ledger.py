#!/usr/bin/env python3
"""Regression tests for the defect ledger, and for verification in particular.

The ledger's whole job is to keep four different claims apart: nobody checked, a sweep did not complain, somebody rechecked the original diagnosis and it failed, and somebody rechecked it and recorded how it passed.
A bug here does not crash, it silently upgrades the weakest of those into the strongest, which is the failure this project is about.
So every transition is pinned here, including the ones that must NOT happen.

Every test runs against a temporary ledger.
The real `ai/defects.jsonl` is hashed before the module runs and again after it, and a change is a test failure, because a test that edits the project's own record of its defects would be destroying evidence to prove a point about evidence.

Run:  python3 -m unittest tests.test_defect_ledger -v
stdlib only; no pytest and no network.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import defect_ledger as dl  # noqa: E402

REAL_LEDGER = ROOT / "ai" / "defects.jsonl"
_REAL_STATE: tuple[bool, str | None] = (False, None)


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setUpModule() -> None:
    global _REAL_STATE
    exists = REAL_LEDGER.exists()
    _REAL_STATE = (exists, _digest(REAL_LEDGER) if exists else None)


def tearDownModule() -> None:
    existed, before = _REAL_STATE
    if REAL_LEDGER.exists() != existed:
        raise AssertionError("a test created or deleted the real ledger ai/defects.jsonl")
    if existed and _digest(REAL_LEDGER) != before:
        raise AssertionError("a test wrote to the real ledger ai/defects.jsonl")


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run the real command line in-process and return the exit code with both streams.

    Going through `main` rather than calling a `cmd_` function keeps argparse, the `--ledger` flag and the exit codes inside the test.
    """
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["defect_ledger.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            dl.main()
            code = 0
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                err.write(str(exc.code) + "\n")
                code = 1
    return code, out.getvalue(), err.getvalue()


def row(rid: str = "AH-001", state: str = "fixed", **kw) -> dict:
    """A ledger row in the legacy shape, with no verification fields at all."""
    base = {"id": rid, "label": f"{rid} label", "severity": "high",
            "quote": f"the quoted span for {rid}", "state": state,
            "first_seen": "2026-08-27", "runs": ["deep-20260827"], "note": ""}
    base.update(kw)
    return base


GIT_ENV = {"GIT_AUTHOR_NAME": "Test Researcher", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "Test Researcher", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git_in(root: pathlib.Path, *args: str) -> str:
    return subprocess.check_output(["git", "-c", "commit.gpgsign=false", *args], cwd=root,
                                   env=dict(os.environ, **GIT_ENV), text=True).strip()


def later_run_repo(root: pathlib.Path, record: str = "20260910-section-4-redraft") -> dict:
    """A Git repository in `root` holding a trace record and a commit that revises AGENTS.md.

    Returns the later run that names both, the shape a recheck's `recheck.later_run` takes.
    A later run resolves only against a tree that has them (rc22), so a test that verifies a corrected default builds one.
    """
    git_in(root, "init", "-q")
    (root / "AGENTS.md").write_text("the old rule\n")
    git_in(root, "add", "AGENTS.md")
    git_in(root, "commit", "-qm", "the rule")
    (root / "AGENTS.md").write_text("the revised rule\n")
    git_in(root, "add", "AGENTS.md")
    git_in(root, "commit", "-qm", "revise the rule")
    rec = root / "ai" / "records" / record
    rec.mkdir(parents=True)
    (rec / "evidence.json").write_text(json.dumps({"record_id": record, "turns": []}))
    return {"record": f"ai/records/{record}",
            "instruction_version": f"AGENTS.md at {git_in(root, 'rev-parse', '--short=7', 'HEAD')}"}


def verified_row(rid: str = "AH-001", **kw) -> dict:
    """A verified row in the shape `verify` writes, with the event that verified it on the row."""
    return row(rid, "verified", previous_states=["fixed"], verifications=[{
        "outcome": "pass", "decision": "verified", "state_before": "fixed",
        "state_after": "verified", "reasons": ["the recheck passed"]}], **kw)


def evidence(defect_id: str = "AH-001", **kw) -> dict:
    """A complete, valid, passing evidence document that tests then spoil on purpose."""
    ev = {
        "target": {
            "defect_id": defect_id,
            "original_diagnosis": "The passage claims agreement that the run does not show.",
            "corrected_object": "sources/example/object.txt",
            "object_hash": "0" * 64,
        },
        "response": {
            "corrective_action": "Replaced the agreement claim with the counted votes.",
            "implementer": "Jane Roe",
        },
        "recheck": {
            "criterion": "The corrected object no longer contains the diagnosed claim.",
            "method": "Re-read the corrected object against the original diagnosis.",
            "outcome": "pass",
            "evidence_ref": "diff at the recorded object hash",
            "timestamp": "2026-09-07T14:20:00+02:00",
        },
        "accountability": {
            "checker": "John Doe",
            "checker_kind": "human",
            "recorder": "claude-opus-5",
            "recorder_kind": "model",
        },
        "limits": {
            "adequacy": "adequate",
            "limitations": "Covers the counted votes only.",
            "unresolved_contrary_evidence": "none",
        },
    }
    for group, fields in kw.items():
        ev.setdefault(group, {}).update(fields)
    return ev


class LedgerCase(unittest.TestCase):
    """Base fixture: a temporary ledger, and the environment pointed at it as a second guard."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.ledger = self.dir / "defects.jsonl"
        env = mock.patch.dict(os.environ, {dl.LEDGER_ENV: str(self.ledger)})
        env.start()
        self.addCleanup(env.stop)

    def seed(self, *rows: dict) -> None:
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def rows(self) -> dict[str, dict]:
        return {r["id"]: r for r in dl.load(self.ledger)}

    def write_evidence(self, ev, name: str = "evidence.json") -> str:
        p = self.dir / name
        p.write_text(ev if isinstance(ev, str) else json.dumps(ev, indent=2))
        return str(p)

    def bytes_now(self) -> bytes:
        return self.ledger.read_bytes()

    def verify(self, rid: str, ev, name: str = "evidence.json") -> tuple[int, str, str]:
        return run_cli("verify", rid, "--evidence", self.write_evidence(ev, name),
                       "--ledger", str(self.ledger))

    def run_dir(self, label: str = "a newly raised finding", quote: str = "some span",
                name: str = "deep-20260907") -> str:
        """A minimal run directory in the shape `ingest` reads: defects.json with issues and defects."""
        d = self.dir / name
        d.mkdir()
        (d / "defects.json").write_text(json.dumps({
            "issues": [{"n": 1, "quote": quote}],
            "defects": [{"label": label, "severity": "high", "members": [1]}],
        }))
        return str(d)


class LegacyRowsKeepWorking(LedgerCase):
    """A ledger written before verification existed has to load, print and verify without migration."""

    def test_status_and_check_read_a_legacy_ledger(self):
        self.seed(row("AH-001", "closed"), row("AH-002", "open"))
        code, out, _ = run_cli("status", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("AH-001", out)
        self.assertIn("0 recheck event(s) recorded", out)
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 0)

    def test_a_legacy_row_can_be_verified_without_a_migration_step(self):
        self.seed(row("AH-001", "fixed"))
        code, out, err = self.verify("AH-001", evidence())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")
        self.assertIn("state fixed -> verified", out)

    def test_a_historical_closed_row_is_never_promoted_by_a_later_sweep(self):
        # LESSON: closure by absence and a recorded recheck are different
        # claims. Nothing in this file may turn one into the other on its own.
        self.seed(row("AH-001", "closed"))
        with mock.patch.object(dl, "key", lambda: "test"), \
                mock.patch.object(dl, "match", lambda known, found, tok: {}):
            run_cli("ingest", "--run", self.run_dir(), "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "closed")
        self.assertNotIn("verifications", r)


class MalformedEvidenceChangesNothing(LedgerCase):
    """Every rejection path is checked byte for byte, because a partial write is the worst outcome here."""

    def setUp(self):
        super().setUp()
        self.seed(row("AH-001", "fixed"))
        self.before = self.bytes_now()

    def assertUntouched(self, code, err):
        self.assertEqual(code, 2)
        self.assertEqual(self.bytes_now(), self.before, "the ledger was written despite a rejection")
        self.assertIn("unchanged", err)

    def test_missing_fields_are_all_reported_at_once(self):
        ev = evidence()
        del ev["recheck"]["criterion"]
        del ev["limits"]["unresolved_contrary_evidence"]
        ev["accountability"].pop("recorder")
        code, _, err = self.verify("AH-001", ev)
        self.assertUntouched(code, err)
        for field in ("recheck.criterion", "limits.unresolved_contrary_evidence",
                      "accountability.recorder"):
            self.assertIn(field, err)

    def test_a_whole_missing_group_is_reported(self):
        ev = evidence()
        del ev["limits"]
        code, _, err = self.verify("AH-001", ev)
        self.assertUntouched(code, err)
        self.assertIn("limits: missing", err)

    def test_an_empty_string_is_not_a_recorded_field(self):
        code, _, err = self.verify("AH-001", evidence(response={"implementer": "   "}))
        self.assertUntouched(code, err)
        self.assertIn("response.implementer", err)

    def test_values_outside_the_closed_vocabularies_are_rejected(self):
        code, _, err = self.verify("AH-001", evidence(
            recheck={"outcome": "mostly"}, limits={"adequacy": "fine"},
            accountability={"checker_kind": "committee"}))
        self.assertUntouched(code, err)
        self.assertIn("recheck.outcome", err)
        self.assertIn("limits.adequacy", err)
        self.assertIn("accountability.checker_kind", err)

    def test_evidence_for_a_different_defect_is_rejected(self):
        code, _, err = self.verify("AH-001", evidence("AH-042"))
        self.assertUntouched(code, err)
        self.assertIn("does not match", err)

    def test_broken_json_is_rejected(self):
        code, _, err = self.verify("AH-001", "{not json")
        self.assertUntouched(code, err)
        self.assertIn("not valid JSON", err)

    def test_a_missing_evidence_file_is_rejected(self):
        code, _, err = run_cli("verify", "AH-001", "--evidence",
                               str(self.dir / "nope.json"), "--ledger", str(self.ledger))
        self.assertUntouched(code, err)
        self.assertIn("cannot read", err)

    def test_an_unknown_defect_id_is_rejected(self):
        code, _, err = self.verify("AH-042", evidence("AH-042"))
        self.assertEqual(code, 2)
        self.assertEqual(self.bytes_now(), self.before)
        self.assertIn("no ledger entry", err)

    def test_a_non_boolean_flag_is_rejected(self):
        code, _, err = self.verify("AH-001", evidence(
            accountability={"checker_implemented": "yes"}))
        self.assertUntouched(code, err)
        self.assertIn("checker_implemented", err)


class PassingRecheckVerifies(LedgerCase):
    def test_a_complete_passing_event_sets_verified_and_keeps_the_evidence(self):
        self.seed(row("AH-001", "fixed"))
        code, out, err = self.verify("AH-001", evidence())
        self.assertEqual(code, 0, err)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        self.assertEqual(r["previous_states"], ["fixed"])
        self.assertEqual(len(r["verifications"]), 1)
        event = r["verifications"][0]
        self.assertEqual((event["outcome"], event["decision"]), ("pass", "verified"))
        self.assertEqual((event["state_before"], event["state_after"]), ("fixed", "verified"))
        self.assertEqual(event["evidence"]["recheck"]["criterion"],
                         evidence()["recheck"]["criterion"])
        self.assertIn("not acceptance", out)

    def test_verification_never_claims_acceptance_or_authorization(self):
        # LESSON: a technical recheck is not a person taking responsibility.
        # The command has to say so where a reader will see it.
        self.seed(row("AH-001", "fixed"))
        _, out, _ = self.verify("AH-001", evidence())
        self.assertIn("does not authorize publication or reliance", out)
        self.assertNotIn("accepted", out)

    def test_extra_keys_such_as_a_consultation_record_survive(self):
        self.seed(row("AH-001", "fixed"))
        ev = evidence()
        ev["consultation"] = {"reporter": "the 2026-08-27 panel", "response": "none"}
        self.verify("AH-001", ev)
        stored = self.rows()["AH-001"]["verifications"][0]["evidence"]
        self.assertEqual(stored["consultation"]["reporter"], "the 2026-08-27 panel")


class InadequateEvidenceRecordsWithoutVerifying(LedgerCase):
    """Recorded and verified are different outcomes, and the difference is the whole mechanism."""

    def setUp(self):
        super().setUp()
        self.seed(row("AH-001", "fixed"))

    def assertRecordedNotVerified(self, out, reason):
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "fixed", "an inadequate recheck must not verify")
        self.assertNotIn("previous_states", r)
        self.assertEqual(len(r["verifications"]), 1)
        self.assertEqual(r["verifications"][0]["decision"], "recorded")
        self.assertIn(reason, out)

    def test_unknown_adequacy_records_the_event_and_leaves_the_state_alone(self):
        _, out, _ = self.verify("AH-001", evidence(limits={"adequacy": "unknown"}))
        self.assertRecordedNotVerified(out, "adequacy is stated as unknown")

    def test_inadequate_evidence_records_the_event_and_leaves_the_state_alone(self):
        _, out, _ = self.verify("AH-001", evidence(limits={"adequacy": "inadequate"}))
        self.assertRecordedNotVerified(out, "adequacy is stated as inadequate")

    def test_missing_essential_context_blocks_verification(self):
        _, out, _ = self.verify("AH-001", evidence(
            limits={"essential_context_missing": True}))
        self.assertRecordedNotVerified(out, "essential context")

    def test_unresolved_contrary_evidence_blocks_verification(self):
        _, out, _ = self.verify("AH-001", evidence(limits={
            "unresolved_contrary_evidence": "a reader reports the same failure at line 40"}))
        self.assertRecordedNotVerified(out, "unresolved contrary evidence")

    def test_unknown_contrary_evidence_is_not_none(self):
        # LESSON: not having looked is not the same as having looked and found
        # nothing. Only an explicit "none" clears this condition.
        _, out, _ = self.verify("AH-001", evidence(
            limits={"unresolved_contrary_evidence": "unknown"}))
        self.assertRecordedNotVerified(out, "unresolved contrary evidence")


class FailingRecheck(LedgerCase):
    def test_a_failure_before_verification_moves_the_row_to_unresolved_and_fails_check(self):
        # LESSON: a recheck that ran and failed is a known uncorrected defect.
        # Leaving the row `fixed` kept it in a state the check passed, so the
        # one defect somebody had actually looked at was the one nobody saw.
        self.seed(row("AH-001", "fixed"))
        code, out, _ = self.verify("AH-001", evidence(recheck={"outcome": "fail"}))
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "unresolved")
        self.assertEqual(r["previous_states"], ["fixed"])
        self.assertEqual(r["verifications"][0]["decision"], "unresolved")
        self.assertEqual((r["verifications"][0]["state_before"],
                          r["verifications"][0]["state_after"]), ("fixed", "unresolved"))
        self.assertIn("state fixed -> unresolved", out)
        self.assertIn("until a later recheck passes", out)
        code, out, _ = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "unresolved")

    def test_a_failure_after_verification_recurs_and_keeps_the_earlier_events(self):
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(), "first.json")
        code, out, _ = self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "second.json")
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "recurred")
        self.assertEqual(r["previous_states"], ["fixed", "verified"])
        self.assertEqual([e["decision"] for e in r["verifications"]], ["verified", "recurred"])
        self.assertEqual(r["verifications"][0]["evidence"]["recheck"]["outcome"], "pass")
        self.assertIn("recurred", out)
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 1)


class SelfCheckIsLabelled(LedgerCase):
    """A self-check is allowed and is not independent review, so it has to be visible."""

    def setUp(self):
        super().setUp()
        self.seed(row("AH-001", "fixed"))

    def test_a_checker_who_implemented_the_fix_is_flagged_from_the_names(self):
        _, out, _ = self.verify("AH-001", evidence(accountability={"checker": "Jane Roe"}))
        event = self.rows()["AH-001"]["verifications"][0]
        self.assertTrue(event["self_check"])
        self.assertIn("derived", event["self_check_basis"])
        self.assertIn("self-check: YES", out)

    def test_an_independent_checker_is_not_flagged(self):
        _, out, _ = self.verify("AH-001", evidence())
        self.assertFalse(self.rows()["AH-001"]["verifications"][0]["self_check"])
        self.assertIn("self-check: no", out)

    def test_a_declared_self_check_is_honoured_when_the_names_differ(self):
        self.verify("AH-001", evidence(accountability={"checker_implemented": True}))
        event = self.rows()["AH-001"]["verifications"][0]
        self.assertTrue(event["self_check"])
        self.assertIn("declared", event["self_check_basis"])

    def test_matching_names_override_a_declared_false(self):
        # LESSON: the cautious reading wins. Declaring independence while
        # holding both roles does not make the check independent.
        self.verify("AH-001", evidence(accountability={
            "checker": "jane roe", "checker_implemented": False}))
        event = self.rows()["AH-001"]["verifications"][0]
        self.assertTrue(event["self_check"])
        self.assertIn("overriding a declared false", event["self_check_basis"])

    def test_a_model_check_records_the_model_kind_and_still_verifies(self):
        self.verify("AH-001", evidence(accountability={
            "checker": "claude-opus-5", "checker_kind": "model"}))
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        self.assertEqual(r["verifications"][0]["evidence"]["accountability"]["checker_kind"], "model")

    def test_status_marks_a_self_checked_row(self):
        self.verify("AH-001", evidence(accountability={"checker": "Jane Roe"}))
        _, out, _ = run_cli("status", "--ledger", str(self.ledger))
        self.assertIn("1*", out)
        self.assertIn("1 of them self-checks", out)


class SweepsAndVerification(LedgerCase):
    """An unrelated later sweep may neither manufacture a verification nor quietly remove one."""

    def ingest(self, matches, run="deep-20260907"):
        with mock.patch.object(dl, "key", lambda: "test"), \
                mock.patch.object(dl, "match", lambda known, found, tok: matches):
            return run_cli("ingest", "--run", self.run_dir(name=run),
                           "--ledger", str(self.ledger))

    def test_a_matched_finding_after_verification_recurs_and_keeps_the_events(self):
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence())
        code, out, _ = self.ingest({0: "AH-001"})
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "recurred")
        self.assertEqual(len(r["verifications"]), 1)
        self.assertEqual(r["verifications"][0]["decision"], "verified")
        self.assertEqual(r["previous_states"], ["fixed", "verified"])
        self.assertIn("RECURRED", out)
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 1)

    def test_the_absence_sweep_closes_fixed_rows_and_leaves_verified_alone(self):
        # LESSON: closure by absence is the weak legacy observation. It must
        # stay out of the way of a row that carries a real recheck.
        self.seed(row("AH-001", "fixed"), row("AH-002", "fixed"))
        self.verify("AH-002", evidence("AH-002"))
        code, out, _ = self.ingest({})
        self.assertEqual(code, 0)
        rows = self.rows()
        self.assertEqual(rows["AH-001"]["state"], "closed")
        self.assertEqual(rows["AH-002"]["state"], "verified")
        self.assertEqual(len(rows["AH-002"]["verifications"]), 1)
        self.assertIn("closed  : AH-001", out)

    def test_a_sweep_never_creates_a_verification(self):
        self.seed(row("AH-001", "fixed"))
        self.ingest({})
        self.assertNotIn("verifications", self.rows()["AH-001"])

    def test_a_new_finding_is_added_as_open_with_no_events(self):
        self.seed(row("AH-001", "closed"))
        self.ingest({})
        r = self.rows()["AH-002"]
        self.assertEqual(r["state"], "open")
        self.assertNotIn("verifications", r)


class MarkCannotForgeVerification(LedgerCase):
    def test_mark_state_verified_is_refused_and_points_at_verify(self):
        self.seed(row("AH-001", "fixed"))
        before = self.bytes_now()
        code, _, err = run_cli("mark", "--id", "AH-001", "--state", "verified",
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 2)
        self.assertEqual(self.bytes_now(), before)
        self.assertIn("needs evidence", err)
        self.assertIn("verify <ID> --evidence", err)

    def test_mark_fixed_still_works_and_records_the_previous_state(self):
        self.seed(row("AH-001", "open"))
        code, out, _ = run_cli("mark", "--id", "AH-001", "--fixed", "--note", "done",
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["note"], r["previous_states"]), ("fixed", "done", ["open"]))
        self.assertIn("marked AH-001", out)

    def test_mark_sets_affected_and_faulty_default(self):
        self.seed(row("AH-001", "open"))
        code, _, _ = run_cli("mark", "--id", "AH-001", "--state", "open",
                             "--affected", "ai/records/r1", "ai/records/r2",
                             "--faulty-default", "AGENTS.md", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["affected"], r["faulty_default"]),
                         ("open", ["ai/records/r1", "ai/records/r2"], "AGENTS.md"))
        self.assertNotIn("previous_states", r)
        run_cli("mark", "--id", "AH-001", "--fixed", "--affected", "ai/records/r3",
                "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["affected"], r["faulty_default"], r["previous_states"]),
                         ("fixed", ["ai/records/r3"], "AGENTS.md", ["open"]))

    def test_mark_without_the_new_flags_leaves_those_fields_alone(self):
        self.seed(row("AH-001", "open", affected=["ai/records/r1"], faulty_default="AGENTS.md"))
        run_cli("mark", "--id", "AH-001", "--fixed", "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["affected"], r["faulty_default"]),
                         ("fixed", ["ai/records/r1"], "AGENTS.md"))

    def test_a_note_alone_keeps_the_rows_state(self):
        # LESSON: `--state` defaulted to open, so a note added to a fixed,
        # closed or verified row reopened it, and a verified row lost its
        # verification without `check` noticing (AH-120, 2026-09-29).
        for state in ("open", "fixed", "closed"):
            with self.subTest(state=state):
                self.seed(row("AH-001", state))
                code, out, _ = run_cli("mark", "--id", "AH-001", "--note", "answered, not fixed",
                                       "--ledger", str(self.ledger))
                self.assertEqual(code, 0)
                r = self.rows()["AH-001"]
                self.assertEqual((r["state"], r["note"]), (state, "answered, not fixed"))
                self.assertNotIn("previous_states", r)
                self.assertIn(f"marked AH-001 as {state}", out)
        self.seed(verified_row())
        run_cli("mark", "--id", "AH-001", "--note", "a later remark", "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        self.assertNotIn("repair_claims", r)

    def test_mark_refuses_a_state_outside_the_vocabulary(self):
        # LESSON: `check` now rejects an unknown state as a malformed ledger, so
        # `mark` must not be the command that writes one.
        self.seed(row("AH-001", "open"))
        before = self.bytes_now()
        code, _, err = run_cli("mark", "--id", "AH-001", "--state", "resolved",
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 2)
        self.assertEqual(self.bytes_now(), before)
        self.assertIn("resolved", err)


class CheckStaysReadOnly(LedgerCase):
    def test_json_check_distinguishes_complete_findings_from_malformed_rows(self):
        # The closed row is a sweep's closure: a closed row no sweep closed is
        # also reported, as information (CLOSED WITHOUT RECHECK, rc22).
        cases = [
            ([row("AH-001", "closed", runs=["deep-20260827", "deep-20260901:absent"])], 0, "ok"),
            ([row("AH-001", "recurred")], 1, "recurred"),
            ([row("AH-001", "unresolved")], 1, "unresolved"),
            ([row("AH-001", "unresolved"), row("AH-002", "recurred")], 1, "recurred"),
            ([row("AH-001", "recurred"), {"id": "AH-002", "state": "recurred"}], 2, "error"),
            ([row("AH-001", "unresolved"), {"id": "AH-002", "state": "unresolved"}], 2, "error"),
        ]
        for rows, expected_code, status in cases:
            with self.subTest(status=status, rows=[r["state"] for r in rows]):
                self.seed(*rows)
                before = self.bytes_now()
                code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
                self.assertEqual(code, expected_code)
                result = json.loads(out)
                self.assertEqual(result["status"], status)
                self.assertEqual(err, "")
                self.assertEqual(self.bytes_now(), before)
                if status == "error":
                    self.assertNotIn("recurred", result)
                    self.assertNotIn("unresolved", result)
                    self.assertIn("AH-002", result["reason"])
                    code, out, err = run_cli("check", "--ledger", str(self.ledger))
                    self.assertEqual(code, 2)
                    self.assertNotIn("RECURRED", err)
                    self.assertNotIn("UNRESOLVED", err)
                    self.assertIn("severity", err)
                else:
                    self.assertEqual(set(result), {"status", "recurred", "unresolved"})
                    for key in ("recurred", "unresolved"):
                        self.assertEqual([r["id"] for r in result[key]],
                                         [r["id"] for r in rows if r["state"] == key])
                        for entry in result[key]:
                            self.assertEqual(set(entry), {"id", "severity", "label"})

    def test_check_exits_zero_without_recurrence_and_one_with_it(self):
        self.seed(verified_row("AH-001"), row("AH-002", "closed"))
        before = self.bytes_now()
        code, out, _ = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("no recurrences", out)
        self.seed(row("AH-001", "recurred"))
        code, _, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 1)
        self.assertIn("RECURRED AH-001", err)
        self.seed(*[json.loads(l) for l in before.decode().splitlines()])
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 0)

    def test_status_orders_verified_after_closed_and_recurred_first(self):
        self.seed(row("AH-001", "closed"), row("AH-002", "verified"),
                  row("AH-003", "recurred"), row("AH-004", "open"),
                  row("AH-005", "unresolved"), row("AH-006", "fixed"))
        _, out, _ = run_cli("status", "--ledger", str(self.ledger))
        listed = [l.split()[0] for l in out.splitlines() if l.startswith("AH-")]
        self.assertEqual(listed, ["AH-003", "AH-005", "AH-004", "AH-006", "AH-001", "AH-002"])
        self.assertIn("1 verified", out)
        self.assertIn("1 unresolved", out)


class LedgerSelection(LedgerCase):
    def test_the_environment_variable_selects_the_ledger(self):
        self.seed(row("AH-001", "fixed"))
        code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("AH-001", out)

    def test_an_explicit_flag_beats_the_environment_variable(self):
        self.seed(row("AH-001", "fixed"))
        other = self.dir / "other.jsonl"
        other.write_text(json.dumps(row("AH-090", "open")) + "\n")
        _, out, _ = run_cli("status", "--ledger", str(other))
        self.assertIn("AH-090", out)
        self.assertNotIn("AH-001", out)


    def test_the_earlier_variable_names_still_select_the_ledger_newest_first(self):
        oldest, old, new = self.dir / "oldest.jsonl", self.dir / "old.jsonl", self.dir / "new.jsonl"
        self.assertEqual((dl.LEDGER_ENV, *dl.EARLIER_LEDGER_ENVS),
                         ("TRACE_OS_DEFECT_LEDGER", "TRACEWARE_DEFECT_LEDGER", "TRACE_KIT_DEFECT_LEDGER"))
        names = (dl.LEDGER_ENV, *dl.EARLIER_LEDGER_ENVS)
        clean = {k: v for k, v in os.environ.items() if k not in names}
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["TRACE_KIT_DEFECT_LEDGER"] = str(oldest)
            self.assertEqual(dl.ledger_path(), oldest)
            os.environ["TRACEWARE_DEFECT_LEDGER"] = str(old)
            self.assertEqual(dl.ledger_path(), old)
            os.environ["TRACE_OS_DEFECT_LEDGER"] = str(new)
            self.assertEqual(dl.ledger_path(), new)

class EndToEndOnASyntheticObject(unittest.TestCase):
    """The whole loop on a real file: a diagnosed failure, a correction that changes its hash, a recheck, and a regression.

    This runs the installed command as a subprocess, so it exercises the shipped entry point rather than an import of it.
    """

    BAD = "The panel agreed unanimously that the effect is large.\n"
    GOOD = "Three of five reviewers reported the effect; two abstained.\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.ledger = self.dir / "defects.jsonl"
        self.obj = self.dir / "object.txt"
        self.env = {**os.environ, dl.LEDGER_ENV: str(self.ledger)}

    def cli(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "defect_ledger.py"), *argv],
            cwd=ROOT, env=self.env, capture_output=True, text=True)

    def sha(self):
        return hashlib.sha256(self.obj.read_bytes()).hexdigest()

    def evidence_for(self, outcome, name):
        ev = evidence("AH-001")
        ev["target"]["corrected_object"] = str(self.obj)
        ev["target"]["object_hash"] = self.sha()
        ev["target"]["original_diagnosis"] = "The object asserts unanimous agreement."
        ev["recheck"]["criterion"] = "The string 'agreed unanimously' is absent from the object."
        ev["recheck"]["method"] = "Read the object at the recorded hash and searched for the diagnosed string."
        ev["recheck"]["outcome"] = outcome
        ev["recheck"]["evidence_ref"] = f"sha256:{self.sha()}"
        p = self.dir / name
        p.write_text(json.dumps(ev, indent=2))
        return str(p)

    def test_diagnose_correct_recheck_and_then_regress(self):
        self.obj.write_bytes(self.BAD.encode())
        failing_hash = self.sha()
        self.ledger.write_text(json.dumps(row("AH-001", "open", quote=self.BAD.strip())) + "\n")

        # Correct the object. The hash changing is what the record points at.
        self.obj.write_bytes(self.GOOD.encode())
        self.assertNotEqual(self.sha(), failing_hash)
        # The criterion is checked here rather than asserted in the record.
        self.assertNotIn("agreed unanimously", self.obj.read_text())

        marked = self.cli("mark", "--id", "AH-001", "--fixed", "--note", "rewrote the claim")
        self.assertEqual(marked.returncode, 0, marked.stderr)

        done = self.cli("verify", "AH-001", "--evidence", self.evidence_for("pass", "pass.json"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("state fixed -> verified", done.stdout)
        self.assertIn("It is not acceptance", done.stdout)

        listing = self.cli("status")
        self.assertIn("verified", listing.stdout)
        self.assertIn("1 recheck event(s) recorded", listing.stdout)
        self.assertEqual(self.cli("check").returncode, 0)

        # The correction comes undone, and a targeted recheck fails.
        self.obj.write_bytes(self.BAD.encode())
        self.assertIn("agreed unanimously", self.obj.read_text())
        back = self.cli("verify", "AH-001", "--evidence", self.evidence_for("fail", "fail.json"))
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertIn("recurred", back.stdout)

        gate = self.cli("check")
        self.assertEqual(gate.returncode, 1)
        self.assertIn("RECURRED AH-001", gate.stderr)

        stored = json.loads(self.ledger.read_text().splitlines()[0])
        self.assertEqual(stored["state"], "recurred")
        self.assertEqual([e["decision"] for e in stored["verifications"]], ["verified", "recurred"])
        self.assertEqual(stored["verifications"][0]["evidence"]["target"]["object_hash"],
                         hashlib.sha256(self.GOOD.encode()).hexdigest())


class OpenRecordsAFinding(LedgerCase):
    """Findings that never came from a review run need a way in, or they are never rechecked."""

    def open(self, *extra):
        return run_cli("open", "--label", "a claim the run does not support",
                       "--severity", "high", *extra, "--ledger", str(self.ledger))

    def test_open_writes_the_expected_row_and_prints_the_id(self):
        self.seed(row("AH-001", "closed"))
        code, out, _ = self.open("--quote", "the quoted span", "--by", "Jane Roe",
                                 "--note", "seen while editing",
                                 "--affected", "ai/records/r1", "ai/records/r2",
                                 "--faulty-default", "AGENTS.md")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "AH-002")
        r = self.rows()["AH-002"]
        self.assertEqual(r["label"], "a claim the run does not support")
        self.assertEqual(r["severity"], "high")
        self.assertEqual(r["quote"], "the quoted span")
        self.assertEqual(r["state"], "open")
        self.assertEqual(r["first_seen"], f"{datetime.date.today():%Y-%m-%d}")
        self.assertEqual(r["runs"], ["manual:Jane Roe"])
        self.assertEqual(r["note"], "seen while editing")
        self.assertEqual(r["opened_by"], "Jane Roe")
        self.assertEqual(r["affected"], ["ai/records/r1", "ai/records/r2"])
        self.assertEqual(r["faulty_default"], "AGENTS.md")
        self.assertNotIn("anchor_at_open", r)
        self.assertNotIn("verifications", r)
        self.assertEqual(self.rows()["AH-001"]["state"], "closed")
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 0)

    def test_open_without_a_name_says_so_rather_than_inventing_one(self):
        code, out, _ = self.open()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "DEF-001")
        r = self.rows()["DEF-001"]
        self.assertEqual((r["runs"], r["opened_by"], r["quote"], r["note"]),
                         (["manual:not stated"], "not stated", "", ""))
        self.assertNotIn("affected", r)
        self.assertNotIn("faulty_default", r)

    def test_open_refuses_a_bad_severity_and_writes_nothing(self):
        self.seed(row("AH-001", "closed"))
        before = self.bytes_now()
        code, out, err = run_cli("open", "--label", "x", "--severity", "urgent",
                                 "--ledger", str(self.ledger))
        self.assertEqual(code, 2)
        self.assertEqual(self.bytes_now(), before)
        self.assertEqual(out, "")
        self.assertIn("severity", err)
        self.assertIn("unchanged", err)
        self.assertFalse((self.dir / "other.jsonl").exists())
        code, _, _ = run_cli("open", "--label", "x", "--severity", "urgent",
                             "--ledger", str(self.dir / "other.jsonl"))
        self.assertEqual(code, 2)
        self.assertFalse((self.dir / "other.jsonl").exists(), "a rejected open created a ledger")

    def test_open_records_the_grounding_verdict_only_when_source_and_quote_are_given(self):
        # The anchor is information about the quote, not a judgment about the
        # finding, so it is stored under its own name and never changes the state.
        src = self.dir / "object.txt"
        src.write_text("The panel agreed unanimously that the effect is large.\n")
        self.open("--quote", "agreed unanimously that the effect is large", "--source", str(src))
        self.assertEqual(self.rows()["DEF-001"]["anchor_at_open"], "grounded")
        self.open("--quote", "a sentence that is nowhere in the object", "--source", str(src))
        self.assertEqual(self.rows()["DEF-002"]["anchor_at_open"], "invented")
        self.open("--quote", "agreed unanimously that the effect is large")
        self.assertNotIn("anchor_at_open", self.rows()["DEF-003"])
        self.open("--source", str(src))
        self.assertNotIn("anchor_at_open", self.rows()["DEF-004"])
        self.assertEqual({r["state"] for r in self.rows().values()}, {"open"})

    def test_open_with_an_unreadable_source_writes_nothing(self):
        before_exists = self.ledger.exists()
        code, _, err = self.open("--quote", "some span", "--source", str(self.dir / "missing.tex"))
        self.assertEqual(code, 2)
        self.assertEqual(self.ledger.exists(), before_exists)
        self.assertIn("unchanged", err)


class FailedRecheckIsUnresolved(LedgerCase):
    """A recheck that ran and failed is a known uncorrected defect, and a known uncorrected defect must fail the gate."""

    def test_a_failed_recheck_moves_open_fixed_and_closed_rows_to_unresolved(self):
        # LESSON: before this rule a failed recheck was kept on the row and the
        # check still passed, so a defect known not to be fixed sat in a
        # publishable state while the row that was never looked at did too.
        self.seed(row("AH-001", "open"), row("AH-002", "fixed"), row("AH-003", "closed"))
        for rid, before in (("AH-001", "open"), ("AH-002", "fixed"), ("AH-003", "closed")):
            code, out, _ = self.verify(rid, evidence(rid, recheck={"outcome": "fail"}), f"{rid}.json")
            self.assertEqual(code, 0)
            r = self.rows()[rid]
            self.assertEqual(r["state"], "unresolved")
            self.assertEqual(r["previous_states"], [before])
            event = r["verifications"][0]
            self.assertEqual((event["decision"], event["state_before"], event["state_after"]),
                             ("unresolved", before, "unresolved"))
            self.assertIn(f"state {before} -> unresolved", out)
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["recurred"], [])
        self.assertEqual([r["id"] for r in result["unresolved"]], ["AH-001", "AH-002", "AH-003"])
        self.assertEqual(err, "")
        code, out, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("UNRESOLVED AH-002 (high): AH-002 label", err)
        self.assertIn("0 correction(s) did not hold, 3 defect(s) unresolved", err)

    def test_a_second_failure_keeps_the_row_unresolved_and_keeps_both_events(self):
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "first.json")
        _, out, _ = self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "second.json")
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "unresolved")
        self.assertEqual(r["previous_states"], ["fixed"])
        self.assertEqual([e["decision"] for e in r["verifications"]], ["unresolved", "unresolved"])
        self.assertIn("state unresolved -> unresolved", out)

    def test_a_later_adequate_passing_recheck_moves_unresolved_to_verified(self):
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "first.json")
        code, out, _ = self.verify("AH-001", evidence(), "second.json")
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        self.assertEqual(r["previous_states"], ["fixed", "unresolved"])
        self.assertEqual([e["decision"] for e in r["verifications"]], ["unresolved", "verified"])
        self.assertIn("state unresolved -> verified", out)
        code, out, _ = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, json.loads(out)), (0, {"status": "ok", "recurred": [], "unresolved": []}))

    def test_a_passing_but_inadequate_recheck_leaves_the_row_unresolved(self):
        # LESSON: unresolved is left the same way it is entered, by a recheck
        # that meets the bar. A pass with unknown adequacy, contrary evidence
        # or missing context is recorded and moves nothing.
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "first.json")
        for name, limits in (("unknown.json", {"adequacy": "unknown"}),
                             ("contrary.json", {"unresolved_contrary_evidence": "a reader still sees it"}),
                             ("context.json", {"essential_context_missing": True})):
            code, out, _ = self.verify("AH-001", evidence(limits=limits), name)
            self.assertEqual(code, 0)
            r = self.rows()["AH-001"]
            self.assertEqual(r["state"], "unresolved", name)
            self.assertEqual(r["verifications"][-1]["decision"], "recorded")
            self.assertIn("state unresolved -> unresolved", out)
        self.assertEqual(self.rows()["AH-001"]["previous_states"], ["fixed"])
        self.assertEqual(len(self.rows()["AH-001"]["verifications"]), 4)
        code, out, _ = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, json.loads(out)["status"]), (1, "unresolved"))

    def test_recurred_takes_precedence_over_unresolved_and_both_are_listed(self):
        self.seed(row("AH-001", "unresolved"), row("AH-002", "recurred"))
        code, out, _ = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["status"], "recurred")
        self.assertEqual([r["id"] for r in result["recurred"]], ["AH-002"])
        self.assertEqual([r["id"] for r in result["unresolved"]], ["AH-001"])
        code, _, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 1)
        self.assertIn("RECURRED AH-002", err)
        self.assertIn("UNRESOLVED AH-001", err)
        self.assertIn("1 correction(s) did not hold, 1 defect(s) unresolved", err)

    def test_the_absence_sweep_never_closes_an_unresolved_row(self):
        # LESSON: closure by absence touches `fixed` only. A row that a recheck
        # found uncorrected is not rescued by a sweep that did not look at it.
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(recheck={"outcome": "fail"}))
        with mock.patch.object(dl, "key", lambda: "test"), \
                mock.patch.object(dl, "match", lambda known, found, tok: {}):
            run_cli("ingest", "--run", self.run_dir(), "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "unresolved")
        self.assertEqual(len(r["verifications"]), 1)
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 1)

    def test_status_counts_unresolved_rows_like_any_other_state(self):
        self.seed(row("AH-001", "unresolved"), row("AH-002", "unresolved"), row("AH-003", "open"))
        code, out, _ = run_cli("status", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("1 open, 2 unresolved", out)
        self.assertTrue(any(l.startswith("AH-001") and "unresolved" in l for l in out.splitlines()))


class RepairClaimsAfterFailedRechecks(LedgerCase):
    def test_repair_claims_keep_a_failed_recheck_visible_until_verification(self):
        for initial in ('open', 'verified'):
            for options in (('--fixed',), ('--state', 'fixed'), ('--state', 'closed'),
                            ('--state', 'open')):
                with self.subTest(initial=initial, options=options):
                    self.seed(verified_row() if initial == 'verified' else row(state='open'))
                    self.verify('AH-001', evidence(recheck={'outcome': 'fail'}))
                    failure = 'recurred' if initial == 'verified' else 'unresolved'
                    code, out, _ = run_cli('mark', '--id', 'AH-001', *options,
                                          '--note', 'Another repair attempted', '--ledger', str(self.ledger))
                    self.assertEqual(code, 0, 'the repair claim is still recorded')
                    current = self.rows()['AH-001']
                    self.assertEqual(current['state'], failure)
                    self.assertIn(failure, out)
                    self.assertEqual(current['repair_claims'][-1]['state'],
                                     'fixed' if options == ('--fixed',) else options[-1])
                    self.assertEqual(run_cli('check', '--ledger', str(self.ledger))[0], 1)
                    self.verify('AH-001', evidence(limits={'adequacy': 'unknown'}))
                    self.assertEqual(run_cli('check', '--ledger', str(self.ledger))[0], 1)
                    self.verify('AH-001', evidence())
                    self.assertEqual(self.rows()['AH-001']['state'], 'verified')
                    self.assertEqual(run_cli('check', '--ledger', str(self.ledger))[0], 0)

    def test_a_legacy_fixed_row_with_an_outstanding_failure_still_fails_check(self):
        self.seed(row(state='open'))
        self.verify('AH-001', evidence(recheck={'outcome': 'fail'}))
        current = self.rows()['AH-001']
        current['state'] = 'fixed'  # produced by mark in 0.1.0-rc1
        self.seed(current)
        code, out, _ = run_cli('check', '--json', '--ledger', str(self.ledger))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['unresolved'][0]['id'], 'AH-001')

    def test_a_review_recurrence_without_a_failed_recheck_can_still_be_marked_fixed(self):
        self.seed(row(state='recurred'))
        code, _, _ = run_cli('mark', '--id', 'AH-001', '--fixed', '--ledger', str(self.ledger))
        self.assertEqual(code, 0)
        self.assertEqual(self.rows()['AH-001']['state'], 'fixed')


class CorrectedDefaultNeedsALaterRun(LedgerCase):
    """A faulty instruction is corrected on later work, not on the one output where it was noticed."""

    def setUp(self):
        super().setUp()
        # The later run resolves in this tree: the record exists and the
        # version names a commit that revises AGENTS.md.
        self.LATER = later_run_repo(self.dir)
        before = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, before)
        self.seed(row("AH-001", "fixed", faulty_default="AGENTS.md", affected=["ai/records/r1"]))

    def test_a_passing_recheck_without_a_later_run_is_recorded_not_verified(self):
        # LESSON: correcting the output a reviewer read leaves the instruction
        # free to shape the next one (Section 4). Verification has to reach a
        # later run produced under the revised instruction.
        code, out, _ = self.verify("AH-001", evidence())
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "fixed")
        self.assertNotIn("previous_states", r)
        self.assertEqual(r["verifications"][0]["decision"], "recorded")
        self.assertIn("later-run recheck", out)
        self.assertIn("AGENTS.md", out)
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 0)

    def test_a_passing_recheck_with_a_later_run_verifies(self):
        code, out, _ = self.verify("AH-001", evidence(recheck={"later_run": self.LATER}))
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        self.assertEqual(r["verifications"][0]["decision"], "verified")
        self.assertEqual(r["verifications"][0]["evidence"]["recheck"]["later_run"], self.LATER)
        self.assertIn("later run : ai/records/20260910-section-4-redraft", out)
        self.assertIn("checked against the original diagnosis", out)
        self.assertIn("resolves  : trace record ai/records/20260910-section-4-redraft", out)
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 0)

    def test_a_later_run_does_not_excuse_inadequate_evidence(self):
        self.verify("AH-001", evidence(recheck={"later_run": self.LATER},
                                       limits={"adequacy": "unknown"}))
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["verifications"][0]["decision"]), ("fixed", "recorded"))

    def test_a_failed_recheck_on_a_faulty_default_row_is_unresolved_like_any_other(self):
        self.verify("AH-001", evidence(recheck={"outcome": "fail", "later_run": self.LATER}))
        self.assertEqual(self.rows()["AH-001"]["state"], "unresolved")

    def test_a_malformed_later_run_is_rejected_and_writes_nothing(self):
        before = self.bytes_now()
        for name, later in (("string.json", "commit 4f3924b"),
                            ("blank.json", {"record": "  ", "instruction_version": "4f3924b"}),
                            ("missing.json", {"record": "ai/records/r2"})):
            code, _, err = self.verify("AH-001", evidence(recheck={"later_run": later}), name)
            self.assertEqual(code, 2, name)
            self.assertEqual(self.bytes_now(), before)
            self.assertIn("later_run", err)
            self.assertIn("unchanged", err)

    def test_a_row_without_a_faulty_default_verifies_with_or_without_a_later_run(self):
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(recheck={"later_run": self.LATER}))
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")

    def test_a_faulty_default_set_by_mark_is_honoured_by_verify(self):
        self.seed(row("AH-001", "open"))
        run_cli("mark", "--id", "AH-001", "--fixed", "--faulty-default", ".skills/x/SKILL.md",
                "--ledger", str(self.ledger))
        _, out, _ = self.verify("AH-001", evidence())
        self.assertEqual(self.rows()["AH-001"]["state"], "fixed")
        self.assertIn(".skills/x/SKILL.md", out)


class CheckValidatesEveryRow(LedgerCase):
    """A ledger the check cannot trust is an error, never a pass and never a finding."""

    def assertMalformed(self, *rows, names):
        self.seed(*rows)
        before = self.bytes_now()
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual(code, 2)
        result = json.loads(out)
        self.assertEqual(result["status"], "error")
        for name in names:
            self.assertIn(name, result["reason"])
        self.assertNotIn("recurred", result)
        self.assertEqual(err, "")
        code, out, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("ERROR", err)
        self.assertNotIn("RECURRED", err)
        self.assertNotIn("UNRESOLVED", err)
        self.assertEqual(self.bytes_now(), before)

    def test_an_unknown_state_string_makes_check_exit_2(self):
        # LESSON: an unknown state used to pass the check because nothing looked
        # at it. "resolved" is not a state, and a typo must not read as a pass.
        self.assertMalformed(row("AH-001", "recurred"), row("AH-002", "resolved"),
                             names=("AH-002", "resolved"))
        self.assertMalformed(row("AH-001", "Verified"), names=("AH-001", "Verified"))

    def test_a_verified_row_with_no_verifying_event_makes_check_exit_2(self):
        # LESSON: a verified row with nothing behind it is the exact shape this
        # ledger exists to prevent, so it is a malformed ledger, not a finding.
        self.assertMalformed(row("AH-001", "verified"), names=("AH-001", "verified"))
        self.assertMalformed(row("AH-001", "verified", verifications=[]), names=("AH-001",))
        self.assertMalformed(row("AH-001", "verified", verifications=[{"decision": "recorded"}]),
                             names=("AH-001",))
        self.assertMalformed(row("AH-001", "verified", verifications="yes"), names=("AH-001",))

    def test_a_verified_row_written_by_verify_passes_check(self):
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence())
        code, out, _ = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, json.loads(out)), (0, {"status": "ok", "recurred": [], "unresolved": []}))

    def test_a_non_string_id_or_state_makes_check_exit_2(self):
        self.assertMalformed({"id": 7, "state": "open"}, names=("row 1", "id"))
        self.assertMalformed(row("AH-001", "open"), {"id": "AH-002", "state": None},
                             names=("row 2", "state"))

    def test_the_legacy_research_ledger_shape_still_passes(self):
        # The project's own ledger holds open and closed rows with no
        # verification fields at all; the stricter check must keep accepting it.
        self.seed(*[row(f"AH-{n:03d}", s) for n, s in enumerate(("open", "closed") * 4, start=1)])
        code, out, _ = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("no recurrences", out)


class ShippedExampleIsValid(unittest.TestCase):
    def test_the_example_evidence_file_passes_validation(self):
        # LESSON: a template nobody runs is a template that stops matching the
        # validator it is supposed to illustrate.
        path = ROOT / "scripts" / "verification-evidence.example.json"
        ev = dl.load_evidence(str(path), json.loads(path.read_text())["target"]["defect_id"])
        state, decision, _ = dl.decide("fixed", ev)
        self.assertEqual((state, decision), ("verified", "verified"))
        # The example carries a later run, so it also verifies a corrected
        # default, in a tree where that run resolves: its record exists and its
        # version names a commit revising the instruction (rc22). The example's
        # commit is a placeholder, so here it is recorded and says why.
        self.assertIn("later_run", ev["recheck"])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            later = later_run_repo(root)
            state, decision, reasons = dl.decide("fixed", ev, "AGENTS.md", root=root)
            self.assertEqual((state, decision), ("fixed", "recorded"))
            self.assertTrue(any("names no commit" in r for r in reasons), reasons)
            ev["recheck"]["later_run"]["instruction_version"] = later["instruction_version"]
            state, decision, _ = dl.decide("fixed", ev, "AGENTS.md", root=root)
            self.assertEqual((state, decision), ("verified", "verified"))


class DraftRecheck(LedgerCase):
    """LESSON (2026-09-22): the recheck is said to the agent and read back.

    `verify` takes a six-group JSON that nothing drafted, so every recheck in
    the project was a self-check by the agent that wrote the fix, or did not
    happen. `draft-recheck` fills what the row and the tree already know, shows
    the row's label as the proposed criterion, and records the checker's words
    as they are: a field the checker did not state reads `not stated`, an
    unstated outcome is a recordable outcome that leaves the state alone, and
    nobody is asked for more than they said.
    """

    def draft(self, rid: str, *extra: str) -> tuple[int, str, str, pathlib.Path]:
        out = self.dir / f"recheck-{rid}.json"
        code, o, e = run_cli("draft-recheck", rid, "--ledger", str(self.ledger), "--out", str(out), *extra)
        return code, o, e, out

    def test_the_draft_is_filled_from_the_row_and_the_tree_and_printed_back(self):
        obj = self.dir / "object.txt"
        obj.write_text("the corrected text\n")
        self.seed(row("AH-001", "fixed", label="the pooled share is a mean of percentages",
                      quote="mean of the yearly", sources=[str(obj)], fixed_by="Jane Roe",
                      opened_by="John Doe", note="returned by John Doe over 2 commit(s)",
                      fix_note="divides cases by patents now"))
        code, out, err, path = self.draft("AH-001", "--checker", "John Doe", "--person",
                                          "--recorder", "some-model", "--said", "I recomputed it")
        self.assertEqual(code, 0, err)
        ev = json.loads(path.read_text())
        self.assertEqual(ev["target"]["original_diagnosis"],
                         'the pooled share is a mean of percentages Quote: "mean of the yearly"')
        self.assertEqual(ev["target"]["corrected_object"], str(obj))
        self.assertEqual(ev["target"]["object_hash"], hashlib.sha256(obj.read_bytes()).hexdigest())
        self.assertEqual(ev["response"], {"corrective_action": "divides cases by patents now", "implementer": "Jane Roe"})
        self.assertEqual(ev["recheck"]["criterion"], "the pooled share is a mean of percentages")
        self.assertIn("proposed from the row's label", ev["recheck"]["criterion_basis"])
        self.assertEqual(ev["recheck"]["method"], "not stated")
        self.assertEqual(ev["recheck"]["outcome"], "not stated")
        self.assertEqual(ev["accountability"]["checker"], "John Doe")
        self.assertEqual(ev["accountability"]["checker_kind"], "human")
        # The self-check flag is derived by verify from the names; the draft
        # declares nothing on the checker's behalf.
        self.assertNotIn("checker_implemented", ev["accountability"])
        self.assertEqual(ev["limits"]["adequacy"], "unknown")
        self.assertEqual(ev["words"]["checker_said"], "I recomputed it")
        # The read-back, field by field, and what recording it would do.
        for line in ("diagnosis :", "criterion :", "compared  : not stated", "found     : not stated",
                     "checker   : John Doe (human); self-check no", "said      : I recomputed it",
                     "recording it would read: recorded, state fixed -> fixed"):
            self.assertIn(line, out)
        self.assertEqual(self.rows()["AH-001"]["state"], "fixed", "a draft writes nothing to the ledger")

    def test_a_thin_recheck_is_recorded_as_thin_and_leaves_the_state_alone(self):
        self.seed(row("AH-001", "fixed"))
        code, _, _, path = self.draft("AH-001", "--checker", "John Doe", "--person", "--said", "looks good")
        self.assertEqual(code, 0)
        code, out, err = run_cli("verify", "AH-001", "--evidence", str(path), "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        entry = self.rows()["AH-001"]
        self.assertEqual(entry["state"], "fixed")
        event = entry["verifications"][-1]
        self.assertEqual(event["decision"], "recorded")
        self.assertEqual(event["outcome"], "not stated")
        self.assertEqual(event["evidence"]["words"]["checker_said"], "looks good")
        self.assertIn("states no outcome", " ".join(event["reasons"]))
        # Kept, and not a recheck that happened: the row still awaits one.
        self.assertFalse(dl.rechecked(entry))

    def full_draft(self, *extra: str) -> tuple[int, str, str, pathlib.Path]:
        return self.draft("AH-001", "--checker", "John Doe", "--person",
                          "--method", "recomputed the share from the two count columns",
                          "--outcome", "pass", "--evidence-ref", "the table at the recorded hash",
                          "--limitations", "the other rows were not checked",
                          "--contrary", "none", "--adequacy", "adequate", *extra)

    def test_a_full_draft_verifies_when_the_checker_states_everything(self):
        obj = self.dir / "table.md"
        obj.write_text("cases over patents\n")
        self.seed(row("AH-001", "fixed", fixed_by="Jane Roe", fix_note="divides cases by patents"))
        code, _, _, path = self.full_draft("--object", str(obj))
        self.assertEqual(code, 0)
        code, out, err = run_cli("verify", "AH-001", "--evidence", str(path), "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")

    def test_a_placeholder_for_the_object_or_the_correction_records_and_does_not_verify(self):
        # A verification is a recheck on the corrected object; a draft whose
        # object, hash, action or implementer read not stated is recorded and
        # says why it did not verify, however complete the judgment fields.
        self.seed(row("AH-001", "fixed"))
        code, out, _, path = self.full_draft()
        self.assertEqual(code, 0)
        self.assertIn("recording it would read: recorded, state fixed -> fixed", out)
        code, out, err = run_cli("verify", "AH-001", "--evidence", str(path), "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        entry = self.rows()["AH-001"]
        self.assertEqual(entry["state"], "fixed")
        reasons = " ".join(entry["verifications"][-1]["reasons"])
        for field in ("target.corrected_object", "target.object_hash", "response.corrective_action", "response.implementer"):
            self.assertIn(f"{field} is not stated", reasons)
        self.assertTrue(dl.rechecked(entry), "a pass with placeholders is still an outcome that was stated")

    def test_the_rows_note_is_never_read_as_the_corrective_action(self):
        self.seed(row("AH-001", "fixed", note="returned by John Doe at t over 2 commit(s); severity medium by default"))
        code, _, _, path = self.draft("AH-001", "--checker", "John Doe", "--person")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())["response"]["corrective_action"], "not stated")

    def test_mark_by_names_the_fixer_and_the_draft_derives_the_self_check(self):
        self.seed(row("AH-001", "open"))
        code, _, err = run_cli("mark", "--id", "AH-001", "--fixed", "--by", "Jane Roe",
                               "--note", "divides cases by patents", "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()["AH-001"]["fixed_by"], "Jane Roe")
        self.assertEqual(self.rows()["AH-001"]["fix_note"], "divides cases by patents")
        code, out, _, path = self.draft("AH-001", "--checker", "jane roe", "--person")
        self.assertEqual(code, 0)
        ev = json.loads(path.read_text())
        self.assertEqual(ev["response"], {"corrective_action": "divides cases by patents", "implementer": "Jane Roe"})
        self.assertNotIn("checker_implemented", ev["accountability"])
        self.assertIn("self-check YES (derived from the checker and implementer names)", out)

    def test_an_unstated_implementer_says_the_flag_could_not_be_derived(self):
        self.seed(row("AH-001", "fixed"))
        code, out, _, path = self.draft("AH-001", "--checker", "John Doe", "--person")
        self.assertEqual(code, 0)
        self.assertNotIn("checker_implemented", json.loads(path.read_text())["accountability"])
        self.assertIn("implementer is not stated", out)

    def test_the_checker_kind_is_declared_never_inferred(self):
        self.seed(row("AH-001", "fixed"))
        code, _, err, _ = self.draft("AH-001", "--checker", "claude-opus-5")
        self.assertEqual(code, 2)
        self.assertIn("--person or --agent", err)
        code, _, _, path = self.draft("AH-001", "--checker", "claude-opus-5", "--agent")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())["accountability"]["checker_kind"], "model")

    def test_open_keeps_the_sources_the_quote_was_grounded_against(self):
        src = self.dir / "s.txt"
        src.write_text("a sentence to object to\n")
        code, out, err = run_cli("open", "--label", "objection", "--severity", "low", "--quote", "sentence to object",
                                 "--source", str(src), "--by", "John Doe", "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        entry = self.rows()[out.strip()]
        self.assertEqual(entry["sources"], [str(src)])
        self.assertEqual(entry["anchor_at_open"], "grounded")


@contextlib.contextmanager
def crlf_text_writes():
    """Text files opened while this is active translate a newline the way Windows does.

    CPython's own text layer translates newlines only on Windows, so the pure-Python layer stands in for it here, with the platform separator set to CRLF.
    """
    import _pyio
    with mock.patch.object(os, "linesep", "\r\n"), mock.patch.object(io, "open", _pyio.open):
        yield


class CurrentCorrection(LedgerCase):
    """LESSON (AH-149): after a failed recheck, `mark --fixed --by X --note N` kept X only in the
    repair claim, and `draft-recheck` read the fixer and the fix of the correction before it.
    The row could not leave unresolved, and a second fixer rechecking their own fix read as
    independent, against the wrong correction. Every fixed claim now records its implementer and
    its action together, and the draft reads the current correction."""

    draft = DraftRecheck.draft
    full_draft = DraftRecheck.full_draft

    def mark(self, *argv: str) -> tuple[int, str, str]:
        return run_cli("mark", "--id", "AH-001", *argv, "--ledger", str(self.ledger))

    def fail_once(self, name: str = "fail.json") -> None:
        self.verify("AH-001", evidence(recheck={"outcome": "fail"}), name)

    def response(self, *extra: str) -> dict:
        code, _, err, path = self.draft("AH-001", "--checker", "John Doe", "--person", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(path.read_text())["response"]

    def test_a_fix_claimed_while_unresolved_names_its_fixer_and_can_verify(self):
        obj = self.dir / "object.txt"
        obj.write_text("the corrected paragraph\n")
        self.seed(row("AH-001", "open", sources=[str(obj)]))
        self.fail_once()
        code, _, err = self.mark("--fixed", "--by", "Fixer", "--note", "rewrote the paragraph")
        self.assertEqual(code, 0, err)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "unresolved", "a repair claim does not clear a failed recheck")
        self.assertEqual((r["fixed_by"], r["fix_note"]), ("Fixer", "rewrote the paragraph"))
        code, out, _, path = self.full_draft()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())["response"],
                         {"corrective_action": "rewrote the paragraph", "implementer": "Fixer"})
        self.assertIn("recording it would read: verified, state unresolved -> verified", out)
        code, _, err = run_cli("verify", "AH-001", "--evidence", str(path), "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")

    def test_a_second_fixer_rechecking_their_own_fix_is_a_self_check(self):
        self.seed(row("AH-001", "open"))
        self.mark("--fixed", "--by", "Fixer", "--note", "first fix")
        self.fail_once()
        self.mark("--fixed", "--by", "Fixer2", "--note", "second fix")
        code, out, _, path = self.draft("AH-001", "--checker", "Fixer2", "--person")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())["response"],
                         {"corrective_action": "second fix", "implementer": "Fixer2"})
        self.assertIn("self-check YES", out)

    def test_a_new_claim_never_pairs_its_fixer_with_the_earlier_fix(self):
        self.seed(row("AH-001", "open"))
        self.mark("--fixed", "--by", "Fixer", "--note", "first fix")
        self.fail_once()
        self.mark("--fixed", "--by", "Fixer2")
        self.assertEqual(self.response(), {"corrective_action": "not stated", "implementer": "Fixer2"})
        self.mark("--fixed")
        self.assertEqual(self.response(), {"corrective_action": "not stated", "implementer": "not stated"})

    def test_the_state_fixed_spelling_records_the_correction_too(self):
        self.seed(row("AH-001", "open"))
        self.mark("--state", "fixed", "--by", "Jane Roe", "--note", "first fix")
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["fixed_by"], r["fix_note"]), ("fixed", "Jane Roe", "first fix"))
        self.fail_once()
        self.mark("--state", "fixed", "--by", "Kim Lee", "--note", "second fix")
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["fixed_by"], r["fix_note"]), ("unresolved", "Kim Lee", "second fix"))
        self.assertEqual(self.response(), {"corrective_action": "second fix", "implementer": "Kim Lee"})

    def test_a_claim_for_another_state_is_never_read_as_the_correction(self):
        self.seed(row("AH-001", "open"))
        self.fail_once()
        self.mark("--state", "open", "--by", "John Doe", "--note", "reopened for discussion")
        self.assertEqual(self.rows()["AH-001"]["repair_claims"][-1]["state"], "open")
        self.assertEqual(self.response(), {"corrective_action": "not stated", "implementer": "not stated"})

    def test_a_repair_claim_written_before_this_rule_is_still_read(self):
        # A kit before AH-149 kept the pair of the first correction and put the
        # second only in the repair claim. The claim is the current correction
        # until a verifying event comes after it.
        failed = {"outcome": "fail", "decision": "unresolved", "state_before": "fixed",
                  "state_after": "unresolved", "recorded": "2026-09-20T10:00:00+02:00"}
        claim = {"state": "fixed", "note": "second fix", "by": "Fixer2",
                 "recorded": "2026-09-21T10:00:00+02:00"}
        self.seed(row("AH-001", "unresolved", fixed_by="Fixer", fix_note="first fix",
                      verifications=[failed], repair_claims=[claim]))
        self.assertEqual(self.response(), {"corrective_action": "second fix", "implementer": "Fixer2"})
        verified = {"outcome": "pass", "decision": "verified", "state_before": "unresolved",
                    "state_after": "verified", "recorded": "2026-09-22T10:00:00+02:00"}
        later = dict(failed, state_before="fixed", recorded="2026-09-24T10:00:00+02:00")
        self.seed(row("AH-001", "unresolved", fixed_by="Fixer3", fix_note="third fix",
                      verifications=[failed, verified, later], repair_claims=[claim]))
        self.assertEqual(self.response(), {"corrective_action": "third fix", "implementer": "Fixer3"})

    def test_restating_a_fixed_row_keeps_what_it_does_not_restate(self):
        self.seed(row("AH-001", "open"))
        self.mark("--fixed", "--by", "Jane Roe", "--note", "divides cases by patents")
        self.mark("--fixed", "--note", "divides cases by patents, both tables")
        self.mark("--fixed", "--affected", "ai/records/r3")
        r = self.rows()["AH-001"]
        self.assertEqual((r["fixed_by"], r["fix_note"]), ("Jane Roe", "divides cases by patents, both tables"))


class VerificationNeedsItsStatedParts(LedgerCase):
    """A verification carries a criterion, a method, an evidence reference, a named checker and a
    stated limit, and a corrected default a named later run under a named revision. A thin recheck
    is recorded as thin, never refused, and never read as having verified."""

    draft = DraftRecheck.draft
    full_draft = DraftRecheck.full_draft

    def test_each_unstated_part_keeps_a_passing_recheck_from_verifying(self):
        # LESSON (AH-151): only the object, its hash, the action and the
        # implementer were checked, so a recheck with no method, evidence
        # reference or limits verified.
        for group, field in (("recheck", "criterion"), ("recheck", "method"),
                             ("recheck", "evidence_ref"), ("accountability", "checker"),
                             ("limits", "limitations")):
            with self.subTest(field=f"{group}.{field}"):
                self.seed(row("AH-001", "fixed"))
                code, out, err = self.verify("AH-001", evidence(**{group: {field: "Not Stated"}}))
                self.assertEqual(code, 0, err)
                r = self.rows()["AH-001"]
                self.assertEqual((r["state"], r["verifications"][-1]["decision"]), ("fixed", "recorded"))
                self.assertIn(f"{group}.{field} is not stated", out)
                self.assertTrue(dl.rechecked(r), "a thin recheck with a stated outcome is still kept")

    def test_a_draft_with_an_outcome_and_nothing_else_does_not_verify(self):
        obj = self.dir / "object.txt"
        obj.write_text("the corrected text\n")
        self.seed(row("AH-001", "fixed", sources=[str(obj)], fixed_by="Jane Roe", fix_note="rewrote it"))
        code, out, _, path = self.draft("AH-001", "--checker", "John Doe", "--person",
                                        "--outcome", "pass", "--adequacy", "adequate",
                                        "--contrary", "none")
        self.assertEqual(code, 0)
        self.assertIn("recording it would read: recorded, state fixed -> fixed", out)
        for field in ("recheck.method", "recheck.evidence_ref", "limits.limitations"):
            self.assertIn(f"{field} is not stated", out)
        run_cli("verify", "AH-001", "--evidence", str(path), "--ledger", str(self.ledger))
        self.assertEqual(self.rows()["AH-001"]["state"], "fixed")

    def test_a_later_run_that_states_no_version_or_record_does_not_verify(self):
        # LESSON (AH-150): the draft writes "not stated" for an omitted
        # --instruction-version, and that string passed as a later run.
        for field, later in (("instruction_version", {"record": "ai/records/later",
                                                      "instruction_version": "not stated"}),
                             ("record", {"record": "Not stated",
                                         "instruction_version": "AGENTS.md at 4f3924b"})):
            with self.subTest(field=field):
                self.seed(row("AH-001", "fixed", faulty_default="AGENTS.md"))
                code, out, err = self.verify("AH-001", evidence(recheck={"later_run": later}))
                self.assertEqual(code, 0, err)
                r = self.rows()["AH-001"]
                self.assertEqual((r["state"], r["verifications"][-1]["decision"]), ("fixed", "recorded"))
                self.assertIn(f"recheck.later_run.{field} is not stated", out)
        obj = self.dir / "object.txt"
        obj.write_text("the corrected text\n")
        self.seed(row("AH-001", "fixed", faulty_default="AGENTS.md", sources=[str(obj)],
                      fixed_by="Jane Roe", fix_note="rewrote it"))
        code, out, _, _ = self.full_draft("--later-run", "ai/records/later")
        self.assertEqual(code, 0)
        self.assertIn("recording it would read: recorded, state fixed -> fixed", out)
        self.assertIn("recheck.later_run.instruction_version is not stated", out)


class LedgerFixes20260930(LedgerCase):
    """Smaller defects found by the 2026-09-30 deep review of the kit."""

    def test_a_percent_sign_outside_latex_is_content(self):
        # LESSON (AH-156): `%` to the line end was stripped from every source,
        # so a verbatim quote from Markdown read as invented.
        memo = self.dir / "memo.md"
        memo.write_text("In 95% of the cases we coded by hand, reviewers agreed.\n")
        tex = self.dir / "draft.tex"
        tex.write_text("Reviewers agreed in 95\\% of cases. % a hidden remark\n")
        code, _, err = run_cli("open", "--label", "x", "--severity", "low", "--quote",
                               "95% of the cases we coded by hand", "--source", str(memo),
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()["DEF-001"]["anchor_at_open"], "grounded")
        _, out, _ = run_cli("status", "--source", str(memo), "--ledger", str(self.ledger))
        self.assertTrue(any(l.startswith("DEF-001") and "present" in l for l in out.splitlines()), out)
        run_cli("open", "--label", "y", "--severity", "low", "--quote", "a hidden remark",
                "--source", str(tex), "--ledger", str(self.ledger))
        self.assertEqual(self.rows()["DEF-002"]["anchor_at_open"], "invented")
        run_cli("open", "--label", "z", "--severity", "low", "--quote", "agreed in 95% of cases",
                "--source", str(tex), str(memo), "--ledger", str(self.ledger))
        self.assertEqual(self.rows()["DEF-003"]["anchor_at_open"], "grounded")

    def test_a_second_failure_after_verification_stays_recurred(self):
        # LESSON (AH-158): the second failed recheck turned `recurred` into
        # `unresolved`, and the row lost that its correction had once held.
        self.seed(row("AH-001", "fixed"))
        self.verify("AH-001", evidence(), "pass.json")
        self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "fail1.json")
        _, out, _ = self.verify("AH-001", evidence(recheck={"outcome": "fail"}), "fail2.json")
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "recurred")
        self.assertEqual([e["decision"] for e in r["verifications"]], ["verified", "recurred", "recurred"])
        self.assertEqual(r["previous_states"], ["fixed", "verified"])
        self.assertIn("state recurred -> recurred", out)
        code, out, _ = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, json.loads(out)["status"]), (1, "recurred"))

    def test_a_faulty_default_declared_after_verification_is_reported(self):
        # LESSON (AH-159): naming a faulty default on a verified row left it
        # verified with no later-run recheck, and check said nothing. The row is
        # not demoted (a metadata edit never is); check reports it.
        later = later_run_repo(self.dir)
        before = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, before)
        self.seed(row("AH-001", "fixed"), row("AH-002", "fixed", faulty_default="AGENTS.md"))
        self.verify("AH-001", evidence(), "one.json")
        self.verify("AH-002", evidence("AH-002", recheck={"later_run": later}), "two.json")
        run_cli("mark", "--id", "AH-001", "--faulty-default", "AGENTS.md", "--ledger", str(self.ledger))
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verified_without_later_run"],
                         [{"id": "AH-001", "faulty_default": "AGENTS.md", "label": "AH-001 label"}])
        code, out, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("no recurrences", out)
        self.assertIn("NOT COVERED AH-001", err)
        self.assertNotIn("AH-002", err)

    def test_check_on_a_missing_ledger_is_an_input_error(self):
        # LESSON (AH-160): a mistyped --ledger read as an empty ledger and passed.
        missing = self.dir / "typo.jsonl"
        code, out, err = run_cli("check", "--json", "--ledger", str(missing))
        self.assertEqual((code, err), (2, ""))
        result = json.loads(out)
        self.assertEqual(result["status"], "error")
        self.assertIn("no ledger file", result["reason"])
        self.assertIn("typo.jsonl", result["reason"])
        code, out, err = run_cli("check", "--ledger", str(missing))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("ERROR no ledger file", err)
        self.assertFalse(missing.exists(), "check never creates a ledger")
        code, _, _ = run_cli("open", "--label", "x", "--severity", "low", "--ledger", str(missing))
        self.assertEqual(code, 0)
        self.assertEqual(run_cli("check", "--ledger", str(missing))[0], 0, "open still starts a ledger")

    def test_mark_names_every_id_it_could_not_find(self):
        # LESSON (AH-161): `mark --id AH-002 AH-03` marked AH-002 and dropped
        # the typo without a word. The ids that match are marked, the others
        # are named, and the exit is non-zero only when none matched.
        self.seed(row("AH-001", "open"), row("AH-002", "open"))
        code, out, err = run_cli("mark", "--id", "AH-002", "AH-03", "--fixed", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertEqual((self.rows()["AH-001"]["state"], self.rows()["AH-002"]["state"]), ("open", "fixed"))
        self.assertIn("marked AH-002 as fixed", out)
        self.assertIn("no ledger entry for AH-03", err)
        self.assertNotIn("AH-002", err)
        before = self.bytes_now()
        code, _, err = run_cli("mark", "--id", "AH-9", "--fixed", "--ledger", str(self.ledger))
        self.assertNotEqual(code, 0)
        self.assertIn("AH-9", err)
        self.assertEqual(self.bytes_now(), before)

    def test_the_ledger_and_a_drafted_recheck_are_written_with_lf(self):
        # LESSON (AH-166): without newline="\n" every rewritten ledger line
        # and every drafted evidence line becomes CRLF on Windows.
        self.seed(row("AH-001", "open"), row("AH-002", "open"))
        out = self.dir / "recheck.json"
        with crlf_text_writes():
            code, _, err = run_cli("mark", "--id", "AH-001", "--fixed", "--by", "Jane Roe",
                                   "--ledger", str(self.ledger))
            self.assertEqual(code, 0, err)
            code, _, err = run_cli("draft-recheck", "AH-001", "--checker", "John Doe", "--person",
                                   "--out", str(out), "--ledger", str(self.ledger))
            self.assertEqual(code, 0, err)
        self.assertNotIn(b"\r\n", self.bytes_now())
        self.assertNotIn(b"\r\n", out.read_bytes())
        self.assertEqual(self.rows()["AH-001"]["state"], "fixed")


class CorrectionCheckedOnEveryObject(LedgerCase):
    """LESSON (2026-10-14): a correction is checked on every object the error reaches.

    Section 2's example: a table divides by the wrong denominator, the same code
    builds two more tables, and the entry stays open until the original
    comparison is rerun on each corrected table. Until this, `--affected` named
    trace records only, a recheck named one corrected object, and `decide`
    never compared it with anything: a row reaching three tables verified after
    one of them was fixed and rechecked.
    """

    FULL = ("--method", "reran the comparison of cases over patents", "--outcome", "pass",
            "--evidence-ref", "the table at the recorded hash", "--limitations", "the other columns",
            "--contrary", "none", "--adequacy", "adequate")

    def setUp(self):
        super().setUp()
        self.tables = []
        for name in ("t1.md", "t2.md", "t3.md"):
            path = self.dir / name
            path.write_text(f"cases over patents in {name}\n")
            self.tables.append(str(path))

    def open_three(self) -> str:
        args = ["open", "--label", "the share divides by firms, not patents", "--severity", "high",
                "--by", "Ana Reviewer", "--affected", "ai/records/r1"]
        for table in self.tables:
            args += ["--object", table]
        code, out, err = run_cli(*args, "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        rid = out.strip()
        code, _, err = run_cli("mark", "--id", rid, "--fixed", "--by", "Jane Roe",
                               "--note", "the shared code divides by patents", "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        return rid

    def recheck(self, rid: str, table: str | None, *extra: str, outcome: str = "pass") -> tuple[str, str]:
        out = self.dir / f"recheck-{len(self.rows()[rid].get('verifications', []))}.json"
        full = list(self.FULL)
        full[full.index("--outcome") + 1] = outcome
        args = ["draft-recheck", rid, "--checker", "Ana Reviewer", "--person", *full, *extra,
                "--out", str(out), "--ledger", str(self.ledger)]
        if table is not None:
            args += ["--object", table]
        code, draft, err = run_cli(*args)
        self.assertEqual(code, 0, err)
        code, said, err = run_cli("verify", rid, "--evidence", str(out), "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        return draft, said

    def check_json(self) -> tuple[int, dict]:
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual(err, "")
        return code, json.loads(out)

    def test_three_tables_verify_only_when_each_is_rechecked(self):
        rid = self.open_three()
        entry = self.rows()[rid]
        self.assertEqual(entry["objects"], self.tables)
        self.assertEqual(entry["affected"], ["ai/records/r1"], "--affected keeps its meaning")
        t1, t2, t3 = self.tables

        _, said = self.recheck(rid, t1)
        entry = self.rows()[rid]
        self.assertEqual(entry["state"], "fixed", "one table of three does not verify the row")
        event = entry["verifications"][-1]
        self.assertEqual((event["decision"], event["covers"]), ("recorded", t1))
        self.assertIn(f"objects   : 1 of 3 objects rechecked; waiting on {t2}, {t3}", said)
        self.assertEqual(dl.object_coverage(entry), ([t1], [t2, t3]))
        code, result = self.check_json()
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertEqual(result["partly_rechecked"], [{"id": rid, "state": "fixed", "rechecked": 1,
                                                       "objects": 3, "waiting": [t2, t3]}])
        code, out, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn(f"PARTLY RECHECKED {rid} (fixed): 1 of 3 objects rechecked", err)
        code, out, _ = run_cli("status", "--ledger", str(self.ledger))
        self.assertIn("1 of 3 objects rechecked", out)

        self.recheck(rid, t2)
        self.assertEqual(self.rows()[rid]["state"], "fixed")
        _, said = self.recheck(rid, t3)
        entry = self.rows()[rid]
        self.assertEqual(entry["state"], "verified")
        self.assertEqual([e["covers"] for e in entry["verifications"]], [t1, t2, t3])
        self.assertIn("objects   : 3 of 3 objects rechecked", said)
        self.assertIn("each of the 3 objects the row lists has its own passing recheck",
                      entry["verifications"][-1]["reasons"][0])
        code, result = self.check_json()
        self.assertEqual(result, {"status": "ok", "recurred": [], "unresolved": []})

    def test_a_failed_recheck_on_one_table_is_unresolved_and_starts_the_round_again(self):
        rid = self.open_three()
        t1, t2, t3 = self.tables
        self.recheck(rid, t1)
        self.recheck(rid, t2, outcome="fail")
        entry = self.rows()[rid]
        self.assertEqual(entry["state"], "unresolved")
        self.assertNotIn("covers", entry["verifications"][-1])
        self.assertEqual(dl.object_coverage(entry), ([], self.tables),
                         "the next correction may change every table, so each is rechecked again")
        code, result = self.check_json()
        self.assertEqual((code, result["status"]), (1, "unresolved"))
        # A new fix, and passing rechecks of two tables, leave it unresolved.
        run_cli("mark", "--id", rid, "--fixed", "--by", "Jane Roe", "--note", "second fix",
                "--ledger", str(self.ledger))
        self.recheck(rid, t2)
        self.recheck(rid, t3)
        self.assertEqual(self.rows()[rid]["state"], "unresolved")
        self.recheck(rid, t1)
        self.assertEqual(self.rows()[rid]["state"], "verified")
        self.assertEqual(self.check_json()[0], 0)

    def test_a_row_without_objects_verifies_exactly_as_before(self):
        # Back-compatibility: a historical row lists no objects, one passing
        # recheck verifies it, the event carries no coverage, and the check
        # and status print nothing new.
        self.seed(row("AH-001", "fixed"), verified_row("AH-002"))
        before = self.rows()["AH-002"]
        code, said, err = self.verify("AH-001", evidence())
        self.assertEqual(code, 0, err)
        entry = self.rows()["AH-001"]
        self.assertEqual(entry["state"], "verified")
        self.assertNotIn("objects", entry)
        self.assertTrue(all("covers" not in e for e in entry["verifications"]))
        self.assertNotIn("objects   :", said)
        self.assertEqual(self.rows()["AH-002"], before, "a historical row is never re-stated")
        self.assertEqual(dl.object_coverage(entry), ([], []))
        code, result = self.check_json()
        self.assertEqual(result, {"status": "ok", "recurred": [], "unresolved": []})
        code, out, _ = run_cli("status", "--ledger", str(self.ledger))
        self.assertNotIn("objects", out)
        self.assertEqual(dl.decide("fixed", evidence()), dl.decide("fixed", evidence(), "", [], set()))

    def test_objects_added_to_a_verified_row_are_reported_and_the_state_stays(self):
        self.seed(verified_row("AH-001"))
        code, _, err = run_cli("mark", "--id", "AH-001", "--object", "tables/t1.md", "--object",
                               "tables/t2.md", "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        entry = self.rows()["AH-001"]
        self.assertEqual((entry["state"], entry["objects"]), ("verified", ["tables/t1.md", "tables/t2.md"]))
        code, result = self.check_json()
        self.assertEqual(code, 0)
        self.assertEqual(result["verified_without_every_object"],
                         [{"id": "AH-001", "objects": 2, "waiting": ["tables/t1.md", "tables/t2.md"]}])
        _, _, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertIn("NOT COVERED AH-001: verified, and no passing recheck", err)

    def test_a_draft_on_several_objects_names_one_or_reads_not_stated(self):
        rid = self.open_three()
        draft, said = self.recheck(rid, None)
        self.assertIn("name the one rechecked with --object", draft)
        entry = self.rows()[rid]
        event = entry["verifications"][-1]
        self.assertEqual(event["evidence"]["target"]["corrected_object"], "not stated")
        self.assertEqual((entry["state"], event["decision"]), ("fixed", "recorded"))
        self.assertNotIn("covers", event)
        self.assertIn("the recheck names none of them", " ".join(event["reasons"]))
        # One listed object is supplied by the draft.
        code, _, err = run_cli("mark", "--id", rid, "--object", self.tables[0], "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.recheck(rid, None)
        self.assertEqual(self.rows()[rid]["state"], "verified")

    def test_an_object_the_row_does_not_list_covers_nothing(self):
        rid = self.open_three()
        other = self.dir / "t9.md"
        other.write_text("another table\n")
        self.recheck(rid, str(other))
        event = self.rows()[rid]["verifications"][-1]
        self.assertEqual(event["decision"], "recorded")
        self.assertNotIn("covers", event)
        self.assertIn("is not one of the objects the row lists", " ".join(event["reasons"]))

    def test_an_inadequate_pass_covers_nothing(self):
        rid = self.open_three()
        self.recheck(rid, self.tables[0], "--adequacy", "unknown")
        entry = self.rows()[rid]
        self.assertNotIn("covers", entry["verifications"][-1])
        self.assertEqual(dl.object_coverage(entry)[0], [])

    def test_names_are_compared_with_slashes_unified_and_no_leading_dot(self):
        self.seed(row("AH-001", "fixed", objects=["tables/t1.md", "tables/t2.md"]))
        ev = evidence(target={"corrected_object": "./tables\\t2.md"})
        self.verify("AH-001", ev)
        event = self.rows()["AH-001"]["verifications"][-1]
        self.assertEqual(event["covers"], "tables/t2.md")
        self.verify("AH-001", evidence(target={"corrected_object": "Tables/t1.md"}), "case.json")
        self.assertNotIn("covers", self.rows()["AH-001"]["verifications"][-1], "no guessing past the spelling")

    def test_a_sweep_that_raises_the_finding_again_starts_the_round_again(self):
        rid = self.open_three()
        t1, t2, t3 = self.tables
        self.recheck(rid, t1)
        matches = self.dir / "matches.json"
        matches.write_text(json.dumps({"matches": [{"n": 0, "id": rid}]}))
        code, _, err = run_cli("ingest", "--run", self.run_dir(), "--matches", str(matches),
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()[rid]["state"], "recurred")
        self.assertEqual(dl.object_coverage(self.rows()[rid])[0], [])
        self.recheck(rid, t2)
        self.recheck(rid, t3)
        self.assertEqual(self.rows()[rid]["state"], "recurred", "t1 was rechecked before the recurrence")
        self.recheck(rid, t1)
        self.assertEqual(self.rows()[rid]["state"], "verified")

    def test_a_recurrence_marked_fixed_again_still_starts_the_round_again(self):
        # LESSON (rc22 release review, bb872f3 F1): the round restarted only
        # when the next event's state_before read recurred, so a sweep's
        # recurrence followed by `mark --fixed` was invisible to it, and the
        # row verified on a recheck of t1 made before the recurrence and before
        # the correction that followed it.
        rid = self.open_three()
        t1, t2, t3 = self.tables
        self.recheck(rid, t1)
        matches = self.dir / "matches.json"
        matches.write_text(json.dumps({"matches": [{"n": 0, "id": rid}]}))
        code, _, err = run_cli("ingest", "--run", self.run_dir(), "--matches", str(matches),
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()[rid]["recurrences"],
                         [{"after_events": 1, "run": "deep-20260907", "date": datetime.date.today().isoformat()}])
        code, _, err = run_cli("mark", "--id", rid, "--fixed", "--by", "Jane Roe", "--note", "second fix",
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        entry = self.rows()[rid]
        self.assertEqual(entry["state"], "fixed")
        self.assertEqual(dl.object_coverage(entry), ([], self.tables))
        self.recheck(rid, t2)
        self.recheck(rid, t3)
        entry = self.rows()[rid]
        self.assertEqual(entry["state"], "fixed", "t1 was rechecked before the recurrence and the second fix")
        self.assertEqual(dl.object_coverage(entry), ([t2, t3], [t1]))
        self.recheck(rid, t1)
        self.assertEqual(self.rows()[rid]["state"], "verified")

    def test_a_recurrence_said_by_hand_or_written_before_the_marker_restarts_the_round_too(self):
        # `mark --state recurred` is a recurrence said by hand, and a row that
        # recurred before rc22 carries no marker; either way the next fixed
        # claim leaves the earlier coverage behind.
        events = [{"outcome": "pass", "decision": "recorded", "state_before": "fixed",
                   "state_after": "fixed", "covers": "a.md"}]
        self.seed(row("AH-001", "fixed", objects=["a.md", "b.md"], verifications=copy.deepcopy(events)),
                  row("AH-002", "recurred", objects=["a.md", "b.md"], previous_states=["fixed"],
                      verifications=copy.deepcopy(events)))
        code, _, err = run_cli("mark", "--id", "AH-001", "--state", "recurred", "--by", "Ana Reviewer",
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows()["AH-001"]["recurrences"][0]["after_events"], 1)
        self.assertEqual(self.rows()["AH-001"]["recurrences"][0]["by"], "Ana Reviewer")
        for rid in ("AH-001", "AH-002"):
            code, _, err = run_cli("mark", "--id", rid, "--fixed", "--by", "Jane Roe", "--note", "again",
                                   "--ledger", str(self.ledger))
            self.assertEqual(code, 0, err)
            self.assertEqual(dl.object_coverage(self.rows()[rid]), ([], ["a.md", "b.md"]), rid)
            self.verify(rid, evidence(rid, target={"corrected_object": "b.md"}), f"{rid}.json")
            self.assertEqual(self.rows()[rid]["state"], "fixed", f"{rid}: a.md was rechecked before the recurrence")

    def test_an_object_that_is_not_a_file_verifies_on_the_hash_the_checker_states(self):
        # LESSON (rc22 release review, bb872f3 F2): the shipped text invites a
        # table or a section as an object, and draft-recheck hashed only a file
        # in the tree, so such an object's hash read `not stated` and no
        # drafted recheck of it could verify. The checker now states the hash
        # with --object-hash: the commit of the correction, or the sha256 of
        # the file that builds the object.
        self.seed(row("AH-001", "fixed", objects=["Table 2", "Section 4.1"], fixed_by="Jane Roe",
                      fix_note="the shared code divides by patents"))
        draft, _ = self.recheck("AH-001", "Table 2")
        self.assertIn("object is not a file in the tree: state its hash with --object-hash", draft)
        event = self.rows()["AH-001"]["verifications"][-1]
        self.assertEqual(event["evidence"]["target"]["object_hash"], "not stated")
        self.assertNotIn("covers", event)
        sha = "3f2a9c1"
        _, said = self.recheck("AH-001", "Table 2", "--object-hash", sha)
        event = self.rows()["AH-001"]["verifications"][-1]
        self.assertEqual(event["evidence"]["target"]["object_hash"], sha)
        self.assertEqual(event["evidence"]["target"]["object_hash_basis"], "stated by the checker")
        self.assertEqual(event["covers"], "Table 2")
        self.recheck("AH-001", "Section 4.1", "--object-hash", "b" * 64)
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")
        # A file in the tree is still hashed from its content.
        self.seed(row("AH-002", "fixed", objects=[self.tables[0]], fixed_by="Jane Roe", fix_note="fix"))
        self.recheck("AH-002", None)
        target = self.rows()["AH-002"]["verifications"][-1]["evidence"]["target"]
        self.assertEqual(target["object_hash"], hashlib.sha256(pathlib.Path(self.tables[0]).read_bytes()).hexdigest())
        self.assertEqual(target["object_hash_basis"], "sha256 of the file's content")


class HandClosureStaysVisible(LedgerCase):
    """LESSON (2026-10-14 review of TRACE-OS against the frozen principles): `mark
    --state closed` by hand passed `check`, and the queue's E line listed fixed
    rows only, so a defect closed by hand with no recheck vanished from what E
    waits on. `closed` is what a later sweep observes; a closure said by hand is
    now a repair claim, and the row stays visible, as information.
    """

    def test_a_closure_by_hand_is_a_repair_claim_and_check_reports_it(self):
        self.seed(row("AH-001", "fixed"))
        code, out, _ = run_cli("mark", "--id", "AH-001", "--state", "closed", "--by", "Jane Roe",
                               "--note", "looks fine now", "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("recorded closure by hand as a repair claim", out)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "closed")
        self.assertEqual((r["repair_claims"][-1]["state"], r["repair_claims"][-1]["by"],
                          r["repair_claims"][-1]["note"]), ("closed", "Jane Roe", "looks fine now"))
        self.assertEqual(r["runs"][-1], "manual:Jane Roe:closed")
        self.assertEqual(dl.closure_basis(r), "hand")
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["closed_without_recheck"],
                         [{"id": "AH-001", "basis": "hand", "by": "Jane Roe"}])
        code, out, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertEqual(code, 0, "information, never a failure")
        self.assertIn("no recurrences", out)
        self.assertIn("CLOSED WITHOUT RECHECK AH-001: closed by hand by Jane Roe", err)

    def test_a_sweep_closure_is_not_listed_and_a_later_closure_by_hand_is(self):
        self.seed(row("AH-001", "fixed"))
        with mock.patch.object(dl, "key", lambda: "test"), \
                mock.patch.object(dl, "match", lambda known, found, tok: {}):
            run_cli("ingest", "--run", self.run_dir(), "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], dl.closure_basis(r)), ("closed", "sweep"))
        self.assertFalse(dl.closed_without_recheck(r))
        self.assertNotIn("closed_without_recheck",
                         json.loads(run_cli("check", "--json", "--ledger", str(self.ledger))[1]))
        run_cli("mark", "--id", "AH-001", "--state", "open", "--ledger", str(self.ledger))
        run_cli("mark", "--id", "AH-001", "--state", "closed", "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertEqual(dl.closure_basis(r), "hand", "the latest closure is the one said by hand")
        self.assertEqual(r["repair_claims"][-1]["by"], "not stated")
        self.assertTrue(dl.closed_without_recheck(r))

    def test_a_historical_closed_row_with_no_sweep_marker_is_reported_and_keeps_its_state(self):
        self.seed(row("AH-001", "closed", runs=["deep-20260827", "fable-closure-20260907:targeted"]))
        before = self.bytes_now()
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["closed_without_recheck"],
                         [{"id": "AH-001", "basis": "not stated", "by": "not stated"}])
        self.assertEqual(self.bytes_now(), before, "check never rewrites a row")
        _, _, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertIn("CLOSED WITHOUT RECHECK AH-001: closed other than by a sweep", err)

    def test_a_passing_recheck_takes_a_hand_closed_row_off_the_list(self):
        self.seed(row("AH-001", "fixed"))
        run_cli("mark", "--id", "AH-001", "--state", "closed", "--ledger", str(self.ledger))
        self.verify("AH-001", evidence())
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        self.assertFalse(dl.closed_without_recheck(r))
        self.assertNotIn("closed_without_recheck",
                         json.loads(run_cli("check", "--json", "--ledger", str(self.ledger))[1]))

    def test_closing_an_already_closed_row_adds_no_claim(self):
        self.seed(row("AH-001", "closed", runs=["deep-20260827", "deep-20260901:absent"]))
        run_cli("mark", "--id", "AH-001", "--state", "closed", "--ledger", str(self.ledger))
        r = self.rows()["AH-001"]
        self.assertNotIn("repair_claims", r)
        self.assertEqual(dl.closure_basis(r), "sweep")


class LaterRunResolves(LedgerCase):
    """LESSON (2026-10-14 review): a later run named `ai/records/NO-SUCH-RECORD`
    under version `deadbeef` verified a corrected default, because only the
    presence of the two strings was checked. From rc22 the record has to exist
    under ai/records/ and the version has to name a commit, or the content of the
    instruction at one, that changes the faulty default.
    """

    def setUp(self):
        super().setUp()
        self.later = later_run_repo(self.dir)
        before = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, before)
        self.seed(row("AH-001", "fixed", faulty_default="AGENTS.md"))

    def assertRecordedBecause(self, later, text):
        code, out, err = self.verify("AH-001", evidence(recheck={"later_run": later}))
        self.assertEqual(code, 0, err)
        r = self.rows()["AH-001"]
        self.assertEqual((r["state"], r["verifications"][-1]["decision"]), ("fixed", "recorded"))
        self.assertIn(text, out)
        self.assertTrue(r["verifications"][-1]["later_run_resolution"]["problems"])
        return r

    def test_a_record_that_does_not_exist_does_not_verify(self):
        r = self.assertRecordedBecause(dict(self.later, record="ai/records/NO-SUCH-RECORD"),
                                       "names no trace record in this repository")
        self.assertIsNone(r["verifications"][-1]["later_run_resolution"]["record"])
        self.assertTrue(r["verifications"][-1]["later_run_resolution"]["commit"])

    def test_a_path_outside_ai_records_is_not_a_trace_record(self):
        (self.dir / "sources").mkdir()
        (self.dir / "sources" / "run.json").write_text("{}")
        self.assertRecordedBecause(dict(self.later, record="sources/run.json, a synthetic run"),
                                   "names no trace record")

    def test_a_version_naming_no_commit_does_not_verify(self):
        self.assertRecordedBecause(dict(self.later, instruction_version="deadbeef"), "names no commit")

    def test_a_commit_that_does_not_change_the_default_does_not_verify(self):
        (self.dir / "other.md").write_text("x\n")
        git_in(self.dir, "add", "other.md")
        git_in(self.dir, "commit", "-qm", "another file")
        other = git_in(self.dir, "rev-parse", "HEAD")
        self.assertRecordedBecause(dict(self.later, instruction_version=other),
                                   "which does not change the faulty default AGENTS.md")

    def test_a_default_git_never_tracked_does_not_verify(self):
        self.seed(row("AH-001", "fixed", faulty_default="the reviewer's default temperature"))
        self.assertRecordedBecause(self.later, "names no file Git has tracked here")

    def test_a_resolving_later_run_verifies_and_the_event_keeps_what_it_resolved_to(self):
        code, out, _ = self.verify("AH-001", evidence(recheck={"later_run": self.later}))
        self.assertEqual(code, 0)
        r = self.rows()["AH-001"]
        self.assertEqual(r["state"], "verified")
        found = r["verifications"][-1]["later_run_resolution"]
        self.assertEqual((found["record"], found["commit"], found["problems"]),
                         ("ai/records/20260910-section-4-redraft", git_in(self.dir, "rev-parse", "HEAD"), []))
        self.assertIn("commit", r["verifications"][-1]["reasons"][0])

    def test_a_bare_record_id_a_line_number_and_a_content_digest_resolve(self):
        digest = hashlib.sha256(b"the revised rule\n").hexdigest()
        self.seed(row("AH-001", "fixed", faulty_default="AGENTS.md:1"))
        later = {"record": "20260910-section-4-redraft", "instruction_version": f"sha256:{digest}"}
        self.verify("AH-001", evidence(recheck={"later_run": later}))
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")

    def test_a_historical_row_written_under_the_old_spelling_keeps_its_state_and_is_reported(self):
        later = {"contribution": "sources/coding/rehearsal.json, a synthetic run",
                 "instruction_version": "AGENTS.md at commit deadbeef"}
        self.seed(row("AH-001", "verified", faulty_default="AGENTS.md", previous_states=["fixed"],
                      verifications=[{"outcome": "pass", "decision": "verified", "state_before": "fixed",
                                      "state_after": "verified",
                                      "evidence": {"recheck": {"later_run": later}}}]))
        before = self.bytes_now()
        code, out, err = run_cli("check", "--json", "--ledger", str(self.ledger))
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["status"], "ok")
        gone = result["verified_on_a_later_run_that_does_not_resolve"]
        self.assertEqual([g["id"] for g in gone], ["AH-001"])
        self.assertEqual(len(gone[0]["problems"]), 2)
        self.assertNotIn("verified_without_later_run", result)
        self.assertEqual(self.bytes_now(), before)
        self.assertEqual(self.rows()["AH-001"]["state"], "verified")
        _, _, err = run_cli("check", "--ledger", str(self.ledger))
        self.assertIn("LATER RUN UNRESOLVED AH-001", err)
        self.assertIn("the row keeps its state", err)

    def test_the_draft_says_what_the_later_run_does_not_resolve_to(self):
        obj = self.dir / "object.txt"
        obj.write_text("the corrected text\n")
        self.seed(row("AH-001", "fixed", faulty_default="AGENTS.md", sources=[str(obj)],
                      fixed_by="Jane Roe", fix_note="rewrote it"))
        code, out, _ = run_cli("draft-recheck", "AH-001", "--checker", "John Doe", "--person",
                               "--method", "m", "--outcome", "pass", "--evidence-ref", "e",
                               "--limitations", "l", "--contrary", "none", "--adequacy", "adequate",
                               "--later-run", "ai/records/NO-SUCH-RECORD",
                               "--instruction-version", "deadbeef",
                               "--out", str(self.dir / "draft.json"), "--ledger", str(self.ledger))
        self.assertEqual(code, 0)
        self.assertIn("recording it would read: recorded, state fixed -> fixed", out)
        self.assertIn("names no trace record", out)


class AgenticReviewFindingsEnterTheLedger(LedgerCase):
    """LESSON (2026-10-14 review): `deep_review.py` does not ship, so `ingest`
    had nothing to read in an installed project, and an agentic review's
    supported findings got a row only if someone typed `open` for each. The
    tracked `ai/reviews/<run>/` directory now ingests, finding by finding.
    """

    def setUp(self):
        super().setUp()
        before = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, before)
        (self.dir / "paper").mkdir()
        (self.dir / "paper" / "s.tex").write_text("The panel agreed on every claim.\n")

    def review(self, findings=None, withheld=False, name="20261014-abc1234-codex"):
        d = self.dir / "ai" / "reviews" / name
        d.mkdir(parents=True)
        (d / "report.md").write_text("# Agentic review\n")
        if withheld:
            return str(d)
        findings = findings if findings is not None else [
            {"id": "F1", "severity": "low", "claim": "a stale comment", "file": "paper/s.tex",
             "line_start": 1, "line_end": 1, "quote": "no such span", "why": "w", "ask": "a"},
            {"id": "F2", "severity": "high", "claim": "the agreement claim is not supported",
             "file": "paper/s.tex", "line_start": 1, "line_end": 1,
             "quote": "The panel agreed on every claim.", "why": "w", "ask": "a"}]
        (d / "findings.json").write_text(json.dumps({"revision": "abc1234", "findings": findings}))
        (d / "check-row.json").write_text(json.dumps({
            "runner": "scripts/agentic_review.py, reviewer gpt-6-astra via codex (identity observed) "
                      "with tools in a packet"}))
        return str(d)

    def ingest(self, run, *extra):
        def no_model(*_):
            raise AssertionError("an agentic review is never matched by a model")
        with mock.patch.object(dl, "match", no_model), mock.patch.object(dl, "key", no_model):
            return run_cli("ingest", "--run", run, *extra, "--ledger", str(self.ledger))

    def matches(self, pairs):
        p = self.dir / "matches.json"
        p.write_text(json.dumps({"matches": [{"n": n, "id": i} for n, i in pairs]}))
        return str(p)

    def test_each_named_finding_becomes_an_open_row_with_its_source_and_reviewer(self):
        run = self.review()
        code, out, err = self.ingest(run, "--finding", "F2")
        self.assertEqual(code, 0, err)
        rows = self.rows()
        self.assertEqual(list(rows), ["DEF-001"])
        r = rows["DEF-001"]
        self.assertEqual((r["label"], r["severity"], r["state"], r["quote"]),
                         ("the agreement claim is not supported", "high", "open",
                          "The panel agreed on every claim."))
        self.assertEqual((r["sources"], r["review"], r["review_finding"], r["opened_by"]),
                         (["paper/s.tex"], run, "F2", "gpt-6-astra"))
        self.assertEqual(r["anchor_at_open"], "grounded")
        self.assertEqual(r["runs"], ["20261014-abc1234-codex"])
        self.assertIn("F2 -> DEF-001", out)
        self.assertIn("not ingested: 1 finding(s)", out)
        self.assertIn("closed nothing by absence", out)

    def test_without_a_finding_named_every_one_is_recorded(self):
        code, _, err = self.ingest(self.review())
        self.assertEqual(code, 0, err)
        rows = self.rows()
        self.assertEqual([r["review_finding"] for r in rows.values()], ["F1", "F2"])
        self.assertEqual(rows["DEF-001"]["anchor_at_open"], "invented")

    def test_a_review_closes_nothing_by_absence_and_asks_for_explicit_matches(self):
        self.seed(row("AH-001", "fixed"))
        run = self.review()
        before = self.bytes_now()
        code, _, err = self.ingest(run, "--finding", "F2")
        self.assertEqual(code, 2)
        self.assertIn("--matches", err)
        self.assertEqual(self.bytes_now(), before)
        code, _, err = self.ingest(run, "--finding", "F2", "--matches", self.matches([]))
        self.assertEqual(code, 0, err)
        rows = self.rows()
        self.assertEqual(rows["AH-001"]["state"], "fixed", "one candidate's review closes nothing")
        self.assertEqual(rows["AH-002"]["review_finding"], "F2")

    def test_a_finding_matched_to_a_fixed_row_recurs(self):
        self.seed(row("AH-001", "fixed"))
        code, out, _ = self.ingest(self.review(), "--finding", "F2", "--matches", self.matches([(1, "AH-001")]))
        self.assertEqual(code, 0)
        self.assertEqual(self.rows()["AH-001"]["state"], "recurred")
        self.assertIn("F2 -> AH-001", out)
        self.assertIn("RECURRED: AH-001", out)

    def test_a_match_on_a_finding_not_selected_is_refused(self):
        self.seed(row("AH-001", "fixed"))
        before = self.bytes_now()
        code, _, err = self.ingest(self.review(), "--finding", "F2", "--matches", self.matches([(0, "AH-001")]))
        self.assertEqual(code, 2)
        self.assertIn("did not select", err)
        self.assertEqual(self.bytes_now(), before)

    def test_an_unknown_finding_id_and_a_withheld_review_write_nothing(self):
        code, _, err = self.ingest(self.review(), "--finding", "F9")
        self.assertEqual(code, 2)
        self.assertIn("F9", err)
        self.assertFalse(self.ledger.exists())
        code, _, err = self.ingest(self.review(withheld=True, name="20261014-def5678-claude"))
        self.assertEqual(code, 2)
        self.assertIn("withheld", err)
        self.assertIn("defect_ledger.py open", err)
        self.assertFalse(self.ledger.exists())

    def test_a_deep_review_run_is_ingested_whole(self):
        code, _, err = run_cli("ingest", "--run", self.run_dir(), "--finding", "F1",
                               "--ledger", str(self.ledger))
        self.assertEqual(code, 2)
        self.assertIn("ingested whole", err)



class TheIdPrefixIsAProjectSetting(LedgerCase):
    """LESSON (rc22, Stage 2): the id prefix was hard-coded as AH, this repository's
    own, so a fresh project's first row read AH-001. The prefix is `ledger_prefix`
    in the project.json beside the ledger; without it a ledger keeps the prefix of
    its rows, and a ledger with no rows gets DEF."""

    def open(self, label="x"):
        return run_cli("open", "--label", label, "--severity", "low", "--ledger", str(self.ledger))

    def configure(self, value) -> None:
        (self.dir / "project.json").write_text(json.dumps({"schema_version": 1, "ledger_prefix": value}))

    def test_a_ledger_with_rows_keeps_their_prefix(self):
        self.seed(row("AH-001", "closed"), row("AH-007", "open"))
        self.assertEqual(self.open()[1].strip(), "AH-008")

    def test_a_fresh_ledger_gets_def(self):
        self.assertEqual(dl.FRESH_PREFIX, "DEF")
        self.assertEqual(self.open()[1].strip(), "DEF-001")
        self.assertEqual(self.open()[1].strip(), "DEF-002")

    def test_the_setting_wins_and_the_number_runs_on_over_every_row(self):
        self.seed(row("AH-001", "closed"), row("AH-002", "open"))
        self.configure("ip")
        self.assertEqual(self.open()[1].strip(), "IP-003")
        self.assertEqual(set(self.rows()), {"AH-001", "AH-002", "IP-003"})
        self.assertEqual(run_cli("check", "--ledger", str(self.ledger))[0], 0)

    def test_a_setting_that_cannot_be_an_id_is_reported_and_not_used(self):
        self.configure("not-a prefix")
        code, out, err = self.open()
        self.assertEqual((code, out.strip()), (0, "DEF-001"))
        self.assertIn("ledger_prefix", err)

    def test_a_ledger_elsewhere_does_not_take_the_projects_setting(self):
        # The default ai/defects.jsonl reads ai/project.json; a temporary ledger
        # reads the folder it is in, so a test never takes a project's prefix.
        self.assertEqual(dl.configured_prefix(self.ledger), None)
        self.configure("XY")
        self.assertEqual(dl.configured_prefix(self.ledger), "XY")


if __name__ == "__main__":
    unittest.main()
