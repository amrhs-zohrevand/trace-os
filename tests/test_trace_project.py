"""New projects must use their own history, and offline ingestion must stay offline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import trace_record as hr
import defect_ledger as dl


class ProjectEpochs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        before = Path.cwd(); os.chdir(self.root); self.addCleanup(os.chdir, before)
        self.env = dict(os.environ, GIT_AUTHOR_NAME='Test Researcher', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test Researcher', GIT_COMMITTER_EMAIL='test@example.invalid')
        self.git('init', '-q')
        (self.root / 'example.txt').write_text('initial')
        self.git('add', 'example.txt'); self.git('commit', '-qm', 'initial fixture')
        self.epoch = self.git('rev-parse', 'HEAD')
        (self.root / 'ai').mkdir()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, env=self.env, text=True).strip()

    def config(self, **kwargs):
        data = {'schema_version': 1, 'policy_epoch': self.epoch, 'accept_epoch': self.epoch}
        data.update(kwargs)
        (self.root / 'ai/project.json').write_text(json.dumps(data))

    def test_project_epochs_override_foreign_research_defaults(self):
        self.config()
        self.assertEqual(hr.reviewable_commits(), [])
        self.assertTrue(hr.is_exempt(self.epoch))
        (self.root / 'example.txt').write_text('new')
        self.git('add', 'example.txt')
        self.git('commit', '-qm', 'fixture change\n\nExecution-agent: test-agent')
        new = self.git('rev-parse', 'HEAD')
        self.assertEqual(hr.reviewable_commits(), [new])
        self.assertFalse(hr.is_exempt(new))

    def test_missing_invalid_and_foreign_epochs_do_not_become_zero_reviews(self):
        for value in (None, '', 'not-a-commit', 'a' * 40):
            with self.subTest(value=value):
                self.config(accept_epoch=value)
                with self.assertRaises(ValueError): hr.reviewable_commits()

    def test_uninitialized_starter_has_no_fallback_history(self):
        with mock.patch.object(hr, 'ACCEPT_EPOCH', None), self.assertRaises(ValueError):
            hr.reviewable_commits()

    def test_invalid_project_schema_is_reported(self):
        (self.root / 'ai/project.json').write_text('[]')
        with self.assertRaises(ValueError): hr.project_config()


class OfflineMatching(unittest.TestCase):
    def test_explicit_mapping_is_validated_before_ingestion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'matches.json'
            known = [{'id': 'AH-001'}]
            path.write_text(json.dumps({'matches': [{'n': 0, 'id': 'AH-001'}]}))
            self.assertEqual(dl.supplied_matches(str(path), known, 1), {0: 'AH-001'})
            for entries in ([{'n': True, 'id': 'AH-001'}], [{'n': 1, 'id': 'AH-001'}],
                            [{'n': 0, 'id': 'AH-999'}], [{'n': 0, 'id': 'AH-001'}] * 2):
                path.write_text(json.dumps({'matches': entries}))
                with self.assertRaises(ValueError): dl.supplied_matches(str(path), known, 1)

    def test_cli_ingest_uses_supplied_matches_without_loading_a_model(self):
        from types import SimpleNamespace
        import contextlib, io
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger = root / 'ledger.jsonl'
            ledger.write_text(json.dumps({'id':'AH-001','state':'fixed','label':'old','quote':'text',
                                           'severity':'high','runs':[]}) + '\n')
            run = root / 'run'; run.mkdir()
            (run / 'defects.json').write_text(json.dumps({'issues':[{'n':1,'quote':'text'}],
                                                        'defects':[{'label':'old','members':[1]}]}))
            matches = root / 'matches.json'
            matches.write_text('{"matches":[{"n":0,"id":"AH-001"}]}')
            args = SimpleNamespace(run=str(run), ledger=str(ledger), matches=str(matches))
            with mock.patch.object(dl, 'key', side_effect=AssertionError('network path used')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dl.cmd_ingest(args), 0)
            self.assertEqual(dl.load(ledger)[0]['state'], 'recurred')
            before = ledger.read_bytes()
            matches.write_text('{"matches":[{"n":3,"id":"AH-001"}]}')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dl.cmd_ingest(args), 2)
            self.assertEqual(ledger.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
