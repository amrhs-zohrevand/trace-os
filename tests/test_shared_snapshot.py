"""Reject incomplete or ambiguous local data without changing the selected snapshot."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import shared_snapshot as ss


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.store = self.root / 'input', self.root / 'store'
        self.source.mkdir()
        (self.source / 'a.txt').write_bytes(b'first')
        (self.source / 'b.txt').write_bytes(b'second')
        self.manifest = ss.create_manifest(self.source, 'synthetic')

    def seed(self):
        result = ss.materialize(self.source, self.manifest, self.store)
        self.before = (self.store / 'current.json').read_bytes()
        return result

    def preserved(self):
        self.assertEqual((self.store / 'current.json').read_bytes(), self.before)

    def test_unreadable_subtree_cannot_disappear_from_inventory(self):
        selected = self.seed()
        hidden = self.source / 'unreadable'
        hidden.mkdir()
        (hidden / 'missing.txt').write_text('must be inventoried')
        real = ss.os.scandir
        def denied(path):
            if Path(path).name == 'unreadable':
                raise PermissionError('fixture: unreadable subtree')
            return real(path)
        with mock.patch.object(ss.os, 'scandir', side_effect=denied):
            with self.assertRaises(PermissionError): ss.create_manifest(self.source)
            with self.assertRaises(PermissionError): ss.materialize(self.source, self.manifest, self.store)
            self.preserved()
            extra = Path(selected['data']) / 'unreadable'
            extra.mkdir()
            (extra / 'unlisted.txt').write_text('extra bytes')
            with self.assertRaises(PermissionError): ss.verify_store(self.store)

    def test_complete_copy_is_repeatable_and_verifies_bytes(self):
        first = self.seed()
        second = ss.materialize(self.source, self.manifest, self.store)
        self.assertEqual(first, second)
        self.assertEqual(ss.verify_store(self.store), first)
        self.assertEqual((Path(first['data']) / 'a.txt').read_bytes(), b'first')
        self.assertEqual(len(list((self.store / 'snapshots').iterdir())), 1)

    def test_changed_source_keeps_old_selection(self):
        self.seed()
        (self.source / 'a.txt').write_bytes(b'wrong')
        with self.assertRaises(ss.SnapshotError):
            ss.materialize(self.source, self.manifest, self.store)
        self.preserved()

    def test_partial_copy_keeps_old_selection_and_removes_only_own_staging(self):
        self.seed()
        (self.source / 'a.txt').write_bytes(b'next')
        manifest = ss.create_manifest(self.source)
        real = ss.shutil.copyfile
        def interrupted(source, target):
            if target.name == 'b.txt':
                raise OSError('simulated interrupted copy')
            return real(source, target)
        with mock.patch.object(ss.shutil, 'copyfile', side_effect=interrupted), self.assertRaises(OSError):
            ss.materialize(self.source, manifest, self.store)
        self.preserved()
        self.assertFalse(list(self.store.glob('.staging-*')))
        self.assertFalse((self.store / '.snapshot.lock').exists())

    def test_competing_writer_is_not_unlocked(self):
        self.seed()
        lock = self.store / '.snapshot.lock'
        lock.write_text('another writer')
        with self.assertRaises(ss.SnapshotError):
            ss.materialize(self.source, self.manifest, self.store)
        self.preserved()
        self.assertEqual(lock.read_text(), 'another writer')

    def test_paths_reject_traversal_absolute_reserved_and_colliding_names(self):
        for name in ('../escape', '/absolute', 'CON.txt', 'a./x', 'a /x', 'a\\x', 'C:/x'):
            manifest = copy.deepcopy(self.manifest)
            manifest['files'][0]['path'] = name
            with self.subTest(name=name), self.assertRaises(ss.SnapshotError):
                ss.validate(manifest)
        for paths in (['A/file', 'a/other'], ['same', 'same'], ['a', 'a/file']):
            with self.subTest(paths=paths), self.assertRaises(ss.SnapshotError):
                ss.paths_ok(paths)

    def test_bad_counts_and_empty_manifest_fail(self):
        for value in (True, -1, '5'):
            manifest = copy.deepcopy(self.manifest)
            manifest['files'][0]['size'] = value
            with self.assertRaises(ss.SnapshotError):
                ss.validate(manifest)
        with self.assertRaises(ss.SnapshotError):
            ss.validate({'schema_version': 1, 'dataset': 'x', 'files': []})

    def test_source_and_store_symlinks_are_rejected(self):
        self.seed()
        try:
            (self.source / 'outside').symlink_to(self.root / 'elsewhere')
        except OSError as exc:
            self.skipTest('symlink creation is unavailable: ' + str(exc))
        with self.assertRaises(ss.SnapshotError):
            ss.materialize(self.source, self.manifest, self.store)
        self.preserved()
        alias = self.root / 'alias'
        alias.symlink_to(self.store)
        with self.assertRaises(ss.SnapshotError):
            ss.verify_store(alias)

    def test_bad_pointer_cannot_escape_store(self):
        self.seed()
        (self.store / 'current.json').write_text(json.dumps({'schema_version': 1, 'snapshot': '../outside'}))
        with self.assertRaises(ss.SnapshotError):
            ss.verify_store(self.store)

    def test_existing_corrupt_snapshot_is_not_replaced(self):
        result = self.seed()
        changed = Path(result['data']) / 'a.txt'
        changed.write_bytes(b'corrupt')
        with self.assertRaises(ss.SnapshotError):
            ss.materialize(self.source, self.manifest, self.store)
        self.preserved()
        self.assertEqual(changed.read_bytes(), b'corrupt')
        with self.assertRaises(ss.SnapshotError):
            ss.verify_store(self.store)

    def test_missing_or_extra_files_are_not_hidden(self):
        self.seed()
        (self.source / 'extra.txt').write_text('extra')
        with self.assertRaises(ss.SnapshotError):
            ss.materialize(self.source, self.manifest, self.store)
        self.preserved()

    def test_manifest_cli_does_not_write_into_source_or_overwrite(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ss.main(['manifest', '--source', str(self.source), '--dataset', 'x',
                                      '--out', str(self.source / 'manifest.json')]), 2)
            output = self.root / 'manifest.json'
            args = ['manifest', '--source', str(self.source), '--dataset', 'x', '--out', str(output)]
            self.assertEqual(ss.main(args), 0)
            before = output.read_bytes()
            self.assertEqual(ss.main(args), 2)
            self.assertEqual(output.read_bytes(), before)
        self.assertFalse((self.source / 'manifest.json').exists())

    def test_nested_store_is_rejected(self):
        with self.assertRaises(ss.SnapshotError):
            ss.materialize(self.source, self.manifest, self.source / 'store')


if __name__ == '__main__':
    unittest.main()
