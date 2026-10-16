"""Find, plan and update several installed projects at once, against temporary repositories only."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import bootstrap as bs
import kit_projects as kp

ENVIRONMENT = {'GIT_AUTHOR_NAME': 'Test Researcher', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
               'GIT_COMMITTER_NAME': 'Test Researcher', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
               # A signing rule in someone's global Git configuration must not fail a fixture commit.
               'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false'}
CHECK_STUB = ('import argparse, json, sys\n'
              'p = argparse.ArgumentParser(); p.add_argument("--out")\n'
              'a = p.parse_args(); open(a.out, "w").write(json.dumps({"status": "pass"}))\n'
              'sys.exit(EXIT)\n')
IGNORED = ''.join(name + '\n' for name in (
    '__pycache__/', '.trace-os/setup.lock', '.trace-os/transaction.json', '.trace-os/backups/',
    '.trace-os/pending-update.json', '.trace-os/pending-plan.json', '.trace-os/update-backups/',
    '.trace-os/receipts/'))
SMOKE = 'import unittest\n\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        pass\n'
# A release's bootstrap.py that plans an update, starts it, and on an interruption takes
# RESTORE seconds to restore the project: longer than subprocess.run waits before it kills.
SLOW_RESTORE = ('import json, os, signal, sys, time\n'
                'from pathlib import Path\n'
                'args = sys.argv[1:]\n'
                'target = Path(args[args.index("--target") + 1])\n'
                'if "--plan" in args:\n'
                '    print(json.dumps({"mode": "upgrade", "changes": {"scripts/example.py": "update"},\n'
                '                      "conflicts": [], "kept_project_edits": []}))\n'
                '    sys.exit(0)\n'
                'def stop(signum, frame):\n'
                '    raise KeyboardInterrupt(signum)\n'
                'signal.signal(signal.SIGTERM, stop)\n'
                'marker = target / ".trace-os/pending-update.json"\n'
                'marker.write_text("{}")\n'
                '(target / ".trace-os/stand-in.pid").write_text(str(os.getpid()))\n'
                'try:\n'
                '    time.sleep(30)\n'
                'except KeyboardInterrupt:\n'
                '    time.sleep(RESTORE)\n'
                '    with (target / ".trace-os/update-history.jsonl").open("a") as stream:\n'
                '        stream.write(json.dumps({"event": "update", "status": "interrupted",\n'
                '                                 "restored": "the previous installation"}) + "\\n")\n'
                '    marker.unlink()\n'
                '    print("setup: interrupted", file=sys.stderr)\n'
                '    sys.exit(130)\n'
                'sys.exit(3)\n').replace('RESTORE', '1.5')


def section(text):
    return '<!-- trace-os:begin core-workflow -->\n' + text + '\n<!-- trace-os:end core-workflow -->\n'


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


def snapshot(root):
    """Every file's bytes, .git included, so a read-only claim covers the repository too."""
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob('*')) if path.is_file()}


class Kit:
    """Releases cut the way export.py cuts them: a manifest and every file under trace-os/.

    Each release ships the real bootstrap.py, because kit_projects runs the
    release's own updater, a smoke test for the updater's release preflight and a
    workflow-check stub for its project postflight.
    """

    def __init__(self, root):
        self.root = root
        self.upgrades, self.releases, self.previous = [], [], None
        bootstrap = (SCRIPTS / 'bootstrap.py').read_bytes().decode('utf-8')
        self.contents = {'AGENTS.md': (section('instructions'), 'seed', 'core'),
                         '.gitignore': (IGNORED, 'managed', 'core'),
                         'scripts/bootstrap.py': (bootstrap, 'managed', 'core'),
                         'scripts/check_workflow.py': (CHECK_STUB.replace('EXIT', '0'), 'managed', 'core'),
                         'scripts/example.py': ('print(1)\n', 'managed', 'core'),
                         'tests/test_smoke.py': (SMOKE, 'managed', 'core'),
                         '.skills/example/SKILL.md': ('skill\n', 'managed', 'core'),
                         'paper/main.tex': ('document\n', 'seed', 'academic')}

    def cut(self, version, example=None, check_exit=0):
        """Cut a release, nominating the pair from the previous one and pinning every earlier archive."""
        if example is not None:
            self.contents['scripts/example.py'] = (example, 'managed', 'core')
        self.contents['scripts/check_workflow.py'] = (CHECK_STUB.replace('EXIT', str(check_exit)), 'managed', 'core')
        source = self.root / ('release-' + version)
        files = {}
        for name, (text, policy, profile) in self.contents.items():
            data = text.encode('utf-8')
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[name] = {'sha256': bs.digest(data), 'size': len(data), 'policy': policy, 'profile': profile}
        agents = self.contents['AGENTS.md'][0].encode('utf-8')
        spec = {'id': 'core-workflow', 'path': 'AGENTS.md', 'version': 1,
                'start': '<!-- trace-os:begin core-workflow -->', 'end': '<!-- trace-os:end core-workflow -->'}
        first, last = bs.marked_range(agents, spec)
        spec.update(sha256=bs.digest(agents[first:last]), size=last - first, legacy=[])
        if self.previous:
            self.upgrades.append({'from_version': self.previous, 'to_version': version,
                                  'profiles': ['academic', 'core'], 'agents': ['claude', 'codex', 'cline', 'none'],
                                  'install_state_schema': 1, 'project_schema': 1, 'record_schema': 1})
        manifest = {'schema_version': 1, 'version': version, 'source_commit': '0' * 40,
                    'compatibility': {'install_state_schema': 1, 'project_schema': 1, 'record_schema': 1},
                    'supported_upgrades': list(self.upgrades), 'renames': [], 'releases': list(self.releases),
                    'instruction_sections': [spec], 'files': files}
        (source / 'starter-manifest.json').write_bytes(bs.encoded(manifest))
        archive = self.root / f'trace-os-{version}.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            for file in sorted(source.rglob('*')):
                if file.is_file():
                    zipped.write(file, 'trace-os/' + file.relative_to(source).as_posix())
        sha256 = bs.digest(archive.read_bytes())
        self.releases.append({'version': version, 'source_commit': '0' * 40, 'archive_sha256': sha256})
        self.previous = version
        return source, archive, sha256


class KitProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patch = mock.patch.dict(os.environ, ENVIRONMENT)
        patch.start()
        self.addCleanup(patch.stop)
        self.kit = Kit(self.root / 'kit')
        self.base = self.root / 'projects'
        self.base.mkdir()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kp.main([str(arg) for arg in argv])
        return code, out.getvalue(), err.getvalue()

    def install(self, name, source):
        """A project set up from a release, with the setup committed, as SETUP-AGENT.md asks."""
        target = self.base / name
        bs.apply(source, target, 'academic', 'claude')
        git(target, 'add', '-A')
        git(target, 'commit', '-qm', 'Record the setup')
        return target

    def fake(self, path, version, folder='.trace-os', **state):
        """A Git working tree carrying only an installation state: enough for discovery and listing.

        `folder` is where it keeps the state: .traceware in 0.1.0-rc19, .trace-kit before it.
        """
        path.mkdir(parents=True)
        git(path, 'init', '-q')
        (path / folder).mkdir()
        (path / folder / 'install.json').write_text(json.dumps(dict({'schema_version': 1, 'version': version,
                                                                     'profile': 'academic', 'agent': 'claude'},
                                                                    **state)))
        (path / 'notes.txt').write_text('research\n')
        git(path, 'add', '-A')
        git(path, 'commit', '-qm', 'fixture')
        return path

    def conflicted(self, name, source):
        """A project that edited, and committed, a managed file the next release changes."""
        target = self.install(name, source)
        (target / 'scripts/example.py').write_text('print("ours")\n')
        git(target, 'commit', '-qam', 'Our own example')
        return target

    @staticmethod
    def version(target):
        return json.loads((target / kp.STATE).read_text())['version']

    def release_args(self, archive, sha256):
        return ['--release-archive', archive, '--expected-sha256', sha256]

    def gh_stand_in(self, published, tag):
        """A gh on PATH that lists `tag` as the newest release and downloads from `published`."""
        bin_dir = self.root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        stand_in = bin_dir / 'gh'
        stand_in.write_text(
            f'#!{sys.executable}\n'
            'import json, shutil, sys\n'
            'from pathlib import Path\n'
            'args = sys.argv[1:]\n'
            'if args[:2] == ["auth", "status"]: sys.exit(0)\n'
            'if args[:2] == ["release", "list"]:\n'
            f'    print(json.dumps([{{"tagName": {tag!r}, "isDraft": False, "createdAt": "2026-09-30"}}])); sys.exit(0)\n'
            'if args[:2] == ["release", "download"]:\n'
            f'    published = Path({str(published)!r})\n'
            '    if (published / args[2]).is_dir(): published = published / args[2]  # one folder per release\n'
            '    out = Path(args[args.index("-D") + 1])\n'
            '    for i, arg in enumerate(args):\n'
            '        if arg == "-p":\n'
            '            for found in published.glob(args[i + 1]): shutil.copy(found, out / found.name)\n'
            '    sys.exit(0)\n'
            'sys.exit(1)\n')
        stand_in.chmod(0o755)
        return mock.patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')})

    def test_list_finds_projects_within_the_depth_bound_and_skips_hidden_and_sync_folders(self):
        search = self.root / 'search'
        near = self.fake(search / 'near', '0.1.0-rc1')
        deep = self.fake(search / 'group/deeper/deep', '0.1.0-rc1')
        deeper = self.fake(search / 'group/deeper/deepest/too-deep', '0.1.0-rc1')
        self.fake(near / 'nested', '0.1.0-rc1')  # inside a project: the project is not searched further
        self.fake(search / '.hidden/project', '0.1.0-rc1')
        self.fake(search / 'node_modules/project', '0.1.0-rc1')
        self.fake(search / 'Dropbox/project', '0.1.0-rc1')
        plain = search / 'plain'
        plain.mkdir()
        git(plain, 'init', '-q')  # a Git working tree without the kit is not a TRACE project
        code, out, _ = self.run_main('list', '--root', search, '--json')
        self.assertEqual(code, 0)
        listing = json.loads(out)
        self.assertEqual([row['path'] for row in listing['projects']], sorted([near.as_posix(), deep.as_posix()]))
        self.assertEqual(listing['passed_over'], [(search / 'Dropbox').as_posix()])
        code, out, _ = self.run_main('list', '--root', search, '--depth', '4', '--json')
        self.assertEqual([row['path'] for row in json.loads(out)['projects']],
                         sorted([near.as_posix(), deep.as_posix(), deeper.as_posix()]))

    def test_projects_are_found_in_every_state_folder(self):
        # A project an earlier release installed keeps .traceware/ or .trace-kit/ until its update moves it,
        # and is still found.
        search = self.root / 'search'
        new = self.fake(search / 'new', '0.1.0-rc20')
        mid = self.fake(search / 'mid', '0.1.0-rc19', folder='.traceware')
        old = self.fake(search / 'old', '0.1.0-rc16', folder='.trace-kit')
        (old / '.trace-kit/pending-update.json').write_text('{}')
        (mid / '.traceware/transaction.json').write_text('{}')
        code, out, _ = self.run_main('list', '--root', search, '--json')
        self.assertEqual(code, 0)
        rows = {Path(row['path']).name: row for row in json.loads(out)['projects']}
        self.assertEqual({name: (row['version'], row['state']) for name, row in rows.items()},
                         {'new': ('0.1.0-rc20', '.trace-os/install.json'),
                          'mid': ('0.1.0-rc19', '.traceware/install.json'),
                          'old': ('0.1.0-rc16', '.trace-kit/install.json')})
        self.assertEqual(rows['old']['unfinished'], ['.trace-kit/pending-update.json'])
        self.assertEqual(rows['mid']['unfinished'], ['.traceware/transaction.json'])
        self.assertEqual(rows['new']['unfinished'], [])
        self.assertEqual(kp.located(new, kp.UPDATE_HISTORY), '.trace-os/update-history.jsonl')
        self.assertEqual(kp.located(mid, kp.UPDATE_HISTORY), '.traceware/update-history.jsonl')
        self.assertEqual(kp.located(old, kp.UPDATE_HISTORY), '.trace-kit/update-history.jsonl')

    @unittest.skipIf(os.name == 'nt', 'the stand-in gh is a POSIX script')
    def test_a_release_archive_is_downloaded_under_the_name_it_was_published_under(self):
        # Releases up to 0.1.0-rc18 were published as `trace-kit-starter-...` or `trace-kit-...`, rc19 as
        # `traceware-...`, later ones as `trace-os-...`.
        self.assertEqual(kp.REPOSITORY, 'amrhs-zohrevand/trace-os')
        for name in ('trace-kit-starter-0.1.0-rc15.zip', 'traceware-0.1.0-rc19.zip', 'trace-os-0.1.0-rc20.zip'):
            with self.subTest(name=name):
                published = self.root / ('published-' + name)
                published.mkdir()
                (published / name).write_bytes(b'zip')
                (published / (name + '.sha256')).write_text('0' * 64 + '  ' + name + '\n')
                store = self.root / ('store-' + name)
                store.mkdir()
                with self.gh_stand_in(published, 'v'):
                    archive = kp.download_archive(kp.gh_command(), 'v', store)
                self.assertEqual(archive, store / name)
                self.assertEqual(sorted(path.name for path in store.iterdir()), [name, name + '.sha256'])

    def test_an_archive_cut_before_rc20_is_read_under_its_old_top_folder(self):
        source, archive, _ = self.kit.cut('0.1.0-rc1')
        for root in ('traceware', 'trace-kit-starter'):
            with self.subTest(root=root):
                old = self.root / f'old-root-{root}.zip'
                with zipfile.ZipFile(archive) as current, zipfile.ZipFile(old, 'w') as written:
                    for info in current.infolist():
                        written.writestr(root + '/' + info.filename.split('/', 1)[1], current.read(info))
                workdir = self.root / f'unpacked-{root}'
                workdir.mkdir()
                self.assertEqual(kp.archive_manifest(old)['version'], '0.1.0-rc1')
                self.assertEqual(kp.unpack(old, workdir), workdir / root)

    def test_list_says_what_each_project_has_and_where_it_stands(self):
        self.kit.cut('0.1.0-rc1')
        self.kit.cut('0.1.0-rc2', example='print(2)\n')
        _, archive, sha256 = self.kit.cut('0.1.0-rc3', example='print(3)\n')
        current = self.fake(self.base / 'a-current', '0.1.0-rc3', agent='codex')
        behind = self.fake(self.base / 'b-behind', '0.1.0-rc2', profile='core')
        chain = self.fake(self.base / 'c-chain', '0.1.0-rc1')
        self.fake(self.base / 'd-newer', '0.1.0-rc9')
        (behind / 'notes.txt').write_text('changed, not committed\n')
        (chain / 'scratch.txt').write_text('untracked only\n')
        code, out, _ = self.run_main('list', '--root', self.base, *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 0)
        rows = {Path(row['path']).name: row for row in json.loads(out)['projects']}
        self.assertEqual((rows['a-current']['version'], rows['a-current']['profile'], rows['a-current']['agent']),
                         ('0.1.0-rc3', 'academic', 'codex'))
        self.assertEqual(rows['b-behind']['profile'], 'core')
        self.assertEqual({name: row['dirty'] for name, row in rows.items()},
                         {'a-current': False, 'b-behind': True, 'c-chain': False, 'd-newer': False})
        self.assertEqual(rows['c-chain']['untracked_files'], 1)
        self.assertEqual({name: (row['release']['kind'], row['release']['hops']) for name, row in rows.items()},
                         {'a-current': ('current', 0), 'b-behind': ('behind-one', 1),
                          'c-chain': ('behind-chain', 2), 'd-newer': ('no-path', 0)})
        # Without an archive, a version alone is compared by number.
        code, out, _ = self.run_main('list', '--project', chain, '--project', current,
                                     '--release', '0.1.0-rc3')
        self.assertEqual(code, 0)
        self.assertIn('2 releases behind: the chain', out)
        self.assertIn('current', out)
        self.assertIn(chain.as_posix(), out)

    def test_a_digest_mismatch_is_refused_before_anything_runs(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        project = self.install('a', source)
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        before = snapshot(project)
        wrong = self.root / 'wrong.sha256'
        wrong.write_text('0' * 64 + '  trace-os-0.1.0-rc2.zip\n')
        with mock.patch.object(kp, 'run_bootstrap') as bootstrap, mock.patch.object(kp, 'read_project') as read:
            for argv in (['plan', '--root', self.base, '--release-archive', archive, '--expected-sha256', '1' * 64],
                         ['plan', '--root', self.base, '--release-archive', archive, '--sha256-file', wrong],
                         ['update', '--root', self.base, '--all', '--yes', '--release-archive', archive,
                          '--sha256-file', wrong]):
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn('SHA-256', err)
                self.assertIn('nothing was planned or applied', err)
            bootstrap.assert_not_called()
            read.assert_not_called()
        self.assertEqual(snapshot(project), before)
        self.assertEqual(self.version(project), '0.1.0-rc1')
        # The same run with the right digest does plan.
        code, out, _ = self.run_main('plan', '--root', self.base, *self.release_args(archive, sha256))
        self.assertEqual(code, 0)
        self.assertIn('would update', out)

    def test_plan_reports_each_project_and_changes_none_of_them(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        clean = self.install('a-clean', source)
        edited = self.conflicted('b-edited', source)
        dirty = self.install('c-dirty', source)
        (dirty / 'paper/main.tex').write_text('work in progress\n')
        kept = self.install('d-kept', source)
        (kept / '.gitignore').write_text(IGNORED + 'our-own-output/\n')
        git(kept, 'commit', '-qam', 'Ignore our own output')
        source2, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        current = self.base / 'e-current'
        bs.apply(source2, current, 'academic', 'claude')
        before = {path: snapshot(path) for path in (clean, edited, dirty, kept, current)}
        code, out, _ = self.run_main('plan', '--root', self.base, *self.release_args(archive, sha256))
        self.assertEqual(code, 1)
        self.assertIn('conflicts: scripts/example.py', out)
        code, out, _ = self.run_main('plan', '--root', self.base, *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 1)
        rows = {Path(row['path']).name: row for row in json.loads(out)['projects']}
        self.assertEqual({name: row['status'] for name, row in rows.items()},
                         {'a-clean': 'update', 'b-edited': 'conflicts', 'c-dirty': 'cannot',
                          'd-kept': 'update', 'e-current': 'current'})
        self.assertEqual(rows['b-edited']['conflicts'], ['scripts/example.py'])
        self.assertIn('uncommitted changes', rows['c-dirty']['reason'])
        self.assertEqual(rows['c-dirty']['planned'], 'update')
        self.assertEqual(rows['d-kept']['kept_project_edits'], ['.gitignore'])
        self.assertEqual(rows['a-clean']['kept_project_edits'], [])
        self.assertGreater(rows['a-clean']['changes'], 0)
        for path, files in before.items():
            self.assertEqual(snapshot(path), files, f'{path.name} changed during a plan')

    def three_projects(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        first = self.install('a-clean', source)
        edited = self.conflicted('b-edited', source)
        last = self.install('c-clean', source)
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        return first, edited, last, archive, sha256

    def test_update_applies_the_clean_projects_and_stops_at_a_conflict(self):
        first, edited, last, archive, sha256 = self.three_projects()
        head = git(first, 'rev-parse', 'HEAD')
        untouched = {path: snapshot(path) for path in (edited, last)}
        code, out, _ = self.run_main('update', '--root', self.base, '--all', '--yes',
                                     *self.release_args(archive, sha256))
        self.assertEqual(code, 1)
        self.assertEqual(self.version(first), '0.1.0-rc2')
        self.assertEqual((first / 'scripts/example.py').read_text(), 'print(2)\n')
        # Applied, never committed: the person reviews and commits.
        self.assertEqual(git(first, 'rev-parse', 'HEAD'), head)
        self.assertIn('scripts/example.py', git(first, 'status', '--porcelain'))
        for path, files in untouched.items():
            self.assertEqual(snapshot(path), files, f'{path.name} changed after the run stopped')
        self.assertIn(f'not attempted: the run stopped at {edited.as_posix()}', out)
        self.assertIn('Nothing was committed or pushed', out)

    def test_keep_going_continues_past_a_conflict(self):
        first, edited, last, archive, sha256 = self.three_projects()
        code, out, _ = self.run_main('update', '--root', self.base, '--all', '--yes', '--keep-going',
                                     *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 1)
        results = {Path(row['path']).name: row['outcome'] for row in json.loads(out)['results']}
        self.assertEqual(results, {'a-clean': 'updated', 'b-edited': 'needs you', 'c-clean': 'updated'})
        self.assertEqual([self.version(path) for path in (first, edited, last)],
                         ['0.1.0-rc2', '0.1.0-rc1', '0.1.0-rc2'])
        self.assertEqual((edited / 'scripts/example.py').read_text(), 'print("ours")\n')

    def test_a_project_with_uncommitted_changes_is_skipped_unless_allowed(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        dirty = self.install('a-dirty', source)
        (dirty / 'paper/main.tex').write_text('work in progress\n')
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        code, out, _ = self.run_main('update', '--project', dirty, '--yes', *self.release_args(archive, sha256))
        self.assertEqual(code, 1)
        self.assertIn('skipped: uncommitted changes', out)
        self.assertEqual(self.version(dirty), '0.1.0-rc1')
        code, out, _ = self.run_main('update', '--project', dirty, '--yes', '--allow-dirty',
                                     *self.release_args(archive, sha256))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.version(dirty), '0.1.0-rc2')
        self.assertEqual((dirty / 'paper/main.tex').read_text(), 'work in progress\n')

    def test_without_a_terminal_and_without_yes_nothing_is_applied(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        project = self.install('a', source)
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        before = snapshot(project)
        run = subprocess.run([sys.executable, str(SCRIPTS / 'kit_projects.py'), 'update', '--project',
                              str(project), '--release-archive', str(archive), '--expected-sha256', sha256],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn('would update', run.stdout)  # the plan is printed first
        self.assertIn('run the same command with --yes', run.stdout)
        self.assertIn('not applied', run.stdout)
        self.assertEqual(snapshot(project), before)

    def test_a_failed_update_stops_the_batch(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        first = self.install('a-first', source)
        second = self.install('b-second', source)
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n', check_exit=1)
        second_before = snapshot(second)
        code, out, _ = self.run_main('update', '--root', self.base, '--all', '--yes',
                                     *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 1)
        results = {Path(row['path']).name: row for row in json.loads(out)['results']}
        self.assertEqual(results['a-first']['outcome'], 'failed')
        self.assertIn('restored the last successful installation', results['a-first']['reason'])
        self.assertEqual(results['b-second']['outcome'], 'not attempted')
        # The release's own updater restored the first project; the second was never touched.
        self.assertEqual(self.version(first), '0.1.0-rc1')
        self.assertEqual((first / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertEqual(snapshot(second), second_before)
        # With --keep-going the second project is attempted too, and restored the same way.
        code, out, _ = self.run_main('update', '--root', self.base, '--all', '--yes', '--keep-going',
                                     *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 1)
        self.assertEqual([row['outcome'] for row in json.loads(out)['results']], ['failed', 'failed'])
        history = [json.loads(line) for line in (second / bs.UPDATE_HISTORY).read_text().splitlines()]
        self.assertEqual([row['status'] for row in history], ['failed'])
        self.assertEqual(self.version(second), '0.1.0-rc1')

    def test_a_project_further_behind_takes_the_chain_through_the_release_store(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        project = self.install('a', source)
        _, middle, _ = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        store = self.root / 'store'
        store.mkdir()
        shutil.copy(middle, store / 'any-name.zip')  # matched by digest, never by name
        _, archive, sha256 = self.kit.cut('0.1.0-rc3', example='print(3)\n')
        code, out, _ = self.run_main('plan', '--project', project, *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 1)
        row = json.loads(out)['projects'][0]
        self.assertEqual(row['status'], 'cannot')
        self.assertIn('--release-store', row['reason'])
        code, out, _ = self.run_main('update', '--project', project, '--yes', '--release-store', store,
                                     *self.release_args(archive, sha256))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.version(project), '0.1.0-rc3')
        self.assertEqual((project / 'scripts/example.py').read_text(), 'print(3)\n')
        history = [json.loads(line) for line in
                   (project / bs.UPDATE_HISTORY).read_text().splitlines() if line.strip()]
        self.assertEqual([(row['status'], row['to']['version']) for row in history],
                         [('succeeded', '0.1.0-rc2'), ('succeeded', '0.1.0-rc3')])

    def test_a_project_that_becomes_dirty_after_the_plan_is_not_updated(self):
        # review 2026-09-30, finding 2: the tree is read again just before each update.
        source, _, _ = self.kit.cut('0.1.0-rc1')
        first = self.install('a-first', source)
        second = self.install('b-second', source)
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')

        def edit_while_confirming(*_):
            (first / 'paper/main.tex').write_text('saved while the confirmation waited\n')
            return True, None

        real_apply = kp.apply_one

        def edit_while_the_first_updates(row, *rest):
            result = real_apply(row, *rest)
            (second / 'paper/main.tex').write_text('saved while the first project updated\n')
            return result

        # Clean at the plan; edited while the confirmation waits.
        with mock.patch.object(kp, 'confirmed', side_effect=edit_while_confirming):
            code, out, _ = self.run_main('update', '--project', first, *self.release_args(archive, sha256),
                                         '--json')
        self.assertEqual(code, 1)
        result = json.loads(out)['results'][0]
        self.assertEqual(result['outcome'], 'skipped')
        self.assertIn('uncommitted changes', result['reason'])
        self.assertIn('--allow-dirty', result['reason'])
        self.assertEqual(self.version(first), '0.1.0-rc1')
        self.assertEqual((first / 'paper/main.tex').read_text(), 'saved while the confirmation waited\n')
        # Clean at the plan; edited while an earlier project updates.
        git(first, 'checkout', '--', 'paper/main.tex')
        with mock.patch.object(kp, 'apply_one', side_effect=edit_while_the_first_updates):
            code, out, _ = self.run_main('update', '--root', self.base, '--all', '--yes',
                                         *self.release_args(archive, sha256), '--json')
        self.assertEqual(code, 1)
        results = {Path(row['path']).name: row for row in json.loads(out)['results']}
        self.assertEqual(results['a-first']['outcome'], 'updated')
        self.assertEqual(results['b-second']['outcome'], 'skipped')
        self.assertIn('--allow-dirty', results['b-second']['reason'])
        self.assertEqual(self.version(second), '0.1.0-rc1')

    @unittest.skipIf(os.name == 'nt', 'the stand-in gh is a POSIX script')
    def test_a_relative_download_dir_serves_the_chain_from_the_project(self):
        # review 2026-09-30, finding 3: bootstrap.py runs in the project, so the store is passed absolute.
        source, _, _ = self.kit.cut('0.1.0-rc1')
        project = self.install('a', source)
        _, middle, _ = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        _, archive, sha256 = self.kit.cut('0.1.0-rc3', example='print(3)\n')
        published = self.root / 'published'
        for version, path in (('0.1.0-rc2', middle), ('0.1.0-rc3', archive)):
            (published / version).mkdir(parents=True)
            shutil.copy(path, published / version / path.name)
        (published / '0.1.0-rc3' / (archive.name + '.sha256')).write_text(f'{sha256}  {archive.name}\n')
        caller = self.root / 'caller'
        caller.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(caller)
        with self.gh_stand_in(published, '0.1.0-rc3'):
            code, out, err = self.run_main('update', '--project', project, '--yes', '--download',
                                           '--download-dir', 'downloads', '--json')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.version(project), '0.1.0-rc3')
        self.assertTrue((caller / 'downloads' / middle.name).is_file())

    def interrupt_an_update(self, send):
        """Run update on a project whose bootstrap.py restores slowly, interrupt it with `send`, collect."""
        self.kit.cut('0.1.0-rc1')
        project = self.fake(self.base / 'a', '0.1.0-rc1')
        self.kit.contents['scripts/bootstrap.py'] = (SLOW_RESTORE, 'managed', 'core')
        _, archive, sha256 = self.kit.cut('0.1.0-rc2')
        pid_file = project / '.trace-os/stand-in.pid'
        wrapper = subprocess.Popen([sys.executable, str(SCRIPTS / 'kit_projects.py'), 'update', '--project',
                                    str(project), '--yes', '--release-archive', str(archive),
                                    '--expected-sha256', sha256, '--json'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True,
                                   # AH-228: a background job starts with SIGINT ignored and the
                                   # script would inherit that; a terminal gives it SIGINT as is.
                                   # The release preflight runs this test wherever an update runs.
                                   preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))

        def end_what_is_left():
            for pid in [wrapper.pid] + ([int(pid_file.read_text())] if pid_file.is_file() else []):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            wrapper.communicate()

        self.addCleanup(end_what_is_left)
        deadline = time.monotonic() + 30
        while not pid_file.is_file() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.is_file(), 'the stand-in bootstrap.py never started its update')
        send(wrapper)
        out, err = wrapper.communicate(timeout=60)
        return project, wrapper.returncode, out, err

    def assert_restored(self, project, out, err):
        self.assertFalse((project / '.trace-os/pending-update.json').exists(),
                         'bootstrap.py was ended before it restored the project: ' + err)
        history = [json.loads(line) for line in (project / bs.UPDATE_HISTORY).read_text().splitlines()]
        self.assertEqual(history[-1]['status'], 'interrupted')
        result = json.loads(out)['results'][0]
        self.assertEqual(result['outcome'], 'restored', result)
        self.assertIn('waiting up to', err)
        self.assertEqual(self.version(project), '0.1.0-rc1')

    @unittest.skipIf(os.name == 'nt', 'POSIX signals and process groups')
    def test_ctrl_c_waits_for_bootstrap_to_restore_its_project(self):
        # review 2026-09-30, finding 1: a terminal's Ctrl-C reaches the whole foreground group.
        project, code, out, err = self.interrupt_an_update(lambda wrapper: os.killpg(wrapper.pid, signal.SIGINT))
        self.assert_restored(project, out, err)
        self.assertEqual(code, 130, err)

    @unittest.skipIf(os.name == 'nt', 'POSIX signals and process groups')
    def test_sigterm_to_the_script_is_passed_on_and_waited_for(self):
        # review 2026-09-30, finding 1: an agent's command timeout signals this script alone.
        project, code, out, err = self.interrupt_an_update(lambda wrapper: wrapper.send_signal(signal.SIGTERM))
        self.assert_restored(project, out, err)
        self.assertEqual(code, 128 + signal.SIGTERM, err)

    @unittest.skipIf(os.name == 'nt', 'POSIX signals and process groups')
    def test_a_bootstrap_still_running_when_the_wait_runs_out_is_ended_and_reported(self):
        # review 2026-09-30, finding 1: the wait is bounded, and running out of it is said.
        child = subprocess.Popen([sys.executable, '-c', 'import signal, time\n'
                                  'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                                  'print("ready", flush=True)\n'
                                  'time.sleep(60)\n'],
                                 stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(child.stdout.close)
        child.stdout.readline()
        started = time.monotonic()
        with mock.patch.object(kp, 'RECOVERY_WAIT', 0.5), contextlib.redirect_stderr(io.StringIO()):
            expired = kp.settle(child, signal.SIGINT, self.root)
        self.assertTrue(expired)
        self.assertIsNotNone(child.returncode)
        self.assertLess(time.monotonic() - started, 10)
        project = self.fake(self.base / 'a', '0.1.0-rc1')
        (project / '.trace-os/pending-update.json').write_text('{}')
        row = dict(kp.read_project(project), status='update')
        result = kp.after_interruption(row, {'version': '0.1.0-rc2'}, 0,
                                       kp.Interrupted(signal.SIGINT, exit_code=child.returncode, expired=True))
        self.assertEqual(result['outcome'], 'recovery incomplete')
        self.assertIn('was ended', result['reason'])
        self.assertIn('--recover', result['reason'])
        self.assertIn('pending-update.json', result['reason'])

    def test_update_needs_the_projects_named(self):
        _, archive, sha256 = self.kit.cut('0.1.0-rc1')
        code, _, err = self.run_main('update', '--root', self.base, *self.release_args(archive, sha256))
        self.assertEqual(code, 2)
        self.assertIn('--project DIR for each, or --all', err)

    def test_download_without_gh_says_how_to_download_by_hand(self):
        with mock.patch.object(kp.shutil, 'which', return_value=None):
            code, _, err = self.run_main('plan', '--root', self.base, '--download')
        self.assertEqual(code, 2)
        self.assertIn('not installed', err)
        self.assertIn('--sha256-file', err)

    @unittest.skipIf(os.name == 'nt', 'the stand-in gh is a POSIX script')
    def test_download_takes_the_digest_from_the_release_sidecar_and_refuses_a_mismatch(self):
        source, _, _ = self.kit.cut('0.1.0-rc1')
        project = self.install('a', source)
        _, archive, sha256 = self.kit.cut('0.1.0-rc2', example='print(2)\n')
        published = self.root / 'published'
        published.mkdir()
        shutil.copy(archive, published / archive.name)
        sidecar = published / (archive.name + '.sha256')
        sidecar.write_text(f'{sha256}  {archive.name}\n')
        with self.gh_stand_in(published, '0.1.0-rc2'):
            code, out, _ = self.run_main('plan', '--project', project, '--download', '--json')
            self.assertEqual(code, 0, out)
            plan = json.loads(out)
            self.assertEqual(plan['release']['version'], '0.1.0-rc2')
            self.assertEqual(plan['release']['sha256'], sha256)
            self.assertIn(".sha256 file", plan['release']['verified_by'])
            self.assertEqual(plan['projects'][0]['status'], 'update')
            sidecar.write_text('f' * 64 + f'  {archive.name}\n')
            code, _, err = self.run_main('plan', '--project', project, '--download', '--release', '0.1.0-rc2')
            self.assertEqual(code, 2)
            self.assertIn('nothing was planned or applied', err)
        self.assertEqual(self.version(project), '0.1.0-rc1')


if __name__ == '__main__':
    unittest.main()
