"""Exercise setup, recovery and upgrades against temporary repositories only."""
import contextlib
import json
import os
import shutil
from pathlib import Path
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


def check_skills_module():
    import check_skills
    return check_skills


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source, self.target = self.root / 'release', self.root / 'project'
        self.source.mkdir()
        patch = mock.patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 'Test Researcher', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test Researcher', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'})
        patch.start(); self.addCleanup(patch.stop)
        self.section_version = 1
        self.prefix = 'trace-os'  # the section markers' name; an earlier release's for its own (MARKERS)
        self.state_folder = None  # the install_state_dir a release's manifest names, when it names one
        self.release_version = None
        self.renames = []
        self.upgrades = []
        self.releases = []
        self.contents = {'AGENTS.md': (self.section('instructions'), 'seed', 'core'),
                         '.gitignore': ('.trace-os/receipts/\n.trace-os/backups/\n.trace-os/setup.lock\n.trace-os/transaction.json\n', 'managed', 'core'),
                         'scripts/example.py': ('print(1)\n', 'managed', 'core'),
                         '.skills/example/SKILL.md': ('skill\n', 'managed', 'core'),
                         'paper/main.tex': ('document\n', 'seed', 'academic')}
        self.release()

    def section(self, text):
        return (f'<!-- {self.prefix}:begin core-workflow -->\n' + text
                + f'\n<!-- {self.prefix}:end core-workflow -->\n')

    def release(self, version='test-1', record_schema=1, from_record_schema=None):
        previous = self.release_version
        files = {}
        for name, (text, policy, profile) in self.contents.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = text.encode()
            path.write_bytes(data)
            files[name] = {'sha256': bs.digest(data), 'size': len(data),
                           'policy': policy, 'profile': profile}
        agents = self.contents['AGENTS.md'][0].encode()
        section_spec = {'id': 'core-workflow', 'path': 'AGENTS.md',
                        'version': self.section_version,
                        'start': f'<!-- {self.prefix}:begin core-workflow -->',
                        'end': f'<!-- {self.prefix}:end core-workflow -->'}
        first, last = bs.marked_range(agents, section_spec)
        section_spec.update(sha256=bs.digest(agents[first:last]), size=last - first, legacy=[])
        if previous and previous != version:
            upgrade = {'from_version': previous, 'to_version': version,
                       'profiles': ['academic', 'core'],
                       'agents': ['claude', 'codex', 'cline', 'none'],
                       'install_state_schema': 1, 'project_schema': 1}
            if from_record_schema is None:
                upgrade['record_schema'] = record_schema
            else:
                upgrade.update(from_record_schema=from_record_schema,
                               to_record_schema=record_schema)
            # Every nominated pair stays listed, as the kit's manifest lists them.
            self.upgrades.append(upgrade)
        manifest = {'schema_version': 1, 'version': version,
                    'source_commit': '0' * 40,
                    'compatibility': {'install_state_schema': 1, 'project_schema': 1,
                                      'record_schema': record_schema},
                    'supported_upgrades': list(self.upgrades),
                    'renames': list(self.renames),
                    'releases': list(self.releases),
                    'instruction_sections': [section_spec], 'files': files}
        if self.state_folder:
            manifest['install_state_dir'] = self.state_folder
        (self.source / 'starter-manifest.json').write_bytes(bs.encoded(manifest))
        self.release_version = version

    def apply(self, **kwargs):
        return bs.apply(self.source, self.target, **kwargs)

    def archive(self):
        path = self.root / ('release-' + self.release_version + '.zip')
        with zipfile.ZipFile(path, 'w') as zipped:
            zipped.write(self.source / 'starter-manifest.json',
                         'trace-os/starter-manifest.json')
        return path, bs.digest(path.read_bytes())

    def update(self, **kwargs):
        archive, expected = self.archive()
        passed = {'status': 'pass', 'exit_code': 0}
        with mock.patch.object(bs, 'preflight_release', return_value=dict(passed, name='release portable tests')), \
             mock.patch.object(bs, 'check_updated_project', return_value=dict(passed, name='updated project workflow')):
            return bs.update(self.source, self.target, kwargs.pop('profile', 'academic'),
                             kwargs.pop('agent', 'claude'), archive, expected, **kwargs)

    def with_shell_script(self):
        """Ship a shell script, the one kind of package file whose mode matters."""
        self.contents['scripts/example.sh'] = ('#!/usr/bin/env bash\necho ok\n', 'managed', 'core')
        self.release()

    CHECK_STUB = ('import argparse, json, sys\n'
                  'p = argparse.ArgumentParser(); p.add_argument("--out")\n'
                  'a = p.parse_args(); open(a.out, "w").write(json.dumps({"status": "pass"}))\n'
                  'sys.exit(EXIT)\n')

    def with_hop_checks(self, check_exit=0):
        """Ship what a real hop runs: a release test for the preflight, a workflow check for the postflight."""
        self.contents['scripts/check_workflow.py'] = (self.CHECK_STUB.replace('EXIT', str(check_exit)), 'managed', 'core')
        self.contents['tests/test_smoke.py'] = ('import unittest\n\n\nclass Smoke(unittest.TestCase):\n'
                                                '    def test_ok(self):\n        pass\n', 'managed', 'core')

    def full_archive(self, root='trace-os'):
        """The whole release as the export ships it, under trace-os/ (an earlier release's root otherwise)."""
        path = self.root / ('full-' + self.release_version + '.zip')
        with zipfile.ZipFile(path, 'w') as zipped:
            for file in sorted(self.source.rglob('*')):
                if file.is_file():
                    zipped.write(file, root + '/' + file.relative_to(self.source).as_posix())
        return path, bs.digest(path.read_bytes())

    def pin_release(self, store, root='trace-os'):
        """Cut the current release into the store and pin it for later manifests."""
        archive, sha256 = self.full_archive(root)
        store.mkdir(exist_ok=True)
        shutil.copy(archive, store / ('anything-' + sha256[:8] + '.zip'))
        self.releases.append({'version': self.release_version, 'source_commit': '0' * 40,
                              'archive_sha256': sha256})
        return sha256

    def chain(self, store, **kwargs):
        archive, expected = self.full_archive()
        return bs.update_chain(self.source, self.target, kwargs.pop('profile', 'academic'),
                               kwargs.pop('agent', 'claude'), archive, expected, store, **kwargs)

    def history(self):
        rows = (self.target / bs.in_layout(self.target, bs.UPDATE_HISTORY)).read_text(encoding='utf-8').splitlines()
        return [json.loads(row) for row in rows if row.strip()]

    def leave_update_unfinished(self, version, code):
        """What an update killed outright during its check leaves: the candidate installed, and its marker."""
        self.contents['scripts/example.py'] = (code, 'managed', 'core')
        self.release(version)
        summary, desired, old = bs.plan(self.source, self.target, 'academic', 'claude')[:3]
        manifest = bs.load(self.source / 'starter-manifest.json')
        state = bs.installation_state(manifest, summary, desired, old, 'academic', 'claude', 'a' * 64)
        bs.begin_update(self.target, bs.update_payload(summary, desired), state, summary, manifest,
                        'a' * 64, 'test-agent')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], version)

    def test_shell_script_mode_is_committed_and_setup_is_repeatable(self):
        self.with_shell_script()
        receipt = self.apply()
        self.assertEqual(receipt['executable']['files'], ['scripts/example.sh'])
        self.assertTrue(bs.git(self.target, 'ls-files', '-s', 'scripts/example.sh').startswith('100755 '))
        if os.name != 'nt':
            self.assertEqual(receipt['executable']['mode'], '0755')
            self.assertTrue(os.access(self.target / 'scripts/example.sh', os.X_OK))
        self.assertNotIn('hooks_path', receipt, 'setup no longer touches core.hooksPath')
        self.assertEqual(bs.git(self.target, 'config', '--local', '--get', 'core.hooksPath', check=False), '')
        again = self.apply()
        self.assertEqual(again['executable']['files'], ['scripts/example.sh'])

    def test_modes_are_reported_as_not_applied_without_posix_permissions(self):
        self.with_shell_script()
        self.apply()
        report = bs.mark_executable(self.target, ['scripts/example.sh', 'scripts/example.py'], posix=False)
        self.assertEqual(report['files'], ['scripts/example.sh'])
        self.assertIsNone(report['mode'])
        self.assertIn('not applied', report['reason'])

    def test_competing_setup_lock_is_not_stolen(self):
        self.target.mkdir()
        with bs.locked(self.target):
            with self.assertRaises(bs.SetupError):
                with bs.locked(self.target):
                    self.fail('second lock acquired')
        with bs.locked(self.target):
            pass

    def test_windows_lock_adapter_locks_one_existing_byte_and_releases(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2
            calls = []

            @classmethod
            def locking(cls, descriptor, mode, count):
                cls.calls.append((mode, count))

        self.target.mkdir()
        with mock.patch.object(bs, 'fcntl', None), mock.patch.object(bs, 'msvcrt', FakeMsvcrt):
            with bs.locked(self.target):
                self.assertEqual((self.target / '.trace-os/setup.lock').read_bytes(), b'\0')
        self.assertEqual(FakeMsvcrt.calls, [(FakeMsvcrt.LK_NBLCK, 1), (FakeMsvcrt.LK_UNLCK, 1)])

    def test_windows_lock_contention_is_reported_without_entering(self):
        class BusyMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            @staticmethod
            def locking(descriptor, mode, count):
                raise OSError('fixture: locked')

        self.target.mkdir()
        with mock.patch.object(bs, 'fcntl', None), mock.patch.object(bs, 'msvcrt', BusyMsvcrt):
            with self.assertRaisesRegex(bs.SetupError, 'another setup process'):
                with bs.locked(self.target):
                    self.fail('busy lock entered')

    def test_new_skill_is_reported_and_installed(self):
        import check_skills
        self.apply()
        new = self.target / '.skills/added/SKILL.md'
        new.parent.mkdir()
        new.write_text('additional canonical skill')
        self.assertTrue(check_skills.check(self.target)[1])
        bs.sync_skills(self.target)
        self.assertFalse(check_skills.check(self.target)[1])
        self.assertEqual((self.target / '.claude/skills/added/SKILL.md').read_bytes(), new.read_bytes())
        new.unlink()
        # AH-172: the unchanged copy of a removed skill goes with it, rather than
        # every later sync refusing; an edited copy is still refused (see below).
        self.assertEqual(bs.sync_skills(self.target)['removed'], ['.claude/skills/added/SKILL.md'])
        self.assertFalse((self.target / '.claude/skills/added').exists())
        self.assertFalse(check_skills.check(self.target)[1])

    def test_denied_skill_install_rolls_back_and_explicit_route_can_run(self):
        real = bs.write_atomic
        def denied(path, data):
            if path == self.target / '.claude/skills/example/SKILL.md':
                raise PermissionError('fixture: protected instruction directory')
            return real(path, data)
        with mock.patch.object(bs, 'write_atomic', side_effect=denied), self.assertRaises(PermissionError):
            self.apply()
        self.assertFalse((self.target / 'AGENTS.md').exists())
        self.assertFalse((self.target / bs.STATE).exists())
        self.assertFalse((self.target / bs.JOURNAL).exists())
        self.apply(agent='none')
        self.assertTrue((self.target / 'ai/project.json').exists())

    def test_interrupted_skill_sync_has_manifest_independent_recovery(self):
        self.apply()
        original = (self.target / '.claude/skills/example/SKILL.md').read_bytes()
        (self.target / '.skills/example/SKILL.md').write_text('edited skill')
        real = bs.write_atomic
        def interrupted(path, data):
            if path == self.target / bs.STATE: raise KeyboardInterrupt()
            return real(path, data)
        with mock.patch.object(bs, 'write_atomic', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            bs.sync_skills(self.target)
        with mock.patch('sys.stdout'):
            self.assertEqual(bs.main(['--target', str(self.target), '--recover']), 0)
        self.assertEqual((self.target / '.claude/skills/example/SKILL.md').read_bytes(), original)
        bs.sync_skills(self.target)
        self.assertEqual((self.target / '.claude/skills/example/SKILL.md').read_text(), 'edited skill')

    def test_plan_does_not_create_a_directory(self):
        report = bs.plan(self.source, self.target)[0]
        self.assertIn('scripts/example.py', report['changes'])
        self.assertFalse(self.target.exists())

    def test_target_with_spaces_and_non_ascii_characters(self):
        self.target = self.root / 'project with café'
        self.apply(agent='none')
        self.assertTrue((self.target / 'ai/project.json').is_file())
        self.assertEqual(bs.git(self.target, 'rev-list', '--count', 'HEAD'), '1')

    def test_fresh_setup_has_own_epochs_empty_history_and_idempotence(self):
        self.apply()
        config = bs.load(self.target / 'ai/project.json')
        head = bs.git(self.target, 'rev-parse', 'HEAD')
        self.assertEqual(config['policy_epoch'], head)
        self.assertEqual(config['accept_epoch'], head)
        self.assertEqual(bs.git(self.target, 'rev-list', '--count', 'HEAD'), '1')
        first = (self.target / bs.STATE).read_bytes()
        second = self.apply()
        self.assertEqual(second['changes'], {})
        self.assertEqual((self.target / bs.STATE).read_bytes(), first)
        self.assertEqual(bs.git(self.target, 'rev-parse', 'HEAD'), head)

    def test_a_bracketed_model_id_is_accepted_and_recorded_as_given(self):
        # Setup asks an agent for its actual model id, and Claude Code reports
        # one with a bracketed context marker.
        executor = 'claude-opus-5-5[1m]'
        self.apply(executor=executor)
        message = bs.git(self.target, 'log', '-1', '--format=%B')
        self.assertIn('Execution-agent: ' + executor, message.splitlines())
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update(executor=executor)
        self.assertEqual(self.history()[-1]['execution_agent'], executor)

    def test_an_execution_agent_with_whitespace_or_control_characters_is_refused(self):
        for executor in ('claude opus', 'claude-opus-5-5[1m] ', 'claude-opus-5-5\nCo-Authored-By: x',
                         'claude-opus-5-5[1 m]', 'claude-opus-5-5[]', 'claude\x07opus'):
            with self.subTest(executor=executor):
                with self.assertRaisesRegex(bs.SetupError, 'one identifier'):
                    self.apply(executor=executor)
                self.assertFalse(self.target.exists())
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        with self.assertRaisesRegex(bs.SetupError, 'one identifier'):
            self.update(executor='claude opus')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')

    def test_existing_history_and_staged_work_are_preserved(self):
        self.target.mkdir()
        bs.git(self.target, 'init', '-q')
        (self.target / 'human.txt').write_text('original')
        bs.git(self.target, 'add', 'human.txt')
        bs.git(self.target, 'commit', '-qm', 'existing research')
        head = bs.git(self.target, 'rev-parse', 'HEAD')
        (self.target / 'human.txt').write_text('staged human edit')
        bs.git(self.target, 'add', 'human.txt')
        index = bs.git(self.target, 'diff', '--cached')
        self.apply()
        self.assertEqual(bs.git(self.target, 'rev-parse', 'HEAD'), head)
        self.assertEqual(bs.git(self.target, 'diff', '--cached'), index)
        self.assertEqual(bs.load(self.target / 'ai/project.json')['policy_epoch'], head)

    def test_conflict_prevents_other_files_being_written(self):
        self.target.mkdir()
        (self.target / 'AGENTS.md').write_text('human instructions')
        with self.assertRaises(bs.SetupError): self.apply()
        self.assertEqual((self.target / 'AGENTS.md').read_text(), 'human instructions')
        self.assertFalse((self.target / 'scripts').exists())

    def test_upgrade_updates_shared_instructions_and_preserves_project_appendix(self):
        self.apply()
        config = (self.target / 'ai/project.json').read_bytes()
        # LF bytes: text mode writes CRLF on Windows, and the section is compared by digest
        (self.target / 'AGENTS.md').write_bytes((self.section('instructions') + '\n## Project rules\nKeep this choice.\n').encode())
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.section_version = 2
        self.contents['AGENTS.md'] = (self.section('revised shared instructions'), 'seed', 'core')
        self.release('test-2')
        receipt = self.update()
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(2)\n')
        instructions = (self.target / 'AGENTS.md').read_text()
        self.assertIn('revised shared instructions', instructions)
        self.assertIn('## Project rules\nKeep this choice.', instructions)
        self.assertEqual((self.target / 'ai/project.json').read_bytes(), config)
        self.assertEqual(receipt['instruction_sections'][0]['action'], 'update')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')

    def rename_the_example_skill(self, agent='claude'):
        """Release a version that renames the shipped skill, as rc5 renames three."""
        self.apply(agent=agent)
        del self.contents['.skills/example/SKILL.md']
        self.contents['.skills/trace-example/SKILL.md'] = ('skill\n', 'managed', 'core')
        self.renames = [{'from_version': self.release_version,
                         'from': '.skills/example/SKILL.md',
                         'to': '.skills/trace-example/SKILL.md'}]
        self.release('test-2')

    def test_a_renamed_skill_replaces_its_old_canonical_and_installed_copies(self):
        # LESSON (2026-09-19): without this the old canonical file and the old
        # generated copy both survive the update, so the project holds nine
        # skills under two naming schemes and the installed set no longer
        # matches the release.
        self.rename_the_example_skill()
        receipt = self.update()
        for gone in ('.skills/example/SKILL.md', '.claude/skills/example/SKILL.md'):
            self.assertFalse((self.target / gone).exists(), gone)
        for present in ('.skills/trace-example/SKILL.md', '.claude/skills/trace-example/SKILL.md'):
            self.assertTrue((self.target / present).is_file(), present)
        self.assertEqual(receipt['renamed']['removed'],
                         ['.claude/skills/example/SKILL.md', '.skills/example/SKILL.md'])
        self.assertEqual(receipt['renamed']['retained'], [])
        self.assertEqual(sorted(receipt['renamed']['pruned_directories']),
                         ['.claude/skills/example', '.skills/example'])
        state = bs.load(self.target / bs.STATE)
        self.assertNotIn('.skills/example/SKILL.md', state['files'])
        self.assertNotIn('.claude/skills/example/SKILL.md', state['files'])
        self.assertNotIn('.skills/example/SKILL.md', receipt['obsolete_preserved'])
        import check_skills
        self.assertEqual(check_skills.check(self.target), (1, []))

    def test_a_rename_under_agent_none_removes_only_the_canonical_file(self):
        self.rename_the_example_skill(agent='none')
        receipt = self.update(agent='none')
        self.assertEqual(receipt['renamed']['removed'], ['.skills/example/SKILL.md'])
        self.assertFalse((self.target / '.skills/example').exists())
        self.assertTrue((self.target / '.skills/trace-example/SKILL.md').is_file())

    def test_a_locally_edited_old_skill_copy_keeps_the_rename_pending(self):
        # Deleting somebody's edit is the one outcome a rollback cannot undo, so
        # the edited path joins the conflict list and the whole update stays
        # pending, exactly as an edited copy of a skill nobody renamed does.
        self.rename_the_example_skill()
        edited = self.target / '.claude/skills/example/SKILL.md'
        edited.write_bytes(b'skill, with my own note\n')
        with self.assertRaisesRegex(bs.SetupError, 'update is pending'):
            self.update()
        self.assertEqual(edited.read_bytes(), b'skill, with my own note\n')
        self.assertTrue((self.target / '.skills/example/SKILL.md').is_file())
        self.assertFalse((self.target / '.skills/trace-example/SKILL.md').exists())
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertIn('.claude/skills/example/SKILL.md', summary['conflicts'])
        retained = summary['renamed']['retained']
        self.assertEqual(retained[0]['edited'], ['.claude/skills/example/SKILL.md'])
        self.assertIn('merge or remove it', retained[0]['reason'])
        pending = json.loads((self.target / bs.PLAN_PENDING).read_text())
        self.assertIn('.claude/skills/example/SKILL.md', pending['conflicts'])

    def test_the_rename_resumes_once_the_edited_copy_is_resolved(self):
        self.rename_the_example_skill()
        edited = self.target / '.claude/skills/example/SKILL.md'
        edited.write_bytes(b'skill, with my own note\n')
        with self.assertRaises(bs.SetupError):
            self.update()
        edited.unlink()
        receipt = self.update()
        # The copy the person deleted leaves the installation state with the
        # canonical file, so nothing records a path that is no longer managed.
        self.assertEqual(receipt['renamed']['removed'],
                         ['.claude/skills/example/SKILL.md', '.skills/example/SKILL.md'])
        self.assertTrue((self.target / '.claude/skills/trace-example/SKILL.md').is_file())
        import check_skills
        self.assertEqual(check_skills.check(self.target), (1, []))

    def test_rolling_back_a_rename_restores_the_old_skill_files(self):
        self.rename_the_example_skill()
        self.update()
        bs.rollback_update(self.target)
        self.assertTrue((self.target / '.skills/example/SKILL.md').is_file())
        self.assertTrue((self.target / '.claude/skills/example/SKILL.md').is_file())
        self.assertFalse((self.target / '.skills/trace-example/SKILL.md').exists())
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')

    def drop_from_release(self, *names):
        """Release a version that no longer ships these files and renames none of them."""
        for name in names:
            del self.contents[name]
        self.release('test-2')

    def test_an_unmodified_file_the_release_stopped_shipping_is_removed(self):
        # Left installed and managed, a dropped file keeps acting: a dropped
        # commit hook that calls a command the kit removed fails every commit.
        self.contents['scripts/hooks/commit-msg'] = ('#!/bin/sh\nexit 1\n', 'managed', 'core')
        self.contents['.skills/other/SKILL.md'] = ('other skill\n', 'managed', 'core')
        self.release()
        self.apply()
        dropped = ['scripts/hooks/commit-msg', '.skills/example/SKILL.md']
        self.drop_from_release(*dropped)
        receipt = self.update()
        state = bs.load(self.target / bs.STATE)
        for gone in dropped + ['.claude/skills/example/SKILL.md']:
            self.assertFalse((self.target / gone).exists(), gone)
            self.assertEqual(receipt['changes'][gone], 'remove')
            self.assertNotIn(gone, state['files'])
        self.assertFalse((self.target / '.skills/example').exists())
        self.assertFalse((self.target / '.claude/skills/example').exists())
        self.assertEqual(receipt['obsolete_preserved'], [])
        self.assertEqual(receipt['renamed']['removed'], [])
        import check_skills
        self.assertEqual(check_skills.check(self.target), (1, []))

    def test_an_edited_file_the_release_stopped_shipping_keeps_the_update_pending(self):
        self.apply()
        code = self.target / 'scripts/example.py'
        code.write_bytes(b'print("my change")\n')
        self.drop_from_release('scripts/example.py')
        with self.assertRaisesRegex(bs.SetupError, r'update is pending: scripts/example.py; .*move the change.*SETUP-AGENT.md, Conflicts'):
            self.update()
        self.assertEqual(code.read_bytes(), b'print("my change")\n')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        pending = json.loads((self.target / bs.PLAN_PENDING).read_text())
        self.assertEqual(pending['conflicts'], ['scripts/example.py'])
        # A conflict, not a file kept on purpose: the plan lists it once.
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], ['scripts/example.py'])
        self.assertNotIn('scripts/example.py', summary['obsolete_preserved'])
        mine = self.target / 'scripts/mine.py'
        code.rename(mine)
        self.update()
        self.assertEqual(mine.read_bytes(), b'print("my change")\n')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')

    WORKFLOW = '.github/workflows/workflow-check.yml'

    def ship_then_drop_the_github_workflow(self, edit=None):
        """Install a release that ships the GitHub workflow, then release one without it.

        0.1.0-rc17 stopped shipping it, since the kit runs no continuous
        integration; an installed project holds the copy an earlier release
        wrote, as a managed file.
        """
        self.contents[self.WORKFLOW] = ('name: workflow checks\non:\n  workflow_dispatch:\n', 'managed', 'core')
        self.release()
        self.apply()
        if edit is not None:
            (self.target / self.WORKFLOW).write_text(edit)
        self.drop_from_release(self.WORKFLOW)

    def test_an_unchanged_github_workflow_leaves_with_the_update(self):
        self.ship_then_drop_the_github_workflow()
        receipt = self.update()
        self.assertFalse((self.target / self.WORKFLOW).exists())
        self.assertEqual(receipt['changes'][self.WORKFLOW], 'remove')
        self.assertNotIn(self.WORKFLOW, bs.load(self.target / bs.STATE)['files'])
        self.assertEqual(receipt['obsolete_preserved'], [])
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')

    def test_a_github_workflow_the_project_turned_on_is_a_named_conflict(self):
        turned_on = "name: workflow checks\non:\n  push:\n    branches: ['**']\n  pull_request:\n"
        self.ship_then_drop_the_github_workflow(edit=turned_on)
        with self.assertRaisesRegex(bs.SetupError, r'update is pending: \.github/workflows/workflow-check\.yml'):
            self.update()
        self.assertEqual((self.target / self.WORKFLOW).read_text(), turned_on)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        pending = json.loads((self.target / bs.PLAN_PENDING).read_text())
        self.assertEqual(pending['conflicts'], [self.WORKFLOW])
        # Moving the project's own copy out of the managed path resolves it.
        (self.target / self.WORKFLOW).rename(self.target / '.github/workflows/ours.yml')
        self.update()
        self.assertEqual((self.target / '.github/workflows/ours.yml').read_text(), turned_on)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')

    def test_a_dropped_file_already_missing_leaves_the_state_with_its_entry(self):
        self.apply()
        (self.target / 'scripts/example.py').unlink()
        self.drop_from_release('scripts/example.py')
        receipt = self.update()
        self.assertFalse((self.target / 'scripts/example.py').exists())
        self.assertNotIn('scripts/example.py', bs.load(self.target / bs.STATE)['files'])
        self.assertEqual(receipt['obsolete_preserved'], [])

    def test_project_skill_copies_and_seeded_files_stay_when_no_release_ships_them(self):
        self.apply()
        mine = self.target / '.skills/mine/SKILL.md'
        mine.parent.mkdir()
        mine.write_bytes(b'project skill\n')
        bs.sync_skills(self.target)
        self.drop_from_release('paper/main.tex')
        receipt = self.update()
        for kept in ('.skills/mine/SKILL.md', '.claude/skills/mine/SKILL.md', 'paper/main.tex'):
            self.assertTrue((self.target / kept).is_file(), kept)
        state = bs.load(self.target / bs.STATE)
        self.assertEqual(state['files']['.claude/skills/mine/SKILL.md']['canonical'], '.skills/mine/SKILL.md')
        self.assertEqual(receipt['obsolete_preserved'], ['.claude/skills/mine/SKILL.md', 'paper/main.tex'])
        self.assertNotIn('remove', receipt['changes'].values())
        import check_skills
        self.assertEqual(check_skills.check(self.target), (2, []))

    def test_rolling_back_restores_a_file_the_release_stopped_shipping(self):
        self.apply()
        entry = bs.load(self.target / bs.STATE)['files']['scripts/example.py']
        self.drop_from_release('scripts/example.py')
        self.update()
        self.assertFalse((self.target / 'scripts/example.py').exists())
        bs.rollback_update(self.target)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        state = bs.load(self.target / bs.STATE)
        self.assertEqual(state['version'], 'test-1')
        self.assertEqual(state['files']['scripts/example.py'], entry)

    def test_a_rename_naming_a_destination_outside_the_release_is_refused(self):
        self.apply()
        self.renames = [{'from_version': self.release_version,
                         'from': '.skills/example/SKILL.md',
                         'to': '.skills/absent/SKILL.md'}]
        self.release('test-2')
        with self.assertRaisesRegex(bs.SetupError, 'outside this release'):
            self.update()

    def test_the_post_update_check_reads_the_candidate_installation_state(self):
        # LESSON (2026-09-19): the state file used to be held back until the
        # checks passed, so the check saw the new files beside the old record of
        # them. The installed-skill check reads that record, and a release that
        # renames a skill therefore failed its own post-update check.
        self.rename_the_example_skill()
        seen = {}

        def inspect(target, run_id):
            state = bs.load(target / bs.STATE)
            seen['version'] = state['version']
            seen['skill_entries'] = sorted(n for n in state['files'] if '/skills/' in n or n.startswith('.skills/'))
            seen['on_disk'] = sorted(p.name for p in (target / '.skills').iterdir())
            return {'name': 'updated project workflow', 'status': 'pass', 'exit_code': 0}

        archive, expected = self.archive()
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}
        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', side_effect=inspect):
            bs.update(self.source, self.target, 'academic', 'claude', archive, expected)
        self.assertEqual(seen['version'], 'test-2')
        self.assertEqual(seen['on_disk'], ['trace-example'])
        self.assertEqual(seen['skill_entries'],
                         ['.claude/skills/trace-example/SKILL.md', '.skills/trace-example/SKILL.md'])

    def test_upgrade_can_advance_the_readable_trace_record_contract(self):
        self.apply()
        self.assertEqual(bs.load(self.target / bs.STATE)['record_schema_version'], 1)
        self.release('test-2', record_schema=2, from_record_schema=1)
        self.update()
        self.assertEqual(bs.load(self.target / bs.STATE)['record_schema_version'], 2)

    def test_convergent_upgrade_adopts_the_new_managed_baseline(self):
        self.apply()
        code = self.target / 'scripts/example.py'
        code.write_bytes(b'print(2)\n')  # LF bytes: the release below is compared by digest
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        installed = bs.load(self.target / bs.STATE)
        self.assertEqual(installed['files']['scripts/example.py']['sha256'], bs.digest(code.read_bytes()))
        self.contents['scripts/example.py'] = ('print(3)\n', 'managed', 'core')
        self.release('test-3')
        self.update()
        self.assertEqual(code.read_text(), 'print(3)\n')

    def test_upgrade_refuses_to_overwrite_locally_edited_code(self):
        self.apply()
        code = self.target / 'scripts/example.py'
        code.write_text('human code edit')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        with self.assertRaises(bs.SetupError): self.update()
        self.assertEqual(code.read_text(), 'human code edit')

    def test_apply_refuses_a_version_change_without_verified_update(self):
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        with self.assertRaisesRegex(bs.SetupError, 'require --update'):
            self.apply()
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')

    def test_locally_edited_instruction_section_keeps_the_whole_update_pending(self):
        self.apply()
        path = self.target / 'AGENTS.md'
        path.write_text(self.section('locally revised instructions'))
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.section_version = 2
        self.contents['AGENTS.md'] = (self.section('release instructions'), 'seed', 'core')
        self.release('test-2')
        with self.assertRaisesRegex(bs.SetupError, 'update is pending'):
            self.update()
        self.assertEqual(path.read_text(), self.section('locally revised instructions'))
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertTrue((self.target / bs.PLAN_PENDING).is_file())
        self.assertIn('"event": "plan-pending"', (self.target / bs.UPDATE_HISTORY).read_text())

    def test_declared_compatible_instruction_override_is_retained(self):
        self.apply()
        path = self.target / 'AGENTS.md'
        path.write_text(self.section('locally revised instructions'))
        project = bs.load(self.target / 'ai/project.json')
        project['trace_os'] = {'instruction_overrides': {'core-workflow': {
            'compatible_with': ['2'], 'basis': 'The local rule includes the release requirement.'}}}
        (self.target / 'ai/project.json').write_bytes(bs.encoded(project))
        self.section_version = 2
        self.contents['AGENTS.md'] = (self.section('release instructions'), 'seed', 'core')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        self.assertEqual(path.read_text(), self.section('locally revised instructions'))
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(2)\n')
        section = bs.load(self.target / bs.STATE)['instruction_sections']['core-workflow']
        self.assertEqual(section['status'], 'override')
        self.assertIn('local rule', section['compatibility_basis'])

    def test_legacy_prefix_becomes_a_bounded_section_and_keeps_project_text(self):
        self.apply()
        legacy = b'legacy shared rules\n'
        (self.target / 'AGENTS.md').write_bytes(legacy + b'\n## Project rules\nKeep me.\n')
        state = bs.load(self.target / bs.STATE)
        state.pop('instruction_sections')
        (self.target / bs.STATE).write_bytes(bs.encoded(state))
        self.section_version = 2
        self.contents['AGENTS.md'] = (self.section('bounded shared rules'), 'seed', 'core')
        self.release('test-2')
        manifest = bs.load(self.source / 'starter-manifest.json')
        manifest['instruction_sections'][0]['legacy'] = [{
            'from_version': 'test-1', 'mode': 'prefix', 'size': len(legacy),
            'sha256': bs.digest(legacy)}]
        (self.source / 'starter-manifest.json').write_bytes(bs.encoded(manifest))
        self.update()
        text = (self.target / 'AGENTS.md').read_text()
        self.assertIn('bounded shared rules', text)
        self.assertIn('## Project rules\nKeep me.', text)

    def test_wrong_archive_digest_stops_before_project_writes(self):
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        archive, unused = self.archive()
        with self.assertRaisesRegex(bs.SetupError, 'does not match'):
            bs.update(self.source, self.target, 'academic', 'claude', archive, '0' * 64)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')

    def test_failed_project_check_restores_the_last_successful_installation(self):
        self.apply()
        old_state = (self.target / bs.STATE).read_bytes()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        archive, expected = self.archive()
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}
        failed = {'name': 'updated project workflow', 'status': 'fail', 'exit_code': 1}
        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', return_value=failed):
            with self.assertRaisesRegex(bs.SetupError, 'restored'):
                bs.update(self.source, self.target, 'academic', 'claude', archive, expected)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertEqual((self.target / bs.STATE).read_bytes(), old_state)
        self.assertIn('"status": "failed"', (self.target / bs.UPDATE_HISTORY).read_text())

    def test_project_check_error_restores_the_last_successful_installation(self):
        self.apply()
        old_state = (self.target / bs.STATE).read_bytes()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        archive, expected = self.archive()
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}
        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', side_effect=OSError('cannot start check')):
            with self.assertRaisesRegex(bs.SetupError, 'could not run; restored'):
                bs.update(self.source, self.target, 'academic', 'claude', archive, expected,
                          executor='test-agent')
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertEqual((self.target / bs.STATE).read_bytes(), old_state)
        history = (self.target / bs.UPDATE_HISTORY).read_text()
        self.assertIn('"status": "failed"', history)
        self.assertIn('"execution_agent": "test-agent"', history)

    def test_ah250_a_refused_preflight_names_the_failed_test_and_keeps_its_output(self):
        self.apply()
        before = (self.target / 'scripts/example.py').read_bytes()
        self.contents['tests/test_smoke.py'] = ('import unittest\n\n\nclass Smoke(unittest.TestCase):\n'
                                                '    def test_breaks(self):\n        self.fail("broken")\n',
                                                'managed', 'core')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        archive, expected = self.archive()
        with self.assertRaises(bs.SetupError) as raised:
            bs.update(self.source, self.target, 'academic', 'claude', archive, expected)
        message = str(raised.exception)
        self.assertIn('release preflight failed before project writes', message)
        self.assertIn('FAIL: test_breaks', message)
        logs = sorted((self.target / '.trace-os/receipts').glob('update-preflight-*.json'))
        self.assertEqual(len(logs), 1)
        self.assertIn(logs[0].relative_to(self.target).as_posix(), message)
        log = bs.load(logs[0])
        self.assertEqual(log['status'], 'fail')
        self.assertTrue(any(name.startswith('FAIL: test_breaks') for name in log['failed_tests']))
        self.assertEqual((self.target / 'scripts/example.py').read_bytes(), before)

    def test_chain_walks_every_nominated_pair_over_each_release_own_archive(self):
        self.with_hop_checks()
        self.release()
        self.apply()
        store = self.root / 'store'
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.pin_release(store)
        self.contents['scripts/example.py'] = ('print(3)\n', 'managed', 'core')
        self.release('test-3')
        receipt = self.chain(store)
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual([(hop['from'], hop['to'], hop['status']) for hop in receipt['hops']],
                         [('test-1', 'test-2', 'succeeded'), ('test-2', 'test-3', 'succeeded')])
        self.assertEqual(receipt['not_started'], [])
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-3')
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(3)\n')
        events = [(row['event'], row['status'], row['to']['version']) for row in self.history()]
        self.assertEqual(events, [('update', 'succeeded', 'test-2'), ('update', 'succeeded', 'test-3')])
        # Each hop ran over its own unpacked archive, not over this release's tree.
        for hop in receipt['hops']:
            self.assertNotEqual(hop['source'], str(self.source))
            self.assertFalse(Path(hop['source']).exists())
            self.assertEqual(hop['receipt']['version'], hop['to'])

    def test_chain_stops_where_a_hop_stops_and_leaves_the_earlier_hop_finalized(self):
        self.with_hop_checks()
        self.release()
        self.apply()
        store = self.root / 'store'
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.pin_release(store)
        self.with_hop_checks(check_exit=1)  # the third release's project check fails
        self.contents['scripts/example.py'] = ('print(3)\n', 'managed', 'core')
        self.release('test-3')
        receipt = self.chain(store)
        self.assertEqual(receipt['status'], 'stopped')
        self.assertNotEqual(receipt['exit_code'], 0)
        self.assertEqual([(hop['to'], hop['status']) for hop in receipt['hops']],
                         [('test-2', 'succeeded'), ('test-3', 'stopped')])
        self.assertIn('restored the last successful installation', receipt['hops'][-1]['error'])
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(2)\n')
        events = [(row['status'], row['to']['version']) for row in self.history()]
        self.assertEqual(events, [('succeeded', 'test-2'), ('failed', 'test-3')])
        self.assertIn('run the same command again', receipt['remaining'][0])

    def test_chain_refuses_to_start_when_an_archive_is_missing(self):
        self.with_hop_checks()
        self.release()
        self.apply()
        before = (self.target / bs.STATE).read_bytes()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        pinned = self.pin_release(self.root / 'elsewhere')  # pinned, but not in the store
        self.contents['scripts/example.py'] = ('print(3)\n', 'managed', 'core')
        self.release('test-3')
        empty = self.root / 'store'
        empty.mkdir()
        with self.assertRaisesRegex(bs.SetupError, 'no release was applied.*test-2.*' + pinned):
            self.chain(empty)
        with self.assertRaisesRegex(bs.SetupError, 'no --release-store'):
            self.chain(None)
        self.assertEqual((self.target / bs.STATE).read_bytes(), before)
        self.assertFalse((self.target / bs.UPDATE_HISTORY).exists())
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')

    def test_chain_needs_a_pinned_digest_and_a_nominated_path(self):
        self.with_hop_checks()
        self.release()
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.contents['scripts/example.py'] = ('print(3)\n', 'managed', 'core')
        self.release('test-3')  # no pin for test-2
        store = self.root / 'store'
        store.mkdir()
        with self.assertRaisesRegex(bs.SetupError, 'pins no digest'):
            self.chain(store)
        self.upgrades.clear()  # a manifest that nominates no pair from the installed release
        self.release('test-3')
        with self.assertRaisesRegex(bs.SetupError, 'no unique nominated pair leads on from test-1'):
            self.chain(store)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')

    def test_chain_is_a_no_op_at_the_current_release(self):
        self.with_hop_checks()
        self.release()
        self.apply()
        receipt = self.chain(None)
        self.assertEqual(receipt['status'], 'already current')
        self.assertEqual(receipt['hops'], [])
        self.assertFalse((self.target / bs.UPDATE_HISTORY).exists())

    def test_a_single_hop_chain_needs_no_store(self):
        self.with_hop_checks()
        self.release()
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        receipt = self.chain(None)
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')

    def test_completed_update_rolls_back_without_touching_research(self):
        self.apply()
        research = self.target / 'sources/research.txt'
        research.parent.mkdir(exist_ok=True)
        research.write_text('keep research')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        result = bs.rollback_update(self.target)
        self.assertEqual(result['active_version'], 'test-1')
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertEqual(research.read_text(), 'keep research')
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        self.assertIn('"event": "rollback"', (self.target / bs.UPDATE_HISTORY).read_text())

    def test_rollback_refuses_to_overwrite_a_later_edit(self):
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        (self.target / 'scripts/example.py').write_text('later human edit')
        with self.assertRaisesRegex(bs.SetupError, 'later edit'):
            bs.rollback_update(self.target)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'later human edit')

    def test_schema_change_is_rejected_before_project_writes(self):
        self.apply()
        project = bs.load(self.target / 'ai/project.json')
        project['schema_version'] = 2
        (self.target / 'ai/project.json').write_bytes(bs.encoded(project))
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        with self.assertRaisesRegex(bs.SetupError, 'project schema migration'):
            self.update()
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')

    def test_a_plan_two_releases_behind_names_the_chain(self):
        # AH-258: the refusal named the pair and not the walk that supports it.
        self.apply()
        self.release('test-2')
        self.release('test-3')
        with self.assertRaises(bs.SetupError) as raised:
            bs.plan(self.source, self.target, 'academic', 'claude')
        self.assertEqual(str(raised.exception), 'unsupported release pair: test-1 -> test-3; a project more than one '
                                                'release behind updates with --update-chain and --release-store '
                                                '(SETUP-AGENT.md, Update)')

    def test_interrupted_transaction_is_recovered_on_next_apply(self):
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-1')
        real = bs.write_atomic
        def interrupted(path, data):
            if path == self.target / bs.STATE: raise KeyboardInterrupt()
            return real(path, data)
        with mock.patch.object(bs, 'write_atomic', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertTrue((self.target / bs.JOURNAL).exists())
        receipt = self.apply()
        self.assertTrue(receipt['recovered'])
        self.assertFalse((self.target / bs.JOURNAL).exists())
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(2)\n')

    def test_recovery_refuses_to_erase_a_later_human_edit(self):
        self.target.mkdir()
        first = self.target / 'one.txt'; first.write_text('old')
        real = bs.write_atomic
        def interrupted(path, data):
            if path.name == 'two.txt': raise KeyboardInterrupt()
            return real(path, data)
        with mock.patch.object(bs, 'write_atomic', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            bs.transaction(self.target, {'one.txt': b'new', 'two.txt': b'new'})
        first.write_text('human later edit')
        with self.assertRaises(bs.SetupError): bs.recover(self.target)
        self.assertEqual(first.read_text(), 'human later edit')

    def test_symlink_and_parent_repository_targets_are_rejected(self):
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        self.target.mkdir()
        try:
            (self.target / 'scripts').symlink_to(elsewhere)
        except OSError as exc:
            self.skipTest('symlink creation is unavailable: ' + str(exc))
        with self.assertRaises(bs.SetupError): self.apply()
        self.assertFalse(list(elsewhere.iterdir()))
        bs.git(self.root, 'init', '-q')
        with self.assertRaises(bs.SetupError): bs.plan(self.source, self.root / 'nested')

    def test_skill_sync_preserves_canonical_source_and_detects_copy_edits(self):
        self.apply()
        source = self.target / '.skills/example/SKILL.md'
        copy = self.target / '.claude/skills/example/SKILL.md'
        source.write_text('changed canonical skill')
        bs.sync_skills(self.target)
        self.assertEqual(copy.read_text(), source.read_text())
        copy.write_text('independent copy edit')
        source.write_text('second canonical edit')
        with self.assertRaises(bs.SetupError): bs.sync_skills(self.target)
        self.assertEqual(copy.read_text(), 'independent copy edit')

    def test_core_profile_omits_documents_and_reruns_in_place(self):
        self.apply(profile='core', agent='none')
        self.assertFalse((self.target / 'paper').exists())
        bs.apply(self.target, self.target, profile='core', agent='none')

    def test_modification_after_plan_is_not_overwritten(self):
        self.target.mkdir()
        path = self.target / 'one.txt'; path.write_text('human edit')
        with self.assertRaises(bs.SetupError):
            bs.transaction(self.target, {'one.txt': b'new'}, {'one.txt': bs.digest(b'old')})
        self.assertEqual(path.read_text(), 'human edit')

    # AH-168: an update stopped during its closing check.

    def test_an_interrupted_check_restores_the_previous_installation_and_records_it(self):
        # Ctrl-C or an agent's command timeout during the post-update check left the
        # unchecked candidate installed, with no history event and nothing pending.
        self.apply()
        old_state = (self.target / bs.STATE).read_bytes()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        archive, expected = self.archive()
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}
        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                bs.update(self.source, self.target, 'academic', 'claude', archive, expected,
                          executor='test-agent')
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertEqual((self.target / bs.STATE).read_bytes(), old_state)
        self.assertFalse((self.target / bs.UPDATE_PENDING).exists())
        event = self.history()[-1]
        self.assertEqual((event['event'], event['status'], event['to']['version']),
                         ('update', 'interrupted', 'test-2'))
        self.assertEqual((event['cause'], event['execution_agent'], event['checks']),
                         ('KeyboardInterrupt', 'test-agent', [passed]))
        self.assertFalse(bs.plan(self.source, self.target, 'academic', 'claude')[0]['pending_recovery'])

    @unittest.skipIf(os.name == 'nt', 'POSIX signals')
    def test_sigterm_during_the_check_restores_instead_of_ending_the_process(self):
        # The fixture's own handler absorbs a SIGTERM the update does not handle,
        # so a regression fails this test rather than ending the test run.
        absorbed = []
        guard = lambda signum, frame: absorbed.append(signum)
        self.addCleanup(signal.signal, signal.SIGTERM, signal.signal(signal.SIGTERM, guard))
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        archive, expected = self.archive()
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}

        def terminated(target, run_id):
            os.kill(os.getpid(), signal.SIGTERM)
            return {'name': 'updated project workflow', 'status': 'pass', 'exit_code': 0}

        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', side_effect=terminated):
            with self.assertRaises(bs.Interrupted):
                bs.update(self.source, self.target, 'academic', 'claude', archive, expected)
        self.assertEqual(absorbed, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), guard)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        self.assertEqual(self.history()[-1]['cause'], 'Interrupted: SIGTERM')

    @unittest.skipIf(os.name == 'nt', 'process groups are POSIX')
    def test_an_interrupted_check_stops_the_processes_it_started(self):
        pids = self.root / 'pids'
        script = ('import subprocess, sys, time\n'
                  'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                  f'open({str(pids)!r}, "w").write(str(child.pid))\n'
                  'time.sleep(60)\n')

        def interrupted(process, *args, **kwargs):
            deadline = time.monotonic() + 20
            while (not pids.exists() or not pids.read_text()) and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt()

        with mock.patch.object(subprocess.Popen, 'communicate', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                bs.command_check([sys.executable, '-c', script], self.root, 'check')
        started = int(pids.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(started, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(started, signal.SIGKILL)
        self.fail('the interrupted check left a process it started running')

    def test_plan_and_apply_find_an_update_that_stopped_without_restoring(self):
        # A process killed outright leaves the candidate and its marker. The plan read
        # only the transaction journal, and a following --apply rewrote install.json,
        # after which recovery, rollback and every later update refused.
        self.apply()
        old_state = (self.target / bs.STATE).read_bytes()
        self.leave_update_unfinished('test-2', 'print(2)\n')
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertTrue(summary['pending_recovery'])
        self.assertEqual(summary['pending_update']['to'], 'test-2')
        self.assertTrue(summary['pending_update']['state'].startswith('unfinished'))
        with self.assertRaisesRegex(bs.SetupError, 'unfinished update was restored first; version changes require --update'):
            self.apply()
        self.assertEqual((self.target / bs.STATE).read_bytes(), old_state)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        self.assertFalse((self.target / bs.UPDATE_PENDING).exists())
        event = self.history()[-1]
        self.assertEqual((event['status'], event['to']['version']), ('interrupted', 'test-2'))
        self.assertIn('a later run restored', event['cause'])
        self.update()
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')

    def test_skill_sync_waits_for_recovery_of_an_unfinished_update(self):
        self.apply()
        self.leave_update_unfinished('test-2', 'print(2)\n')
        with self.assertRaisesRegex(bs.SetupError, 'run --recover'):
            bs.sync_skills(self.target)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')
        with mock.patch('sys.stdout'):
            self.assertEqual(bs.main(['--target', str(self.target), '--recover']), 0)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        self.assertEqual(self.history()[-1]['status'], 'interrupted')
        bs.sync_skills(self.target)

    def test_chain_restores_an_unfinished_update_before_reading_the_installed_release(self):
        # The chain read the candidate's version from install.json and reported
        # "already current" over an update nobody had checked.
        self.with_hop_checks()
        self.release()
        self.apply()
        self.leave_update_unfinished('test-2', 'print(2)\n')
        receipt = self.chain(None)
        self.assertEqual(receipt['recovered']['to'], 'test-2')
        self.assertEqual([(hop['from'], hop['to'], hop['status']) for hop in receipt['hops']],
                         [('test-1', 'test-2', 'succeeded')])
        self.assertEqual([(row['status'], row['to']['version']) for row in self.history()],
                         [('interrupted', 'test-2'), ('succeeded', 'test-2')])

    # AH-169: rollback keeps its documented meaning and reports a recovery apart.

    def test_rollback_reports_a_restored_unfinished_update_apart_from_the_rollback(self):
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        self.leave_update_unfinished('test-3', 'print(3)\n')
        with mock.patch('sys.stdout'):
            self.assertEqual(bs.main(['--target', str(self.target), '--rollback']), 0)
        receipt = bs.load(self.target / '.trace-os/receipts/latest.json')
        self.assertEqual(receipt['recovered_update']['to'], 'test-3')
        self.assertIn('had not finished', receipt['recovered_update']['outcome'])
        self.assertEqual(receipt['active_version'], 'test-1')
        self.assertEqual([(row['event'], row.get('status'), row['to']['version']) for row in self.history()],
                         [('update', 'succeeded', 'test-2'), ('update', 'interrupted', 'test-3'),
                          ('rollback', None, 'test-1')])

    def test_rollback_with_only_an_unfinished_update_says_it_restored_that(self):
        self.apply()
        self.leave_update_unfinished('test-2', 'print(2)\n')
        with self.assertRaisesRegex(bs.SetupError, 'had not finished.*update to test-2.*nothing more was rolled back'):
            bs.rollback(self.target)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
        self.assertEqual(self.history()[-1]['status'], 'interrupted')

    def test_a_completed_update_whose_marker_survived_is_not_reported_as_restored(self):
        # Finalizing can stop after the history records success and before the
        # marker goes. That update completed; rollback undoes it once.
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        done = self.history()[-1]
        marker = {key: done[key] for key in ('run_id', 'from', 'to', 'entries', 'changes',
                                              'instruction_sections', 'execution_agent')}
        marker.update(schema_version=1, started_at=done['recorded_at'])
        (self.target / bs.UPDATE_PENDING).write_bytes(bs.encoded(marker))
        self.assertTrue(bs.plan(self.source, self.target, 'academic', 'claude')[0]
                        ['pending_update']['state'].startswith('completed'))
        with mock.patch('sys.stdout'):
            self.assertEqual(bs.main(['--target', str(self.target), '--rollback']), 0)
        receipt = bs.load(self.target / '.trace-os/receipts/latest.json')
        self.assertIn('only its leftover marker was cleared', receipt['recovered_update']['outcome'])
        self.assertEqual(receipt['active_version'], 'test-1')
        self.assertEqual([row['event'] for row in self.history()], ['update', 'rollback'])

    def test_rerunning_apply_after_an_update_keeps_the_archive_identity_and_rollback(self):
        # AH-170: the repeatability rerun SETUP-AGENT.md recommends dropped
        # release.archive_sha256 while reporting no changes, and rollback then refused.
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        self.update()
        updated = (self.target / bs.STATE).read_bytes()
        self.assertIn('archive_sha256', bs.load(self.target / bs.STATE)['release'])
        self.assertEqual(self.apply()['changes'], {})
        self.assertEqual((self.target / bs.STATE).read_bytes(), updated)
        self.assertEqual(bs.rollback_update(self.target)['active_version'], 'test-1')

    # AH-171: the seed an earlier release installed, never edited, is a legacy baseline.

    def legacy_seed_project(self, appended=b''):
        """An install whose AGENTS.md is still an earlier release's seed, while the
        migration's legacy baseline names the release just before this one."""
        self.apply()
        first = b'rules the first release installed\n'
        (self.target / 'AGENTS.md').write_bytes(first + appended)
        state = bs.load(self.target / bs.STATE)
        state.pop('instruction_sections')
        state['files']['AGENTS.md']['sha256'] = bs.digest(first)
        (self.target / bs.STATE).write_bytes(bs.encoded(state))
        self.section_version = 2
        self.contents['AGENTS.md'] = (self.section('bounded shared rules'), 'seed', 'core')
        self.release('test-2')
        manifest = bs.load(self.source / 'starter-manifest.json')
        second = b'the previous release rules, which this project never received\n'
        manifest['instruction_sections'][0]['legacy'] = [{
            'from_version': 'test-1', 'mode': 'prefix', 'size': len(second), 'sha256': bs.digest(second)}]
        (self.source / 'starter-manifest.json').write_bytes(bs.encoded(manifest))

    def test_an_unedited_earlier_seed_counts_as_the_legacy_baseline(self):
        # An untouched rc2 install stopped at rc4 to rc5 on ai/SKILLS-IN-USE.md for this.
        self.legacy_seed_project()
        receipt = self.update()
        self.assertEqual((self.target / 'AGENTS.md').read_text(), self.section('bounded shared rules'))
        row = receipt['instruction_sections'][0]
        self.assertEqual((row['action'], row['baseline']),
                         ('adopt-bounded-section', 'the seed an earlier release installed, unedited'))

    def test_project_text_after_an_earlier_seed_is_kept(self):
        self.legacy_seed_project(b'\n## Project rules\nKeep me.\n')
        self.update()
        self.assertEqual((self.target / 'AGENTS.md').read_text(),
                         self.section('bounded shared rules') + '\n## Project rules\nKeep me.\n')

    def test_an_edited_earlier_seed_still_keeps_the_update_pending(self):
        self.legacy_seed_project()
        (self.target / 'AGENTS.md').write_bytes(b'rules the first release installed, reworded here\n')
        with self.assertRaisesRegex(bs.SetupError, 'update is pending: instruction:core-workflow'):
            self.update()
        self.assertIn('reworded here', (self.target / 'AGENTS.md').read_text())

    # AH-172: retiring a shipped example skill, and files that are not skills.

    def test_a_removed_example_skill_is_retired_and_stays_retired(self):
        # SKILLS-IN-USE.md invites replacing the examples, but removing one made
        # check_skills fail, --sync-skills refuse every skill and --apply recreate it.
        import check_skills
        self.contents['.skills/other/SKILL.md'] = ('other skill\n', 'managed', 'core')
        self.release()
        self.apply()
        shutil.rmtree(self.target / '.skills/other')
        self.assertIn('canonical skill removed', ' '.join(check_skills.check(self.target)[1]))
        result = bs.sync_skills(self.target)
        self.assertEqual((result['retired'], result['removed']), (['other'], ['.claude/skills/other/SKILL.md']))
        self.assertFalse((self.target / '.claude/skills/other').exists())
        self.assertEqual(check_skills.check(self.target), (1, []))
        retired = (self.target / bs.STATE).read_bytes()
        self.assertEqual(self.apply()['changes'], {})
        self.assertFalse((self.target / '.skills/other').exists())
        self.assertEqual((self.target / bs.STATE).read_bytes(), retired)
        # An update keeps it retired, under a new name too.
        del self.contents['.skills/other/SKILL.md']
        self.contents['.skills/renamed-other/SKILL.md'] = ('other skill\n', 'managed', 'core')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.renames = [{'from_version': 'test-1', 'from': '.skills/other/SKILL.md',
                         'to': '.skills/renamed-other/SKILL.md'}]
        self.release('test-2')
        self.update()
        self.assertFalse((self.target / '.skills/renamed-other').exists())
        self.assertEqual(bs.load(self.target / bs.STATE)['retired_skills'], ['other', 'renamed-other'])
        self.assertEqual(check_skills.check(self.target), (1, []))

    def test_an_in_place_plan_and_apply_run_after_a_retirement(self):
        # AH-221: in an in-place install the project is its own source, so after a
        # retirement the skill's files are gone and setup, reading every release
        # file first, failed with FileNotFoundError on --plan and --apply.
        self.contents['.skills/other/SKILL.md'] = ('other skill\n', 'managed', 'core')
        self.release()
        self.apply()
        shutil.rmtree(self.target / '.skills/other')
        bs.sync_skills(self.target)
        retired = (self.target / bs.STATE).read_bytes()
        summary = bs.plan(self.target, self.target)[0]
        self.assertEqual(summary['conflicts'], [])
        bs.apply(self.target, self.target)
        self.assertFalse((self.target / '.skills/other').exists())
        self.assertEqual(bs.load(self.target / bs.STATE)['retired_skills'], ['other'])
        self.assertEqual((self.target / bs.STATE).read_bytes(), retired)

    def test_a_removed_example_skill_is_retired_under_agent_none(self):
        self.apply(agent='none')
        shutil.rmtree(self.target / '.skills/example')
        self.assertEqual(bs.sync_skills(self.target)['retired'], ['example'])
        self.assertEqual(bs.apply(self.source, self.target, agent='none')['changes'], {})
        self.assertFalse((self.target / '.skills/example').exists())

    def test_a_retirement_is_undone_by_removing_the_id_from_the_state(self):
        # LESSON (AH-172): the seed invited replacing the examples without saying how
        # a retirement is made or undone; the undo it now states is this one.
        import check_skills
        self.apply()
        shutil.rmtree(self.target / '.skills/example')
        self.assertEqual(bs.sync_skills(self.target)['retired'], ['example'])
        state = bs.load(self.target / bs.STATE)
        state['retired_skills'].remove('example')
        (self.target / bs.STATE).write_bytes(bs.encoded(state))
        self.apply()
        self.assertTrue((self.target / '.skills/example/SKILL.md').is_file())
        self.assertTrue((self.target / '.claude/skills/example/SKILL.md').is_file())
        self.assertEqual(check_skills.check(self.target)[1], [])

    @unittest.skipUnless((SCRIPTS.parents[1] / 'export-manifest.json').is_file(),
                         'the starter package is read only in the kit repository')
    def test_the_seed_and_the_setup_guide_say_how_to_retire_and_restore(self):
        # LESSON (AH-172): an installed project's copy of the seed is its own to edit.
        files = SCRIPTS.parent
        for name in ('ai/SKILLS-IN-USE.md', 'SETUP-AGENT.md'):
            with self.subTest(name=name):
                text = ' '.join((files / name).read_text(encoding='utf-8').split())
                self.assertIn('remove its id from `retired_skills` in `.trace-os/install.json`', text)

    def test_an_edited_copy_of_a_removed_skill_is_protected(self):
        self.apply()
        copy = self.target / '.claude/skills/example/SKILL.md'
        copy.write_text('my own notes on the example')
        shutil.rmtree(self.target / '.skills/example')
        with self.assertRaisesRegex(bs.SetupError, 'edited separately.*claude/skills/example/SKILL.md'):
            bs.sync_skills(self.target)
        self.assertEqual(copy.read_text(), 'my own notes on the example')
        self.assertNotIn('retired_skills', bs.load(self.target / bs.STATE))

    def test_operating_system_and_ignored_files_are_not_skills(self):
        # A .DS_Store under .skills/ was synced as a managed copy, and once it went,
        # sync refused and check_skills stopped on a file error.
        import check_skills
        self.apply()
        litter = self.target / '.skills/example/.DS_Store'
        litter.write_bytes(b'\0finder')
        with open(self.target / '.git/info/exclude', 'a', encoding='utf-8') as exclude:
            exclude.write('*.scratch\n')
        (self.target / '.skills/example/draft.scratch').write_text('draft')
        self.assertEqual(check_skills.check(self.target), (1, []))
        self.assertEqual(bs.sync_skills(self.target)['synced'], 0)
        self.assertFalse((self.target / '.claude/skills/example/.DS_Store').exists())
        self.assertFalse((self.target / '.claude/skills/example/draft.scratch').exists())
        # An entry an earlier sync made for one is dropped, and the file going breaks nothing.
        state = bs.load(self.target / bs.STATE)
        state['files']['.claude/skills/example/.DS_Store'] = {
            'sha256': bs.digest(b'\0finder'), 'policy': 'managed', 'canonical': '.skills/example/.DS_Store'}
        (self.target / bs.STATE).write_bytes(bs.encoded(state))
        litter.unlink()
        self.assertEqual(check_skills.check(self.target), (1, []))
        bs.sync_skills(self.target)
        self.assertNotIn('.claude/skills/example/.DS_Store', bs.load(self.target / bs.STATE)['files'])

    def test_a_deleted_instruction_file_is_a_named_conflict_with_a_restoration_hint(self):
        # AH-173: every update plan stopped on a file error instead.
        self.apply()
        (self.target / 'AGENTS.md').unlink()
        self.section_version = 2
        self.contents['AGENTS.md'] = (self.section('revised shared instructions'), 'seed', 'core')
        self.release('test-2')
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], ['missing-instruction:AGENTS.md'])
        self.assertIn('git checkout HEAD -- AGENTS.md', summary['instruction_sections'][0]['hint'])
        with self.assertRaisesRegex(bs.SetupError, r'^update is pending: missing-instruction:AGENTS.md; '
                                    r'restore AGENTS.md \(git checkout HEAD -- AGENTS.md\), then update again$'):
            self.update()
        self.assertFalse((self.target / 'AGENTS.md').exists())
        bs.git(self.target, 'checkout', 'HEAD', '--', 'AGENTS.md')
        self.update()
        self.assertIn('revised shared instructions', (self.target / 'AGENTS.md').read_text())

    @unittest.skipIf(os.name == 'nt', 'file modes are not applied on Windows')
    def test_restored_and_candidate_shell_scripts_keep_their_mode(self):
        # AH-174: files are written anew with the default mode, and the mode was set
        # only after a successful update, so rollback, recovery and a failed update
        # left scripts/example.sh non-executable, and the check saw it that way.
        self.with_shell_script()
        self.apply()
        script = self.target / 'scripts/example.sh'
        self.contents['scripts/example.sh'] = ('#!/usr/bin/env bash\necho two\n', 'managed', 'core')
        self.release('test-2')
        archive, expected = self.archive()
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}
        seen = []

        def failing(target, run_id):
            seen.append(os.access(script, os.X_OK))
            return {'name': 'updated project workflow', 'status': 'fail', 'exit_code': 1}

        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', side_effect=failing):
            with self.assertRaises(bs.SetupError):
                bs.update(self.source, self.target, 'academic', 'claude', archive, expected)
        self.assertEqual(seen, [True], 'the check saw the candidate script without its mode')
        self.assertTrue(os.access(script, os.X_OK), 'after a failed update')
        self.update()
        bs.rollback_update(self.target)
        self.assertTrue(os.access(script, os.X_OK), 'after a rollback')
        real = bs.write_atomic

        def interrupted(path, data):
            if path == self.target / 'scripts/example.py':
                raise KeyboardInterrupt()
            return real(path, data)

        with mock.patch.object(bs, 'write_atomic', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            bs.transaction(self.target, {'scripts/example.sh': b'#!/bin/sh\n', 'scripts/example.py': b'print(9)\n'})
        bs.recover(self.target)
        self.assertTrue(os.access(script, os.X_OK), 'after recovering a transaction')

    def test_an_edit_saved_between_plan_and_write_is_not_overwritten_by_an_update(self):
        # AH-175: --apply compared the plan's hashes before writing; --update took a
        # fresh snapshot, so the edit survived only in the local backup.
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        real = bs.update_payload
        for name, edit in (('scripts/example.py', b'print("saved while the update ran")\n'),
                           (bs.STATE, None)):
            with self.subTest(name=name):
                path = self.target / name
                original = path.read_bytes()
                edit = edit or original.replace(b'{', b'{\n  "note": "mine",', 1)

                def edited_meanwhile(summary, desired):
                    path.write_bytes(edit)
                    return real(summary, desired)

                with mock.patch.object(bs, 'update_payload', side_effect=edited_meanwhile):
                    with self.assertRaisesRegex(bs.SetupError, 'changed since the update plan: ' + name):
                        self.update()
                self.assertEqual(path.read_bytes(), edit)
                self.assertFalse((self.target / bs.UPDATE_PENDING).exists())
                self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')
                path.write_bytes(original)

    def test_a_held_rename_does_not_list_its_untouched_copy_as_kept(self):
        # AH-178: obsolete_preserved named the untouched generated copy, which the
        # update removes as soon as the rename resumes.
        self.rename_the_example_skill()
        (self.target / '.skills/example/SKILL.md').write_bytes(b'skill, edited\n')
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], ['.skills/example/SKILL.md'])
        self.assertEqual(summary['renamed']['retained'][0]['paths'],
                         ['.skills/example/SKILL.md', '.claude/skills/example/SKILL.md'])
        self.assertEqual(summary['obsolete_preserved'], [])

    # AH-122: a project's edit to a managed file the new release ships unchanged.

    def edit_the_ignore_file_and_release(self, version='test-2'):
        """Append a project rule below the kit's lines, then release a version that leaves the file alone."""
        self.apply()
        ignore = self.target / '.gitignore'
        kit = ignore.read_bytes()
        ignore.write_bytes(kit + b'# project rules\nmy-data/\n')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release(version)
        return ignore, kit, ignore.read_bytes()

    def test_a_project_edit_to_a_file_the_release_leaves_unchanged_is_kept(self):
        # Met updating ai-coordination-writing from rc15 to rc16: rc16's .gitignore
        # was byte-identical to rc15's, and the project's own rules below the kit's
        # lines were the plan's only conflict.
        ignore, kit, edited = self.edit_the_ignore_file_and_release()
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], [])
        self.assertEqual(summary['kept_project_edits'], ['.gitignore'])
        self.assertNotIn('.gitignore', summary['changes'])
        receipt = self.update()
        self.assertEqual(ignore.read_bytes(), edited)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(2)\n')
        self.assertEqual(receipt['kept_project_edits'], ['.gitignore'])
        event = self.history()[-1]
        self.assertEqual(event['kept_project_edits'], ['.gitignore'])
        self.assertNotIn('.gitignore', event['entries'])
        state = bs.load(self.target / bs.STATE)
        self.assertEqual(state['version'], 'test-2')
        # The state keeps the kit's digest, not the project's, so the edit stays visible.
        self.assertEqual(state['files']['.gitignore']['sha256'], bs.digest(kit))

    def test_a_kept_edit_is_a_conflict_once_a_release_changes_the_file(self):
        ignore, kit, edited = self.edit_the_ignore_file_and_release()
        self.update()
        self.contents['.gitignore'] = (kit.decode() + 'kit-rule/\n', 'managed', 'core')
        self.release('test-3')
        with self.assertRaisesRegex(bs.SetupError, r'update is pending: \.gitignore; each was changed in this project'):
            self.update()
        self.assertEqual(ignore.read_bytes(), edited)
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-2')
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual((summary['conflicts'], summary['kept_project_edits']), (['.gitignore'], []))

    def test_rolling_back_an_update_that_kept_a_project_edit_leaves_the_edit(self):
        ignore, kit, edited = self.edit_the_ignore_file_and_release()
        self.update()
        bs.rollback_update(self.target)
        self.assertEqual(ignore.read_bytes(), edited)
        self.assertEqual((self.target / 'scripts/example.py').read_text(), 'print(1)\n')
        state = bs.load(self.target / bs.STATE)
        self.assertEqual(state['version'], 'test-1')
        self.assertEqual(state['files']['.gitignore']['sha256'], bs.digest(kit))

    def test_an_edited_skill_the_release_leaves_unchanged_is_kept_with_its_synced_copy(self):
        import check_skills
        self.apply()
        canonical = self.target / '.skills/example/SKILL.md'
        copy = self.target / '.claude/skills/example/SKILL.md'
        canonical.write_bytes(b'skill, with the project\'s own step\n')
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        # Out of step with its copy, the edit is still a conflict: keeping it would
        # leave the installed-skill check failing after the update.
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], ['.skills/example/SKILL.md'])
        bs.sync_skills(self.target)
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], [])
        self.assertEqual(summary['kept_project_edits'],
                         ['.claude/skills/example/SKILL.md', '.skills/example/SKILL.md'])
        self.update()
        self.assertEqual(canonical.read_bytes(), b'skill, with the project\'s own step\n')
        self.assertEqual(copy.read_bytes(), canonical.read_bytes())
        self.assertEqual(check_skills.check(self.target), (1, []))
        # A copy then edited on its own puts the pair out of step: both are conflicts again.
        copy.write_bytes(b'a separate note in the copy\n')
        self.contents['scripts/example.py'] = ('print(3)\n', 'managed', 'core')
        self.release('test-3')
        summary = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertEqual(summary['conflicts'], ['.claude/skills/example/SKILL.md', '.skills/example/SKILL.md'])
        self.assertEqual(summary['kept_project_edits'], [])

    @unittest.skipIf(os.name == 'nt', 'process groups are POSIX')
    def test_an_interrupted_check_stops_a_process_that_ignores_sigterm(self):
        # AH-168 recheck: the check itself ended on SIGTERM, so the group never got
        # SIGKILL and a process it started that ignores SIGTERM kept running.
        pids = self.root / 'pids'
        child = ('import os, signal, time\n'
                 'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                 f'open({str(pids)!r}, "w").write(str(os.getpid()))\n'
                 'time.sleep(60)\n')
        script = ('import subprocess, sys, time\n'
                  f'subprocess.Popen([sys.executable, "-c", {child!r}])\n'
                  'time.sleep(60)\n')

        def interrupted(process, *args, **kwargs):
            deadline = time.monotonic() + 20
            while (not pids.exists() or not pids.read_text()) and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt()

        with mock.patch.object(subprocess.Popen, 'communicate', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                bs.command_check([sys.executable, '-c', script], self.root, 'check')
        started = int(pids.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(started, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(started, signal.SIGKILL)
        self.fail('a process the check started that ignores SIGTERM kept running')

    def test_on_windows_an_interrupted_check_ends_its_process_tree(self):
        # AH-168: the Windows branch killed the check alone, leaving what it started.
        process = mock.Mock(pid=4321, stdout=None, stderr=None)
        process.poll.return_value = None
        with mock.patch.object(bs.subprocess, 'run') as run:
            bs.stop_process(process, windows=True)
        self.assertEqual(run.call_args.args[0], ['taskkill', '/T', '/F', '/PID', '4321'])
        process.wait.assert_called()

    def test_an_edit_saved_after_the_plan_check_and_before_the_backup_is_not_overwritten(self):
        # AH-175 recheck: begin_update took a fresh hash as the baseline after checking
        # the plan's, so an edit saved in between became the baseline, was backed up
        # and overwritten, surviving only in the backup.
        self.apply()
        self.contents['scripts/example.py'] = ('print(2)\n', 'managed', 'core')
        self.release('test-2')
        path = self.target / 'scripts/example.py'
        edit = b'print("saved between the check and the backup")\n'
        real_hash, real_begin = bs.file_hash, bs.begin_update

        def begin(*args, **kwargs):
            def hash_then_edit(candidate):
                result = real_hash(candidate)
                if candidate == path and not path.read_bytes() == edit:
                    path.write_bytes(edit)  # just after the plan's hash was checked
                return result
            with mock.patch.object(bs, 'file_hash', side_effect=hash_then_edit):
                return real_begin(*args, **kwargs)

        with mock.patch.object(bs, 'begin_update', side_effect=begin):
            with self.assertRaisesRegex(bs.SetupError, 'changed since the update plan: scripts/example.py'):
                self.update()
        self.assertEqual(path.read_bytes(), edit)
        self.assertFalse((self.target / bs.UPDATE_PENDING).exists())
        self.assertEqual(bs.load(self.target / bs.STATE)['version'], 'test-1')

    def test_retiring_every_shipped_skill_leaves_the_parity_check_passing(self):
        # AH-208: with the last shipped skill retired, zero installed skills is the
        # intended set, and the check reported it as a problem.
        import check_skills
        self.apply()
        shutil.rmtree(self.target / '.skills/example')
        self.assertEqual(bs.sync_skills(self.target)['retired'], ['example'])
        self.assertEqual(check_skills.check(self.target), (0, []))
        state = bs.load(self.target / bs.STATE)
        del state['retired_skills']
        (self.target / bs.STATE).write_bytes(bs.encoded(state))
        self.assertEqual(check_skills.check(self.target),
                         (0, ['no installed skills recorded for the selected agent']))

    # The state folder's names, 0.1.0-rc19 and 0.1.0-rc20 ---------------------------------------
    # The moves themselves are tested once per hop, in StateFolderMove below.

    def test_a_fresh_install_writes_the_new_state_folder_and_markers(self):
        self.state_folder = bs.STATE_DIR
        self.release()
        receipt = self.apply()
        self.assertTrue((self.target / '.trace-os/install.json').is_file())
        self.assertTrue((self.target / '.trace-os/receipts/latest.json').is_file())
        for earlier in bs.EARLIER_STATE_DIRS:
            self.assertFalse((self.target / earlier).exists())
        self.assertEqual(bs.state_dir(self.target), '.trace-os')
        self.assertIn('<!-- trace-os:begin core-workflow -->', (self.target / 'AGENTS.md').read_text())
        self.assertNotIn('state_folder', receipt)
        self.assertIn('.trace-os/install.json', self.git_target('ls-files'))

    def test_the_newest_key_wins_over_the_earlier_ones(self):
        self.apply()
        project = bs.load(self.target / 'ai/project.json')
        project['trace_kit'] = {'instruction_overrides': {'core-workflow': {'basis': 'oldest'}}}
        self.assertEqual(self.overrides_with(project), {'core-workflow': {'basis': 'oldest'}})
        project['traceware'] = {'instruction_overrides': {'core-workflow': {'basis': 'old'}}}
        self.assertEqual(self.overrides_with(project), {'core-workflow': {'basis': 'old'}})
        project['trace_os'] = {'instruction_overrides': {'core-workflow': {'basis': 'new'}}}
        self.assertEqual(self.overrides_with(project), {'core-workflow': {'basis': 'new'}})

    def overrides_with(self, project):
        (self.target / 'ai/project.json').write_bytes(bs.encoded(project))
        return bs.instruction_overrides(self.target)

    def git_target(self, *args):
        return subprocess.run(['git', *args], cwd=self.target, capture_output=True, text=True,
                              check=True).stdout

    def test_every_archive_root_is_read_and_two_are_refused(self):
        self.assertEqual(bs.ARCHIVE_ROOTS, ('trace-os', 'traceware', 'trace-kit-starter'))
        for root in bs.ARCHIVE_ROOTS:
            with self.subTest(root=root):
                archive, sha256 = self.full_archive(root)
                self.assertEqual(bs.verify_release_archive(self.source, archive, sha256), sha256)
                workdir = Path(tempfile.mkdtemp(dir=self.root))
                self.assertEqual(bs.unpack_release(archive, workdir), workdir / root)
        both = self.root / 'both.zip'
        with zipfile.ZipFile(both, 'w') as zipped:
            for root in bs.ARCHIVE_ROOTS[:2]:
                zipped.write(self.source / 'starter-manifest.json', root + '/starter-manifest.json')
        with self.assertRaises(bs.SetupError):
            bs.verify_release_archive(self.source, both, bs.digest(both.read_bytes()))
        with self.assertRaisesRegex(bs.SetupError, 'one top folder'):
            bs.unpack_release(both, Path(tempfile.mkdtemp(dir=self.root)))

    def test_state_paths_are_read_in_the_newest_folder_first_then_each_earlier_one(self):
        self.assertEqual(bs.STATE_DIRS, ('.trace-os', '.traceware', '.trace-kit'))
        self.target.mkdir()
        self.assertEqual(bs.state_dir(self.target), '.trace-os')
        (self.target / '.trace-kit').mkdir()
        self.assertEqual(bs.in_layout(self.target, bs.STATE), '.trace-kit/install.json')
        (self.target / '.traceware').mkdir()
        self.assertEqual(bs.state_dir(self.target), '.traceware')
        (self.target / '.trace-os').mkdir()
        (self.target / '.trace-kit/install.json').write_text('{}')
        # The folder holding install.json wins over a stray folder of a newer name.
        self.assertEqual(bs.state_dir(self.target), '.trace-kit')
        (self.target / '.traceware/install.json').write_text('{}')
        self.assertEqual(bs.state_dir(self.target), '.traceware')
        (self.target / '.trace-os/install.json').write_text('{}')
        self.assertEqual(bs.state_dir(self.target), '.trace-os')
        (self.target / '.trace-os/install.json').unlink()
        (self.target / '.trace-os/update-backups').mkdir()
        (self.target / '.trace-os/update-backups/x').write_text('kept')
        # A path a history recorded before one move, or two, is read where it is now.
        for earlier in bs.EARLIER_STATE_DIRS:
            self.assertEqual(bs.recorded_path(self.target, earlier + '/update-backups/x'),
                             '.trace-os/update-backups/x')
        self.assertEqual(bs.recorded_path(self.target, '.trace-kit/install.json'), '.trace-kit/install.json')
        # A move goes to a newer name only.
        self.assertTrue(bs.newer_state_dir('.trace-os', '.trace-kit'))
        self.assertTrue(bs.newer_state_dir('.traceware', '.trace-kit'))
        self.assertFalse(bs.newer_state_dir('.traceware', '.trace-os'))
        self.assertFalse(bs.newer_state_dir('.trace-os', '.trace-os'))
        self.assertFalse(bs.newer_state_dir(None, '.trace-kit'))
        self.assertFalse(bs.newer_state_dir('.elsewhere', '.trace-kit'))

    def test_a_manifest_naming_an_older_folder_or_none_moves_nothing(self):
        self.target.mkdir()
        (self.target / '.traceware').mkdir()
        (self.target / '.traceware/install.json').write_text('{}')
        for named in (None, '.trace-kit', '.traceware', '.elsewhere'):
            with self.subTest(named=named):
                manifest = {} if named is None else {'install_state_dir': named}
                self.assertIsNone(bs.planned_move(manifest, self.target))
        self.assertEqual(bs.planned_move({'install_state_dir': '.trace-os'}, self.target),
                         {'from': '.traceware', 'to': '.trace-os'})

    def test_the_ignore_rules_gain_the_new_folder_after_each_earlier_one(self):
        data = (b'# kit\n.trace-kit/receipts/\r\n.traceware/receipts/\n/.trace-kit/backups/\n!.traceware/keep\n'
                b'other/.trace-kit-like/\n.traceware')
        result, added = bs.with_state_ignores(data, {'from': '.traceware', 'to': '.trace-os'})
        self.assertEqual(result, b'# kit\n.trace-kit/receipts/\r\n.trace-os/receipts/\r\n.traceware/receipts/\n'
                                 b'/.trace-kit/backups/\n/.trace-os/backups/\n!.traceware/keep\n!.trace-os/keep\n'
                                 b'other/.trace-kit-like/\n.traceware\n.trace-os\n')
        self.assertEqual(added, ['.trace-os/receipts/', '/.trace-os/backups/', '!.trace-os/keep', '.trace-os'])
        # A rule already present is not added twice.
        self.assertEqual(bs.with_state_ignores(result, {'from': '.traceware', 'to': '.trace-os'}), (result, []))
        # The hop to 0.1.0-rc19 adds .traceware/ after .trace-kit/ only, never a newer name's rule.
        result, added = bs.with_state_ignores(b'.trace-kit/receipts/\n.trace-os/x\n',
                                              {'from': '.trace-kit', 'to': '.traceware'})
        self.assertEqual((result, added), (b'.trace-kit/receipts/\n.traceware/receipts/\n.trace-os/x\n',
                                           ['.traceware/receipts/']))

    def test_the_chain_moves_the_folder_twice_from_the_oldest_name(self):
        """rc18 to rc19 to rc20 in one chain: .trace-kit/ to .traceware/ to .trace-os/, markers alike."""
        self.with_hop_checks()
        store = self.root / 'store'
        steps = (('trace-kit', None, '.trace-kit'), ('traceware', '.traceware', '.traceware'),
                 ('trace-os', '.trace-os', '.trace-os'))
        versions = ('0.1.0-rc18', '0.1.0-rc19', '0.1.0-rc20')
        for (prefix, folder, ignored), version in zip(steps, versions):
            self.prefix, self.state_folder = prefix, folder
            self.contents['AGENTS.md'] = (self.section('instructions'), 'seed', 'core')
            self.contents['.gitignore'] = (f'{ignored}/receipts/\n{ignored}/backups/\n{ignored}/setup.lock\n'
                                           f'{ignored}/transaction.json\n', 'managed', 'core')
            self.contents['scripts/example.py'] = (f'print({version!r})\n', 'managed', 'core')
            self.release(version)
            if version == versions[0]:
                self.apply()
                os.rename(self.target / bs.STATE_DIR, self.target / '.trace-kit')
                (self.target / '.trace-kit/skills.lock.json').write_text('{"schema_version": 1, "skills": {}}\n')
                (self.target / 'AGENTS.md').write_text(self.section('instructions') + '\n## Ours\nKept.\n')
                self.git_target('add', '-A')
                self.git_target('commit', '-qm', 'A project installed by 0.1.0-rc18')
                before = {name: (self.target / name).read_bytes() for name in ('AGENTS.md', '.gitignore')}
                state_before = self.tree_of('.trace-kit')
            elif version == versions[1]:
                self.pin_release(store, root='traceware')
        receipt = self.chain(store)
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual([hop['receipt']['state_folder']['how'] for hop in receipt['hops']], ['renamed', 'renamed'])
        self.assertEqual([row.get('state_folder') for row in self.history()],
                         [{'from': '.trace-kit', 'to': '.traceware'}, {'from': '.traceware', 'to': '.trace-os'}])
        for earlier in bs.EARLIER_STATE_DIRS:
            self.assertFalse((self.target / earlier).exists())
        self.assertTrue((self.target / '.trace-os/skills.lock.json').is_file())
        agents = (self.target / 'AGENTS.md').read_text()
        self.assertTrue(agents.startswith('<!-- trace-os:begin core-workflow -->'), agents)
        self.assertIn('## Ours\nKept.', agents)
        self.assertEqual((self.target / '.gitignore').read_text(), self.contents['.gitignore'][0])
        self.git_target('add', '-A')
        self.assertIn('R  .trace-kit/skills.lock.json -> .trace-os/skills.lock.json',
                      self.git_target('status', '--porcelain'))
        # Back one hop: the folder returns to .traceware/ with the rc19 markers.
        bs.rollback(self.target)
        self.assertEqual(bs.state_dir(self.target), '.traceware')
        self.assertEqual(bs.load(self.target / '.traceware/install.json')['version'], '0.1.0-rc19')
        self.assertTrue((self.target / 'AGENTS.md').read_text().startswith('<!-- traceware:begin core-workflow'))
        # The chain again moves it once more.
        receipt = self.chain(store)
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(bs.state_dir(self.target), '.trace-os')
        # Back both hops: the project is as rc18 left it.
        bs.rollback(self.target)
        bs.rollback(self.target)
        self.assertEqual(bs.state_dir(self.target), '.trace-kit')
        for name, data in before.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)
        restored = self.tree_of('.trace-kit')
        for name, data in state_before.items():
            if not name.endswith(('receipts/latest.json', 'update-history.jsonl')):
                self.assertEqual(restored[name], data, name)

    def tree_of(self, folder):
        root = self.target / folder
        return {path.relative_to(self.target).as_posix(): path.read_bytes()
                for path in sorted(root.rglob('*')) if path.is_file()} if root.is_dir() else {}

    def setup_guide(self):
        return ' '.join((SCRIPTS.parent / 'SETUP-AGENT.md').read_text(encoding='utf-8').split())

    @unittest.skipUnless((SCRIPTS.parents[1] / 'export-manifest.json').is_file(),
                         'the starter package is read only in the kit repository')
    def test_the_guide_says_how_an_interrupted_update_stops_the_check(self):
        # AH-168: the guide promised the check's processes stop; say how, on each platform.
        self.assertIn('its process group on macOS and Linux', self.setup_guide())
        self.assertIn('`taskkill /T /F`', self.setup_guide())

    @unittest.skipUnless((SCRIPTS.parents[1] / 'export-manifest.json').is_file(),
                         'the starter package is read only in the kit repository')
    def test_the_guide_undoes_a_retirement_from_a_fresh_copy_of_the_release(self):
        # AH-212: in an in-place install the retired skill's files were the source,
        # so "the same --apply again" stopped on a missing file.
        self.assertIn('run `--apply` from a fresh unpacked copy of the installed release', self.setup_guide())

    @unittest.skipUnless((SCRIPTS.parents[1] / 'export-manifest.json').is_file(),
                         'the starter package is read only in the kit repository')
    def test_the_guide_says_what_an_update_chain_writes_before_its_first_hop(self):
        # AH-213: the chain restores an unfinished update and writes a receipt at the
        # current release, so "nothing written" and "writes nothing" were untrue.
        text = self.setup_guide()
        self.assertNotIn('nothing written', text)
        self.assertNotIn('writes nothing', text)
        self.assertIn('resolved before any hop is applied', text)
        self.assertIn('writes only its receipt', text)

    @unittest.skipUnless((SCRIPTS.parents[1] / 'export-manifest.json').is_file(),
                         'the starter package is read only in the kit repository')
    def test_the_guide_gives_the_workflow_check_its_required_out_argument(self):
        # AH-218: `--out` is required, so the bare command stopped on argument parsing.
        import re
        self.assertEqual(re.findall(r'scripts/check_workflow\.py(?! --out)', self.setup_guide()), [])


class StateFolderMove(BootstrapTests):
    """One hop that moves the state folder, its markers and its ignore rules to a newer name.

    Run once per hop below: 0.1.0-rc18 to 0.1.0-rc19 (.trace-kit/ to .traceware/, a hop a
    chain still applies), 0.1.0-rc19 to 0.1.0-rc20 (.traceware/ to .trace-os/), and a project
    still in .trace-kit/ updated to 0.1.0-rc20 directly. Only the tests defined here run in
    these classes; BootstrapTests' own run once, in BootstrapTests.
    """
    OLD = ('.trace-kit', 'trace-kit', 'trace_kit', '0.1.0-rc18')
    NEW = ('.traceware', 'traceware', 'traceware', '0.1.0-rc19')

    def setUp(self):
        super().setUp()
        self.old_dir, self.old_prefix, self.old_key, self.old_version = self.OLD
        self.new_dir, self.new_prefix, self.new_key, self.new_version = self.NEW
        self.old_ignore = ''.join(f'{self.old_dir}/{name}\n' for name in
                                  ('receipts/', 'backups/', 'setup.lock', 'transaction.json', 'update-backups/'))
        self.new_ignore = self.old_ignore.replace(self.old_dir, self.new_dir)

    def markers_note(self):
        return f'{self.old_prefix}: markers became {self.new_prefix}: markers'

    def state_file(self, name):
        """A state path under the folder the project keeps it in now."""
        return self.target / bs.in_layout(self.target, name)

    def tree(self, folder):
        """Every file under a folder of the project, with its bytes; {} when the folder is absent."""
        return self.tree_of(folder)

    def legacy_project(self, edit_ignore=None, agent='claude'):
        """A project as the earlier release left it, committed, and the next release cut.

        Its state is in the earlier folder, its section markers read the earlier
        name, and its .gitignore names the earlier folder. A tracked lock file
        stands in for the skill tool's state, which must arrive in the new folder
        as a rename.
        """
        self.prefix = self.old_prefix
        self.contents['AGENTS.md'] = (self.section('instructions'), 'seed', 'core')
        self.contents['.gitignore'] = (self.old_ignore, 'managed', 'core')
        self.release(self.old_version)
        self.apply(agent=agent)
        os.rename(self.target / bs.STATE_DIR, self.target / self.old_dir)
        (self.target / self.old_dir / 'skills.lock.json').write_text('{"schema_version": 1, "skills": {}}\n')
        (self.target / 'AGENTS.md').write_bytes((self.section('instructions')
                                                 + '\n## Project rules\nKeep this choice.\n').encode())
        if edit_ignore:
            with (self.target / '.gitignore').open('a') as stream:
                stream.write(edit_ignore)
        self.git_target('add', '-A')
        self.git_target('commit', '-qm', 'A project installed by the earlier release')
        self.prefix, self.state_folder = self.new_prefix, self.new_dir
        self.contents['AGENTS.md'] = (self.section('instructions'), 'seed', 'core')
        self.contents['.gitignore'] = (self.new_ignore, 'managed', 'core')
        self.contents['scripts/example.py'] = ('print(19)\n', 'managed', 'core')
        self.release(self.new_version)
        return {name: (self.target / name).read_bytes() for name in ('AGENTS.md', '.gitignore')}

    def test_the_update_to_the_new_release_moves_the_state_folder_whole(self):
        before = self.legacy_project()
        legacy = self.tree(self.old_dir)
        receipt = self.update()
        self.assertFalse((self.target / self.old_dir).exists())
        moved = self.tree(self.new_dir)
        # Everything the old folder held is in the new one: tracked and ignored state alike.
        for name, data in legacy.items():
            if not name.endswith(('install.json', 'setup.lock', 'receipts/latest.json')):
                self.assertEqual(moved[self.new_dir + name[len(self.old_dir):]], data, name)
        self.assertEqual(bs.state_dir(self.target), self.new_dir)
        self.assertEqual(bs.load(self.state_file(bs.STATE))['version'], self.new_version)
        self.assertEqual(self.history()[-1]['state_folder'], {'from': self.old_dir, 'to': self.new_dir})
        self.assertEqual(receipt['state_folder']['how'], 'renamed')
        self.assertTrue(self.state_file(bs.RECEIPT).is_file())
        self.assertIn(f'git add -A {self.old_dir} {self.new_dir}', receipt['remaining'][0])
        # The kit's lines were the kit's own, so the ignore file is this release's.
        self.assertEqual((self.target / '.gitignore').read_text(), self.new_ignore)
        agents = (self.target / 'AGENTS.md').read_text()
        self.assertTrue(agents.startswith(f'<!-- {self.new_prefix}:begin core-workflow -->'))
        self.assertNotIn(f'{self.old_prefix}:', agents)
        self.assertIn('## Project rules\nKeep this choice.', agents)
        self.assertEqual(receipt['instruction_sections'][0]['markers'], self.markers_note())
        # The section took the release's text as well as its markers, and the receipt claims no more than that.
        self.assertIn("The managed sections in AGENTS.md now open and close with the release's markers ("
                      + self.markers_note() + ').', receipt['remaining'])
        self.assertNotEqual(before['AGENTS.md'], agents.encode())
        # Staged, the tracked state shows as renamed.
        self.git_target('add', '-A')
        status = self.git_target('status', '--porcelain')
        self.assertIn(f'R  {self.old_dir}/skills.lock.json -> {self.new_dir}/skills.lock.json', status)
        self.assertNotIn('?? ' + self.old_dir, status)
        self.assertEqual(check_skills_module().check(self.target)[1], [])

    def test_rolling_back_the_move_restores_the_old_folder_markers_and_ignore_file(self):
        before = self.legacy_project()
        legacy = self.tree(self.old_dir)
        self.update()
        result = bs.rollback(self.target)
        self.assertEqual(result['active_version'], self.old_version)
        self.assertEqual(result['state_folder'], {'from': self.new_dir, 'to': self.old_dir})
        self.assertFalse((self.target / self.new_dir).exists())
        restored = self.tree(self.old_dir)
        for name, data in legacy.items():
            if not name.endswith('receipts/latest.json'):  # the rollback's own receipt
                self.assertEqual(restored[name], data, name)
        for name, data in before.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)
        events = [(row['event'], row.get('status')) for row in self.history_at(self.old_dir)]
        self.assertEqual(events, [('update', 'succeeded'), ('rollback', None)])
        self.assertEqual(bs.state_dir(self.target), self.old_dir)
        # Rolled back, the same update moves it again.
        self.update()
        self.assertFalse((self.target / self.old_dir).exists())
        self.assertEqual(bs.load(self.state_file(bs.STATE))['version'], self.new_version)

    def history_at(self, folder):
        rows = (self.target / folder / 'update-history.jsonl').read_text(encoding='utf-8').splitlines()
        return [json.loads(row) for row in rows if row.strip()]

    def test_an_edited_ignore_file_keeps_its_lines_and_gains_the_new_rules(self):
        before = self.legacy_project(edit_ignore='# our own\nresults/\n')
        receipt = self.update()
        expected = ''.join(line + '\n' + line.replace(self.old_dir, self.new_dir) + '\n'
                           for line in self.old_ignore.splitlines()) + '# our own\nresults/\n'
        self.assertEqual((self.target / '.gitignore').read_text(), expected)
        self.assertEqual(receipt['changes']['.gitignore'], 'state-folder-ignores')
        self.assertEqual(len(receipt['state_folder']['ignore_file']['added']), 5)
        # The state keeps the kit's digest, so a later release that changes the file still finds the edit.
        self.assertEqual(bs.load(self.state_file(bs.STATE))['files']['.gitignore']['sha256'],
                         bs.digest(self.new_ignore.encode()))
        self.assertTrue(any(line.startswith('.gitignore: ') for line in receipt['remaining']))
        self.assertEqual(self.git_target('check-ignore', f'{self.new_dir}/receipts/latest.json').strip(),
                         f'{self.new_dir}/receipts/latest.json')
        bs.rollback(self.target)
        self.assertEqual((self.target / '.gitignore').read_bytes(), before['.gitignore'])

    def test_an_overridden_section_under_the_old_key_keeps_its_text_and_gets_new_markers(self):
        self.legacy_project()
        path = self.target / 'AGENTS.md'
        self.prefix = self.old_prefix
        path.write_text(self.section('locally revised instructions'))
        self.prefix = self.new_prefix
        project = bs.load(self.target / 'ai/project.json')
        # The key the earlier release documented still declares the override.
        project[self.old_key] = {'instruction_overrides': {'core-workflow': {
            'compatible_with': ['1'], 'basis': 'Our rules keep the shared contract.'}}}
        (self.target / 'ai/project.json').write_bytes(bs.encoded(project))
        self.git_target('commit', '-qam', 'Our own section')
        receipt = self.update()
        self.assertEqual(path.read_text(), self.section('locally revised instructions'))
        row = receipt['instruction_sections'][0]
        self.assertEqual((row['action'], row['markers']), ('retain-compatible-override', self.markers_note()))
        self.assertEqual(bs.load(self.target / 'ai/project.json'), project)

    def test_a_refused_or_failed_update_leaves_the_old_folder_where_it_was(self):
        before = self.legacy_project()
        tracked = {name: (self.target / name).read_bytes() for name in (f'{self.old_dir}/install.json', 'AGENTS.md')}
        failed = {'status': 'fail', 'exit_code': 1, 'name': 'updated project workflow'}
        passed = {'status': 'pass', 'exit_code': 0, 'name': 'release portable tests'}
        archive, expected = self.archive()
        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
             mock.patch.object(bs, 'check_updated_project', return_value=failed):
            with self.assertRaisesRegex(bs.SetupError, 'restored the last successful installation'):
                bs.update(self.source, self.target, 'academic', 'claude', archive, expected)
        self.assertFalse((self.target / self.new_dir).exists())
        for name, data in {**tracked, **before}.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)
        self.assertEqual(self.history_at(self.old_dir)[-1]['status'], 'failed')
        # A name both folders hold is named before anything is written.
        (self.target / self.new_dir).mkdir()
        (self.target / self.new_dir / 'skills.lock.json').write_text('{}')
        with self.assertRaisesRegex(bs.SetupError, 'state-folder:' + self.new_dir.replace('.', r'\.') + '/'):
            self.update()
        self.assertEqual(sorted(path.name for path in (self.target / self.new_dir).iterdir()), ['skills.lock.json'])
        for name, data in tracked.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)

    def test_a_move_the_update_could_not_make_is_made_by_the_next_command(self):
        self.legacy_project()
        with mock.patch.object(bs, 'move_state_folder', side_effect=OSError('folder in use')):
            receipt = self.update()
        self.assertIn(f'stays in {self.old_dir}/', receipt['remaining'][0])
        self.assertEqual(bs.state_dir(self.target), self.old_dir)
        self.assertEqual(bs.load(self.state_file(bs.STATE))['version'], self.new_version)
        # Every reader still finds it there.
        self.assertEqual(check_skills_module().check(self.target)[1], [])
        plan = bs.plan(self.source, self.target, 'academic', 'claude')[0]
        self.assertIn('--recover moves it', plan['state_folder_pending']['pending'])
        self.assertNotIn('state_folder', plan)
        self.assertEqual(bs.main(['--target', str(self.target), '--recover']), 0)
        self.assertFalse((self.target / self.old_dir).exists())
        self.assertEqual(bs.state_dir(self.target), self.new_dir)
        self.assertEqual(bs.load(self.state_file(bs.STATE))['version'], self.new_version)

    def test_where_the_folder_cannot_be_renamed_its_entries_move_and_the_lock_goes_after(self):
        # Windows keeps the folder of an open file in place; the held lock file is such a file.
        self.legacy_project()
        rename = os.rename

        def no_folder_rename(source, destination):
            if Path(source) in (self.target / self.old_dir, self.target / self.new_dir):
                raise PermissionError('the folder holds an open file')
            return rename(source, destination)

        with mock.patch.object(bs.os, 'rename', side_effect=no_folder_rename):
            receipt = self.update()
        self.assertIn('entry by entry', receipt['state_folder']['how'])
        self.assertFalse((self.target / self.old_dir).exists())
        self.assertTrue((self.target / self.new_dir / 'skills.lock.json').is_file())
        with mock.patch.object(bs.os, 'rename', side_effect=no_folder_rename):
            bs.rollback(self.target)
        self.assertFalse((self.target / self.new_dir).exists())
        self.assertEqual(bs.load(self.target / self.old_dir / 'install.json')['version'], self.old_version)

    def test_the_chain_moves_the_folder_on_the_hop_to_the_release_that_names_it(self):
        self.with_hop_checks()
        self.legacy_project()
        # Undo the next release the fixture cut: an older hop comes first, one that names no new folder.
        store = self.root / 'store'
        self.upgrades.pop()
        self.prefix, self.state_folder = self.old_prefix, None
        self.contents['AGENTS.md'] = (self.section('instructions'), 'seed', 'core')
        self.contents['.gitignore'] = (self.old_ignore, 'managed', 'core')
        self.release_version = self.old_version
        self.contents['scripts/example.py'] = ('print(18.5)\n', 'managed', 'core')
        self.release(self.old_version + '-2')
        # An archive keeps its release's own root: trace-kit-starter/ up to 0.1.0-rc18, traceware/ in rc19.
        self.pin_release(store, root='trace-kit-starter' if self.old_dir == '.trace-kit' else 'traceware')
        self.prefix, self.state_folder = self.new_prefix, self.new_dir
        self.contents['AGENTS.md'] = (self.section('instructions'), 'seed', 'core')
        self.contents['.gitignore'] = (self.new_ignore, 'managed', 'core')
        self.contents['scripts/example.py'] = ('print(19)\n', 'managed', 'core')
        self.release(self.new_version)
        receipt = self.chain(store)
        self.assertEqual(receipt['status'], 'completed', receipt)
        first, second = receipt['hops']
        self.assertNotIn('state_folder', first['receipt'])
        self.assertEqual(second['receipt']['state_folder']['how'], 'renamed')
        self.assertFalse((self.target / self.old_dir).exists())
        self.assertEqual([(row['to']['version'], row.get('state_folder')) for row in self.history()],
                         [(self.old_version + '-2', None),
                          (self.new_version, {'from': self.old_dir, 'to': self.new_dir})])
        self.assertTrue(any(line.startswith('The installation state moved') for line in receipt['remaining']))
        self.assertTrue(self.state_file(bs.RECEIPT).is_file())
        # Back one hop, then the next: the folder moves back with the first and stays with the second.
        bs.rollback(self.target)
        self.assertEqual(bs.state_dir(self.target), self.old_dir)
        bs.rollback(self.target)
        self.assertEqual(bs.load(self.target / self.old_dir / 'install.json')['version'], self.old_version)


class StateFolderMoveToTraceOs(StateFolderMove):
    OLD = ('.traceware', 'traceware', 'traceware', '0.1.0-rc19')
    NEW = ('.trace-os', 'trace-os', 'trace_os', '0.1.0-rc20')


class StateFolderMoveFromTheOldestName(StateFolderMove):
    """A project still in .trace-kit/ (an rc19 update whose move never finished, say) updated to 0.1.0-rc20."""
    OLD = ('.trace-kit', 'trace-kit', 'trace_kit', '0.1.0-rc19')
    NEW = ('.trace-os', 'trace-os', 'trace_os', '0.1.0-rc20')


# The move classes inherit BootstrapTests' fixtures, not its tests, which already run once there.
for _case in (StateFolderMove, StateFolderMoveToTraceOs, StateFolderMoveFromTheOldestName):
    for _name in dir(BootstrapTests):
        if _name.startswith('test_') and _name not in StateFolderMove.__dict__:
            setattr(_case, _name, None)


if __name__ == '__main__':
    unittest.main()
