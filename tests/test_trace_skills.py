"""Bring skill packages into a project with trace_skills.py, against temporary repositories only.

No network and no credential: every source is a bare repository in a
temporary folder, Git may use only the file protocol, HOME is a temporary
folder, and the audience observation is patched except where it is the
subject.

Each kind of fixture (a source, an installed project, an uninstalled one) is
made once per run of this module, by section 13's steps, and every test that
asks for it gets its own copy: a copied Git repository is a whole repository,
so the tests stay independent of each other and of their order.
"""
import atexit
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
# In the kit's source tree, trace_record.py and check_skill_handoff.py live in the
# repository's scripts/, not beside the tool; an exported project has them all in scripts/ (A19).
REPOSITORY_SCRIPTS = TESTS.parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
if not (SCRIPTS / 'trace_record.py').is_file() and (REPOSITORY_SCRIPTS / 'trace_record.py').is_file():
    sys.path.insert(1, str(REPOSITORY_SCRIPTS))
import bootstrap as bs  # noqa: E402
import check_skills  # noqa: E402
import trace_record  # noqa: E402
import trace_skills as ts  # noqa: E402

ENVIRONMENT = {'GIT_AUTHOR_NAME': 'Test Researcher', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
               'GIT_COMMITTER_NAME': 'Test Researcher', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
               # A signing rule in someone's global Git configuration must not fail a fixture commit.
               'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false'}
IGNORED = ''.join(name + '\n' for name in (
    '__pycache__/', '.trace-os/setup.lock', '.trace-os/transaction.json', '.trace-os/backups/',
    '.trace-os/pending-update.json', '.trace-os/pending-plan.json', '.trace-os/update-backups/',
    '.trace-os/receipts/', '.trace-os/skill-operation.json'))
# Section 13's list, plus record_privacy.py, which trace_story.add_commands imports whenever
# trace_record.py runs a command.
PROJECT_MODULES = ('trace_record.py', 'trace_story.py', 'input_provenance.py', 'trace_defaults.py',
                   'record_privacy.py', 'check_skill_handoff.py', 'trace_skills.py', 'bootstrap.py',
                   'check_skills.py')
VECTOR = {'LICENSE.txt': b'Apache License 2.0\n',
          'SKILL.md': b'---\nname: example-skill\ndescription: An example skill for tests.\n---\nBody.\n',
          'scripts/run.py': (b"print('ok')\n", '100755')}
VECTOR_DIGEST = '00c6ea65a3bcf3089fb6c48e684604dc26207fac72c61276d41c0d291e5396d3'
VECTOR_TRACKED = 'c28c830a561645ef6da9382caefb0072a7ebf60f20fff0a9efa5c4dceec31324'
ROUTING = ('# Skills in use\n\n<!-- trace-os:begin skill-routing -->\nRoute work to the skills below.\n'
           '<!-- trace-os:end skill-routing -->\n')
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
STAMP = '20261001T120000Z'
FAKE_TOKEN = 'ghp_FAKE0000000000000000000000000000000000'
# AH-237, AH-257: each shadowing line states what the skills pilot observed for the folder it names, written out
# here as the contract (11.1) words it.
SHADOW_CLAUDE = ("Claude Code 2.1.280 was observed to load the personal copy instead of the project's (the skills "
                 'pilot, 2026-10-01); other clients and later versions were not rehearsed')
SHADOW_CODEX = ('Codex 0.154.0 was observed to list personal skills only (the skills pilot, 2026-10-01); other '
                'clients and later versions were not rehearsed')
SHADOW_UNREHEARSED = 'no client was rehearsed for this folder'
# Every fixture patches the observation; the audience tests read the real one from here.
REAL_OBSERVE = ts.observe_audience


def kit_script(name):
    """A module the project needs: beside the tool in an export, in the repository's scripts/ in the source tree."""
    for folder in (SCRIPTS, REPOSITORY_SCRIPTS):
        if (folder / name).is_file():
            return folder / name
    raise FileNotFoundError(name)


def git(path, *args, data=None):
    run = subprocess.run(['git', *args], cwd=path, capture_output=True, input=data, check=True)
    return run.stdout.decode('utf-8', 'replace').strip()


def skill_md(name, description='A skill for tests.', body='Body.\n', extra=''):
    return f'---\nname: {name}\ndescription: {description}\n{extra}---\n{body}'.encode()


def tree_state(root):
    """Every file's bytes and mode outside .git and the setup state bootstrap leaves, for before/after checks."""
    found = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(('.git/', '.trace-os/backups/')) or relative == '.trace-os/setup.lock':
            continue
        if path.is_file() and not path.is_symlink():
            found[relative] = (path.read_bytes(), path.stat().st_mode & stat.S_IXUSR != 0)
    return found


def mode_of(path):
    return '100755' if Path(path).stat().st_mode & stat.S_IXUSR else '100644'


# Fixtures made once per run of the module -------------------------------------
#
# Each kind of fixture is made the first time a test asks for it, by section 13's steps, and kept;
# every later test that asks for it gets its own copy, and a copied Git repository is a whole
# repository. Every test works in the same root folder, emptied before it, so a copied fixture names
# the absolute paths it would name had that test made it.

_RUN = {}


def run_folder():
    """This run's folder: the root every test works in, and the fixtures kept so far."""
    if 'folder' not in _RUN:
        folder = Path(tempfile.mkdtemp(prefix='test-trace-skills-')).resolve()
        atexit.register(shutil.rmtree, folder, True)
        _RUN.update(folder=folder, made={})
    return _RUN['folder']


def tearDownModule():
    folder = _RUN.pop('folder', None)
    _RUN.clear()
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def compact(folder):
    """Pack a kept repository's objects and drop Git's sample hooks, which Git never runs, so a copy is few files."""
    for repository in (folder / '.git', folder):
        if (repository / 'objects').is_dir() and (repository / 'HEAD').is_file():
            git(folder, '--git-dir', str(repository), 'repack', '-a', '-d', '-q')
            for sample in (repository / 'hooks').glob('*.sample'):
                sample.unlink()


def relocate(folders, moves):
    """Rewrite each moved path where a file outside .git states it, as a fixture made at the new path states it."""
    pairs = []
    for old, new in sorted(moves, key=lambda move: -len(str(move[0]))):
        for before, after in ((json.dumps(str(old))[1:-1], json.dumps(str(new))[1:-1]), (str(old), str(new)),
                              (old.as_posix(), new.as_posix())):
            pairs.append((before.encode(), after.encode()))
    for folder in folders if pairs else ():
        for top, dirs, files in os.walk(folder):
            # A compiled module states its source's path inside marshalled data, which a byte edit would break.
            dirs[:] = [name for name in dirs if name not in ('.git', '__pycache__')]
            for name in files:
                path = Path(top) / name
                if path.is_symlink():
                    continue
                data = changed = path.read_bytes()
                for before, after in pairs:
                    changed = changed.replace(before, after)
                if changed != data:
                    path.write_bytes(changed)


def made(key, folders, make, replace=False, states_its_path=False):
    """The folders as make() makes them: made the first time this run asks for `key`, and kept; copied
    from what was kept every later time, to the folders asked for then. With `replace`, a copy replaces
    the folder already there. A fixture whose files state its own path has that path rewritten in a
    copy made to another folder."""
    kept = _RUN['made']
    if key not in kept:
        result = make()
        store = run_folder() / 'made' / str(len(kept))
        copies = [store / str(number) for number in range(len(folders))]
        for folder, copy in zip(folders, copies):
            shutil.copytree(folder, copy, symlinks=True)
        kept[key] = {'copies': copies, 'folders': list(folders), 'result': result, 'compact': False}
        return result
    entry = kept[key]
    if not entry['compact']:
        # Asked for a second time, so packed once and copied as a few files from now on.
        for copy in entry['copies']:
            compact(copy)
        entry['compact'] = True
    for copy, folder in zip(entry['copies'], folders):
        if replace:
            shutil.rmtree(folder, ignore_errors=True)
        shutil.copytree(copy, folder, symlinks=True)
    if states_its_path:
        relocate(folders, [(old, new) for old, new in zip(entry['folders'], folders) if old != new])
    return entry['result']


class Fixture(unittest.TestCase):
    """Sources, installed and uninstalled projects, and run_main, as section 13 describes them."""

    def setUp(self):
        # Every test works in the same folder, emptied before it, so a fixture made in an earlier test
        # and copied here names the paths it would name had this test made it.
        self.root = run_folder() / 'test'
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / 'home'
        self.home.mkdir()
        (self.root / 'tmp').mkdir()
        environment = dict(ENVIRONMENT, HOME=str(self.home), GIT_CONFIG_NOSYSTEM='1',
                           GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0')
        for patch in (mock.patch.dict(os.environ, environment),
                      # The tool's temporary folders go under the test's own folder, outside every project.
                      mock.patch.object(tempfile, 'tempdir', str(self.root / 'tmp')),
                      mock.patch.object(ts, 'observe_audience', return_value='not checked: test'),
                      mock.patch.object(ts, 'utc_now', return_value=NOW)):
            patch.start()
            self.addCleanup(patch.stop)
        self.sources = 0

    # Running the tool ------------------------------------------------------

    def run_main(self, *argv, stdin='', terminal=False):
        out, err = io.StringIO(), io.StringIO()

        class Input(io.StringIO):
            def isatty(self):
                return terminal

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, 'stdin', Input(stdin)):
            code = ts.main([str(arg) for arg in argv])
        return code, out.getvalue(), err.getvalue()

    def tool(self, project):
        return f'python3 {Path(ts.__file__).resolve().as_posix()} --project {project.as_posix()}'

    # Sources ---------------------------------------------------------------

    def make_source(self, packages, special=(), branch='main', name=None):
        """A bare source whose HEAD names `branch`, holding the files given; returns its path, commit and work tree."""
        self.sources += 1
        name = name or f'library{self.sources}'
        bare, work = self.root / f'{name}.git', self.root / f'{name}-work'

        def make():
            git(self.root, 'init', '-q', '--bare', '-b', branch, str(bare))
            git(self.root, 'init', '-q', '-b', branch, str(work))
            return self.commit_source(work, bare, packages, special, branch=branch)
        key = ('source', branch, tuple(sorted(packages.items())), tuple(special))
        return bare, made(key, (bare, work), make), work

    def commit_source(self, work, bare, files, special=(), branch='main', message='library'):
        executables = []
        for path, value in files.items():
            data, mode = value if isinstance(value, tuple) else (value, '100644')
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if mode == '100755':
                executables.append(path)
                os.chmod(target, 0o755)
        git(work, 'add', '-A')
        for path in executables:
            git(work, 'update-index', '--chmod=+x', '--', path)
        # Special entries are made without the file system, so they work on every platform.
        for path, mode, content in special:
            oid = content if mode == '160000' else git(work, 'hash-object', '-w', '--stdin', data=content)
            git(work, 'update-index', '--add', '--cacheinfo', f'{mode},{oid},{path}')
        git(work, 'commit', '-qm', message)
        git(work, 'push', '-q', str(bare), branch)
        return git(work, 'rev-parse', 'HEAD')

    def library(self, extra=None, special=()):
        """The standard library: the 4.6 vector package and a few named packages."""
        files = {f'skills/example-skill/{path}': value for path, value in VECTOR.items()}
        files.update({'skills/literature-processing/SKILL.md': skill_md('literature-processing',
                                                                        body='Upstream method.\n'),
                      'skills/example/SKILL.md': skill_md('example', body='Library version.\n'),
                      'skills/example/LICENSE.txt': b'Apache License 2.0\n',
                      'skills/own/SKILL.md': skill_md('own'),
                      'skills/legacy/SKILL.md': skill_md('legacy'),
                      'README.md': b'library\n'})
        files.update(extra or {})
        return self.make_source(files, special)

    # Projects --------------------------------------------------------------

    def release(self, folder, skills=None):
        """A release cut as export.py cuts one: a manifest and its files, with a kit skill `example`."""
        contents = {'AGENTS.md': ('<!-- trace-os:begin core-workflow -->\ninstructions\n'
                                  '<!-- trace-os:end core-workflow -->\n', 'seed'),
                    '.gitignore': (IGNORED, 'managed'),
                    '.skills/example/SKILL.md': (skill_md('example', 'The kit example.', 'Kit version.\n').decode(),
                                                 'managed'),
                    'ai/SKILLS-IN-USE.md': (ROUTING, 'seed')}
        contents.update(skills or {})
        files = {}
        for name, (text, policy) in contents.items():
            data = text.encode('utf-8')
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[name] = {'sha256': bs.digest(data), 'size': len(data), 'policy': policy, 'profile': 'core'}
        agents = contents['AGENTS.md'][0].encode()
        spec = {'id': 'core-workflow', 'path': 'AGENTS.md', 'version': 1,
                'start': '<!-- trace-os:begin core-workflow -->', 'end': '<!-- trace-os:end core-workflow -->'}
        first, last = bs.marked_range(agents, spec)
        spec.update(sha256=bs.digest(agents[first:last]), size=last - first, legacy=[])
        manifest = {'schema_version': 1, 'version': 'test-1', 'source_commit': '0' * 40,
                    'compatibility': {'install_state_schema': 1, 'project_schema': 1, 'record_schema': 1},
                    'supported_upgrades': [], 'renames': [], 'releases': [],
                    'instruction_sections': [spec], 'files': files}
        (folder / 'starter-manifest.json').write_bytes(bs.encoded(manifest))
        return folder

    def add_modules(self, project):
        (project / 'scripts').mkdir(exist_ok=True)
        for name in PROJECT_MODULES:
            shutil.copy2(kit_script(name), project / 'scripts' / name)

    def installed(self, agent='claude', name='project', skills=None, before_commit=None):
        """A project set up from a release with bootstrap.apply, the modules copied in, and committed."""
        project = self.root / name

        def make():
            source = self.release(self.root / f'release-{name}', skills)
            bs.apply(source, project, 'academic', agent)
            self.add_modules(project)
            git(project, 'config', 'user.name', 'Test Researcher')
            git(project, 'config', 'user.email', 'test@example.invalid')
            if before_commit:
                before_commit(project)
            git(project, 'add', '-A')
            git(project, 'commit', '-qm', 'Install the kit')
        if before_commit:
            # The test's own step runs before the commit, so this project is made here every time.
            make()
        else:
            # Bootstrap's setup receipt names the project's folder.
            made(('installed', agent, tuple(sorted((skills or {}).items()))), (self.root / f'release-{name}', project),
                 make, states_its_path=True)
        return project

    def uninstalled(self, name='bare-project', link_agent_root=False):
        """A project with no install state, like the kit's development repository itself, holding its own and adapted skills."""
        project = self.root / name

        def make():
            project.mkdir()
            git(project, 'init', '-q', '-b', 'main')
            git(project, 'config', 'user.name', 'Test Researcher')
            git(project, 'config', 'user.email', 'test@example.invalid')
            (project / 'README.md').write_text('project\n')
            (project / '.gitignore').write_text(IGNORED)
            for path, data in {'.skills/own/SKILL.md': skill_md('own', body='Our own method.\n'),
                               '.skills/legacy.md': b'# A legacy single-file skill\n',
                               '.skills/literature-processing/SKILL.md': skill_md(
                                   'literature-processing', body='Upstream method, adapted here.\n'),
                               }.items():
                (project / path).parent.mkdir(parents=True, exist_ok=True)
                (project / path).write_bytes(data)
            if link_agent_root:
                (project / '.claude').mkdir()
                os.symlink('../.skills', project / '.claude' / 'skills')
            self.add_modules(project)
            git(project, 'add', '-A')
            git(project, 'commit', '-qm', 'Start the project')
        made(('uninstalled', link_agent_root), (project,), make)
        return project

    # Reading results -------------------------------------------------------

    def lock(self, project):
        return json.loads((project / ts.LOCK).read_text())

    def receipt(self, project, operation_id):
        return json.loads((project / ts.RECEIPTS / f'{operation_id}.json').read_text())

    def intake_rows(self, project):
        path = project / ts.INTAKE
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def adopt(self, project, bare, path, *extra):
        code, out, err = self.run_main('--project', project, 'adopt', bare, path, '--ref', self.head(bare),
                                       '--yes', *extra)
        self.assertEqual((code, err), (0, ''), out)
        return out

    def head(self, bare):
        return git(bare, 'rev-parse', 'HEAD')

    def assert_parity(self, project, sync=True):
        """The equivalence criterion of section 9: check_skills clean, and sync_skills a no-op."""
        checked, problems = check_skills.check(project)
        self.assertEqual(problems, [])
        if sync:
            self.assertEqual(bs.sync_skills(project), {'synced': 0, 'agent': json.loads(
                (project / bs.STATE).read_text())['agent'], 'removed': [], 'retired': []})

    def assert_valid_registries(self, project):
        problems, _ = trace_record.validate_intake(self.intake_rows(project))
        self.assertEqual(problems, [])
        path = project / ts.TRANSITIONS
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        problems, _ = trace_record.validate_transitions(rows)
        self.assertEqual(problems, [])


# Section 2.3, 2.5 and 4.6: no Git needed --------------------------------------

class ReaderTests(unittest.TestCase):
    def test_digest_vector(self):
        files = []
        for path, value in VECTOR.items():
            data, mode = value if isinstance(value, tuple) else (value, '100644')
            files.append({'path': path, 'mode': mode, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.assertEqual(ts.package_digest(files), VECTOR_DIGEST)
        self.assertEqual(ts.package_digest([item for item in files if item['path'] != 'scripts/run.py']),
                         VECTOR_TRACKED)
        self.assertEqual(ts.package_digest(list(reversed(files))), VECTOR_DIGEST)

    def test_reads_every_kit_skill(self):
        folders = [SCRIPTS.parent / '.skills', TESTS.parents[2] / '.skills']
        found = [path for folder in folders if folder.is_dir() for path in sorted(folder.glob('*/SKILL.md'))]
        self.assertTrue(found)
        for path in found:
            front = ts.read_front_matter(path.read_text(encoding='utf-8'))
            self.assertEqual(front['name'], path.parent.name, path)
            self.assertTrue(front['description'], path)
            # The kit's descriptions hold ": ", which a strict YAML parser would reject.
            self.assertNotIn('\n', front['description'])

    def test_readable_forms(self):
        text = ('﻿---\r\n'
                'name: demo\r\n'
                '# a comment\r\n'
                '\r\n'
                'description: Plain: with a colon\r\n'
                '  and a continued line\r\n'
                'quoted: "say \\"hi\\" \\\\ there"\r\n'
                "single: 'it''s'\r\n"
                'literal: |\r\n'
                '  line one\r\n'
                '\r\n'
                '  line two\r\n'
                'folded: >-\r\n'
                '  one\r\n'
                '  two\r\n'
                'metadata:\r\n'
                '  changes: "what changed"\r\n'
                '  version: 2\r\n'
                'joined:\r\n'
                '  a\r\n'
                '  b\r\n'
                'empty:\r\n'
                '---\r\n'
                'Body\r\n')
        self.assertEqual(ts.read_front_matter(text), {
            'name': 'demo', 'description': 'Plain: with a colon and a continued line',
            'quoted': 'say "hi" \\ there', 'single': "it's", 'literal': 'line one\n\nline two',
            'folded': 'one two', 'metadata': {'changes': 'what changed', 'version': '2'},
            'joined': 'a b', 'empty': ''})

    def test_unreadable_forms(self):
        cases = {'no front matter\n': (None, 'no front matter'),
                 '---\nname: x\n': (None, 'no front matter'),
                 '---\nname: x\n\tdescription: y\n---\n': (3, 'not readable'),
                 '---\nname: x\n- item\n---\n': (3, 'not readable'),
                 '---\n  indented: first\n---\n': (2, 'not readable'),
                 '---\nname: "open\n---\n': (2, 'not readable'),
                 '---\nname: "x"\n  more\n---\n': (3, 'not readable'),
                 '---\nmetadata:\n  a: 1\n    b: 2\n---\n': (4, 'not readable'),
                 '---\nname: x\nname: y\n---\n': (3, 'duplicate key name')}
        for text, expected in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(ts.FrontMatterError) as caught:
                    ts.read_front_matter(text)
                self.assertEqual((caught.exception.line, caught.exception.reason), expected)

    def test_definition_states(self):
        good = ts.member('SKILL.md', '100644', skill_md('demo', extra='license: MIT\nunknown-key: x\n'))
        states, notes, _ = ts.definition([good], 'demo')
        self.assertEqual(states['format'], 'format valid')
        self.assertEqual(states['licence'], 'licence stated')
        self.assertEqual(states['handoff'], 'handoff not stated: contract named, record mechanism named, '
                                            'procedure set to its own id')
        self.assertEqual(notes, ['front matter keys not in the specification: unknown-key'])
        bad = ts.member('SKILL.md', '100644', b'---\nname: Bad_Name\ncompatibility: \'\'\nmetadata: flat\n'
                                              b'license: \'\'\n---\n')
        states, _, _ = ts.definition([bad, ts.member('LICENSE', '100644', b'x')], 'demo')
        self.assertEqual(states['format'], 'format invalid: name Bad_Name is not 1 to 64 lowercase letters, digits '
                                           'and single hyphens; name Bad_Name differs from the folder demo; '
                                           'no description; compatibility is not 1 to 500 characters; '
                                           'metadata is not a mapping of strings; license is empty')
        self.assertEqual(states['licence'], 'licence stated')
        states, _, _ = ts.definition([], 'demo')
        self.assertEqual((states['format'], states['licence']), ('format invalid: no SKILL.md', 'licence not stated'))
        states, _, _ = ts.definition([ts.member('SKILL.md', '100644', b'\xff')], 'demo')
        self.assertEqual(states['format'], 'format invalid: SKILL.md is not UTF-8 text')
        with mock.patch.object(ts, 'handoff_module', return_value=None):
            states, _, _ = ts.definition([good], 'demo')
        self.assertEqual(states['handoff'], 'handoff not checked: scripts/check_skill_handoff.py is not here')

    def test_escape(self):
        self.assertEqual(ts.escape('a\tb\nc\rd\x1b[31m\x7f\x85‮x⁦'),
                         'a\tb\nc\\rd\\x1b[31m\\x7f\\x85\\u202ex\\u2066')


# Section 2.2 and the fetching errors ------------------------------------------

class SourceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.project = self.uninstalled()

    def refused(self, files, special=(), path='skills/bad'):
        bare, commit, _ = self.make_source(files, special)
        code, out, err = self.run_main('--project', self.project, 'show', bare, path)
        self.assertEqual(code, 1, out)
        self.assertFalse(list((self.root / 'tmp').iterdir()), 'a refused package leaves no temporary folder')
        return err

    def test_every_package_refusal(self):
        md = skill_md('bad')
        blob = b'target'
        cases = [
            ({'skills/bad/SKILL.md': md}, [('skills/bad/link', '120000', blob)], 'link is a symbolic link'),
            ({'skills/bad/SKILL.md': md}, [('skills/bad/sub', '160000', 'a' * 40)], 'sub is a submodule'),
            ({'skills/bad/SKILL.md': md}, [('skills/bad/a b.md', '100644', blob)],
             'a b.md uses characters outside A-Z, a-z, 0-9, ".", "_", "-" and "/"'),
            ({'skills/bad/SKILL.md': md}, [('skills/bad/a\\b', '100644', blob)],
             'a\\b uses characters outside A-Z, a-z, 0-9, ".", "_", "-" and "/"'),
            ({'skills/bad/SKILL.md': md}, [('skills/bad/A.md', '100644', blob), ('skills/bad/a.md', '100644', blob)],
             'A.md and a.md differ only in letter case'),
            ({'skills/bad/SKILL.md': md, 'skills/bad/.gitattributes': b'* -text\n'}, [],
             ".gitattributes is a Git control file, which would change how this project's Git treats files"),
            ({'skills/bad/SKILL.md': md}, [('skills/bad/con.txt', '100644', blob)],
             'con.txt uses a name Windows reserves'),
            ({'skills/bad/SKILL.md': md}, [('skills/bad/name.', '100644', blob)], 'name. uses a name Windows reserves'),
            ({'skills/bad/README.md': md}, [], 'it has no SKILL.md'),
            ({'skills/bad/SKILL.md': b'\xff\xfe'}, [], 'SKILL.md is not UTF-8 text'),
            ({'skills/bad/SKILL.md': b'name: bad\n'}, [], 'SKILL.md has no front matter between --- lines'),
            ({'skills/bad/SKILL.md': b'---\nname: bad\n\tdescription: x\n---\n'}, [],
             'SKILL.md front matter line 3 is not readable'),
            ({'skills/bad/SKILL.md': b'---\nname: bad\nname: bad\n---\n'}, [],
             'SKILL.md front matter key name appears twice'),
            ({'skills/bad/SKILL.md': b'---\ndescription: x\n---\n'}, [], 'SKILL.md names no skill'),
            ({'skills/bad/SKILL.md': skill_md('Bad_Name')}, [],
             'its name Bad_Name is not 1 to 64 lowercase letters, digits and single hyphens'),
            ({'skills/bad/SKILL.md': skill_md('other')}, [], 'its name other differs from its folder bad'),
        ]
        for files, special, reason in cases:
            with self.subTest(reason=reason):
                err = self.refused(files, special)
                self.assertEqual(err, f'trace-skills: the package at skills/bad is refused: {reason}\n')

    def test_screen_refusals_git_cannot_index(self):
        # Git refuses a .git component in its own index, so these come only from a folder on disk.
        entry = {'path': 'SKILL.md', 'mode': '100644', 'data': skill_md('bad')}
        for path, reason in (('sub/.GIT/config', "sub/.GIT/config is a Git control file, which would change how "
                                                 "this project's Git treats files"),
                             ('AUX', 'AUX uses a name Windows reserves'),
                             ('lpt9.log', 'lpt9.log uses a name Windows reserves'),
                             ('notes.', 'notes. uses a name Windows reserves'),
                             ('café.md', 'café.md uses characters outside A-Z, a-z, 0-9, ".", "_", "-" and '
                                              '"/"')):
            with self.subTest(path=path):
                with self.assertRaises(ts.Refused) as caught:
                    ts.screen([entry, {'path': path, 'mode': '100644', 'data': b''}])
                self.assertEqual(caught.exception.reason, reason)
        with self.assertRaises(ts.Refused) as caught:
            ts.screen([entry, {'path': 'pipe', 'mode': 'other', 'data': None}])
        self.assertEqual(caught.exception.reason, 'pipe is not a regular file')
        # Steps run one at a time over every entry: a symlink later in path order beats a bad name earlier.
        with self.assertRaises(ts.Refused) as caught:
            ts.screen([entry, {'path': 'a b', 'mode': '100644', 'data': b''},
                       {'path': 'z', 'mode': '120000', 'data': None}])
        self.assertEqual(caught.exception.ident, 'P1')

    def test_litter_is_left_out_and_listed(self):
        bare, commit, _ = self.make_source({'skills/tidy/SKILL.md': skill_md('tidy'),
                                            'skills/tidy/.DS_Store': b'\0junk', 'skills/tidy/._SKILL.md': b'x'})
        code, out, err = self.run_main('--project', self.project, 'show', bare, 'skills/tidy')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Left out as operating-system files: .DS_Store, ._SKILL.md\n', out)
        self.assertIn('Package: skills/tidy (tidy), 1 files', out)

    def test_address_and_fetch_errors(self):
        bare, commit, work = self.library()
        git(work, 'tag', 'twin')
        git(work, 'branch', 'twin')
        git(work, 'push', '-q', str(bare), 'refs/heads/twin', 'refs/tags/twin')
        project = self.project
        cases = [
            (['show', 'https://user:secret@example.invalid/x.git', 'skills/example'],
             "the repository address carries a user name and password, a token, a query or a fragment; give it "
             "without them and let Git's credential helper supply access", 2),
            (['show', f'https://{FAKE_TOKEN}@github.com/o/r.git', 'skills/example'], None, 2),
            (['show', 'https://example.invalid/x.git?token=1', 'skills/example'], None, 2),
            (['show', 'https://example.invalid/x.git#frag', 'skills/example'], None, 2),
            (['show', 'user:pw@example.invalid:o/r.git', 'skills/example'], None, 2),
            # "--" ends the options, so the address reaches the tool as given.
            (['show', '--', '-upload-pack=x', 'skills/example'], 'a repository address cannot begin with "-"', 2),
            (['show', bare, 'skills/example', '--ref', 'nothing'], 'the source has no branch or tag named nothing', 2),
            (['show', bare, 'skills/example', '--ref', 'twin'],
             'twin names both a branch and a tag in the source; pass refs/heads/twin or refs/tags/twin', 2),
            (['show', bare, 'skills/example', '--ref', '1' * 40],
             f"the source does not serve commit {'1' * 40}", 2),
            (['show', bare, 'skills/missing'], f'there is no package folder skills/missing at commit {commit[:12]}', 2),
            (['show', bare, 'README.md'], f'there is no package folder README.md at commit {commit[:12]}', 2),
        ]
        for path in ('../skills/example', '.', 'skills//example', '/skills/example', 'skills/example/'):
            cases.append((['show', bare, path], 'the package path must be a relative path inside the repository, '
                                                f'with no empty, "." or ".." part: {path}', 2))
        s1 = cases[0][1]
        for argv, message, code in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_main('--project', project, *argv),
                                 (code, '', 'trace-skills: ' + (message or s1) + '\n'))
        # ssh with a user name is how SSH is written, so it is not S1; it fails later, at the fetch (S5).
        result = self.run_main('--project', project, 'list', 'ssh://git@example.invalid/o/r.git')
        self.assertEqual(result[0], 2)
        self.assertTrue(result[2].startswith('trace-skills: could not fetch ssh://git@example.invalid/o/r.git: '))

    def test_a_source_head_with_no_branch_is_S3(self):
        bare = self.root / 'unborn.git'
        git(self.root, 'init', '-q', '--bare', str(bare))
        git(bare, 'symbolic-ref', 'HEAD', 'refs/heads/nowhere')
        code, _, err = self.run_main('--project', self.project, 'list', bare)
        self.assertEqual((code, err), (2, 'trace-skills: the source has no branch or tag named HEAD\n'))

    def test_list(self):
        bare, commit, _ = self.library(extra={'skills/long/SKILL.md': skill_md('long', 'x' * 250)},
                                       special=[('skills/linked/SKILL.md', '120000', b'elsewhere')])
        code, out, err = self.run_main('--project', self.project, 'list', bare)
        self.assertEqual((code, err), (0, ''))
        handoff = ('handoff not stated: contract named, record mechanism named, procedure set to its own id')
        self.assertEqual(out.splitlines(), [
            f'Source: {bare.as_posix()} at {commit[:12]} (requested HEAD)',
            'Packages: 7',
            '  skills/example: example', '    A skill for tests.',
            f'    definition 1: format valid; licence stated; {handoff}',
            '  skills/example-skill: example-skill', '    An example skill for tests.',
            f'    definition 1: format valid; licence stated; {handoff}',
            '  skills/legacy: legacy', '    A skill for tests.',
            f'    definition 1: format valid; licence not stated; {handoff}',
            '  skills/linked: refused: SKILL.md is a symbolic link',
            '  skills/literature-processing: literature-processing', '    A skill for tests.',
            f'    definition 1: format valid; licence not stated; {handoff}',
            '  skills/long: long', '    ' + 'x' * 200 + '...',
            f'    definition 1: format valid; licence not stated; {handoff}',
            '  skills/own: own', '    A skill for tests.',
            f'    definition 1: format valid; licence not stated; {handoff}',
            'Nothing was copied into any project.',
            ts.LIMIT])
        self.assertFalse(list((self.root / 'tmp').iterdir()), 'list removes its folder')
        empty, commit, _ = self.make_source({'README.md': b'x\n'})
        code, out, _ = self.run_main('--project', self.project, 'list', empty)
        self.assertEqual(out.splitlines()[1], 'Packages: 0 (no folder holding a SKILL.md at this commit)')

    def test_show_and_its_candidate(self):
        control = skill_md('shown', 'Shown.', 'Line\x1b[2J\r\n‮evil\n')
        bare, commit, _ = self.make_source({'skills/shown/SKILL.md': control,
                                            'skills/shown/data.bin': b'\x00\x01\x02',
                                            'skills/shown/scripts/go.sh': (b'#!/bin/sh\necho hi', '100755'),
                                            'skills/shown/.DS_Store': b'x'})
        code, out, err = self.run_main('--project', self.project, 'show', bare, 'skills/shown', '--ref', 'main')
        self.assertEqual((code, err), (0, ''))
        staging = next((self.root / 'tmp').iterdir())
        candidate = json.loads((staging / 'candidate.json').read_text())
        package = candidate['package']
        lines = out.splitlines()
        self.assertEqual(lines[:6], [
            f'Source: {bare.as_posix()} at {commit} (requested main)',
            f"Package: skills/shown (shown), 3 files, {sum(item['size'] for item in package['files'])} bytes",
            f"Package digest: {package['tree_sha256']}",
            'definition 1: format valid; licence not stated; handoff not stated: contract named, record mechanism '
            'named, procedure set to its own id',
            'Left out as operating-system files: .DS_Store',
            ''])
        skill = next(item for item in package['files'] if item['path'] == 'SKILL.md')
        self.assertIn(f"--- SKILL.md (100644, {skill['size']} bytes, sha256 {skill['sha256']})\n"
                      '---\nname: shown\ndescription: Shown.\n---\nLine\\x1b[2J\\r\n\\u202eevil\n'
                      '--- end of SKILL.md\n', out)
        self.assertIn('--- data.bin (100644, 3 bytes, sha256 ', out)
        self.assertIn('(not UTF-8 text: 3 bytes; not printed)\n--- end of data.bin\n', out)
        self.assertIn('#!/bin/sh\necho hi\n--- end of scripts/go.sh\n', out)
        file = (staging / 'candidate.json').as_posix()
        self.assertEqual(lines[-4:], [
            f'Candidate: {file}',
            f'To adopt exactly these bytes: {self.tool(self.project)} adopt {bare.as_posix()} skills/shown '
            f'--candidate {file}',
            f'The staged copy at {(staging / "package").as_posix()} is removed when adopt applies it.',
            ts.LIMIT])
        self.assertEqual(candidate['schema_version'], 1)
        self.assertEqual(candidate['kind'], 'trace-skills-candidate')
        self.assertEqual(candidate['contract'], 1)
        self.assertEqual(candidate['created_at'], '2026-10-01T12:00:00Z')
        self.assertEqual(candidate['created_by'], 'show')
        self.assertEqual(candidate['for'], {'operation': 'adopt', 'name': 'shown', 'from_commit': None})
        self.assertEqual(candidate['source'], {'repository': bare.as_posix(), 'path': 'skills/shown',
                                               'requested_ref': 'main', 'commit': commit})
        self.assertEqual(package['litter'], ['.DS_Store'])
        self.assertEqual(package['front_matter_notes'], [])
        self.assertEqual(package['definition']['version'], 1)
        self.assertEqual(candidate['staged'], (staging / 'package').as_posix())
        self.assertEqual(mode_of(staging / 'package/scripts/go.sh'), '100755')
        self.assertFalse((staging / 'package/.DS_Store').exists())
        self.assertEqual(package['tree_sha256'], ts.package_digest(package['files']))
        self.assertEqual((staging / 'candidate.json').read_bytes(), bs.encoded(candidate))

    def test_the_vector_package_has_the_vector_digest(self):
        bare, commit, _ = self.library()
        code, out, _ = self.run_main('--project', self.project, 'show', bare, 'skills/example-skill')
        self.assertEqual(code, 0)
        self.assertIn(f'Package digest: {VECTOR_DIGEST}\n', out)

    def test_E10_a_temporary_folder_inside_the_project(self):
        bare, commit, _ = self.library()
        inner = self.project / 'scratch'
        inner.mkdir()
        with mock.patch.object(tempfile, 'tempdir', str(inner)):
            code, _, err = self.run_main('--project', self.project, 'show', bare, 'skills/example')
        self.assertEqual(code, 2)
        self.assertRegex(err, r'^trace-skills: the temporary folder .*/scratch/trace-skills-\w+ is inside the '
                              r'project; set TMPDIR to a folder outside it\n$')
        self.assertEqual(list(inner.iterdir()), [])

    def test_E1_not_a_project_root(self):
        code, _, err = self.run_main('--project', self.project / '.skills', 'status')
        self.assertEqual((code, err), (2, f"trace-skills: {(self.project / '.skills').as_posix()} is not the top of "
                                          "a Git working tree; run this from the project's root, or pass --project\n"))

    def test_the_commands_builder_D_adds(self):
        # Builder D's commands answer with the catalogue (6.5 to 6.9); tests/test_trace_skills_lifecycle.py covers them.
        for argv in (['update', 'x'], ['update', 'x', '--detach'], ['update', 'x', '--accept-merged'],
                     ['update', 'x', '--attach'], ['retire', 'x']):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_main('--project', self.project, *argv),
                                 (1, '', 'trace-skills: no skill named x is adopted here\n'))
        code, out, err = self.run_main('--project', self.project, 'declare', 'x', '--held-by', 'A')
        self.assertEqual((code, err), (1, ''))
        self.assertTrue(out.endswith(f'run: {self.tool(self.project)} declare x --held-by A --yes\n'), out)


# adopt (6.3), with the agent copies of section 9 -------------------------------

class AdoptTests(Fixture):
    maxDiff = None

    def expected_inventory(self, rows):
        adopted = ('| Skill id | Source | Commit | Audience | Detached | Receipt |\n'
                   '| --- | --- | --- | --- | --- | --- |\n' + ''.join(row + '\n' for row in rows)) if rows else 'None.\n'
        return ('<!-- trace-skills:begin inventory -->\n'
                '## Skills from outside this project\n\n'
                "Written by the kit's skill tool (scripts/trace_skills.py in an installed project) from "
                '.trace-os/skills.lock.json; change it with that tool, not by hand.\n\n'
                '### Adopted from a source\n\n' + adopted + '\n'
                '### Relied on, not in the project\n\nNone.\n\n'
                '### Retired\n\nNone.\n'
                '<!-- trace-skills:end inventory -->\n')

    def test_the_package_then_the_plan_and_A1(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                     '--yes')
        self.assertEqual((code, err), (1, 'trace-skills: the source (not checked: test) is not known to be public, '
                                          'and the project is not checked: the project has no origin remote: copying '
                                          'it here lets everyone who can read the project read it. If the person '
                                          'wants that, pass --share-with-project, with their words in --words\n'))
        self.assertFalse((project / '.skills/example-skill').exists())
        code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                       '--yes', '--share-with-project', '--words', 'Yes, it may be read here.')
        self.assertEqual((code, err), (0, ''), out)
        self.assertTrue(out.startswith(f'Source: {bare.as_posix()} at {commit} (requested {commit})\n'
                                       'Package: skills/example-skill (example-skill), 3 files, 106 bytes\n'
                                       f'Package digest: {VECTOR_DIGEST}\n'))
        self.assertIn('--- scripts/run.py (100755, 12 bytes, sha256 ', out)
        self.assertIn('--- end of scripts/run.py\n\n' + '\n'.join([
            f'Plan: adopt example-skill into {project.as_posix()}',
            f'  from: {bare.as_posix()} skills/example-skill at {commit} (requested {commit})',
            f'  package digest: {VECTOR_DIGEST}',
            '  definition 1: format valid; licence stated; handoff not stated: contract named, record mechanism '
            'named, procedure set to its own id',
            '  name: example-skill is free',
            '  audience: source not checked: test; project not checked: the project has no origin remote; source '
            'not known to be public: copying it here lets everyone who can read the project read it',
            '  Shared with the project: by Test Researcher; words: Yes, it may be read here.',
            '  writes, in this order:']), out)

    def test_installed_claude_every_file_and_record(self):
        project = self.installed('claude')
        bare, commit, _ = self.library(extra={'skills/example-skill/scripts/__pycache__/run.cpython-312.pyc': b'\0pyc'})
        head_before = git(project, 'rev-parse', 'HEAD')
        out = self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public',
                         '--audience', 'project=collaborators', '--by', 'Ann Author', '--words', 'yes, adopt it',
                         '--authority', 'T3 of ai/records/x', '--execution-agent', 'claude-opus-5-5',
                         '--used-for', 'tests')
        operation = f'{STAMP}-example-skill-adopt'
        receipt_path = f'.trace-os/skill-adoptions/{operation}.json'
        pyc = 'scripts/__pycache__/run.cpython-312.pyc'
        tool = self.tool(project)
        self.assertIn('\n'.join([
            '  name: example-skill is free',
            '  audience: source declared public; project declared collaborators; source public; the copy widens '
            'nothing',
            '  writes, in this order:',
            '    4 file(s) under .skills/example-skill/',
            '    .trace-os/skill-bases/README.md (says the originals there are not skills to follow)',
            f'    4 file(s) under .trace-os/skill-bases/{self.lock(project)["skills"]["example-skill"]["upstream"]["tree_sha256"]}/',
            '    3 agent copies under .claude/skills/example-skill/',
            '    .trace-os/install.json',
            '    .trace-os/skills.lock.json',
            '    ai/SKILLS-IN-USE.md (the inventory section)',
            f'  then: an intake row in sources/intake.jsonl, and last the receipt {receipt_path}',
            f'  not tracked by Git here: .skills/example-skill/{pyc}, '
            f'.trace-os/skill-bases/{self.lock(project)["skills"]["example-skill"]["upstream"]["tree_sha256"]}/{pyc}',
            '']), out)
        self.assertTrue(out.endswith('\n'.join([
            f'Adopted example-skill from {bare.as_posix()} skills/example-skill at {commit[:12]}.',
            f'  receipt: {receipt_path}',
            '  intake: IN-001 in sources/intake.jsonl',
            '  agent copies: 3 written for claude',
            'Next: review and commit these files, then record the adoption against that commit:',
            f'  {tool} link {receipt_path} --commit <sha>',
            'Nothing was committed or pushed.', ''])))
        # Files and modes: the copy, the original and the agent copies.
        tree = {path: value for path, value in VECTOR.items()}
        tree[pyc] = b'\0pyc'
        lock = self.lock(project)
        entry = lock['skills']['example-skill']
        snapshot = entry['upstream']['snapshot']
        for path, value in tree.items():
            data = value[0] if isinstance(value, tuple) else value
            self.assertEqual((project / '.skills/example-skill' / path).read_bytes(), data)
            self.assertEqual((project / snapshot / path).read_bytes(), data)
        for path in VECTOR:
            self.assertEqual((project / '.claude/skills/example-skill' / path).read_bytes(),
                             (project / '.skills/example-skill' / path).read_bytes())
        self.assertFalse((project / '.claude/skills/example-skill' / pyc).exists())
        for root in ('.skills/example-skill', snapshot, '.claude/skills/example-skill'):
            self.assertEqual(mode_of(project / root / 'scripts/run.py'), '100755')
            self.assertEqual(mode_of(project / root / 'SKILL.md'), '100644')
        # The lock (4.1, 4.2).
        self.assertEqual(lock['schema_version'], 1)
        self.assertEqual(lock['project_audience'], {'declared': 'collaborators', 'declared_by': 'Ann Author',
                                                    'declared_at': '2026-10-01T12:00:00Z'})
        tree_sha256 = entry['upstream']['tree_sha256']
        self.assertNotEqual(tree_sha256, VECTOR_DIGEST)
        self.assertEqual(entry, {
            'manager': 'external',
            'source': {'repository': bare.as_posix(), 'path': 'skills/example-skill', 'requested_ref': commit,
                       'commit': commit, 'audience': {'declared': 'public', 'declared_by': 'Ann Author',
                                                      'observed': 'not checked: test'}},
            'project_audience': {'declared': 'collaborators', 'declared_by': 'Ann Author',
                                 'observed': 'not checked: the project has no origin remote'},
            'upstream': {'tree_sha256': tree_sha256, 'tracked_sha256': VECTOR_DIGEST,
                         'snapshot': f'.trace-os/skill-bases/{tree_sha256}'},
            'adopted': {'path': '.skills/example-skill', 'tree_sha256': VECTOR_DIGEST, 'receipt': receipt_path,
                        'kept_existing': False},
            'detached': False, 'replaced_kit_copy': False, 'receipts': [receipt_path]})
        self.assertEqual((project / ts.LOCK).read_bytes(), bs.encoded(lock))
        # The receipt (5.1).
        receipt = self.receipt(project, operation)
        self.assertEqual((project / receipt_path).read_bytes(), bs.encoded(receipt))
        rows = self.intake_rows(project)
        self.assertEqual(len(rows), 1)
        line = (project / ts.INTAKE).read_bytes().split(b'\n')[0]
        self.assertEqual(receipt['records'], {'intake': {'id': 'IN-001', 'registry': 'sources/intake.jsonl',
                                                         'row_sha256': hashlib.sha256(line).hexdigest()},
                                              'transition': 'not yet: link writes it after the commit'})
        for key, value in {'schema_version': 1, 'kind': 'trace-skills-receipt', 'contract': 1, 'operation': 'adopt',
                           'operation_id': operation, 'name': 'example-skill', 'started_at': '2026-10-01T12:00:00Z',
                           'completed_at': '2026-10-01T12:00:00Z', 'recovered': False, 'previous': None,
                           'not_tracked': [f'.skills/example-skill/{pyc}', f'{snapshot}/{pyc}'],
                           'limits': ts.RECEIPT_LIMITS}.items():
            self.assertEqual(receipt[key], value, key)
        self.assertEqual(receipt['tool'], {'path': Path(ts.__file__).resolve().as_posix(),
                                           'sha256': hashlib.sha256(Path(ts.__file__).read_bytes()).hexdigest()})
        self.assertEqual(receipt['project'], {'installed': True, 'kit_version': 'test-1', 'agent': 'claude',
                                              'head': head_before})
        self.assertEqual(receipt['source'], {'repository': bare.as_posix(), 'path': 'skills/example-skill',
                                             'requested_ref': commit, 'commit': commit})
        self.assertEqual(receipt['upstream']['tree_sha256'], tree_sha256)
        self.assertEqual(receipt['upstream']['tracked_sha256'], VECTOR_DIGEST)
        self.assertEqual([item['path'] for item in receipt['upstream']['files']],
                         ['LICENSE.txt', 'SKILL.md', pyc, 'scripts/run.py'])
        self.assertEqual(receipt['upstream']['litter'], [])
        self.assertEqual(receipt['copy']['tree_sha256'], VECTOR_DIGEST)
        self.assertEqual([item['path'] for item in receipt['copy']['files']], ['LICENSE.txt', 'SKILL.md',
                                                                              'scripts/run.py'])
        self.assertEqual(receipt['definition']['format'], 'format valid')
        self.assertEqual(receipt['audience'], {
            'source': entry['source']['audience'], 'project': entry['project_audience'],
            'comparison': 'source public; the copy widens nothing',
            'share_with_project': {'required': False, 'given': False}})
        self.assertEqual(receipt['ownership'], {'before': 'free', 'replaced_kit_copy': False,
                                                'kit_files_removed': [], 'retired_skills_added': []})
        self.assertEqual(receipt['agent_copies'], {
            'agent': 'claude', 'removed': [], 'kept': [], 'note': ts.NOTE_ADAPTER,
            'written': [f'.claude/skills/example-skill/{path}' for path in ('LICENSE.txt', 'SKILL.md',
                                                                             'scripts/run.py')]})
        self.assertEqual(receipt['modes'], {f'{root}/scripts/run.py': '100755' for root in (
            '.skills/example-skill', snapshot, '.claude/skills/example-skill')})
        self.assertEqual(receipt['decided'], {'by': 'Ann Author', 'words': 'yes, adopt it',
                                              'authority': 'T3 of ai/records/x',
                                              'execution_agent': 'claude-opus-5-5', 'confirmed': '--yes'})
        self.assertEqual(sorted(receipt['writes']), sorted(
            [f'.skills/example-skill/{path}' for path in tree] + [f'{snapshot}/{path}' for path in tree]
            + [f'.claude/skills/example-skill/{path}' for path in VECTOR]
            + [ts.BASES_NOTE, '.trace-os/install.json', ts.LOCK, ts.INVENTORY]))
        self.assertEqual(receipt['writes'][ts.LOCK], {'before': None, 'after': bs.digest((project / ts.LOCK).read_bytes())})
        # The inventory section, placed after the routing block, byte for byte.
        row = (f'| example-skill | {bare.as_posix()} `skills/example-skill` | {commit[:12]} | source public | no | '
               f'`{receipt_path}` |')
        self.assertEqual((project / ts.INVENTORY).read_text(), ROUTING + '\n' + self.expected_inventory([row]))
        # The intake row, validated by trace_record.
        self.assert_valid_registries(project)
        self.assertEqual(rows[0]['what'], f'skill package example-skill ({bare.as_posix()} skills/example-skill) at '
                                          f'commit {commit}, package digest {tree_sha256}')
        self.assertEqual(rows[0]['origin'], f'{bare.as_posix()} skills/example-skill at commit {commit}, requested '
                                            f'as {commit}')
        self.assertEqual(rows[0]['admitted_by'], 'Ann Author')
        self.assertEqual(rows[0]['checked'], (
            "the whole package (4 files, 110 bytes) was printed before writing and the person confirmed it (--yes); "
            "every file's SHA-256 and the package digest were re-checked just before writing; definition 1: format "
            'valid; licence stated; handoff not stated: contract named, record mechanism named, procedure set to its '
            'own id; audience: source public; the copy widens nothing'))
        self.assertEqual(rows[0]['used_for'], 'tests')
        self.assertEqual(rows[0]['object'], receipt_path)
        self.assertNotIn('object_sha256', rows[0])
        self.assertEqual(rows[0]['authority'], 'T3 of ai/records/x')
        # No journal, no staging folder, nothing committed; the parity criterion holds.
        self.assertFalse((project / ts.OPERATION).exists())
        self.assertFalse((project / bs.JOURNAL).exists())
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])
        self.assertEqual(git(project, 'rev-parse', 'HEAD'), head_before)
        self.assert_parity(project)
        # The same adoption again is nothing to do.
        code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                       '--yes')
        self.assertEqual((code, out, err), (0, f'Already adopted: example-skill from {bare.as_posix()} '
                                               f'skills/example-skill at {commit[:12]}; nothing to do.\n', ''))

    def test_installed_with_agent_none(self):
        project = self.installed('none')
        bare, commit, _ = self.library()
        out = self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        self.assertIn('    no agent copies (agent none)\n', out)
        self.assertNotIn('.trace-os/install.json\n', out[out.index('writes, in this order'):])
        self.assertIn('  agent copies: none (agent none)\n', out)
        receipt = self.receipt(project, f'{STAMP}-example-skill-adopt')
        self.assertEqual(receipt['agent_copies'], {'agent': 'none', 'written': [], 'kept': [], 'removed': [],
                                                   'note': 'agent none: no copies; agents read .skills/ directly'})
        self.assertNotIn('.trace-os/install.json', receipt['writes'])
        self.assertFalse((project / '.claude').exists())
        self.assert_parity(project, sync=False)

    def test_uninstalled_project(self):
        links = os.name != 'nt'
        project = self.uninstalled(link_agent_root=links)
        bare, commit, _ = self.library()
        out = self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        self.assertIn('    no agent copies (no adapter state here)\n', out)
        self.assertIn('  agent copies: none (no adapter state here)\n', out)
        receipt = self.receipt(project, f'{STAMP}-example-skill-adopt')
        self.assertEqual(receipt['project'], {'installed': False, 'kit_version': None, 'agent': None,
                                              'head': git(project, 'rev-parse', 'HEAD')})
        self.assertEqual(receipt['agent_copies'], {'agent': None, 'written': [], 'kept': [], 'removed': [],
                                                   'note': 'no adapter state here'})
        self.assertFalse((project / bs.STATE).exists())
        if links:
            self.assertTrue((project / '.claude/skills').is_symlink())
            self.assertEqual(os.readlink(project / '.claude/skills'), '../.skills')
        # A missing ai/SKILLS-IN-USE.md is created holding the section alone.
        receipt_path = f'{ts.RECEIPTS}/{STAMP}-example-skill-adopt.json'
        self.assertEqual((project / ts.INVENTORY).read_text(), self.expected_inventory([
            f'| example-skill | {bare.as_posix()} `skills/example-skill` | {commit[:12]} | source public | no | '
            f'`{receipt_path}` |']))
        self.assert_valid_registries(project)
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example', '--ref', commit, '--yes',
                                     '--replace-kit-copy')
        self.assertEqual((code, err), (1, 'trace-skills: --replace-kit-copy: example is not a skill the kit ships '
                                          'here; nothing to replace\n'))

    def test_replace_kit_copy(self):
        project = self.installed('claude', skills={'.skills/example/notes.md': ('kit notes\n', 'managed'),
                                                   'scripts/example_helper.py': ("print('kit')\n", 'managed'),
                                                   'tests/test_example_helper.py': ('# the kit test\n', 'managed')})
        bare, commit, _ = self.library()
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example', '--ref', commit, '--yes',
                                     '--audience', 'source=public')
        self.assertEqual((code, err), (1, "trace-skills: example is a skill the kit ships; adopting it would leave "
                                          "two copies under one name. Pass --replace-kit-copy to remove the kit's "
                                          "copy and keep the kit's updater from installing it again, or choose "
                                          "another package\n"))
        (project / '.skills/example/notes.md').write_text('edited\n')
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example', '--ref', commit, '--yes',
                                     '--audience', 'source=public', '--replace-kit-copy')
        self.assertEqual((code, err), (1, "trace-skills: the kit's copy of example was edited here "
                                          "(.skills/example/notes.md); --replace-kit-copy removes only an unedited "
                                          "kit copy. Move the edit to a file of your own, then run this again\n"))
        git(project, 'checkout', '--', '.skills/example/notes.md')
        out = self.adopt(project, bare, 'skills/example', '--audience', 'source=public', '--replace-kit-copy')
        # AH-255: the kit's files outside .skills/ whose path holds the name are named, and stay.
        self.assertIn("  name: the kit's copy is replaced (--replace-kit-copy)\n"
                      "  the kit's files that share this skill's name stay where the kit ships them: "
                      'scripts/example_helper.py, tests/test_example_helper.py\n', out)
        self.assertIn('    2 kit file(s) removed: .skills/example/SKILL.md, .skills/example/notes.md\n', out)
        for path in ('scripts/example_helper.py', 'tests/test_example_helper.py'):
            self.assertTrue((project / path).is_file(), path)
        # A name match: the kit's meeting example names its helper meeting_quotes.
        meeting = {'files': {path: {'sha256': 'a' * 64, 'policy': policy} for path, policy in (
            ('scripts/meeting_quotes.py', 'managed'), ('tests/test_meeting_quotes.py', 'managed'),
            ('.skills/meeting-and-idea-intake/SKILL.md', 'managed'), ('scripts/other.py', 'managed'),
            ('ai/meeting-and-idea-intake.md', 'seed'))}}
        self.assertEqual(ts.kit_companions(meeting, 'meeting-and-idea-intake'),
                         ['scripts/meeting_quotes.py', 'tests/test_meeting_quotes.py'])
        self.assertEqual((project / '.skills/example/SKILL.md').read_bytes(), skill_md('example', body='Library version.\n'))
        self.assertFalse((project / '.skills/example/notes.md').exists())
        self.assertFalse((project / '.claude/skills/example/notes.md').exists())
        self.assertEqual((project / '.claude/skills/example/LICENSE.txt').read_bytes(), b'Apache License 2.0\n')
        state = json.loads((project / bs.STATE).read_text())
        self.assertEqual(state['retired_skills'], ['example'])
        self.assertNotIn('.skills/example/SKILL.md', state['files'])
        self.assertEqual(state['files']['.claude/skills/example/SKILL.md']['canonical'], '.skills/example/SKILL.md')
        receipt = self.receipt(project, f'{STAMP}-example-adopt')
        self.assertEqual(receipt['ownership'], {'before': 'kit', 'replaced_kit_copy': True,
                                                'kit_files_removed': ['.skills/example/SKILL.md',
                                                                      '.skills/example/notes.md'],
                                                'retired_skills_added': ['example']})
        self.assertEqual(receipt['agent_copies']['removed'], ['.claude/skills/example/notes.md'])
        self.assertTrue(self.lock(project)['skills']['example']['replaced_kit_copy'])
        self.assert_parity(project)

    def test_the_kit_updater_keeps_a_retired_name(self):
        project = self.installed('claude')
        shutil.rmtree(project / '.skills/example')
        bs.sync_skills(project)
        git(project, 'add', '-A')
        git(project, 'commit', '-qm', 'Retire the example')
        bare, commit, _ = self.library()
        out = self.adopt(project, bare, 'skills/example', '--audience', 'source=public')
        self.assertIn("  name: example is free\n  the kit's updater keeps example retired; this adoption does not "
                      "change that\n", out)
        self.assert_parity(project)

    def test_names_N7_N8_N10_N11_and_keep_existing(self):
        project = self.uninstalled()
        bare, commit, _ = self.library()
        base = ['--project', project, 'adopt', bare]
        tail = ['--ref', commit, '--yes', '--audience', 'source=public']
        self.assertEqual(self.run_main(*base, 'skills/own', *tail), (1, '', (
            'trace-skills: own already exists in this project (.skills/own/), written here or of unknown origin; '
            'pass --keep-existing to keep it and record this package as its upstream original, or move it first\n')))
        self.assertEqual(self.run_main(*base, 'skills/legacy', *tail), (1, '', (
            'trace-skills: a single-file skill .skills/legacy.md has this name; turn it into .skills/legacy/SKILL.md '
            'or remove it first\n')))
        self.assertEqual(self.run_main(*base, 'skills/legacy', *tail, '--keep-existing')[2],
                         'trace-skills: a single-file skill .skills/legacy.md has this name; turn it into '
                         '.skills/legacy/SKILL.md or remove it first\n')
        self.assertEqual(self.run_main(*base, 'skills/example-skill', *tail, '--keep-existing'), (1, '', (
            'trace-skills: --keep-existing: there is no .skills/example-skill/ here to keep\n')))
        self.assertEqual(self.run_main(*base, 'skills/own', *tail, '--keep-existing', '--replace-kit-copy'), (2, '', (
            'trace-skills: --replace-kit-copy and --keep-existing exclude each other\n')))
        before = (project / '.skills/literature-processing/SKILL.md').read_bytes()
        out = self.adopt(project, bare, 'skills/literature-processing', '--audience', 'source=public',
                         '--keep-existing')
        self.assertIn('  name: the existing copy is kept and connected (--keep-existing)\n', out)
        self.assertIn('    .skills/literature-processing/ is kept as it is\n', out)
        self.assertEqual((project / '.skills/literature-processing/SKILL.md').read_bytes(), before)
        entry = self.lock(project)['skills']['literature-processing']
        self.assertTrue(entry['adopted']['kept_existing'])
        kept = ts.package_digest([ts.manifest([ts.member('SKILL.md', '100644', before)])[0]])
        self.assertEqual(entry['adopted']['tree_sha256'], kept)
        self.assertNotEqual(entry['upstream']['tracked_sha256'], kept)
        snapshot = project / entry['upstream']['snapshot'] / 'SKILL.md'
        self.assertEqual(snapshot.read_bytes(), skill_md('literature-processing', body='Upstream method.\n'))
        receipt = self.receipt(project, f'{STAMP}-literature-processing-adopt')
        self.assertEqual(receipt['ownership']['before'], 'project')
        self.assertTrue(receipt['copy']['kept_existing'])
        self.assertNotIn('.skills/literature-processing/SKILL.md', receipt['writes'])

    def test_keep_existing_N20_and_a_special_file(self):
        project = self.uninstalled()
        bare, commit, _ = self.library()
        (project / '.skills/literature-processing/SKILL.md').write_bytes(skill_md('something-else'))
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/literature-processing', '--ref',
                                     commit, '--yes', '--audience', 'source=public', '--keep-existing')
        self.assertEqual((code, err), (1, 'trace-skills: the skill at .skills/literature-processing is named '
                                          'something-else, not literature-processing\n'))
        if hasattr(os, 'mkfifo'):
            (project / '.skills/literature-processing/SKILL.md').write_bytes(skill_md('literature-processing'))
            os.mkfifo(project / '.skills/literature-processing/pipe')
            code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/literature-processing',
                                         '--ref', commit, '--yes', '--audience', 'source=public', '--keep-existing')
            self.assertEqual((code, err), (1, 'trace-skills: the package at .skills/literature-processing is refused: '
                                              'pipe is not a regular file\n'))

    def test_N2_and_N3(self):
        project = self.installed('claude')
        bare, first, work = self.library()
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        second = self.commit_source(work, bare, {'skills/example-skill/SKILL.md': skill_md(
            'example-skill', 'An example skill for tests.', 'Changed.\n')})
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', second,
                                     '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (1, f'trace-skills: example-skill is adopted from this source at {first[:12]}; '
                                          'use update example-skill to move it\n'))
        other, commit, _ = self.make_source({'lib/example-skill/SKILL.md': skill_md('example-skill')})
        code, _, err = self.run_main('--project', project, 'adopt', other, 'lib/example-skill', '--ref', commit,
                                     '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (1, f'trace-skills: example-skill is adopted from another source '
                                          f'({bare.as_posix()} skills/example-skill); retire it first: '
                                          f'{self.tool(project)} retire example-skill\n'))
        lock = self.lock(project)
        lock['skills']['own'] = {'manager': 'outside', 'held_by': 'Bea Holder', 'path': None,
                                 'digest': {'tree_sha256': None, 'basis': 'not stated'}, 'limitation': 'x',
                                 'declared_by': 'Bea Holder', 'receipt': f'{ts.RECEIPTS}/own.json',
                                 'receipts': [f'{ts.RECEIPTS}/own.json']}
        (project / ts.LOCK).write_bytes(bs.encoded(lock))
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/own', '--ref', second, '--yes',
                                     '--audience', 'source=public')
        self.assertEqual((code, err), (1, 'trace-skills: own is declared as held outside the project by Bea Holder; '
                                          f'retire the declaration first: {self.tool(project)} retire own\n'))

    def test_N23_and_E20_for_agent_copies(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        stray = project / '.claude/skills/example-skill/SKILL.md'
        stray.parent.mkdir(parents=True)
        stray.write_text('a copy somebody edited\n')
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                     '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (1, 'trace-skills: a generated copy of example-skill was edited separately '
                                          '(.claude/skills/example-skill/SKILL.md); merge or remove it, then run this '
                                          'again\n'))
        self.assertFalse((project / '.skills/example-skill').exists())
        if os.name == 'nt':
            return
        shutil.rmtree(project / '.claude/skills/example-skill')
        (project / '.claude/skills').rename(project / '.claude/real')
        os.symlink('real', project / '.claude/skills')
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                     '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (2, 'trace-skills: symlink is not supported: '
                                          '.claude/skills/example-skill/LICENSE.txt\n'))

    def test_the_audience_rule(self):
        bare, commit, _ = self.library()
        cases = [
            ({}, 'not checked: test', True, 'source not known to be public: copying it here lets everyone who can '
                                            'read the project read it'),
            ({'source': 'public'}, 'not checked: test', False, 'source public; the copy widens nothing'),
            ({}, 'public (gh, 2026-10-01)', False, 'source public; the copy widens nothing'),
            ({'source': 'private', 'project': 'collaborators'}, 'not checked: test', True,
             'source narrower than the project'),
            ({'source': 'collaborators'}, 'restricted (gh, 2026-10-01)', True,
             'source not known to be public: copying it here lets everyone who can read the project read it'),
        ]
        for number, (declared, observed, required, comparison) in enumerate(cases):
            with self.subTest(declared=declared, observed=observed):
                project = self.installed('claude', name=f'audience-{number}')
                flags = [flag for side, value in declared.items() for flag in ('--audience', f'{side}={value}')]

                def seen(address, observed=observed):
                    return observed if address == bare.as_posix() else 'not checked: the project has no origin remote'

                with mock.patch.object(ts, 'observe_audience', side_effect=seen):
                    code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill',
                                                   '--ref', commit, '--yes', '--by', 'Ann Author', *flags)
                    if required:
                        self.assertEqual(code, 1)
                        self.assertTrue(err.startswith('trace-skills: the source ('))
                        self.assertTrue(err.endswith('If the person wants that, pass --share-with-project, with their '
                                                     'words in --words\n'))
                        code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill',
                                                       '--ref', commit, '--yes', '--by', 'Ann Author', *flags,
                                                       '--share-with-project', '--words', 'Yes, copy it here.')
                self.assertEqual((code, err), (0, ''))
                self.assertIn(f'; {comparison}\n', out)
                receipt = self.receipt(project, f'{STAMP}-example-skill-adopt')
                self.assertEqual(receipt['audience']['comparison'], comparison)
                self.assertEqual(receipt['audience']['share_with_project'], {'required': required, 'given': required})
                self.assertEqual(receipt['audience']['source']['observed'], observed)
                self.assertEqual(receipt['audience']['source']['declared'], declared.get('source', 'not declared'))
                self.assertEqual(receipt['audience']['source']['declared_by'],
                                 'Ann Author' if 'source' in declared else None)
                inventory = (project / ts.INVENTORY).read_text()
                if required:
                    self.assertIn('  Shared with the project: by Ann Author; words: Yes, copy it here.\n', out)
                    self.assertEqual(receipt['decided']['words'], 'Yes, copy it here.')
                    self.assertIn('; copied here with --share-with-project by Ann Author |', inventory)
                else:
                    self.assertNotIn('Shared with the project', out)
                    self.assertNotIn('--share-with-project', inventory)
        cells = {'private': 'source private to Ann Author; updates come through them',
                 'collaborators': 'source shared with named collaborators', 'public': 'source public'}
        for value, cell in cells.items():
            entry = {'source': {'audience': {'declared': value, 'declared_by': 'Ann Author', 'observed': 'x'}}}
            self.assertEqual(ts.audience_cell(entry, None), cell)
        for observed, cell in (('public (gh, 2026-10-01)', 'source public (observed)'),
                               ('restricted (gh, 2026-10-01)', 'source not public (observed; who can read it is not known)'),
                               ('not checked: test', 'source audience not known')):
            entry = {'source': {'audience': {'declared': 'not declared', 'declared_by': None, 'observed': observed}}}
            self.assertEqual(ts.audience_cell(entry, None), cell)
        self.assertEqual(self.run_main('--project', project, 'adopt', bare, 'skills/own', '--audience', 'source=open'),
                         (2, '', 'trace-skills: --audience takes source=<value> or project=<value>, where the value is '
                                 'private, collaborators or public\n'))

    def test_observe_audience_forms(self):
        observe = REAL_OBSERVE
        self.assertEqual(observe('/some/path.git'), 'not checked: a local repository')
        self.assertEqual(observe('https://gitlab.com/o/r.git'), 'not checked: not a GitHub repository')
        with mock.patch.object(shutil, 'which', return_value=None):
            self.assertEqual(observe('https://github.com/o/r.git'), 'not checked: gh is not installed')
        for word, result in (('PUBLIC', 'public (gh, 2026-10-01)'), ('PRIVATE', 'restricted (gh, 2026-10-01)'),
                             ('INTERNAL', 'restricted (gh, 2026-10-01)'),
                             ('', 'not checked: gh could not see the repository')):
            run = subprocess.CompletedProcess([], 0, word + '\n', '')
            with mock.patch.object(shutil, 'which', return_value='/bin/gh'), \
                    mock.patch.object(ts.subprocess, 'run', return_value=run) as called:
                for address in ('https://github.com/o/r.git', 'git@github.com:o/r.git', 'ssh://git@github.com/o/r'):
                    self.assertEqual(observe(address), result)
            self.assertEqual(called.call_args.args[0], ['gh', 'repo', 'view', 'o/r', '--json', 'visibility',
                                                        '--jq', '.visibility'])

    def test_E18_and_E19(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        with (project / '.gitignore').open('a') as stream:
            stream.write('.trace-os/skill-adoptions/\n')
        code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                     '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (1, f"trace-skills: Git ignores .trace-os/skill-adoptions/{STAMP}-example-skill-"
                                          "adopt.json in this project, so it would not reach the project's history; "
                                          "change that ignore rule first\n"))
        git(project, 'checkout', '--', '.gitignore')
        (project / '.gitattributes').write_text('* text=auto eol=lf\n')
        crlf, commit, _ = self.make_source({'skills/crlf/SKILL.md': skill_md('crlf'),
                                            'skills/crlf/notes.md': b'one\r\ntwo\r\n'})
        code, _, err = self.run_main('--project', project, 'adopt', crlf, 'skills/crlf', '--ref', commit, '--yes',
                                     '--audience', 'source=public')
        self.assertEqual((code, err), (1, 'trace-skills: Git would rewrite .skills/crlf/notes.md when it is committed '
                                          '(a line-ending rule or a filter in .gitattributes, or core.autocrlf), so its '
                                          'recorded digest would not survive a clone; exclude .skills/crlf from '
                                          'conversion in .gitattributes, for example ".skills/crlf/** -text", then run '
                                          'this again\n'))
        self.assertFalse((project / '.skills/crlf').exists())
        with (project / '.gitattributes').open('a') as stream:
            stream.write('.skills/crlf/** -text\n')
        code, _, err = self.run_main('--project', project, 'adopt', crlf, 'skills/crlf', '--ref', commit, '--yes',
                                     '--audience', 'source=public')
        self.assertEqual(code, 1)
        self.assertIn('exclude .trace-os/skill-bases from conversion', err)
        with (project / '.gitattributes').open('a') as stream:
            stream.write('.trace-os/skill-bases/** -text\n.claude/skills/crlf/** -text\n')
        code, _, err = self.run_main('--project', project, 'adopt', crlf, 'skills/crlf', '--ref', commit, '--yes',
                                     '--audience', 'source=public')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((project / '.skills/crlf/notes.md').read_bytes(), b'one\r\ntwo\r\n')

    def test_I1(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        for text in (ROUTING + ts.INVENTORY_BEGIN + '\n', ROUTING + ts.INVENTORY_END + '\n' + ts.INVENTORY_BEGIN + '\n',
                     '<!-- trace-os:begin skill-routing -->\n' + ts.INVENTORY_BEGIN + '\n' + ts.INVENTORY_END
                     + '\n<!-- trace-os:end skill-routing -->\n'):
            with self.subTest(text=text):
                (project / ts.INVENTORY).write_text(text)
                code, _, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref',
                                             commit, '--yes', '--audience', 'source=public')
                self.assertEqual((code, err), (1, (
                    'trace-skills: the inventory markers in ai/SKILLS-IN-USE.md are not one begin and one end, in '
                    "that order, outside the kit's skill-routing block; fix them by hand, then run this again\n")))
        # An existing section is replaced in place, with what follows it kept.
        (project / ts.INVENTORY).write_text('top\n' + ts.INVENTORY_BEGIN + '\nold\n' + ts.INVENTORY_END + '\nafter\n')
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        text = (project / ts.INVENTORY).read_text()
        self.assertTrue(text.startswith('top\n' + ts.INVENTORY_BEGIN + '\n## Skills from outside this project\n'))
        self.assertTrue(text.endswith(ts.INVENTORY_END + '\nafter\n'))
        self.assertEqual(ts.place_inventory(b'no newline', b'S\n'), b'no newline\n\nS\n')
        self.assertEqual(ts.place_inventory(None, b'S\n'), b'S\n')

    def test_pending_E7_E8_E9(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        argv = ['--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit, '--yes']
        tool = self.tool(project)
        (project / ts.OPERATION).write_text(json.dumps({'operation_id': 'X-adopt'}))
        self.assertEqual(self.run_main(*argv)[2], f'trace-skills: an earlier operation (X-adopt) did not finish; run '
                                                  f'{tool} recover first\n')
        (project / ts.OPERATION).unlink()
        (project / bs.JOURNAL).write_text('{}')
        self.assertEqual(self.run_main(*argv)[2], 'trace-skills: an unfinished setup transaction is pending '
                                                  f'(.trace-os/transaction.json); run {tool} recover first\n')
        (project / bs.JOURNAL).unlink()
        (project / bs.UPDATE_PENDING).write_text('{}')
        self.assertEqual(self.run_main(*argv), (1, '', 'trace-skills: a kit update did not finish; run python3 '
                                                       'scripts/bootstrap.py --recover first\n'))

    def test_E3_E4_E5_E11_E12(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        argv = ['--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit, '--yes',
                '--audience', 'source=public']
        (project / ts.LOCK).write_text('{not json')
        self.assertEqual(self.run_main(*argv)[2], 'trace-skills: cannot read .trace-os/skills.lock.json (not valid '
                                                  'JSON); restore it from Git: git checkout HEAD -- '
                                                  '.trace-os/skills.lock.json\n')
        (project / ts.LOCK).write_text(json.dumps({'schema_version': 1, 'skills': {'x': {'manager': 'odd'}}}))
        self.assertIn("unknown manager 'odd'", self.run_main(*argv)[2])
        (project / ts.LOCK).write_text(json.dumps({'schema_version': 2, 'skills': {}}))
        self.assertEqual(self.run_main(*argv), (2, '', 'trace-skills: .trace-os/skills.lock.json has schema_version '
                                                       '2; this tool reads version 1\n'))
        (project / ts.LOCK).unlink()
        state = (project / bs.STATE).read_bytes()
        (project / bs.STATE).write_text('[]')
        self.assertEqual(self.run_main(*argv)[0], 2)
        self.assertTrue(self.run_main(*argv)[2].startswith('trace-skills: cannot read .trace-os/install.json ('))
        (project / bs.STATE).write_bytes(state)
        self.assertEqual(self.run_main(*argv, '--execution-agent', 'two words'), (
            2, '', 'trace-skills: execution agent must be one identifier\n'))
        (project / ts.TRACE_RECORD).unlink()
        self.assertEqual(self.run_main(*argv), (2, '', 'trace-skills: scripts/trace_record.py is not in this project, '
                                                       'so the intake row cannot be recorded; nothing was written\n'))

    def test_E6_a_busy_lock(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        # The setup lock is per open file, so this process holding it is another owner to the tool.
        with bs.locked(project):
            code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref',
                                           commit, '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (2, 'trace-skills: another setup process owns this target; wait for it to '
                                          'finish, then run this again\n'))
        self.assertFalse((project / '.skills/example-skill').exists())

    def test_ah239_keep_existing_shows_how_the_kept_copy_differs(self):
        """AH-239: before the yes, the plan lists how the kept copy differs from the package, as update's list does."""
        project = self.uninstalled()
        bare, commit, _ = self.library()
        kept = project / '.skills/example-skill'
        (kept / 'scripts').mkdir(parents=True)
        # Every kind of difference: LICENSE.txt missing, SKILL.md changed, a file added, run.py not executable.
        (kept / 'SKILL.md').write_bytes(VECTOR['SKILL.md'] + b'Our line.\n')
        (kept / 'notes.md').write_bytes(b'Our notes.\n')
        (kept / 'scripts/run.py').write_bytes(VECTOR['scripts/run.py'][0])
        argv = ['--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit, '--audience',
                'source=public', '--keep-existing']
        before = tree_state(project)
        code, out, err = self.run_main(*argv)
        self.assertEqual((code, err), (1, ''))
        self.assertIn('\n'.join([
            '  name: the existing copy is kept and connected (--keep-existing)',
            '  difference from the package to the kept copy (status will report the copy adapted):',
            '    removed LICENSE.txt', '    changed SKILL.md', '    added notes.md', '    mode scripts/run.py',
            '--- package/SKILL.md']), out)
        self.assertEqual(tree_state(project), before, 'a plan nobody confirmed wrote nothing')
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public', '--keep-existing')
        status = self.run_main('--project', project, 'status', '--offline')[1]
        self.assertIn('  copy: adapted (4 file(s) differ from the upstream original: LICENSE.txt, SKILL.md, notes.md, '
                      'scripts/run.py)\n', status)
        # A kept copy holding the package's bytes, and the line that says so.
        (project / '.skills/literature-processing/SKILL.md').write_bytes(
            skill_md('literature-processing', body='Upstream method.\n'))
        out = self.adopt(project, bare, 'skills/literature-processing', '--audience', 'source=public',
                         '--keep-existing')
        self.assertIn('  name: the existing copy is kept and connected (--keep-existing)\n'
                      '  difference from the package to the kept copy: none (status will report the copy '
                      'unchanged)\n', out)
        status = self.run_main('--project', project, 'status', '--offline')[1]
        self.assertIn('literature-processing\n  owner: external, ', status)
        self.assertIn('  copy: unchanged\n', status[status.index('literature-processing\n'):])
        # An adoption that keeps nothing prints no such line.
        self.assertNotIn('difference from the package', self.adopt(project, bare, 'skills/example',
                                                                   '--audience', 'source=public'))

    def test_ah251_keep_existing_names_the_template_scripts_folder(self):
        """AH-251: a scripts/<name>/ the template route made is named in the plan and the receipt, and not compared."""
        project = self.uninstalled()
        bare, commit, _ = self.library()
        (project / '.skills/example-skill').mkdir()
        (project / '.skills/example-skill/SKILL.md').write_bytes(VECTOR['SKILL.md'])
        (project / 'scripts/example-skill').mkdir()
        (project / 'scripts/example-skill/run.py').write_bytes(b"print('ours')\n")
        code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit,
                                       '--audience', 'source=public', '--keep-existing')
        self.assertEqual((code, err), (1, ''))
        self.assertIn('\n'.join([
            '  difference from the package to the kept copy (status will report the copy adapted):',
            '    removed LICENSE.txt', '    removed scripts/run.py',
            "  the project holds scripts/example-skill/, where the template route put this skill's scripts; the "
            'package keeps its scripts under .skills/example-skill/scripts/, and the two are not compared. To move '
            "them into the package's folder, change every command in .skills/example-skill/ that names "
            'scripts/example-skill/ to the new path in the same step and run one of them before connecting; or keep '
            'them where they are and know that status compares only .skills/example-skill/',
            '  audience: ']), out)
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public', '--keep-existing')
        self.assertEqual(self.receipt(project, f'{STAMP}-example-skill-adopt')['kept_outside'],
                         ['scripts/example-skill'])
        self.assertEqual((project / 'scripts/example-skill/run.py').read_bytes(), b"print('ours')\n")
        # With no such folder the plan says nothing of it, and the list is empty.
        out = self.adopt(project, bare, 'skills/literature-processing', '--audience', 'source=public',
                         '--keep-existing')
        self.assertNotIn('the project holds scripts/', out)
        self.assertEqual(self.receipt(project, f'{STAMP}-literature-processing-adopt')['kept_outside'], [])

    def test_ah254_the_intake_row_says_the_copy_was_kept(self):
        """AH-254: a --keep-existing adoption's intake row says the project's copy was kept, and how it differs."""
        project = self.uninstalled()
        bare, commit, _ = self.library()
        self.adopt(project, bare, 'skills/literature-processing', '--audience', 'source=public', '--keep-existing')
        size = len(skill_md('literature-processing', body='Upstream method.\n'))
        self.assertEqual(self.intake_rows(project)[0]['checked'], (
            "the project's existing copy was kept and connected to the package (differences from the package: "
            f'changed SKILL.md); the whole package (1 files, {size} bytes) was printed before writing and the person '
            "confirmed it (--yes); every file's SHA-256 and the package digest were re-checked just before writing; "
            'definition 1: format valid; licence not stated; handoff not stated: contract named, record mechanism '
            'named, procedure set to its own id; audience: source public; the copy widens nothing'))
        # A kept copy holding the package's bytes says none.
        (project / '.skills/example').mkdir()
        (project / '.skills/example/SKILL.md').write_bytes(skill_md('example', body='Library version.\n'))
        (project / '.skills/example/LICENSE.txt').write_bytes(b'Apache License 2.0\n')
        self.adopt(project, bare, 'skills/example', '--audience', 'source=public', '--keep-existing')
        self.assertTrue(self.intake_rows(project)[1]['checked'].startswith(
            "the project's existing copy was kept and connected to the package (differences from the package: "
            'none); the whole package (2 files, '))
        self.assert_valid_registries(project)

    def test_ah261_link_says_a_kept_copy_changed_no_instruction(self):
        """AH-261: linking a --keep-existing adoption says the copy was kept, and that the commit left SKILL.md alone."""
        project = self.uninstalled()
        bare, commit, _ = self.library()
        self.adopt(project, bare, 'skills/literature-processing', '--audience', 'source=public', '--keep-existing',
                   '--by', 'Ann Author')
        git(project, 'add', '-A')
        git(project, 'commit', '-qm', 'Connect literature-processing')
        adopted = git(project, 'rev-parse', 'HEAD')
        receipt = f'.trace-os/skill-adoptions/{STAMP}-literature-processing-adopt.json'
        code, out, err = self.run_main('--project', project, 'link', receipt, '--commit', adopted)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, (
            f'Linked {receipt} to {adopted[:12]}: TR-001 in sources/transitions.jsonl.\n'
            "The receipt kept the project's existing .skills/literature-processing/; the row says so, and that "
            f'{adopted[:12]} did not change its SKILL.md.\n'))
        row = json.loads((project / ts.TRANSITIONS).read_text())
        self.assertEqual(row['destination'], (
            ".skills/literature-processing/ (the project's existing copy, kept and connected to the package, which "
            'did not replace it; this commit did not change its SKILL.md, so no instruction changed)'))
        self.assert_valid_registries(project)

    def test_ah256_keep_existing_prints_the_text_of_each_change(self):
        """AH-256: after the list, a unified diff of each changed text member, escaped as show escapes."""
        project = self.uninstalled()
        bare, commit, _ = self.library()
        (project / '.skills/literature-processing/SKILL.md').write_bytes(
            skill_md('literature-processing', body='Upstream method, adapted \x1b[1mhere.\n'))
        code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/literature-processing', '--ref',
                                       commit, '--audience', 'source=public', '--keep-existing')
        self.assertEqual((code, err), (1, ''))
        self.assertIn('\n'.join([
            '  difference from the package to the kept copy (status will report the copy adapted):',
            '    changed SKILL.md',
            '--- package/SKILL.md',
            '+++ copy/SKILL.md',
            '@@ -2,4 +2,4 @@',
            ' name: literature-processing',
            ' description: A skill for tests.',
            ' ---',
            '-Upstream method.',
            '+Upstream method, adapted \\x1b[1mhere.',
            '  audience: ']), out)

    def test_ah242_the_originals_folder_says_they_are_not_skills(self):
        """AH-242: an operation writing an original writes the folder's README.md when it is not there, only then."""
        note_bytes = (b'The folders here are the untouched originals of skills adopted with scripts/trace_skills.py, '
                      b'kept for comparison.\n'
                      b"They are not skills to follow; the project's skills are under .skills/.\n")
        note_line = '    .trace-os/skill-bases/README.md (says the originals there are not skills to follow)\n'
        project = self.installed('claude')
        bare, commit, _ = self.library()
        with mock.patch.object(ts.bootstrap, 'transaction', wraps=ts.bootstrap.transaction) as transaction:
            out = self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        note = project / ts.BASES / 'README.md'
        self.assertEqual(note.read_bytes(), note_bytes)
        snapshot = self.lock(project)['skills']['example-skill']['upstream']['snapshot']
        self.assertIn('    3 file(s) under .skills/example-skill/\n' + note_line + f'    3 file(s) under {snapshot}/\n',
                      out)
        # Outside every snapshot, after the copy and before the original it sits beside (12.3).
        order = list(transaction.call_args.args[1])
        self.assertEqual(order[3:5], [ts.BASES_NOTE, f'{snapshot}/LICENSE.txt'])
        receipt = self.receipt(project, f'{STAMP}-example-skill-adopt')
        self.assertEqual(receipt['writes'][ts.BASES_NOTE],
                         {'before': None, 'after': hashlib.sha256(note_bytes).hexdigest()})
        self.assertNotIn(ts.BASES_NOTE, receipt['not_tracked'])
        self.assertEqual(git(project, 'status', '--porcelain', '--untracked-files=all', '--', ts.BASES_NOTE),
                         f'?? {ts.BASES_NOTE}')
        self.assert_parity(project)
        # Already there: a later adoption leaves it alone and does not plan it.
        out = self.adopt(project, bare, 'skills/literature-processing', '--audience', 'source=public')
        self.assertNotIn(note_line, out)
        self.assertNotIn(ts.BASES_NOTE, self.receipt(project, f'{STAMP}-literature-processing-adopt')['writes'])
        # Gone from a folder that holds originals: the next operation that writes one puts it back.
        note.unlink()
        out = self.adopt(project, bare, 'skills/own', '--audience', 'source=public')
        self.assertIn(note_line, out)
        self.assertEqual(note.read_bytes(), note_bytes)

    def test_ah244_only_a_path_git_could_convert_is_hashed_both_ways(self):
        """AH-244: one git check-attr per check; only a path with a converting attribute or CRLF is hashed both ways."""
        project = self.installed('claude')
        bare, commit, _ = self.library()
        (project / '.gitattributes').write_text('* text=auto eol=lf\n*.py ident\n')
        git(project, 'add', '.gitattributes')
        git(project, 'commit', '-qm', 'Line endings, and ident for scripts')
        hashed, commands, real, run = [], [], ts.conversion_changes, subprocess.run

        def counting(target, path, data):
            hashed.append(path)
            return real(target, path, data)

        def watched(args, *rest, **options):
            if args[:2] in (['git', 'check-attr'], ['git', 'hash-object']):
                commands.append(args[1])
            return run(args, *rest, **options)
        with mock.patch.object(ts, 'conversion_changes', counting), mock.patch.object(subprocess, 'run', watched):
            self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        snapshot = self.lock(project)['skills']['example-skill']['upstream']['snapshot']
        scripts = ['.claude/skills/example-skill/scripts/run.py', '.skills/example-skill/scripts/run.py',
                   f'{snapshot}/scripts/run.py']
        # At the plan and again under the lock: the scripts, whose ident attribute Git could act on, and nothing else.
        self.assertEqual(sorted(hashed), sorted(scripts * 2))
        self.assertEqual(commands.count('check-attr'), 2)
        self.assertEqual(commands.count('hash-object'), 2 * len(hashed))
        # The same answers as hashing every file both ways, for the cases the shortcut skips and the ones it keeps.
        self.assertEqual(ts.converting_attributes(project, ['a/x.py', 'a/x.md']), {'a/x.py'})
        items = [('a/plain.md', b'one\ntwo\n'), ('a/lone.md', b'one\rtwo\r'), ('a/crlf.md', b'one\r\ntwo\r\n')]
        self.assertEqual(ts.converted(project, items), 'a/crlf.md')
        self.assertEqual([path for path, data in items if ts.conversion_changes(project, path, data)], ['a/crlf.md'])
        self.assertIsNone(ts.converted(project, items[:2] + [('a/x.py', b"print('ok')\n")]))
        plain = self.uninstalled()
        self.assertEqual(ts.converting_attributes(plain, ['a/x.py']), set())
        self.assertIsNone(ts.converted(plain, items))



# The candidate flow (6.0, 5.2) and the stale plan ------------------------------

class CandidateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.project = self.installed('claude')
        self.bare, self.commit, self.work = self.library()
        self.base = ['--project', self.project, 'adopt', self.bare, 'skills/example-skill',
                     '--audience', 'source=public']

    def show(self):
        code, out, _ = self.run_main('--project', self.project, 'show', self.bare, 'skills/example-skill')
        self.assertEqual(code, 0)
        return out.splitlines()[-4][len('Candidate: '):]

    def test_C1_then_the_candidate_with_yes(self):
        code, out, err = self.run_main(*self.base, '--words', 'go ahead')
        self.assertEqual((code, err), (1, ''))
        staging = next((self.root / 'tmp').iterdir())
        file = (staging / 'candidate.json').as_posix()
        self.assertTrue(out.endswith('Nothing was applied: no terminal to confirm on. Show the plan above to the '
                                     'person; when they agree, run: ' + self.tool(self.project) + ' adopt '
                                     f'{self.bare.as_posix()} skills/example-skill --audience source=public --words '
                                     f"'go ahead' --candidate {file} --yes\n"))
        candidate = json.loads(Path(file).read_text())
        self.assertEqual((candidate['created_by'], candidate['for']['operation']), ('adopt', 'adopt'))
        self.assertFalse((self.project / '.skills/example-skill').exists())
        code, out, err = self.run_main(*self.base, '--candidate', file, '--yes')
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn(f'100755 12 {hashlib.sha256(VECTOR["scripts/run.py"][0]).hexdigest()} scripts/run.py\n', out)
        self.assertNotIn('--- SKILL.md', out)
        self.assertFalse(staging.exists(), 'adopt removes the folder it applied')
        row = self.intake_rows(self.project)[0]
        self.assertTrue(row['checked'].startswith('the whole package (3 files, 106 bytes) was printed by adopt before '
                                                  'writing and the person confirmed it (--yes); '))

    def test_C2_and_a_yes_at_the_terminal(self):
        code, out, err = self.run_main(*self.base, '--ref', self.commit, stdin='no\n', terminal=True)
        self.assertEqual((code, err), (1, ''))
        self.assertTrue(out.endswith('Apply? Nothing is committed or pushed. [y/N] Nothing was applied: not '
                                     'confirmed.\n'))
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])
        code, out, err = self.run_main(*self.base, '--ref', self.commit, stdin='YES\n', terminal=True)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(self.receipt(self.project, f'{STAMP}-example-skill-adopt')['decided']['confirmed'],
                         'at the terminal')

    def test_C3_C4_C5_C6(self):
        self.assertEqual(self.run_main(*self.base, '--yes'), (1, '', (
            'trace-skills: --yes applies what the person saw: pass the --candidate file that show, adopt or update '
            'wrote, or --ref with a full commit id\n')))
        self.assertEqual(self.run_main(*self.base, '--ref', 'main', '--yes')[0], 1)
        file = self.show()
        code, _, err = self.run_main('--project', self.project, 'adopt', self.bare, 'skills/example', '--candidate',
                                     file, '--yes')
        self.assertEqual((code, err), (1, f'trace-skills: the candidate is for {self.bare.as_posix()} '
                                          f'skills/example-skill at {self.commit[:12]}, which does not match this '
                                          f'command ({self.bare.as_posix()} skills/example, ref HEAD)\n'))
        code, _, err = self.run_main(*self.base, '--candidate', file, '--ref', 'v9', '--yes')
        self.assertEqual(code, 1)
        self.assertIn('which does not match this command', err)
        staged = Path(json.loads(Path(file).read_text())['staged'])
        (staged / 'SKILL.md').write_bytes(b'---\nname: example-skill\ndescription: Swapped.\n---\n')
        self.assertEqual(self.run_main(*self.base, '--candidate', file, '--yes'), (1, '', (
            "trace-skills: the package's bytes changed since they were shown (SKILL.md); run show again and review "
            'it\n')))
        self.assertTrue(staged.exists(), "a stale candidate's folder is the person's to inspect")
        not_candidate = self.root / 'other.json'
        not_candidate.write_text('{"kind": "something"}')
        self.assertEqual(self.run_main(*self.base, '--candidate', not_candidate, '--yes'), (2, '', (
            f'trace-skills: {not_candidate} is not a trace-skills candidate file\n')))
        self.assertFalse((self.project / '.skills/example-skill').exists())

    def test_a_candidate_whose_folder_is_gone_is_fetched_again(self):
        file = self.show()
        staged = Path(json.loads(Path(file).read_text())['staged'])
        kept = Path(file).read_bytes()
        shutil.rmtree(staged.parent)
        Path(file).parent.mkdir(parents=True)
        Path(file).write_bytes(kept)
        code, out, err = self.run_main(*self.base, '--candidate', file, '--yes')
        self.assertEqual((code, err), (0, ''), out)
        self.assertEqual(self.lock(self.project)['skills']['example-skill']['upstream']['tree_sha256'], VECTOR_DIGEST)
        self.assertEqual([path.name for path in (self.root / 'tmp').iterdir()], [Path(file).parent.name])
        self.assertTrue(self.intake_rows(self.project)[0]['checked'].startswith(
            'the whole package (3 files, 106 bytes) was printed by show before writing and the person confirmed it '
            '(--yes); '))

    def test_E14_a_stale_plan(self):
        real = ts.bootstrap.locked

        @contextlib.contextmanager
        def edited_while_waiting(target):
            # The person edits the inventory file between the plan and the lock.
            with (target / ts.INVENTORY).open('a') as stream:
                stream.write('a late note\n')
            with real(target):
                yield

        before = tree_state(self.project)
        with mock.patch.object(ts.bootstrap, 'locked', edited_while_waiting):
            code, out, err = self.run_main(*self.base, '--ref', self.commit, '--yes')
        self.assertEqual((code, err), (1, 'trace-skills: ai/SKILLS-IN-USE.md changed after the plan was shown; nothing '
                                          'was written. Run the command again for a new plan\n'))
        after = tree_state(self.project)
        self.assertEqual(after.pop(ts.INVENTORY)[0], before.pop(ts.INVENTORY)[0] + b'a late note\n')
        self.assertEqual(after, before)
        self.assertFalse((self.project / ts.OPERATION).exists())

    def test_E15_and_E13_undo_everything(self):
        before = tree_state(self.project)
        real = ts.bootstrap.write_atomic

        def edit_then_write(path, data):
            if Path(path).name == 'skills.lock.json' and not Path(path).exists():
                (self.project / ts.INVENTORY).write_text('edited mid-write\n')
            return real(path, data)

        with mock.patch.object(ts.bootstrap, 'write_atomic', edit_then_write):
            code, _, err = self.run_main(*self.base, '--ref', self.commit, '--yes')
        self.assertEqual((code, err), (1, 'trace-skills: ai/SKILLS-IN-USE.md changed while this operation was writing; '
                                          'everything else it wrote was undone, and ai/SKILLS-IN-USE.md keeps the '
                                          'later edit\n'))
        after = tree_state(self.project)
        self.assertEqual(after.pop(ts.INVENTORY)[0], b'edited mid-write\n')
        before.pop(ts.INVENTORY)
        self.assertEqual(after, before)
        self.assertFalse((self.project / '.skills/example-skill').exists())
        self.assertFalse((self.project / ts.OPERATION).exists())
        git(self.project, 'checkout', '--', ts.INVENTORY)

        def disk_full(path, data):
            if Path(path).name == 'skills.lock.json':
                raise OSError('No space left on device')
            return real(path, data)

        before = tree_state(self.project)
        with mock.patch.object(ts.bootstrap, 'write_atomic', disk_full):
            code, _, err = self.run_main(*self.base, '--ref', self.commit, '--yes')
        self.assertEqual((code, err), (2, 'trace-skills: writing failed (No space left on device); everything this '
                                          'operation wrote was undone\n'))
        self.assertEqual(tree_state(self.project), before)
        self.assertFalse((self.project / '.skills/example-skill').exists())
        self.assertEqual([path for path in (self.project / ts.BASES).rglob('*') if path.is_file()], [])
        self.assertFalse((self.project / ts.OPERATION).exists())
        self.assertFalse((self.project / bs.JOURNAL).exists())


# link (6.10) and later-run discovery -------------------------------------------

class LinkTests(Fixture):
    def test_link(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public', '--by', 'Ann Author',
                   '--authority', 'T3 of ai/records/x')
        receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json'
        before = git(project, 'rev-parse', 'HEAD')
        git(project, 'add', receipt)
        git(project, 'commit', '-qm', 'Only the receipt')
        receipt_only = git(project, 'rev-parse', 'HEAD')
        git(project, 'add', '-A')
        git(project, 'commit', '-qm', 'Adopt example-skill')
        adopted = git(project, 'rev-parse', 'HEAD')
        base = ['--project', project, 'link']
        self.assertEqual(self.run_main(*base, 'README.md', '--commit', adopted),
                         (2, '', 'trace-skills: README.md is not a receipt this tool wrote\n'))
        fake = project / ts.RECEIPTS / 'fake.json'
        fake.write_text('{"kind": "other", "schema_version": 1}')
        self.assertEqual(self.run_main(*base, f'{ts.RECEIPTS}/fake.json', '--commit', adopted)[2],
                         f'trace-skills: {ts.RECEIPTS}/fake.json is not a receipt this tool wrote\n')
        detach = dict(json.loads((project / receipt).read_text()), operation='detach')
        fake.write_bytes(bs.encoded(detach))
        self.assertEqual(self.run_main(*base, f'{ts.RECEIPTS}/fake.json', '--commit', adopted), (1, '', (
            f'trace-skills: {ts.RECEIPTS}/fake.json records a detach, which adopts no instruction; link records '
            'adoptions, updates and accepted merges\n')))
        fake.unlink()
        self.assertEqual(self.run_main(*base, receipt, '--commit', 'deadbeef'),
                         (2, '', 'trace-skills: not a commit in this repository: deadbeef\n'))
        self.assertEqual(self.run_main(*base, receipt, '--commit', before), (1, '', (
            f'trace-skills: commit {before[:12]} does not contain {receipt} as it is here; name the commit that '
            'recorded this operation\n')))
        self.assertEqual(self.run_main(*base, receipt, '--commit', receipt_only), (1, '', (
            f'trace-skills: commit {receipt_only[:12]} holds no .skills/example-skill/SKILL.md\n')))
        code, out, err = self.run_main(*base, receipt, '--commit', adopted)
        self.assertEqual((code, out, err), (0, f'Linked {receipt} to {adopted[:12]}: TR-001 in sources/transitions.jsonl.\n',
                                            ''))
        row = json.loads((project / ts.TRANSITIONS).read_text())
        digest = bs.digest((project / '.skills/example-skill/SKILL.md').read_bytes())
        for key, value in {'id': 'TR-001', 'kind': 'default-adoption', 'object': '.skills/example-skill/SKILL.md',
                           'object_sha256': digest, 'checks': receipt, 'by': 'Ann Author',
                           'authority': 'T3 of ai/records/x', 'decision': 'adopt',
                           'destination': '.skills/example-skill/ (a standing instruction for the agents in this '
                                          'project)', 'commits': [adopted], 'later_run': 'nothing stated'}.items():
            self.assertEqual(row[key], value, key)
        self.assert_valid_registries(project)
        self.assertEqual(self.run_main(*base, receipt, '--commit', adopted),
                         (0, 'Already linked: TR-001 (sources/transitions.jsonl); nothing written.\n', ''))
        (project / 'notes.txt').write_text('later\n')
        git(project, 'add', '-A')
        git(project, 'commit', '-qm', 'Later work')
        later = git(project, 'rev-parse', 'HEAD')
        self.assertEqual(self.run_main(*base, receipt, '--commit', later), (1, '', (
            f'trace-skills: {receipt} is already linked to {adopted[:12]} by TR-001; a transition row is not '
            'amended\n')))
        self.assertEqual(len((project / ts.TRANSITIONS).read_text().splitlines()), 1)
        # Later-run discovery finds a record whose evidence lists the adopted SKILL.md with that digest.
        record = project / 'ai/records/20261002-later-run'
        record.mkdir(parents=True)
        (record / 'evidence.json').write_text(json.dumps({'instructions': {
            'base_commit': later, 'files': {'.skills/example-skill/SKILL.md': digest}}}))
        cwd = os.getcwd()
        os.chdir(project)
        try:
            found = trace_record.later_run_candidates(row)
        finally:
            os.chdir(cwd)
        self.assertIn('  produced under this version of it: 20261002-later-run', found)

    def test_a_project_installed_before_rc20_keeps_working_and_links_across_the_moves(self):
        for route in (('.trace-kit', '.trace-os'), ('.traceware', '.trace-os'), ('.trace-kit', '.traceware', '.trace-os')):
            with self.subTest(route=route):
                self.links_across(route)

    def links_across(self, route):
        earlier = route[0]
        legacy_ignore = ''.join(name.replace('.trace-os/', earlier + '/') + '\n'
                                for name in IGNORED.splitlines() if name.startswith('.trace-os/'))

        def as_before_the_move(project):
            os.rename(project / '.trace-os', project / earlier)
            with (project / '.gitignore').open('a') as stream:
                stream.write(legacy_ignore)

        project = self.installed('claude', name='legacy-project-' + '-'.join(name[1:] for name in route),
                                 before_commit=as_before_the_move)
        bare, commit, _ = self.library()
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        # Every write went to the folder the project has; none created the new one.
        self.assertFalse((project / '.trace-os').exists())
        receipt = f'{earlier}/skill-adoptions/{STAMP}-example-skill-adopt.json'
        for name in (f'{earlier}/skills.lock.json', receipt):
            self.assertTrue((project / name).is_file(), name)
        state = json.loads((project / earlier / 'install.json').read_text())
        self.assertIn('.claude/skills/example-skill/SKILL.md', state['files'])
        git(project, 'add', '-A')
        git(project, 'commit', '-qm', 'Adopt example-skill')
        adopted = git(project, 'rev-parse', 'HEAD')
        # Each update moves the folder; the lock and the receipt keep the paths they recorded.
        for old, new in zip(route, route[1:]):
            os.rename(project / old, project / new)
            git(project, 'add', '-A')
            git(project, 'commit', '-qm', 'The state folder moves')
        code, out, err = self.run_main('--project', project, 'status', '--offline')
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn(f'receipt: {receipt}\n', out)
        moved = '.trace-os' + receipt[len(earlier):]
        for given in (receipt, moved):
            with self.subTest(given=given):
                code, out, err = self.run_main('--project', project, 'link', given, '--commit', adopted)
                self.assertEqual(code, 0, err)
        rows = [json.loads(line) for line in (project / ts.TRANSITIONS).read_text().splitlines()]
        # One row, naming the receipt as the adopting commit holds it.
        self.assertEqual([row['checks'] for row in rows], [receipt])

    def test_link_notes_a_committed_skill_that_differs(self):
        project = self.uninstalled()
        bare, commit, _ = self.library()
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        (project / '.skills/example-skill/SKILL.md').write_bytes(skill_md('example-skill', body='Adapted.\n'))
        git(project, 'add', '-A')
        git(project, 'commit', '-qm', 'Adopt and adapt')
        receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json'
        code, out, _ = self.run_main('--project', project, 'link', receipt, '--commit', 'HEAD')
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith('The committed .skills/example-skill/SKILL.md differs from the one this receipt '
                                     'wrote; the row records the committed file.\n'))

    def test_link_E11(self):
        project = self.uninstalled()
        (project / ts.TRACE_RECORD).unlink()
        self.assertEqual(self.run_main('--project', project, 'link', 'x.json', '--commit', 'HEAD'), (
            2, '', 'trace-skills: scripts/trace_record.py is not in this project, so the transition row cannot be '
                   'recorded; nothing was written\n'))


# recover (6.11, 12.5) ------------------------------------------------------------

class RecoverTests(Fixture):
    def interrupted(self, project, bare, commit, at, *extra, path='skills/example-skill', signal_class=KeyboardInterrupt):
        """Adopt with write_atomic raising an interruption the first time `at(path)` is true."""
        real, fired = ts.bootstrap.write_atomic, []

        def wrapper(target, data):
            relative = Path(target).resolve().relative_to(project).as_posix() \
                if Path(target).resolve().is_relative_to(project) else ''
            if not fired and at(relative):
                fired.append(relative)
                raise signal_class()
            return real(target, data)

        with mock.patch.object(ts.bootstrap, 'write_atomic', wrapper):
            result = self.run_main('--project', project, 'adopt', bare, path, '--ref', commit, '--yes',
                                   '--audience', 'source=public', *extra)
        self.assertTrue(fired, 'the interruption point was reached')
        return result

    def test_four_boundaries(self):
        name = 'example-skill'
        boundaries = {'before the copy': (lambda path: path.startswith(f'.skills/{name}/'), 'Undone'),
                      'after the copy': (lambda path: path.startswith('.trace-os/skill-bases/'), 'Undone'),
                      'before the lock': (lambda path: path == ts.LOCK, 'Undone'),
                      'before the journal of the transaction': (lambda path: path == bs.JOURNAL, 'Cleared'),
                      'before the receipt': (lambda path: path.startswith(ts.RECEIPTS + '/'), 'Completed')}
        for number, (label, (at, outcome)) in enumerate(boundaries.items()):
            with self.subTest(label=label):
                project = self.installed('claude', name=f'recover-{number}')
                bare, commit, _ = self.library()
                before = tree_state(project)
                operation = f'{STAMP}-{name}-adopt'
                code, out, err = self.interrupted(project, bare, commit, at)
                self.assertEqual(code, 130)
                self.assertEqual(err, f'trace-skills: interrupted; run {self.tool(project)} recover to finish or undo '
                                      f'{operation}\n')
                self.assertTrue((project / ts.OPERATION).exists())
                code, status, _ = self.run_main('--project', project, 'status', '--offline')
                self.assertIn(f'Pending: operation {operation} did not finish; run {self.tool(project)} recover\n',
                              status)
                code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example', '--ref', commit,
                                               '--yes')
                self.assertEqual((code, err), (1, f'trace-skills: an earlier operation ({operation}) did not finish; '
                                                  f'run {self.tool(project)} recover first\n'))
                code, out, err = self.run_main('--project', project, 'recover')
                self.assertEqual((code, err), (0, ''))
                if outcome == 'Undone':
                    self.assertEqual(out, f'Undone: {operation}; the project is as it was before. Run the command '
                                          'again to adopt.\n')
                    self.assertEqual(tree_state(project), before)
                    self.assertFalse((project / '.skills' / name).exists())
                    self.assertFalse((project / '.claude/skills' / name).exists())
                    self.assertEqual(list((self.root / 'tmp').iterdir()), [])
                elif outcome == 'Cleared':
                    self.assertEqual(out, f'Cleared: nothing of {operation} had been written.\n')
                    self.assertEqual(tree_state(project), before)
                else:
                    self.assertEqual(out, f'Completed: {operation}; receipt {ts.RECEIPTS}/{operation}.json.\n')
                    receipt = self.receipt(project, operation)
                    self.assertTrue(receipt['recovered'])
                    self.assertEqual(receipt['records']['intake']['id'], 'IN-001')
                    self.assertEqual(len(self.intake_rows(project)), 1)
                    self.assertEqual(mode_of(project / '.skills' / name / 'scripts/run.py'), '100755')
                    self.assertEqual(list((self.root / 'tmp').iterdir()), [])
                    self.assert_parity(project)
                self.assertFalse((project / ts.OPERATION).exists())
                self.assertFalse((project / bs.JOURNAL).exists())
                checked, problems = check_skills.check(project)
                self.assertEqual(problems, [])
                self.assertEqual(self.run_main('--project', project, 'recover'), (0, 'Nothing to recover.\n', ''))
                # After an undo the same command plans again; after completion it is already adopted.
                code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref',
                                               commit, '--yes', '--audience', 'source=public')
                self.assertEqual((code, err), (0, ''))
                for item in (self.root / 'tmp').iterdir():
                    shutil.rmtree(item)

    def test_modes_restored_on_undo(self):
        def executable_kit_script(project):
            path = project / '.skills/example/run.sh'
            os.chmod(path, 0o755)
        project = self.installed('claude', skills={'.skills/example/run.sh': ('#!/bin/sh\n', 'managed')},
                                 before_commit=executable_kit_script)
        bare, commit, _ = self.library(extra={'skills/example/run.sh': b'#!/bin/sh\necho library\n'})
        before = tree_state(project)
        self.assertTrue(before['.skills/example/run.sh'][1])
        code, _, _ = self.interrupted(project, bare, commit, lambda path: path == ts.LOCK, '--replace-kit-copy',
                                      path='skills/example')
        self.assertEqual(code, 130)
        journal = json.loads((project / ts.OPERATION).read_text())
        self.assertEqual(journal['modes_before'].get('.skills/example/run.sh'), '100755')
        self.assertEqual(self.run_main('--project', project, 'recover')[0], 0)
        self.assertEqual(tree_state(project), before)
        self.assertEqual(mode_of(project / '.skills/example/run.sh'), '100755')

    def test_a_mode_only_change_is_journalled_both_ways(self):
        project = self.uninstalled()
        bare, commit, _ = self.library()
        (project / '.skills/literature-processing/run.py').write_bytes(b'print(1)\n')
        os.chmod(project / '.skills/literature-processing/run.py', 0o755)
        with mock.patch.object(ts.bootstrap, 'transaction', side_effect=KeyboardInterrupt):
            code, _, _ = self.run_main('--project', project, 'adopt', bare, 'skills/literature-processing', '--ref',
                                       commit, '--yes', '--audience', 'source=public', '--keep-existing')
        self.assertEqual(code, 130)
        journal = json.loads((project / ts.OPERATION).read_text())
        self.assertEqual(journal['state'], 'applying')
        # A kept copy keeps its modes: nothing to change there, and nothing to restore.
        self.assertEqual(journal['modes_after'], {})
        self.assertNotIn('.skills/literature-processing/run.py', journal['modes_before'])
        self.assertEqual(self.run_main('--project', project, 'recover')[1],
                         f'Cleared: nothing of {STAMP}-literature-processing-adopt had been written.\n')
        writes = ts.Writes(project)
        writes.put('copy', '.skills/own/SKILL.md', (project / '.skills/own/SKILL.md').read_bytes(), '100755')
        writes.put('copy', '.skills/literature-processing/run.py', b'print(1)\n', '100644')
        writes.put('copy', '.skills/new/x.py', b'x', '100755')
        writes.put('copy', '.skills/new/y.md', b'y', '100644')
        after, before, left = writes.modes()
        self.assertEqual(after, {'.skills/own/SKILL.md': '100755', '.skills/literature-processing/run.py': '100644',
                                 '.skills/new/x.py': '100755'})
        self.assertEqual(before, {'.skills/own/SKILL.md': '100644', '.skills/literature-processing/run.py': '100755'})
        self.assertEqual(writes.updates, {'.skills/new/x.py': b'x', '.skills/new/y.md': b'y'})
        ts.set_modes(project, after)
        self.assertEqual(mode_of(project / '.skills/own/SKILL.md'), '100755')
        self.assertEqual(mode_of(project / '.skills/literature-processing/run.py'), '100644')

    def test_an_intake_that_fails_is_completed_by_recover(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        operation = f'{STAMP}-example-skill-adopt'
        with mock.patch.object(ts, 'run_trace_record', return_value=(1, '', 'registry locked')):
            code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref',
                                           commit, '--yes', '--audience', 'source=public')
        self.assertEqual((code, err), (2, 'trace-skills: the files were written, but the intake row failed '
                                          '(trace_record.py exited 1: registry locked); run '
                                          f'{self.tool(project)} recover to finish {operation}\n'))
        self.assertFalse((project / ts.RECEIPTS / f'{operation}.json').exists())
        self.assertTrue((project / '.skills/example-skill/SKILL.md').exists())
        code, out, err = self.run_main('--project', project, 'recover')
        self.assertEqual((code, out), (0, f'Completed: {operation}; receipt {ts.RECEIPTS}/{operation}.json.\n'))
        self.assertEqual(len(self.intake_rows(project)), 1)
        self.assert_valid_registries(project)

    def test_R1_R2_R3(self):
        project = self.installed('claude')
        (project / bs.JOURNAL).write_text(json.dumps({'schema_version': 1, 'entries': {'AGENTS.md': {}}}))
        self.assertEqual(self.run_main('--project', project, 'recover'), (1, '', (
            "trace-skills: the pending setup transaction is not this tool's (it names AGENTS.md); run python3 "
            'scripts/bootstrap.py --recover\n')))
        (project / bs.JOURNAL).unlink()
        (project / ts.OPERATION).write_text('{broken')
        code, _, err = self.run_main('--project', project, 'recover')
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith('trace-skills: the operation journal .trace-os/receipts/skill-operation.json '
                                       'cannot be read ('))
        bare, commit, _ = self.library()
        (project / ts.OPERATION).unlink()
        self.assertEqual(self.interrupted(project, bare, commit, lambda path: path == bs.JOURNAL)[0], 130)
        (project / '.skills/example-skill').mkdir(parents=True)
        (project / '.skills/example-skill/SKILL.md').write_text('somebody else\n')
        self.assertEqual(self.run_main('--project', project, 'recover'), (1, '', (
            'trace-skills: recovery would overwrite a later edit: .skills/example-skill/SKILL.md\n')))

    def test_ah247_status_reports_a_pending_name_as_pending(self):
        """AH-247: while the journal exists, the name it covers reads as pending; its half-written folder is not read."""
        project = self.installed('claude')
        bare, commit, _ = self.library()
        operation = f'{STAMP}-example-skill-adopt'
        tool = self.tool(project)
        # Interrupted after the copy: .skills/example-skill/ is written, the original is not.
        self.assertEqual(self.interrupted(project, bare, commit, lambda path: path.startswith(ts.BASES + '/'))[0], 130)
        self.assertTrue((project / '.skills/example-skill/SKILL.md').is_file())
        out = self.run_main('--project', project, 'status', '--offline')[1]
        self.assertIn(f'Pending: operation {operation} did not finish; run {tool} recover\n\nexample\n', out)
        self.assertIn(f'\nexample-skill\n  owner: pending (operation {operation} did not finish; run {tool} '
                      'recover)\n\n', out)
        # Nothing of the half-written folder is read: no owner from it, no definition line read from its SKILL.md.
        self.assertNotIn('.skills/example-skill/', out)
        self.assertEqual(out.count('  definition 1:'), 1, out)
        self.assertEqual(self.run_main('--project', project, 'recover')[0], 0)
        self.assertNotIn('example-skill', self.run_main('--project', project, 'status', '--offline')[1])
        # A pending declaration names no folder at all; the name is still shown, as pending.
        with mock.patch.object(ts.bootstrap, 'transaction', side_effect=KeyboardInterrupt):
            code = self.run_main('--project', project, 'declare', 'held-skill', '--held-by', 'Bea Holder', '--yes')[0]
        self.assertEqual(code, 130)
        out = self.run_main('--project', project, 'status', '--offline')[1]
        self.assertIn(f'\nheld-skill\n  owner: pending (operation {STAMP}-held-skill-declare did not finish; run '
                      f'{tool} recover)\n\n', out)
        # A journal that cannot be read names no operation, so no name is pending.
        (project / ts.OPERATION).write_text('{broken')
        out = self.run_main('--project', project, 'status', '--offline')[1]
        self.assertIn(f'Pending: operation {ts.OPERATION} did not finish; run {tool} recover\n', out)
        self.assertNotIn('owner: pending', out)


# Rule 1 and rule 4: nothing runs, no credential is written ------------------------

class SafetyTests(Fixture):
    def test_no_package_file_is_ever_run(self):
        marker = self.root / 'marker'
        script = f'open({str(marker)!r}, "w").write("ran")\n'.encode()
        bare, commit, work = self.make_source({
            'skills/marker-skill/SKILL.md': skill_md('marker-skill'),
            'skills/marker-skill/scripts/run.py': (script, '100755'),
            'skills/marker-skill/tests/test_marker.py': script,
            'skills/marker-skill/setup.py': script,
            'skills/marker-skill/hooks/post-checkout': (b'#!/bin/sh\ntouch ' + str(marker).encode() + b'\n',
                                                        '100755')})
        project = self.installed('claude')
        for argv in (['list', bare], ['show', bare, 'skills/marker-skill'],
                     ['adopt', bare, 'skills/marker-skill', '--ref', commit, '--yes', '--audience', 'source=public'],
                     ['status']):
            self.assertEqual(self.run_main('--project', project, *argv)[0], 0, argv)
        self.assertFalse(marker.exists())
        self.assertEqual(mode_of(project / '.skills/marker-skill/scripts/run.py'), '100755')

    def test_no_credential_is_written(self):
        project = self.installed('claude')
        bare, commit, _ = self.library()
        planted = {'GH_TOKEN': FAKE_TOKEN, 'GITHUB_TOKEN': FAKE_TOKEN, 'GIT_ASKPASS_TOKEN': FAKE_TOKEN}
        with mock.patch.dict(os.environ, planted):
            code, _, err = self.run_main('--project', project, 'show',
                                         f'https://x-access-token:{FAKE_TOKEN}@github.com/o/r.git', 'skills/example')
            self.assertEqual(code, 2)
            self.assertIn('carries a user name and password, a token', err)
            self.assertEqual(self.run_main('--project', project, 'show', bare, 'skills/example-skill')[0], 0)
            with mock.patch.object(ts.bootstrap, 'transaction', side_effect=KeyboardInterrupt):
                self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--ref', commit, '--yes',
                              '--audience', 'source=public')
            journal = (project / ts.OPERATION).read_text()
            self.run_main('--project', project, 'recover')
            self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        files = [journal, (project / ts.LOCK).read_text()]
        files += [path.read_text() for path in (project / ts.RECEIPTS).iterdir()]
        files += [path.read_text() for path in (self.root / 'tmp').rglob('candidate.json')]
        self.assertGreaterEqual(len(files), 4)
        for text in files:
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertNotIn('x-access-token', text)
            self.assertNotIn('@github.com', text)
            self.assertNotIn(str(self.home), text)


# status (section 11) ----------------------------------------------------------------

class StatusTests(Fixture):
    def test_every_owner_kind(self):
        project = self.installed('claude')
        bare, first, work = self.library(extra={'skills/adapted/SKILL.md': skill_md('adapted'),
                                                'skills/adapted/a.md': b'a\n', 'skills/adapted/b.md': b'b\n'})
        (project / '.skills/mine').mkdir()
        (project / '.skills/mine/SKILL.md').write_bytes(skill_md('mine'))
        (project / '.skills/legacy.md').write_text('# legacy\n')
        bs.sync_skills(project)
        self.adopt(project, bare, 'skills/example-skill', '--audience', 'source=public')
        self.adopt(project, bare, 'skills/adapted', '--audience', 'source=public')
        (project / '.skills/adapted/a.md').write_text('changed\n')
        (project / '.skills/adapted/c.md').write_text('added\n')
        (project / '.skills/adapted/SKILL.md').write_bytes(skill_md('adapted', extra='metadata:\n  changes: edited a\n'))
        lock = self.lock(project)
        lock['skills']['held'] = {'manager': 'outside', 'held_by': 'Bea Holder', 'path': '~/.claude/skills/held',
                                  'digest': {'tree_sha256': 'f' * 64, 'basis': 'attested'},
                                  'limitation': 'Not in the project.', 'declared_by': 'Bea Holder',
                                  'receipt': f'{ts.RECEIPTS}/held.json', 'receipts': [f'{ts.RECEIPTS}/held.json']}
        lock['skills']['gone'] = {'manager': 'retired', 'was': 'external',
                                  'source': {'repository': 'https://example.invalid/r.git', 'path': 'skills/gone',
                                             'requested_ref': 'main', 'commit': 'a' * 40},
                                  'held_by': None, 'upstream': {'tree_sha256': 'b' * 64,
                                                                'snapshot': f"{ts.BASES}/{'b' * 64}", 'dropped': False},
                                  'retired_at': '2026-10-01T12:00:00Z', 'receipt': f'{ts.RECEIPTS}/gone.json',
                                  'receipts': [f'{ts.RECEIPTS}/gone.json']}
        lock['skills']['let-go'] = {'manager': 'retired', 'was': 'outside', 'source': None, 'held_by': 'Cy Holder',
                                    'upstream': None, 'retired_at': '2026-10-01T12:00:00Z',
                                    'receipt': f'{ts.RECEIPTS}/let-go.json', 'receipts': [f'{ts.RECEIPTS}/let-go.json']}
        (project / ts.LOCK).write_bytes(bs.encoded(lock))
        (project / '.skills/example-skill.md').write_text('# a second claim\n')
        (self.home / '.codex/skills/mine').mkdir(parents=True)
        before = tree_state(project)
        code, out, err = self.run_main('--project', project, 'status', '--offline')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(tree_state(project), before, 'status writes nothing')
        receipt = f'{ts.RECEIPTS}/{STAMP}-adapted-adopt.json'
        handoff = 'handoff not stated: contract named, record mechanism named, procedure set to its own id'
        self.assertEqual(out, '\n'.join([
            f'Skills in {project.name} (trace_skills.py status, contract 1)',
            'Install state: test-1, agent claude',
            'Lock: .trace-os/skills.lock.json, 5 entries',
            '',
            'adapted',
            f'  owner: external, from {bare.as_posix()} skills/adapted at {first[:12]} (requested {first})',
            '  copy: adapted (3 file(s) differ from the upstream original: SKILL.md, a.md, c.md)',
            '  detached: no',
            '  upstream: unknown (not fetched (--offline))',
            '  change notice: present',
            f'  definition 1: format valid; licence not stated; {handoff}',
            '  shadowed: no',
            f'  receipt: {receipt}',
            '',
            'example',
            '  owner: kit',
            f'  definition 1: format valid; licence not stated; {handoff}',
            '  shadowed: no',
            '',
            'example-skill',
            '  owner: conflict (an adopted entry in the lock, a single file .skills/example-skill.md)',
            '',
            'held',
            '  owner: outside, held by Bea Holder; digest ffffffffffff (attested)',
            '  limitation: Not in the project.',
            f'  receipt: {ts.RECEIPTS}/held.json (missing)',
            '',
            'legacy',
            '  owner: project (single file .skills/legacy.md)',
            '  definition 1: not applicable (a single-file skill, not a package)',
            '  shadowed: no',
            '',
            'mine',
            '  owner: project (.skills/mine/)',
            f'  definition 1: format valid; licence not stated; {handoff}',
            f'  shadowed: by a personal skill at ~/.codex/skills/mine; {SHADOW_CODEX}',
            '',
            'Retired:',
            f"  gone: was adopted from https://example.invalid/r.git skills/gone at aaaaaaaaaaaa; original kept at "
            f"{ts.BASES}/{'b' * 64}; receipt {ts.RECEIPTS}/gone.json",
            f'  let-go: was declared as held by Cy Holder; receipt {ts.RECEIPTS}/let-go.json',
            ts.LIMIT,
            ts.PARITY_LIMIT, '']))

    def test_upstream_states_and_the_copy(self):
        project = self.uninstalled()
        bare, first, work = self.library()
        code, out, err = self.run_main('--project', project, 'adopt', bare, 'skills/example-skill', '--yes',
                                       '--audience', 'source=public', '--ref', first)
        self.assertEqual(code, 0, err)
        self.assertIn('upstream: unknown (pinned to a commit; update --ref <ref> compares with a branch or tag)',
                      self.run_main('--project', project, 'status')[1])
        lock = self.lock(project)
        lock['skills']['example-skill']['source']['requested_ref'] = 'main'
        (project / ts.LOCK).write_bytes(bs.encoded(lock))

        def upstream():
            return [line for line in self.run_main('--project', project, 'status')[1].splitlines()
                    if line.startswith('  upstream:')][0]

        self.assertEqual(upstream(), '  upstream: current')
        moved = self.commit_source(work, bare, {'README.md': b'changed outside the package\n'})
        self.assertEqual(upstream(), f'  upstream: current (the source moved to {moved[:12]}; the package is unchanged)')
        behind = self.commit_source(work, bare, {'skills/example-skill/scripts/run.py': (b"print('new')\n", '100755')})
        self.assertEqual(upstream(), f'  upstream: behind {behind[:12]}')
        git(work, 'rm', '-rq', 'skills/example-skill')
        git(work, 'commit', '-qm', 'gone')
        git(work, 'push', '-q', str(bare), 'main')
        gone = git(work, 'rev-parse', 'HEAD')
        self.assertEqual(upstream(), f'  upstream: unknown (the package folder skills/example-skill is gone at '
                                     f'{gone[:12]})')
        lock['skills']['example-skill']['source']['requested_ref'] = 'nowhere'
        (project / ts.LOCK).write_bytes(bs.encoded(lock))
        self.assertEqual(upstream(), '  upstream: unknown (the source has no branch or tag named nowhere)')
        lock['skills']['example-skill']['source']['requested_ref'] = 'main'
        lock['skills']['example-skill']['source']['repository'] = (self.root / 'missing.git').as_posix()
        (project / ts.LOCK).write_bytes(bs.encoded(lock))
        self.assertTrue(upstream().startswith('  upstream: unknown (fetch failed: '))
        self.assertIn('  copy: unchanged\n', self.run_main('--project', project, 'status', '--offline')[1])
        self.assertIn('  change notice: not needed (copy unchanged)\n',
                      self.run_main('--project', project, 'status', '--offline')[1])
        (project / ts.RECEIPTS / f'{STAMP}-example-skill-adopt.json').unlink()
        (project / '.skills/example-skill/LICENSE.txt').write_text('changed\n')
        out = self.run_main('--project', project, 'status', '--offline')[1]
        self.assertIn('  copy: adapted (the files cannot be listed: the receipt is missing)\n', out)
        self.assertIn('  change notice: absent\n', out)
        self.assertIn(f'  receipt: {ts.RECEIPTS}/{STAMP}-example-skill-adopt.json (missing)\n', out)
        shutil.rmtree(project / '.skills/example-skill')
        self.assertIn('  copy: missing (.skills/example-skill/SKILL.md is not here)\n',
                      self.run_main('--project', project, 'status', '--offline')[1])

    def test_ah257_each_personal_folder_states_its_own_observation(self):
        """AH-237, AH-257: one shadowing line per personal folder holding the name, with that folder's observation."""
        project = self.uninstalled()
        for root in ('.codex/skills', '.claude/skills'):
            (self.home / root / 'own').mkdir(parents=True)
        (self.home / '.agents/skills/literature-processing').mkdir(parents=True)
        out = self.run_main('--project', project, 'status', '--offline')[1]
        # Every folder holding the name, in the order of 11.3; Claude Code's observation only for its own folder.
        self.assertIn('own\n  owner: project (.skills/own/)\n', out)
        self.assertIn(f'  shadowed: by a personal skill at ~/.claude/skills/own; {SHADOW_CLAUDE}\n'
                      f'  shadowed: by a personal skill at ~/.codex/skills/own; {SHADOW_CODEX}\n\n', out)
        self.assertIn('literature-processing\n  owner: project (.skills/literature-processing/)\n', out)
        self.assertIn(f'  shadowed: by a personal skill at ~/.agents/skills/literature-processing; '
                      f'{SHADOW_UNREHEARSED}\n\n', out)
        self.assertEqual(out.count('  shadowed: no\n'), 1, out)
        self.assertNotIn('not established', out)
        self.assertNotIn('precedence', out)

    def test_without_install_state_or_lock(self):
        project = self.uninstalled()
        code, out, err = self.run_main('--project', project, 'status')
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(out.startswith(f'Skills in {project.name} (trace_skills.py status, contract 1)\n'
                                       "Install state: none here (every skill is the project's own unless the lock "
                                       'says otherwise; no agent copies are managed)\nLock: none\n\n'))
        self.assertIn('\nRetired: none\n', out)
        self.assertFalse((project / '.trace-os').exists(), 'status writes nothing, not even .trace-os/')

    def test_status_from_the_projects_own_copy(self):
        project = self.uninstalled()
        run = subprocess.run([sys.executable, 'scripts/trace_skills.py', 'status', '--offline'], cwd=project,
                             capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stderr), (0, ''))
        self.assertIn('literature-processing\n  owner: project (.skills/literature-processing/)\n', run.stdout)


if __name__ == '__main__':
    unittest.main()
