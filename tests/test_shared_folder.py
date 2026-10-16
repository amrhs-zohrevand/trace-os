"""Connect a project to a shared folder without losing, copying into Git or overwriting a file."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import shared_folder as sf

GIT = shutil.which('git')


def can_symlink(where: Path) -> bool:
    probe = where / 'probe-link'
    try:
        os.symlink(where, probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        return os.name == 'nt'  # a junction stands in for the symlink there
    probe.unlink()
    return True


class SharedFolderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        if not can_symlink(base):
            self.skipTest('this machine cannot make a directory link')
        self.project = base / 'project'
        self.cloud = base / 'cloud' / 'SURFdrive' / 'Team folder'
        (self.project / 'workspace' / 'shared').mkdir(parents=True)
        (self.project / 'ai').mkdir()
        self.cloud.mkdir(parents=True)
        # The rule the starter ships: it names the folder, and Git treats a link as a file.
        (self.project / '.gitignore').write_text('workspace/shared/\n', encoding='utf-8')
        if GIT:
            subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.link = self.project / 'workspace' / 'shared'

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sf.main(['--project', str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def test_empty_shared_path_becomes_a_link(self):
        result = sf.connect(self.project, str(self.cloud))
        self.assertEqual(result['state'], 'connected')
        self.assertTrue(sf.is_link(self.link))
        self.assertEqual(sf.link_target(self.link).resolve(), self.cloud)
        self.assertEqual(result['target_synced_hint'], 'SURFdrive')
        (self.link / 'big.pdf').write_bytes(b'%PDF')
        self.assertTrue((self.cloud / 'big.pdf').is_file())

    @unittest.skipUnless(GIT, 'git is not installed')
    def test_the_link_is_ignored_even_where_the_rule_names_a_directory(self):
        result = sf.connect(self.project, str(self.cloud))
        self.assertEqual(result['gitignore_added'], '/workspace/shared')
        self.assertTrue(sf.ignored(self.project, 'workspace/shared'))
        status = subprocess.run(['git', '-C', str(self.project), 'status', '--porcelain'],
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn('workspace', status)

    def test_existing_files_are_not_moved_without_consent(self):
        (self.link / 'notes.pdf').write_bytes(b'mine')
        with self.assertRaises(sf.FolderError):
            sf.connect(self.project, str(self.cloud))
        self.assertFalse(sf.is_link(self.link))
        self.assertEqual((self.link / 'notes.pdf').read_bytes(), b'mine')

    def test_move_existing_moves_and_refuses_on_a_clash(self):
        (self.link / 'notes.pdf').write_bytes(b'mine')
        (self.cloud / 'notes.pdf').write_bytes(b'theirs')
        with self.assertRaises(sf.FolderError) as caught:
            sf.connect(self.project, str(self.cloud), move_existing=True)
        self.assertIn('notes.pdf', str(caught.exception))
        self.assertEqual((self.link / 'notes.pdf').read_bytes(), b'mine')
        self.assertEqual((self.cloud / 'notes.pdf').read_bytes(), b'theirs')
        (self.cloud / 'notes.pdf').unlink()
        result = sf.connect(self.project, str(self.cloud), move_existing=True)
        self.assertEqual(result['moved'], ['notes.pdf'])
        self.assertEqual((self.cloud / 'notes.pdf').read_bytes(), b'mine')

    def test_a_file_that_arrives_during_the_move_is_never_replaced(self):
        (self.link / 'input.csv').write_bytes(b'local input')

        def arrives(entry, target):  # a coauthor's file, synced in after the clash check
            (self.cloud / 'input.csv').write_bytes(b'coauthor input')
            return True

        with mock.patch.object(sf, 'same_device', side_effect=arrives):
            with self.assertRaises(sf.FolderError) as caught:
                sf.connect(self.project, str(self.cloud), move_existing=True)
        self.assertIn('nothing was replaced', str(caught.exception))
        self.assertEqual((self.cloud / 'input.csv').read_bytes(), b'coauthor input')
        self.assertEqual((self.link / 'input.csv').read_bytes(), b'local input')
        self.assertFalse(sf.is_link(self.link))

    def test_a_move_across_devices_copies_then_renames_without_replacing(self):
        (self.link / 'wave1').mkdir()
        (self.link / 'wave1' / 'a.csv').write_text('1')
        (self.link / 'notes.pdf').write_bytes(b'mine')
        with mock.patch.object(sf, 'same_device', return_value=False):
            sf.move_into(self.link / 'wave1', self.cloud)
            (self.cloud / 'notes.pdf').write_bytes(b'theirs')
            with self.assertRaises(FileExistsError):
                sf.move_into(self.link / 'notes.pdf', self.cloud)
        self.assertEqual((self.cloud / 'wave1' / 'a.csv').read_text(), '1')
        self.assertFalse((self.link / 'wave1').exists())
        self.assertEqual((self.cloud / 'notes.pdf').read_bytes(), b'theirs')
        self.assertEqual((self.link / 'notes.pdf').read_bytes(), b'mine')
        self.assertEqual([p.name for p in self.cloud.iterdir() if p.name.startswith('.trace-os-moving-')], [])

    def test_a_staging_name_already_in_the_shared_folder_is_left_alone(self):
        (self.link / 'wave1').mkdir()
        (self.link / 'wave1' / 'a.csv').write_text('1')
        theirs = self.cloud / ('.trace-os-moving-' + str(os.getpid()) + '-wave1')  # the old fixed name
        theirs.mkdir()
        (theirs / 'keep.txt').write_text('a coauthor\'s')
        (self.cloud / 'wave1').mkdir()
        (self.cloud / 'wave1' / 'b.csv').write_text('2')
        with mock.patch.object(sf, 'same_device', return_value=False):
            with self.assertRaises(FileExistsError):
                sf.move_into(self.link / 'wave1', self.cloud)
        self.assertEqual((theirs / 'keep.txt').read_text(), "a coauthor's")
        self.assertEqual((self.cloud / 'wave1' / 'b.csv').read_text(), '2')
        self.assertEqual((self.link / 'wave1' / 'a.csv').read_text(), '1')
        self.assertEqual(sorted(p.name for p in self.cloud.iterdir()), sorted([theirs.name, 'wave1']))

    @unittest.skipIf(os.name == 'nt', 'Windows refuses an existing destination itself')
    def test_without_an_exclusive_rename_the_name_is_claimed_not_replaced(self):
        (self.link / 'notes.pdf').write_bytes(b'mine')
        (self.link / 'wave1').mkdir()
        (self.link / 'wave1' / 'a.csv').write_text('1')
        (self.cloud / 'taken.pdf').write_bytes(b'theirs')
        with mock.patch.object(sf.sys, 'platform', 'elsewhere'):
            sf.rename_no_replace(self.link / 'wave1', self.cloud / 'wave1')
            # taken.pdf arrives just after any check would have looked
            with mock.patch.object(sf.os.path, 'lexists', return_value=False):
                with self.assertRaises(FileExistsError):
                    sf.rename_no_replace(self.link / 'notes.pdf', self.cloud / 'taken.pdf')
            sf.rename_no_replace(self.link / 'notes.pdf', self.cloud / 'notes.pdf')
        self.assertEqual((self.cloud / 'wave1' / 'a.csv').read_text(), '1')
        self.assertFalse((self.link / 'wave1').exists())
        self.assertEqual((self.cloud / 'taken.pdf').read_bytes(), b'theirs')
        self.assertEqual((self.cloud / 'notes.pdf').read_bytes(), b'mine')
        self.assertFalse((self.link / 'notes.pdf').exists())

    def test_plan_changes_nothing(self):
        (self.link / 'a.csv').write_text('1')
        result = sf.connect(self.project, str(self.cloud), move_existing=True, plan=True)
        self.assertEqual(result['action'], 'plan')
        self.assertTrue((self.link / 'a.csv').is_file())
        self.assertFalse(sf.is_link(self.link))
        self.assertEqual(list(self.cloud.iterdir()), [])

    def test_missing_folder_needs_create(self):
        missing = self.cloud.parent / 'not yet'
        with self.assertRaises(sf.FolderError):
            sf.connect(self.project, str(missing))
        sf.connect(self.project, str(missing), create=True)
        self.assertTrue(missing.is_dir())

    def test_relative_nested_and_enclosing_folders_are_refused(self):
        for path in ('cloud', str(self.project / 'sources'), str(self.project.parent)):
            with self.subTest(path=path), self.assertRaises(sf.FolderError):
                sf.connect(self.project, path)

    def test_reconnecting_and_replacing(self):
        sf.connect(self.project, str(self.cloud))
        self.assertEqual(sf.connect(self.project, str(self.cloud))['action'], 'already connected')
        other = self.cloud.parent / 'Other'
        other.mkdir()
        with self.assertRaises(sf.FolderError):
            sf.connect(self.project, str(other))
        sf.connect(self.project, str(other), replace=True)
        self.assertEqual(sf.link_target(self.link).resolve(), other)

    def test_disconnect_keeps_the_shared_files(self):
        (self.cloud / 'keep.txt').write_text('stays')
        sf.connect(self.project, str(self.cloud))
        sf.disconnect(self.project)
        self.assertFalse(self.link.exists() or sf.is_link(self.link))
        self.assertEqual((self.cloud / 'keep.txt').read_text(), 'stays')
        with self.assertRaises(sf.FolderError):
            sf.disconnect(self.project)

    def test_status_names_each_state(self):
        self.assertEqual(sf.status(self.project)['state'], 'local only')
        self.link.rmdir()
        self.assertEqual(sf.status(self.project)['state'], 'not connected')
        sf.connect(self.project, str(self.cloud))
        self.assertEqual(sf.status(self.project)['state'], 'connected')
        moved = self.cloud.parent / 'Renamed'
        self.cloud.rename(moved)
        broken = sf.status(self.project)
        self.assertEqual(broken['state'], 'broken')
        self.assertIn('replace', broken['next'])

    def test_project_in_a_synced_folder_is_named(self):
        self.assertIsNone(sf.synced_hint(Path('/home/a/projects/study')))
        self.assertEqual(sf.synced_hint(Path('/Users/a/Library/CloudStorage/OneDrive-Uni/x')), 'CloudStorage')
        self.assertEqual(sf.synced_hint(Path('/Users/a/Dropbox (Personal)/x')), 'Dropbox (Personal)')
        self.assertIsNone(sf.synced_hint(Path('/home/a/boxes/x')))

    def test_shared_root_in_project_json_is_honoured(self):
        (self.project / 'ai' / 'project.json').write_text(json.dumps({'shared_root': 'data/bulk'}))
        sf.connect(self.project, str(self.cloud))
        self.assertTrue(sf.is_link(self.project / 'data' / 'bulk'))
        (self.project / 'ai' / 'project.json').write_text(json.dumps({'shared_root': '../outside'}))
        self.assertEqual(sf.shared_path(self.project), 'workspace/shared')

    def test_shared_root_is_never_the_project_itself_or_git(self):
        for value, expected in (('.', 'workspace/shared'), ('./', 'workspace/shared'), ('.git/bulk', 'workspace/shared'),
                                ('a/../..', 'workspace/shared'), (' /tmp/bulk', 'workspace/shared'),
                                ('//tmp/bulk', 'workspace/shared'), ('C:/bulk', 'workspace/shared'),
                                ('\\\\server\\share', 'workspace/shared'), ('./data/bulk/', 'data/bulk')):
            with self.subTest(shared_root=value):
                (self.project / 'ai' / 'project.json').write_text(json.dumps({'shared_root': value}))
                self.assertEqual(sf.shared_path(self.project), expected)

    def test_outside_material_waits_in_incoming_and_status_counts_it(self):
        # LESSON (rc22, Stage 2): nothing said where outside material waits before
        # a decision. It waits in incoming/ inside the shared folder, or where
        # incoming_root names; status names the folder and counts what is there.
        self.assertEqual(sf.incoming_path(self.project), 'workspace/shared/incoming')
        sf.connect(self.project, str(self.cloud))
        self.assertEqual(sf.incoming_files(self.project), [])
        (self.cloud / 'incoming').mkdir()
        (self.cloud / 'incoming' / 'panel_v4.csv').write_text('id\n')
        (self.cloud / 'incoming' / '.DS_Store').write_text('noise')
        self.assertEqual(sf.incoming_files(self.project), ['workspace/shared/incoming/panel_v4.csv'])
        result = sf.status(self.project)
        self.assertEqual((result['incoming'], result['incoming_files']), ('workspace/shared/incoming', 1))
        self.assertIn('held in workspace/shared/incoming/', result['next'])
        self.assertTrue((self.cloud / 'incoming' / 'panel_v4.csv').is_file(), 'status moves nothing')
        (self.project / 'ai' / 'project.json').write_text(json.dumps({'incoming_root': 'sources/incoming'}))
        self.assertEqual(sf.incoming_path(self.project), 'sources/incoming')
        (self.project / 'ai' / 'project.json').write_text(json.dumps({'incoming_root': '../elsewhere',
                                                                      'shared_root': 'data/bulk'}))
        self.assertEqual(sf.incoming_path(self.project), 'data/bulk/incoming')

    @unittest.skipUnless(GIT, 'git is not installed')
    def test_a_folder_git_tracks_cannot_become_the_shared_folder(self):
        (self.project / 'sources').mkdir()
        (self.project / 'sources' / 'notes.md').write_text('tracked')
        subprocess.run(['git', '-C', str(self.project), 'add', 'sources/notes.md'], check=True)
        (self.project / 'ai' / 'project.json').write_text(json.dumps({'shared_root': 'sources'}))
        with self.assertRaises(sf.FolderError) as caught:
            sf.connect(self.project, str(self.cloud), move_existing=True)
        self.assertIn('Git tracks files under sources', str(caught.exception))
        self.assertEqual((self.project / 'sources' / 'notes.md').read_text(), 'tracked')
        self.assertEqual(list(self.cloud.iterdir()), [])

    def test_snapshot_is_local_verified_and_ignored(self):
        sf.connect(self.project, str(self.cloud))
        (self.cloud / 'survey').mkdir()
        (self.cloud / 'survey' / 'wave1.csv').write_text('id,x\n1,2\n')
        result = sf.snapshot(self.project, 'survey', 'survey-w1')
        data = Path(result['data'])
        self.assertTrue(sf.inside(data, self.project / 'workspace' / 'snapshots' / 'survey-w1'))
        self.assertEqual((data / 'wave1.csv').read_text(), 'id,x\n1,2\n')
        self.assertEqual((result['files'], result['source']), (1, 'workspace/shared/survey'))
        (self.cloud / 'survey' / 'wave1.csv').write_text('changed later')
        self.assertEqual((data / 'wave1.csv').read_text(), 'id,x\n1,2\n')
        if GIT:
            self.assertTrue(sf.ignored(self.project, 'workspace/snapshots/survey-w1/current.json'))
        for bad in ('../x', ''):
            with self.subTest(subpath=bad), self.assertRaises(sf.FolderError):
                sf.snapshot(self.project, bad or 'missing', 'd')
        with self.assertRaises(sf.FolderError):
            sf.snapshot(self.project, 'survey', '../escape')

    @unittest.skipUnless(GIT, 'git is not installed')
    def test_keep_local_adds_once_and_names_tracked_files(self):
        exports = self.project / 'exports'
        exports.mkdir()
        (exports / 'old.csv').write_text('1')
        subprocess.run(['git', '-C', str(self.project), 'add', 'exports/old.csv'], check=True)
        result = sf.keep_local(self.project, ['exports/'])
        self.assertEqual(result['added'], ['exports/'])
        self.assertEqual(result['already_tracked'], {'exports/': ['exports/old.csv']})
        self.assertEqual(sf.keep_local(self.project, ['exports/'])['added'], [])
        lines = (self.project / '.gitignore').read_text().splitlines()
        self.assertEqual(lines.count('exports/'), 1)

    @unittest.skipUnless(GIT, 'git is not installed')
    def test_keep_local_names_tracked_files_the_rule_matches_below_the_root(self):
        nested = self.project / 'analysis' / 'exports'
        nested.mkdir(parents=True)
        (nested / 'participants list.csv').write_text('1')
        subprocess.run(['git', '-C', str(self.project), 'add', '.'], check=True)
        result = sf.keep_local(self.project, ['exports/'])
        self.assertEqual(result['already_tracked'], {'exports/': ['analysis/exports/participants list.csv']})
        self.assertIn('stay tracked', result['note'])

    def test_cli_reports_errors_without_a_traceback(self):
        code, out, err = self.run_cli('connect', 'relative/path')
        self.assertEqual(code, 2)
        self.assertIn('full path', err)
        code, out, _ = self.run_cli('status', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['state'], 'local only')


class SyncTroubleAndInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.project = base / 'project'
        self.shared = self.project / 'workspace' / 'shared'
        self.shared.mkdir(parents=True)
        (self.project / 'ai').mkdir()

    def put(self, name, data='x'):
        target = self.shared / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)
        return target

    def test_find_lists_the_documented_places_only_when_they_exist(self):
        home = self.project.parent / 'home'
        cloud = home / 'Library' / 'CloudStorage'
        (cloud / 'OneDrive-Universiteit Leiden').mkdir(parents=True)
        (cloud / 'GoogleDrive-a@b.org' / 'My Drive').mkdir(parents=True)
        (home / 'Dropbox').mkdir()
        (home / 'SURFdrive').mkdir()
        (home / 'Documents').mkdir()
        windows = home / 'OneDrive - Uni'
        windows.mkdir()
        found = sf.find_folders(home, {'OneDriveCommercial': str(windows), 'OneDrive': str(home / 'gone')})
        providers = sorted(f['provider'] for f in found)
        self.assertEqual(providers, ['Dropbox', 'Google Drive', 'OneDrive', 'OneDrive (work or school)', 'SURFdrive'])
        self.assertNotIn('Documents', json.dumps(found))
        self.assertEqual(len({f['path'] for f in found}), len(found))

    def test_find_lists_a_linked_folder_once(self):
        # On macOS ~/Google Drive and ~/OneDrive - <org> are links into ~/Library/CloudStorage.
        home = self.project.parent / 'home'
        cloud = home / 'Library' / 'CloudStorage'
        onedrive = cloud / 'OneDrive-UniversityCollegeLondon'
        google = cloud / 'GoogleDrive-a@b.org'
        onedrive.mkdir(parents=True)
        (google / 'My Drive').mkdir(parents=True)
        try:
            (home / 'OneDrive - University College London').symlink_to(onedrive, target_is_directory=True)
            (home / 'Google Drive').symlink_to(google, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'symbolic links unavailable: {exc}')
        found = sf.find_folders(home, {})
        self.assertEqual(sorted(f['provider'] for f in found), ['Google Drive', 'OneDrive'])
        self.assertEqual(sorted(f['path'] for f in found), sorted([str(onedrive), str(google / 'My Drive')]))

    def test_check_names_conflict_copies_shortcuts_and_temporary_files(self):
        for name in ('data.csv', 'data-DESKTOP-1AB2C3D.csv', 'report.pdf', 'report (1).pdf',
                     'notes (conflicted copy 2026-10-01).txt', 'memo_conflict-20261001-101010.md',
                     'table[Conflict].xlsx', 'plan-v2.pdf', 'plan.pdf', 'sheet.gsheet', '~$draft.docx',
                     'sub/.DS_Store'):
            self.put(name)
        result = sf.check(self.project)
        flagged = ' '.join(result['conflict_copies'])
        for name in ('data-DESKTOP-1AB2C3D.csv', 'report (1).pdf', 'notes (conflicted copy',
                     'memo_conflict-', 'table[Conflict]'):
            self.assertIn(name, flagged)
        self.assertNotIn('plan-v2.pdf', flagged, 'a version suffix is not a computer name')
        self.assertEqual(result['google_shortcuts'], ['sheet.gsheet'])
        self.assertEqual(sorted(result['temporary']), ['sub/.DS_Store', '~$draft.docx'])
        self.assertIn('delete nothing yourself', result['next'])
        self.assertTrue(all(p.exists() for p in self.shared.rglob('*')))

    def test_online_only_flags_are_read_from_stat(self):
        class Info:
            st_file_attributes = 0x00400000
        self.assertTrue(sf.online_only(Info()))
        class Mac:
            st_flags = 0x40000000
        self.assertTrue(sf.online_only(Mac()))
        self.assertFalse(sf.online_only(os.stat(self.project)))

    def test_inventory_then_verify_names_each_difference(self):
        self.put('a.csv', '1')
        self.put('sub/b.csv', '2')
        self.put('gone.csv', '3')
        self.put('~$lock.docx', 'noise')
        first = sf.inventory(self.project)
        self.assertTrue(first['changed'])
        recorded = json.loads((self.project / sf.INVENTORY).read_text())
        self.assertEqual([r['path'] for r in recorded['files']], ['a.csv', 'gone.csv', 'sub/b.csv'])
        self.assertNotIn(str(self.project), json.dumps(recorded), 'no machine path enters Git')
        self.assertFalse(sf.inventory(self.project)['changed'])
        self.assertEqual(sf.verify(self.project)['next'], 'nothing; this machine has the recorded files')
        (self.shared / 'gone.csv').unlink()
        self.put('a.csv', '9')
        self.put('new.csv', '4')
        result = sf.verify(self.project)
        self.assertEqual((result['missing_here'], result['changed'], result['not_in_inventory']),
                         (['gone.csv'], ['a.csv'], ['new.csv']))
        self.assertEqual(result['matching'], 1)

    def test_same_size_change_is_caught_although_the_hash_is_cached(self):
        target = self.put('a.csv', 'abc')
        sf.inventory(self.project)
        stamp = target.stat()
        target.write_text('xyz')
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        self.assertEqual(sf.verify(self.project)['changed'], ['a.csv'])

    def test_a_different_file_with_the_same_size_and_time_is_read_again(self):
        target = self.put('a.csv', 'abc')
        sf.inventory(self.project)
        stamp = target.stat()
        other = self.put('other.tmp', 'xyz')  # as a replaced shared folder would hold it
        os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(other, target)
        self.assertEqual((target.stat().st_size, target.stat().st_mtime_ns), (stamp.st_size, stamp.st_mtime_ns))
        self.assertEqual(sf.verify(self.project)['changed'], ['a.csv'])

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'permissions do not hide a folder here')
    def test_a_folder_that_cannot_be_read_stops_the_inventory(self):
        self.put('a.csv', 'abc')
        self.put('closed/b.csv', 'def')
        closed = self.shared / 'closed'
        closed.chmod(0)
        self.addCleanup(closed.chmod, 0o755)
        with self.assertRaises(sf.FolderError) as caught:
            sf.inventory(self.project)
        self.assertIn('closed', str(caught.exception))
        self.assertFalse((self.project / sf.INVENTORY).exists())
        self.assertEqual(sf.check(self.project)['unreadable'], ['closed'])
        closed.chmod(0o755)
        sf.inventory(self.project)
        closed.chmod(0)
        result = sf.verify(self.project)
        self.assertEqual((result['unreadable'], result['missing_here']), (['closed'], ['closed/b.csv']))

    def test_an_online_only_file_keeps_its_recorded_digest(self):
        target = self.put('a.csv', 'abc')
        sf.inventory(self.project)
        recorded = json.loads((self.project / sf.INVENTORY).read_text())['files'][0]['sha256']
        self.put('new.csv', 'n')
        with mock.patch.object(sf, 'online_only', return_value=True):
            result = sf.inventory(self.project)
            self.assertEqual(result['kept_recorded_digest_online_only'], ['a.csv'])
            rows = {r['path']: r for r in json.loads((self.project / sf.INVENTORY).read_text())['files']}
            self.assertEqual((rows['a.csv']['sha256'], rows['new.csv']['sha256']), (recorded, None))
            target.write_text('abcd')  # a new size: the recorded digest no longer describes it
            sf.inventory(self.project)
            rows = {r['path']: r for r in json.loads((self.project / sf.INVENTORY).read_text())['files']}
            self.assertIsNone(rows['a.csv']['sha256'])

    def test_verify_without_an_inventory_says_so(self):
        with self.assertRaises(sf.FolderError):
            sf.verify(self.project)


RCLONE = shutil.which('rclone')


class BucketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.project = base / 'project'
        (self.project / 'ai').mkdir(parents=True)
        (self.project / 'ai' / 'project.json').write_text(json.dumps({'profile': 'academic', 'epochs': [1]}))
        self.bucket = base / 'bucket' / 'study'
        self.bucket.mkdir(parents=True)
        os.environ['RCLONE_CONFIG_TRACETEST_TYPE'] = 'local'
        self.addCleanup(os.environ.pop, 'RCLONE_CONFIG_TRACETEST_TYPE', None)
        self.location = 'tracetest:' + self.bucket.as_posix()

    def test_without_rclone_the_person_is_told_how_to_install_it(self):
        os.environ['TRACE_OS_RCLONE'] = 'no-such-rclone-here'
        self.addCleanup(os.environ.pop, 'TRACE_OS_RCLONE', None)
        with self.assertRaises(sf.FolderError) as caught:
            sf.remote_set(self.project, self.location)
        self.assertIn('winget install Rclone.Rclone', str(caught.exception))

    def test_a_location_needs_a_remote_and_a_path(self):
        for bad in ('justabucket', ':path', 'tracetest:'):
            with self.subTest(location=bad), self.assertRaises(sf.FolderError):
                sf.remote_set(self.project, bad)

    @unittest.skipUnless(RCLONE, 'rclone is not installed')
    def test_unknown_remote_asks_for_rclone_config(self):
        with self.assertRaises(sf.FolderError) as caught:
            sf.remote_set(self.project, 'nobody:bucket/x')
        self.assertIn('rclone config', str(caught.exception))

    @unittest.skipUnless(RCLONE, 'rclone is not installed')
    def test_pull_and_push_copy_and_never_delete(self):
        (self.bucket / 'a.csv').write_text('from bucket')
        (self.bucket / 'only-there.csv').write_text('stays')
        result = sf.remote_set(self.project, self.location, note='a stand-in')
        project = json.loads((self.project / 'ai' / 'project.json').read_text())
        self.assertEqual(project['epochs'], [1], 'other keys are kept')
        self.assertEqual(project['shared_remote']['location'], self.location)
        self.assertEqual(sf.status(self.project)['state'], 'bucket copy')
        local = self.project / 'workspace' / 'shared'
        plan = sf.transfer(self.project, 'pull', plan=True)
        self.assertEqual(plan['to_copy_count'], 2)
        self.assertEqual(list(local.iterdir()), [])
        sf.transfer(self.project, 'pull', plan=False)
        self.assertEqual((local / 'a.csv').read_text(), 'from bucket')
        (local / 'only-there.csv').unlink()
        (local / 'mine.csv').write_text('new here')
        pushed = sf.transfer(self.project, 'push', plan=False)
        self.assertEqual(pushed['copied_count'], 1)
        self.assertEqual((self.bucket / 'mine.csv').read_text(), 'new here')
        self.assertEqual((self.bucket / 'only-there.csv').read_text(), 'stays', 'push never deletes')

    @unittest.skipUnless(RCLONE, 'rclone is not installed')
    def test_a_newer_file_at_the_destination_is_kept(self):
        sf.remote_set(self.project, self.location)
        local = self.project / 'workspace' / 'shared'
        (self.bucket / 'a.csv').write_text('old in bucket')
        (local / 'a.csv').write_text('newer here')
        older = (local / 'a.csv').stat().st_mtime_ns - 10**10
        os.utime(self.bucket / 'a.csv', ns=(older, older))
        result = sf.transfer(self.project, 'pull', plan=False)
        self.assertEqual((local / 'a.csv').read_text(), 'newer here')
        self.assertEqual(result['kept_newer_at_destination'], ['a.csv'])

    @unittest.skipUnless(RCLONE, 'rclone is not installed')
    def test_bucket_and_sync_folder_are_not_mixed(self):
        sf.remote_set(self.project, self.location)
        with self.assertRaises(sf.FolderError):
            sf.connect(self.project, str(self.bucket))
        sf.remote_unset(self.project)
        self.assertNotIn('shared_remote', json.loads((self.project / 'ai' / 'project.json').read_text()))


if __name__ == '__main__':
    unittest.main()
