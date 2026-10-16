"""The lifecycle after adoption: update, update --accept-merged, --detach, --attach, retire and declare.

Builder D's tests for scripts/trace_skills.py, against sections 6.5 to 6.9 of
scripts/trace-skills-contract.md with the answers of its section 14 and the
AH-230 and AH-231 amendments, plus the core fixes AH-233 and AH-234 and the
skills pilot's AH-238 (with AH-248), AH-240, AH-241 and AH-242. Every
source is a bare repository in a temporary folder, Git may use only the file
protocol, HOME is a temporary folder, the audience observation and the clock
are patched, and no credential is used. Nothing here imports another test
module; the fixtures are copied from test_trace_skills.py and
test_trace_skills_scenarios.py.

Each kind of fixture (a source holding a given series of commits, an
installed or uninstalled project, and an installed project with the
test-vector package adopted and committed) is made once per run of this
module, by section 13's steps, and every test that asks for it gets its own
copy: a copied Git repository is a whole repository, so the tests stay
independent of each other and of their order.
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
try:
    import check_skill_handoff  # noqa: E402
except ImportError:  # the state then reads "handoff not checked"
    check_skill_handoff = None

ENVIRONMENT = {'GIT_AUTHOR_NAME': 'Test Researcher', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
               'GIT_COMMITTER_NAME': 'Test Researcher', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
               'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'}
IGNORED = ''.join(name + '\n' for name in (
    '__pycache__/', '.trace-os/setup.lock', '.trace-os/transaction.json', '.trace-os/backups/',
    '.trace-os/pending-update.json', '.trace-os/pending-plan.json', '.trace-os/update-backups/',
    '.trace-os/receipts/', '*.local'))
PROJECT_MODULES = ('trace_record.py', 'trace_story.py', 'input_provenance.py', 'trace_defaults.py',
                   'record_privacy.py', 'check_skill_handoff.py', 'trace_skills.py', 'bootstrap.py',
                   'check_skills.py')
NOW = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
STAMP = '20261001T120000Z'
AT = '2026-10-01T12:00:00Z'
BY = 'Ann Author'
WORDS = 'yes, take it'
AUTHORITY = 'T3 of ai/records/20261001-lifecycle'
AGENT_ID = 'claude-opus-5-5'
WHO = ['--by', BY, '--words', WORDS, '--authority', AUTHORITY, '--execution-agent', AGENT_ID]
PUBLIC = ['--audience', 'source=public']
DECIDED = {'by': BY, 'words': WORDS, 'authority': AUTHORITY, 'execution_agent': AGENT_ID, 'confirmed': '--yes'}
ROUTING = ('# Skills in use\n\n<!-- trace-os:begin skill-routing -->\nRoute work to the skills below.\n'
           '<!-- trace-os:end skill-routing -->\n')
LIMIT = ("Limit: definition 1 reads the skill's text; it does not establish that a method is valid, "
         'that its scripts are safe, or that any agent discovers the skill.')
RECEIPT_LIMITS = ['This receipt records what the tool wrote and what it was told; it is not acceptance, and it does '
                  'not establish that the method is valid or that its scripts are safe.',
                  'The intake row names this receipt by its path; this receipt names the row by its id and digest.']
RETIRE_LIMIT = 'A file that was never committed is not kept anywhere.'
HISTORY = 'Git history keeps every file this operation removed, once the removal is committed.'
NOTE_ADAPTER = 'copies of the members Git does not ignore, as scripts/bootstrap.py --sync-skills makes them'
NOTE_NONE = 'agent none: no copies; agents read .skills/ directly'
NOTE_NO_STATE = 'no adapter state here'
TRANSITION = 'not yet: link writes it after the commit'
# AH-240: a receipt link refuses (L2) promises no transition row.
NO_TRANSITION = 'not applicable: no instruction was adopted by this operation'
DEFAULT_LIMITATION = ('Not in the project: co-authors cannot read or check this method; '
                      'a method run records the digest it read.')
PUBLIC_AUDIENCE = 'audience: source declared public; project not checked: the project has no origin remote; ' \
                  'source public; the copy widens nothing'
OWNERSHIP = {'before': 'external', 'replaced_kit_copy': False, 'kit_files_removed': [], 'retired_skills_added': []}


def skill_md(name, body='Body.\n', description='An example skill for tests.'):
    return f'---\nname: {name}\ndescription: {description}\n---\n{body}'.encode()


RUN = b"print('ok')\n"
# The package of the contract's test vector (4.6).
VECTOR = {'LICENSE.txt': (b'Apache License 2.0\n', '100644'), 'SKILL.md': (skill_md('example-skill'), '100644'),
          'scripts/run.py': (RUN, '100755')}
VECTOR_DIGEST = '00c6ea65a3bcf3089fb6c48e684604dc26207fac72c61276d41c0d291e5396d3'


def kit_script(name):
    for folder in (SCRIPTS, REPOSITORY_SCRIPTS):
        if (folder / name).is_file():
            return folder / name
    raise FileNotFoundError(name)


def git(path, *args, data=None):
    run = subprocess.run(['git', *map(str, args)], cwd=path, capture_output=True, input=data, check=False)
    if run.returncode:
        raise AssertionError(f'git {" ".join(map(str, args))} failed: {run.stderr.decode("utf-8", "replace")}')
    return run.stdout.decode('utf-8', 'replace').strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def executable(path):
    return bool(Path(path).stat().st_mode & stat.S_IXUSR)


def package_digest(members):
    """Section 4.6, written again here so the tool's digest is checked against an independent one."""
    lines = ''.join(f'{mode} {len(data)} {sha(data)} {path}\n' for path, (data, mode) in sorted(members.items()))
    return sha(lines.encode('ascii'))


def manifest(members):
    return [{'path': path, 'mode': mode, 'size': len(data), 'sha256': sha(data)}
            for path, (data, mode) in sorted(members.items())]


def folder_members(folder):
    return {path.relative_to(folder).as_posix(): (path.read_bytes(), '100755' if executable(path) else '100644')
            for path in sorted(Path(folder).rglob('*')) if path.is_file()}


def package(name, members, root='skills'):
    return {f'{root}/{name}/{path}': value for path, value in members.items()}


def tree_state(root):
    """Every file's bytes and execute bit outside .git and bootstrap's scratch, for before and after checks."""
    found = {}
    for path in sorted(Path(root).rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(('.git/', '.trace-os/backups/')) or relative == '.trace-os/setup.lock' or \
                '__pycache__' in relative:
            continue
        if path.is_file() and not path.is_symlink():
            found[relative] = (path.read_bytes(), executable(path))
    return found


def handoff(text, name):
    if check_skill_handoff is None:
        return 'handoff not checked: scripts/check_skill_handoff.py is not here'
    absent = check_skill_handoff.absent_signals(text, name)
    return 'handoff stated' if not absent else 'handoff not stated: ' + ', '.join(absent)


def definition(members, name, licence='licence stated'):
    return {'version': 1, 'format': 'format valid', 'licence': licence,
            'handoff': handoff(members['SKILL.md'][0].decode(), name)}


INVENTORY_HEAD = ('<!-- trace-skills:begin inventory -->\n## Skills from outside this project\n\n'
                  "Written by the kit's skill tool (scripts/trace_skills.py in an installed project) from "
                  '.trace-os/skills.lock.json; change it with that tool, not by hand.\n\n')
ADOPTED_HEADER = '| Skill id | Source | Commit | Audience | Detached | Receipt |\n| --- | --- | --- | --- | --- | --- |\n'
RELIED_HEADER = '| Skill id | Held by | Digest | Limitation |\n| --- | --- | --- | --- |\n'
RETIRED_HEADER = '| Skill id | Was | Source or holder | Original kept | Receipt |\n| --- | --- | --- | --- | --- |\n'


def inventory(adopted=(), relied=(), retired=()):
    """The inventory section of 3.2, byte for byte, from its rows."""
    def part(title, header, rows):
        return f'### {title}\n\n' + (header + ''.join(row + '\n' for row in rows) if rows else 'None.\n')
    return (INVENTORY_HEAD + part('Adopted from a source', ADOPTED_HEADER, adopted) + '\n'
            + part('Relied on, not in the project', RELIED_HEADER, relied) + '\n'
            + part('Retired', RETIRED_HEADER, retired) + '<!-- trace-skills:end inventory -->\n')


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
        folder = Path(tempfile.mkdtemp(prefix='test-trace-skills-lifecycle-')).resolve()
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
            git(folder, '--git-dir', repository, 'repack', '-a', '-d', '-q')
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


def signature(folder):
    """Every entry under a folder with its mode, size, inode and times, read without opening a file."""
    found = []
    for top, dirs, files in os.walk(folder):
        for name in dirs + files:
            path = os.path.join(top, name)
            info = os.lstat(path)
            found.append((os.path.relpath(path, folder), info.st_mode, info.st_size, info.st_ino, info.st_mtime_ns,
                          info.st_ctime_ns))
    return sorted(found)


class Source:
    """A bare repository and the work tree that pushes to it; members are staged by blob id and mode (section 13).

    The repositories are made with the first commit. A source changes only through commit(), so the commits
    it holds name it: a history this run has made before is a copy of what it made.
    """

    def __init__(self, root, history=()):
        self.root = root
        self.bare, self.work = root / 'src.git', root / 'work'
        self.history = tuple(history)

    def commit(self, members, message='Change the package'):
        started = bool(self.history)
        self.history += ((tuple(sorted(members.items())), message),)

        def make():
            if not started:
                self.root.mkdir(parents=True)
                git(self.root, 'init', '-q', '--bare', '-b', 'main', self.bare)
                git(self.root, 'init', '-q', '-b', 'main', self.work)
            return self.add(members, message)
        return made(('source', *self.history), (self.root,), make, replace=True)

    def add(self, members, message):
        for path, value in members.items():
            if value is None:
                git(self.work, 'update-index', '--force-remove', '--', path)
                continue
            data, mode = value
            oid = git(self.work, 'hash-object', '-w', '--stdin', data=data)
            git(self.work, 'update-index', '--add', '--cacheinfo', f'{mode},{oid},{path}')
        git(self.work, 'commit', '-q', '--allow-empty', '-m', message)
        git(self.work, 'push', '-q', self.bare, 'main')
        return git(self.work, 'rev-parse', 'HEAD')


class Fixture(unittest.TestCase):
    """Sources, installed and uninstalled projects and run_main, as section 13 describes them."""
    maxDiff = None

    def setUp(self):
        # Every test works in the same folder, emptied before it, so a fixture made in an earlier test
        # and copied here names the paths it would name had this test made it.
        self.root = run_folder() / 'test'
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        # Each project installed() made, until a fixture or the test acts on it: its key and its state.
        self.fresh = {}
        for patch in (mock.patch.dict(os.environ, dict(ENVIRONMENT, HOME=str(self.home))),
                      # The tool's temporary folders go under the test's own folder, outside every project.
                      mock.patch.object(tempfile, 'tempdir', str(self.tmp)),
                      mock.patch.object(ts, 'observe_audience', return_value='not checked: test'),
                      mock.patch.object(ts, 'utc_now', return_value=NOW)):
            patch.start()
            self.addCleanup(patch.stop)
        self.sources = 0

    # Running the tool ------------------------------------------------------

    def run_main(self, project, *argv, stdin='', terminal=False):
        out, err = io.StringIO(), io.StringIO()

        class Input(io.StringIO):
            def isatty(self):
                return terminal

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, 'stdin', Input(stdin)):
            code = ts.main(['--project', str(project), *[str(arg) for arg in argv]])
        return code, out.getvalue(), err.getvalue()

    def tool(self, project):
        return f'python3 {Path(ts.__file__).resolve().as_posix()} --project {project.as_posix()}'

    def refused(self, result, code, text):
        self.assertEqual((result[0], result[2]), (code, f'trace-skills: {text}\n'), result[1])

    # Sources and projects --------------------------------------------------

    def source(self):
        self.sources += 1
        return Source(self.root / f'source-{self.sources}')

    def release(self, folder, skills=None):
        contents = {'AGENTS.md': ('<!-- trace-os:begin core-workflow -->\ninstructions\n'
                                  '<!-- trace-os:end core-workflow -->\n', 'seed'),
                    '.gitignore': (IGNORED, 'managed'),
                    '.skills/example/SKILL.md': (skill_md('example', 'Kit version.\n', 'The kit example.').decode(),
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
        data = {'schema_version': 1, 'version': 'test-1', 'source_commit': '0' * 40,
                'compatibility': {'install_state_schema': 1, 'project_schema': 1, 'record_schema': 1},
                'supported_upgrades': [], 'renames': [], 'releases': [], 'instruction_sections': [spec],
                'files': files}
        (folder / 'starter-manifest.json').write_bytes(bs.encoded(data))
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
            self.commit(project, 'Install the kit')
        if before_commit:
            # The test's own step runs before the commit, so this project is made here every time.
            make()
            return project
        key = ('installed', agent, tuple(sorted((skills or {}).items())))
        # Bootstrap's setup receipt names the project's folder.
        made(key, (self.root / f'release-{name}', project), make, states_its_path=True)
        self.fresh[project] = (key, self.state(project))
        return project

    def uninstalled(self, name='plain'):
        """A project with no install state, like the kit's development repository itself, holding its own skills."""
        project = self.root / name

        def make():
            project.mkdir()
            git(project, 'init', '-q', '-b', 'main')
            git(project, 'config', 'user.name', 'Test Researcher')
            git(project, 'config', 'user.email', 'test@example.invalid')
            for path, data in {'README.md': b'project\n', '.gitignore': IGNORED.encode(),
                               '.skills/own/SKILL.md': skill_md('own', 'Our own method.\n'),
                               '.skills/legacy.md': b'# A legacy single-file skill\n'}.items():
                (project / path).parent.mkdir(parents=True, exist_ok=True)
                (project / path).write_bytes(data)
            self.add_modules(project)
            self.commit(project, 'Start the project')
        made(('uninstalled',), (project,), make)
        return project

    def state(self, project):
        """What a test can change in a project, its repository and this test's own folders, for telling that
        nothing did."""
        return tuple(signature(folder) for folder in (project, self.home, self.tmp))

    @staticmethod
    def commit(project, message='Record the operation'):
        git(project, 'add', '-A')
        git(project, 'commit', '-q', '--allow-empty', '-m', message)
        return git(project, 'rev-parse', 'HEAD')

    def adopted(self, project, members=None, via_show=False, extra=()):
        """The package adopted into the project and committed; returns the source, the commit and the receipt."""
        number = self.sources + 1

        def make():
            source = self.source()
            commit = source.commit(package('example-skill', members or VECTOR), 'The example skill')
            if via_show:
                candidate = self.show(project, source)
                code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example-skill', '--candidate',
                                               candidate, '--yes', *PUBLIC, *WHO, *extra)
            else:
                code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example-skill', '--ref', commit,
                                               '--yes', *PUBLIC, *WHO, *extra)
            self.assertEqual((code, err), (0, ''), out)
            self.commit(project, 'Adopt example-skill')
            return commit
        fresh = self.fresh.pop(project, None)
        if fresh is not None and fresh[1] == self.state(project):
            # Into a project as installed() made it: this run adopts there once, and copies the result after.
            # The records name the source's folder, and the compiled modules the project's, so both are in the key.
            key = ('adopted', fresh[0], project.name, number, tuple(sorted((members or VECTOR).items())), via_show,
                   tuple(extra), ts.on_windows())
            commit = made(key, (project, self.root / f'source-{number}'), make, replace=True)
            self.sources = number
        else:
            commit = make()
        history = ((tuple(sorted(package('example-skill', members or VECTOR).items())), 'The example skill'),)
        return Source(self.root / f'source-{number}', history), commit, \
            f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json'

    def show(self, project, source, ref='main'):
        code, out, err = self.run_main(project, 'show', source.bare, 'skills/example-skill', '--ref', ref)
        self.assertEqual((code, err), (0, ''), out)
        return Path(next(line for line in out.splitlines() if line.startswith('Candidate: '))[len('Candidate: '):])

    # Reading results -------------------------------------------------------

    @staticmethod
    def lock(project):
        return json.loads((project / ts.LOCK).read_text())

    @staticmethod
    def read(project, name):
        return json.loads((project / name).read_text())

    @staticmethod
    def rows(project, name):
        path = project / name
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []

    def section(self, project):
        text = (project / ts.INVENTORY).read_text()
        return text[text.index('<!-- trace-skills:begin inventory -->'):]

    def staging(self):
        return sorted(path.name for path in self.tmp.iterdir())

    @contextlib.contextmanager
    def transactions(self):
        """Record the paths of every bootstrap.transaction, in the order the tool hands them over (12.3)."""
        seen, real = [], ts.bootstrap.transaction

        def wrapper(target, updates, expected=None):
            seen.append(list(updates))
            return real(target, updates, expected)
        with mock.patch.object(ts.bootstrap, 'transaction', wrapper):
            yield seen

    def interrupt_at(self, project, match):
        """Raise KeyboardInterrupt at the first atomic write `match(path, data)` selects (section 13)."""
        real, fired = ts.bootstrap.write_atomic, []

        def wrapper(path, data):
            try:
                relative = Path(path).resolve().relative_to(project.resolve()).as_posix()
            except ValueError:
                relative = None
            if not fired and relative is not None and match(relative, data):
                fired.append(relative)
                raise KeyboardInterrupt
            return real(path, data)
        return mock.patch.object(ts.bootstrap, 'write_atomic', wrapper)

    # Common assertions -------------------------------------------------------

    def assert_parity(self, project):
        """The equivalence criterion of section 9: check_skills clean and, with an adapter, sync_skills a no-op."""
        _, problems = check_skills.check(project)
        self.assertEqual(problems, [])
        state_path = project / bs.STATE
        before = state_path.read_bytes()
        state = json.loads(before)
        if state['agent'] in bs.AGENTS:
            self.assertEqual(bs.sync_skills(project), {'synced': 0, 'agent': state['agent'], 'removed': [],
                                                       'retired': []})
            self.assertEqual(state_path.read_bytes(), before)

    def assert_registries(self, project):
        self.assertEqual(trace_record.validate_intake(self.rows(project, ts.INTAKE))[0], [])
        self.assertEqual(trace_record.validate_transitions(self.rows(project, ts.TRANSITIONS))[0], [])

    def assert_receipt(self, project, path, operation, expected, head=None):
        """The fields of 5.1 every receipt carries, then the operation's own; the file is in encoded form."""
        data = (project / path).read_bytes()
        receipt = json.loads(data)
        self.assertEqual(data, bs.encoded(receipt))
        state = self.read(project, bs.STATE) if (project / bs.STATE).exists() else None
        common = {'schema_version': 1, 'kind': 'trace-skills-receipt', 'contract': 1, 'operation': operation,
                  'operation_id': Path(path).stem, 'name': receipt['name'], 'started_at': AT, 'completed_at': AT,
                  'recovered': False, 'decided': DECIDED,
                  'tool': {'path': Path(ts.__file__).resolve().as_posix(),
                           'sha256': sha(Path(ts.__file__).read_bytes())},
                  'project': {'installed': state is not None, 'kit_version': state['version'] if state else None,
                              'agent': state['agent'] if state else None, 'head': head}}
        common.update(expected)
        self.assertEqual({key: receipt.get(key) for key in common}, common)
        self.assertEqual(receipt['records']['transition'],
                         TRANSITION if operation in ('adopt', 'update', 'accept-merged') else NO_TRANSITION)
        self.assertEqual(list(receipt['writes']), sorted(receipt['writes']))
        for name, entry in receipt['writes'].items():
            target = project / name
            self.assertEqual(sha(target.read_bytes()) if target.is_file() else None, entry['after'], name)
        return receipt

    def assert_intake(self, project, receipt):
        """The receipt names the last intake row by id and by the SHA-256 of its line, and the row names the receipt."""
        lines = [line for line in (project / ts.INTAKE).read_bytes().split(b'\n') if line.strip()]
        row = json.loads(lines[-1])
        self.assertEqual(receipt['records']['intake'], {'id': row['id'], 'registry': ts.INTAKE,
                                                        'row_sha256': sha(lines[-1])})
        self.assertEqual(row['object'], f".trace-os/skill-adoptions/{receipt['operation_id']}.json")
        self.assertNotIn('object_sha256', row)
        return row


# update (6.5) ---------------------------------------------------------------------

V1 = dict(VECTOR, **{'references/old.md': (b'Old notes.\n', '100644'), 'scripts/tool.sh': (b'#!/bin/sh\n', '100644')})
V2_SKILL = skill_md('example-skill', 'Body.\nRevised.\n')
V2 = {'LICENSE.txt': VECTOR['LICENSE.txt'], 'SKILL.md': (V2_SKILL, '100644'),
      'references/new.md': (b'New notes.\n', '100644'), 'scripts/run.py': (RUN, '100755'),
      'scripts/tool.sh': (b'#!/bin/sh\n', '100755')}
V2_CHANGE = {'skills/example-skill/SKILL.md': (V2_SKILL, '100644'),
             'skills/example-skill/references/new.md': (b'New notes.\n', '100644'),
             'skills/example-skill/references/old.md': None,
             'skills/example-skill/scripts/tool.sh': (b'#!/bin/sh\n', '100755')}


class UpdateTests(Fixture):
    def test_an_unchanged_copy_takes_the_new_package(self):
        project = self.installed()
        source, first, adopt_receipt = self.adopted(project, V1)
        second = source.commit(V2_CHANGE, 'Revise the package')
        head = git(project, 'rev-parse', 'HEAD')
        seed = (project / ts.INVENTORY).read_bytes()
        tree1, tree2 = package_digest(V1), package_digest(V2)
        old_base, new_base = f'.trace-os/skill-bases/{tree1}', f'.trace-os/skill-bases/{tree2}'
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        operation = f'{STAMP}-example-skill-update'
        receipt_path = f'.trace-os/skill-adoptions/{operation}.json'
        definition_text = (f"definition 1: format valid; licence stated; "
                           f"{handoff(V2_SKILL.decode(), 'example-skill')}")
        # Shown: the whole new package as show prints it, the difference, the plan and the limit (6.5).
        self.assertTrue(out.startswith(f'Source: {source.bare.as_posix()} at {second} (requested {second})\n'
                                       f'Package: skills/example-skill (example-skill), 5 files, '
                                       f'{sum(len(data) for data, _ in V2.values())} bytes\n'
                                       f'Package digest: {tree2}\n{definition_text}\n\n'), out)
        self.assertIn('--- references/new.md (100644, 11 bytes, sha256 ' + sha(b'New notes.\n') + ')\nNew notes.\n'
                      '--- end of references/new.md\n', out)
        self.assertIn('\n'.join([
            f'Difference from {first[:12]}, the current upstream:',
            '  changed SKILL.md', '  added references/new.md', '  removed references/old.md', '  mode scripts/tool.sh',
            '',
            f'Plan: update example-skill from {first[:12]} to {second[:12]}',
            f'  from: {source.bare.as_posix()} skills/example-skill at {second} (requested {second})',
            f'  package digest: {tree2}',
            '  ' + definition_text,
            '  ' + PUBLIC_AUDIENCE,
            '  writes, in this order:',
            '    2 file(s) written, 1 removed and 1 mode change(s) under .skills/example-skill/',
            f'    5 file(s) under {new_base}/',
            '    5 agent copies under .claude/skills/example-skill/, 1 removed',
            f'    5 file(s) of the old original removed from {old_base}/',
            '    .trace-os/install.json', '    .trace-os/skills.lock.json', '    ai/SKILLS-IN-USE.md (the inventory section)',
            f'  then: an intake row in sources/intake.jsonl, and last the receipt {receipt_path}',
            '  not tracked by Git here: none', LIMIT, '']), out)
        self.assertTrue(out.endswith('\n'.join([
            f'Updated example-skill from {first[:12]} to {second[:12]}.', f'  receipt: {receipt_path}',
            '  intake: IN-002 in sources/intake.jsonl', '  agent copies: 2 written for claude',
            'Next: review and commit these files, then record the adoption against that commit:',
            f'  {self.tool(project)} link {receipt_path} --commit <sha>', 'Nothing was committed or pushed.', ''])), out)
        # The transaction's order (12.3): copy, original, agent copies, old original, state, lock, inventory.
        copies = '.claude/skills/example-skill'
        self.assertEqual(seen, [[
            '.skills/example-skill/SKILL.md', '.skills/example-skill/references/new.md',
            '.skills/example-skill/references/old.md',
            *[f'{new_base}/{path}' for path in sorted(V2)],
            f'{copies}/SKILL.md', f'{copies}/references/new.md', f'{copies}/references/old.md',
            *[f'{old_base}/{path}' for path in sorted(V1)],
            '.trace-os/install.json', ts.LOCK, ts.INVENTORY]])
        # Files and modes: the copy and its agent copies are the new package; the old original is gone.
        self.assertEqual(folder_members(project / '.skills/example-skill'), V2)
        self.assertEqual(folder_members(project / copies), V2)
        self.assertEqual(folder_members(project / new_base), V2)
        self.assertFalse((project / old_base).exists())
        self.assertTrue((project / ts.BASES).is_dir())
        # The lock (4.2).
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual(entry, {
            'manager': 'external',
            'source': {'repository': source.bare.as_posix(), 'path': 'skills/example-skill', 'requested_ref': second,
                       'commit': second, 'audience': {'declared': 'public', 'declared_by': BY,
                                                      'observed': 'not checked: test'}},
            'project_audience': {'declared': 'not declared', 'declared_by': None,
                                 'observed': 'not checked: the project has no origin remote'},
            'upstream': {'tree_sha256': tree2, 'tracked_sha256': tree2, 'snapshot': new_base},
            'adopted': {'path': '.skills/example-skill', 'tree_sha256': tree2, 'receipt': receipt_path,
                        'kept_existing': False},
            'detached': False, 'replaced_kit_copy': False, 'receipts': [adopt_receipt, receipt_path]})
        # The receipt (5.1).
        receipt = self.assert_receipt(project, receipt_path, 'update', {
            'source': {'repository': source.bare.as_posix(), 'path': 'skills/example-skill', 'requested_ref': second,
                       'commit': second},
            'upstream': {'tree_sha256': tree2, 'tracked_sha256': tree2, 'snapshot': new_base, 'files': manifest(V2),
                         'litter': []},
            'previous': {'commit': first, 'tree_sha256': tree1, 'adopted_tree_sha256': tree1},
            'copy': {'path': '.skills/example-skill', 'tree_sha256': tree2, 'kept_existing': False,
                     'files': manifest(V2)},
            'definition': definition(V2, 'example-skill'),
            'ownership': OWNERSHIP,
            'agent_copies': {'agent': 'claude', 'note': NOTE_ADAPTER, 'removed': [f'{copies}/references/old.md'],
                             'written': [f'{copies}/SKILL.md', f'{copies}/references/new.md'],
                             'kept': [f'{copies}/LICENSE.txt', f'{copies}/scripts/run.py',
                                      f'{copies}/scripts/tool.sh']},
            'modes': {path: '100755' for path in (
                '.skills/example-skill/scripts/run.py', '.skills/example-skill/scripts/tool.sh',
                f'{new_base}/scripts/run.py', f'{new_base}/scripts/tool.sh',
                f'{copies}/scripts/run.py', f'{copies}/scripts/tool.sh')},
            'not_tracked': [], 'limits': RECEIPT_LIMITS}, head=head)
        self.assertEqual(receipt['audience']['comparison'], 'source public; the copy widens nothing')
        self.assertEqual(receipt['writes']['.skills/example-skill/references/old.md']['after'], None)
        self.assertNotIn('.skills/example-skill/scripts/tool.sh', receipt['writes'])
        # The intake row (10.1): the opening clause of an update.
        row = self.assert_intake(project, receipt)
        self.assertEqual({key: row.get(key) for key in ('id', 'what', 'origin', 'admitted_by', 'checked', 'used_for',
                                                       'authority', 'decision')}, {
            'id': 'IN-002',
            'what': f'skill package example-skill ({source.bare.as_posix()} skills/example-skill) at commit {second}, '
                    f'package digest {tree2}',
            'origin': f'{source.bare.as_posix()} skills/example-skill at commit {second}, requested as {second}',
            'admitted_by': BY,
            'checked': f'the whole new package (5 files, {sum(len(data) for data, _ in V2.values())} bytes) and its '
                       f'difference from {first[:12]} were printed before writing and the person confirmed it '
                       f"(--yes); every file's SHA-256 and the package digest were re-checked just before writing; "
                       f'{definition_text}; audience: source public; the copy widens nothing',
            'used_for': 'a standing instruction for the agents in this project, at .skills/example-skill/',
            'authority': AUTHORITY, 'decision': 'admit'})
        self.assert_registries(project)
        # The inventory (3.2), replaced in place after the routing block.
        self.assertEqual((project / ts.INVENTORY).read_text(), ROUTING + '\n' + inventory(adopted=[
            f'| example-skill | {source.bare.as_posix()} `skills/example-skill` | {second[:12]} | source public | no '
            f'| `{receipt_path}` |']))
        self.assertNotEqual((project / ts.INVENTORY).read_bytes(), seed)
        self.assertFalse((project / ts.OPERATION).exists())
        self.assertEqual(self.staging(), [])
        self.assert_parity(project)
        # Nothing moved: the same update again is Current, and writes nothing.
        before = tree_state(project)
        self.assertEqual(self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *WHO), (
            0, f'Current: example-skill is at {second[:12]}, the commit {second} resolves to; nothing to do.\n', ''))
        self.assertEqual(tree_state(project), before)
        self.assertEqual(self.staging(), [])
        # The update's receipt is linked like an adoption's (6.10).
        recorded = self.commit(project, 'Update example-skill')
        code, out, err = self.run_main(project, 'link', receipt_path, '--commit', recorded)
        self.assertEqual((code, out, err), (0, f'Linked {receipt_path} to {recorded[:12]}: TR-001 in '
                                               'sources/transitions.jsonl.\n', ''))
        self.assert_registries(project)

    def test_a_commit_that_leaves_the_package_unchanged_records_only_the_commit(self):
        project = self.installed()
        source, first, adopt_receipt = self.adopted(project)
        second = source.commit({'README.md': (b'library\n', '100644')}, 'Outside the package')
        before = tree_state(project)
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        receipt_path = f'.trace-os/skill-adoptions/{STAMP}-example-skill-update.json'
        self.assertIn(f'Difference from {first[:12]}, the current upstream:\n  none: the package is unchanged\n', out)
        self.assertIn('    nothing under .skills/example-skill/ or .trace-os/skill-bases/: the package is unchanged, '
                      'so only the new commit is recorded\n    3 agent copies under .claude/skills/example-skill/\n'
                      '    .trace-os/skills.lock.json\n', out)
        self.assertEqual(seen, [[ts.LOCK, ts.INVENTORY]])
        after = tree_state(project)
        for name in before:
            if name not in (ts.LOCK, ts.INVENTORY, ts.INTAKE):
                self.assertEqual(after[name], before[name], name)
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['source']['commit'], entry['upstream'], entry['adopted']['tree_sha256'],
                          entry['receipts']),
                         (second, {'tree_sha256': VECTOR_DIGEST, 'tracked_sha256': VECTOR_DIGEST,
                                   'snapshot': f'.trace-os/skill-bases/{VECTOR_DIGEST}'}, VECTOR_DIGEST,
                          [adopt_receipt, receipt_path]))
        receipt = self.assert_receipt(project, receipt_path, 'update', {
            'previous': {'commit': first, 'tree_sha256': VECTOR_DIGEST, 'adopted_tree_sha256': VECTOR_DIGEST},
            'copy': {'path': '.skills/example-skill', 'tree_sha256': VECTOR_DIGEST, 'kept_existing': False,
                     'files': manifest(VECTOR)}}, head=git(project, 'rev-parse', 'HEAD'))
        self.assertEqual(sorted(receipt['writes']), [ts.LOCK, ts.INVENTORY])
        self.assert_intake(project, receipt)
        self.assert_parity(project)

    def test_ah242_an_update_writes_the_originals_note_when_it_is_not_there(self):
        """AH-242: a folder of originals with no README.md gets it from the next update that writes an original."""
        project = self.installed()
        source, first, _ = self.adopted(project)
        note = project / ts.BASES_NOTE
        self.assertTrue(note.is_file(), 'the adoption wrote it')
        note.unlink()
        self.commit(project, 'A project from before the note')
        second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        new_base = f".trace-os/skill-bases/{package_digest(dict(VECTOR, **{'SKILL.md': (V2_SKILL, '100644')}))}"
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn('    1 file(s) written, 0 removed and 0 mode change(s) under .skills/example-skill/\n'
                      '    .trace-os/skill-bases/README.md (says the originals there are not skills to follow)\n'
                      f'    3 file(s) under {new_base}/\n', out)
        self.assertEqual(seen[0][:3], ['.skills/example-skill/SKILL.md', ts.BASES_NOTE, f'{new_base}/LICENSE.txt'])
        self.assertEqual(note.read_bytes(), ts.BASES_NOTE_TEXT)
        self.assert_parity(project)
        # A new commit that leaves the package as it was writes no original, so no note either.
        self.commit(project, 'Update example-skill')
        note.unlink()
        self.commit(project, 'Remove the note again')
        third = source.commit({'README.md': (b'library\n', '100644')}, 'Outside the package')
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', third, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertNotIn('README.md (says', out)
        self.assertFalse(note.exists())

    def test_current_when_the_ref_resolves_to_the_same_commit(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        before = tree_state(project)
        self.assertEqual(self.run_main(project, 'update', 'example-skill'), (
            0, f'Current: example-skill is at {first[:12]}, the commit main resolves to; nothing to do.\n', ''))
        self.assertEqual(tree_state(project), before)
        self.assertEqual(self.staging(), [])

    def test_no_terminal_writes_a_candidate_that_a_later_yes_applies(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        before = tree_state(project)
        code, out, err = self.run_main(project, 'update', 'example-skill', *PUBLIC, *WHO)
        self.assertEqual((code, err), (1, ''), out)
        last = out.rstrip('\n').splitlines()[-1]
        prefix = ('Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, '
                  f'run: {self.tool(project)} update example-skill --audience source=public --by \'Ann Author\' '
                  f"--words 'yes, take it' --authority 'T3 of ai/records/20261001-lifecycle' --execution-agent "
                  f'{AGENT_ID} --candidate ')
        self.assertTrue(last.startswith(prefix) and last.endswith(' --yes'), last)
        candidate = Path(last[len(prefix):-len(' --yes')])
        data = json.loads(candidate.read_text())
        self.assertEqual((data['created_by'], data['for'], data['source']['commit'], data['source']['requested_ref']),
                         ('update', {'operation': 'update', 'name': 'example-skill', 'from_commit': first}, second,
                          'main'))
        self.assertEqual(tree_state(project), before)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--candidate', candidate, '--yes',
                                       *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        # The candidate's run printed the contents; this one prints the header and the manifest.
        self.assertNotIn('--- end of SKILL.md', out)
        self.assertIn(f"100644 {len(V2_SKILL)} {sha(V2_SKILL)} SKILL.md\n", out)
        rows = self.rows(project, ts.INTAKE)
        self.assertTrue(rows[-1]['checked'].startswith(
            f'the whole new package (3 files, {len(V2_SKILL) + 31} bytes) and its difference from {first[:12]} were '
            'printed by update before writing and the person confirmed it (--yes); '), rows[-1]['checked'])
        self.assertEqual(self.lock(project)['skills']['example-skill']['source']['requested_ref'], 'main')
        self.assertEqual(self.staging(), [])
        self.assert_parity(project)

    def test_a_candidate_written_by_show_says_which_run_printed_what(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        candidate = self.show(project, source)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--candidate', candidate, '--yes',
                                       *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertTrue(self.rows(project, ts.INTAKE)[-1]['checked'].startswith(
            f'the whole new package (3 files, {len(V2_SKILL) + 31} bytes) was printed by show, and its difference '
            f'from {first[:12]} by update, before writing, and the person confirmed it (--yes); '))
        self.assert_parity(project)

    def test_a_terminal_yes_applies_and_records_it(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        code, out, err = self.run_main(project, 'update', 'example-skill', *PUBLIC, *WHO, stdin='y\n', terminal=True)
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn('Apply? Nothing is committed or pushed. [y/N] ', out)
        receipt = self.read(project, f'.trace-os/skill-adoptions/{STAMP}-example-skill-update.json')
        self.assertEqual(receipt['decided']['confirmed'], 'at the terminal')
        self.assertIn('the person confirmed it (at the terminal)', self.rows(project, ts.INTAKE)[-1]['checked'])
        before = tree_state(project)
        source.commit({'skills/example-skill/SKILL.md': (skill_md('example-skill', 'Third.\n'), '100644')})
        code, out, err = self.run_main(project, 'update', 'example-skill', *PUBLIC, *WHO, stdin='n\n', terminal=True)
        self.assertEqual((code, out.splitlines()[-1], err), (1, 'Apply? Nothing is committed or pushed. [y/N] '
                                                                'Nothing was applied: not confirmed.', ''))
        self.assertEqual(tree_state(project), before)

    def test_refusals_name_the_reason_and_write_nothing(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        before = tree_state(project)
        cases = [
            (('update', 'nothing', '--ref', second, '--yes'), 1, 'no skill named nothing is adopted here'),
            (('update', '../escape', '--ref', second, '--yes'), 1, 'no skill named ../escape is adopted here'),
            (('update', 'example', '--ref', second, '--yes'), 1,
             "example is a skill the kit ships; the kit's updater updates it (SETUP-AGENT.md, Update)"),
            (('update', 'example-skill', '--yes'), 1,
             '--yes applies what the person saw: pass the --candidate file that show, adopt or update wrote, or --ref '
             'with a full commit id'),
            # The adoption asked for a commit id: the update needs a ref to move to (N25).
            (('update', 'example-skill'), 2,
             f'example-skill is pinned to commit {first[:12]}; name the ref to update to with --ref'),
            (('update', 'example-skill', '--ref', second, '--yes', '--audience', 'source=everyone'), 2,
             '--audience takes source=<value> or project=<value>, where the value is private, collaborators or public'),
            (('update', 'example-skill', '--ref', second, '--yes', '--execution-agent', 'two words'), 2,
             'execution agent must be one identifier')]
        for argv, code, text in cases:
            with self.subTest(argv=argv):
                self.refused(self.run_main(project, *argv, *WHO[:6]), code, text)
                self.assertEqual(tree_state(project), before)
        self.assertEqual(self.staging(), [])
        # A1: a source not known to be public needs --share-with-project, every time.
        other = self.installed(name='shared')
        code, out, err = self.run_main(other, 'adopt', source.bare, 'skills/example-skill', '--ref', first, '--yes',
                                       '--share-with-project', '--words', 'it may be read here')
        self.assertEqual((code, err), (0, ''), out)
        self.refused(self.run_main(other, 'update', 'example-skill', '--ref', second, '--yes'), 1,
                     'the source (not checked: test) is not known to be public, and the project is not checked: the '
                     'project has no origin remote: copying it here lets everyone who can read the project read it. '
                     'If the person wants that, pass --share-with-project, with their words in --words')
        # N26: the copy is gone.
        shutil.rmtree(project / '.skills/example-skill')
        self.refused(self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes'), 1,
                     'the copy .skills/example-skill/ is not here; retire example-skill, or adopt it again')
        git(project, 'checkout', '--', '.skills/example-skill')
        # N14 and N15 and N16 in a project with its own skill, a declaration and a retired entry.
        plain = self.uninstalled()
        self.refused(self.run_main(plain, 'update', 'own', '--ref', second, '--yes'), 1,
                     "own is the project's own skill; this tool updates and retires only skills it adopted or declared")
        self.assertEqual(self.run_main(plain, 'declare', 'held-skill', '--held-by', 'Alex Holder', '--yes')[0], 0)
        self.refused(self.run_main(plain, 'update', 'held-skill', '--ref', second, '--yes'), 1,
                     'held-skill is declared, not in the project; there is nothing to update')
        for flag, operation in (('--accept-merged', 'accept-merged'), ('--detach', 'detach'), ('--attach', 'attach')):
            self.refused(self.run_main(plain, 'update', 'held-skill', flag, '--ref', second, '--yes'), 1,
                         f'held-skill is a outside entry; {operation} applies to adopted skills only')
        self.assertEqual(self.run_main(plain, 'retire', 'held-skill', '--yes')[0], 0)
        self.refused(self.run_main(plain, 'update', 'held-skill', '--ref', second, '--yes'), 1,
                     'held-skill is retired; adopt it again to bring it back')

    def test_a_renamed_package_is_another_skill(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (skill_md('other-name'), '100644')})
        before = tree_state(project)
        self.refused(self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC), 1,
                     'the package at skills/example-skill is now named other-name; a renamed package is another '
                     'skill: retire example-skill, then adopt other-name')
        self.assertEqual(tree_state(project), before)
        self.assertEqual(self.staging(), [])

    def test_an_edited_original_or_agent_copy_is_refused(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        base = project / f'.trace-os/skill-bases/{VECTOR_DIGEST}/LICENSE.txt'
        base.write_bytes(b'Edited.\n')
        before = tree_state(project)
        self.refused(self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC), 1,
                     f'the upstream original under .trace-os/skill-bases/{VECTOR_DIGEST} was edited '
                     f'(.trace-os/skill-bases/{VECTOR_DIGEST}/LICENSE.txt); it is the record of what was adopted. '
                     f'Restore it with git checkout HEAD -- .trace-os/skill-bases/{VECTOR_DIGEST}/LICENSE.txt, then '
                     'run this again')
        self.assertEqual(tree_state(project), before)
        git(project, 'checkout', '--', '.trace-os/skill-bases')
        copy = project / '.claude/skills/example-skill/LICENSE.txt'
        copy.write_bytes(b'Edited separately.\n')
        before = tree_state(project)
        self.refused(self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC), 1,
                     'a generated copy of example-skill was edited separately (.claude/skills/example-skill/'
                     'LICENSE.txt); merge or remove it, then run this again')
        self.assertEqual(tree_state(project), before)

    def test_edits_between_the_plan_and_the_write(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        real = ts.bootstrap.locked

        @contextlib.contextmanager
        def edited_while_waiting(target):
            # The person edits a file the update leaves alone, between the plan and the lock.
            with (target / '.skills/example-skill/LICENSE.txt').open('a') as stream:
                stream.write('a late note\n')
            with real(target):
                yield

        before = tree_state(project)
        with mock.patch.object(ts.bootstrap, 'locked', edited_while_waiting):
            result = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC)
        self.refused(result, 1, '.skills/example-skill/LICENSE.txt changed after the plan was shown; nothing was '
                                'written. Run the command again for a new plan')
        after = tree_state(project)
        self.assertEqual(after.pop('.skills/example-skill/LICENSE.txt')[0], b'Apache License 2.0\na late note\n')
        before.pop('.skills/example-skill/LICENSE.txt')
        self.assertEqual(after, before)
        self.assertFalse((project / ts.OPERATION).exists())
        git(project, 'checkout', '--', '.skills')
        # E15: edited mid-write, everything else undone and the edit kept (the amended catalogue wording).
        real_write = ts.bootstrap.write_atomic

        def edit_then_write(path, data):
            if Path(path).name == 'skills.lock.json' and '.trace-os/backups' not in Path(path).as_posix():
                (project / ts.INVENTORY).write_text('edited mid-write\n')
            return real_write(path, data)

        before = tree_state(project)
        with mock.patch.object(ts.bootstrap, 'write_atomic', edit_then_write):
            result = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC)
        self.refused(result, 1, 'ai/SKILLS-IN-USE.md changed while this operation was writing; everything else it '
                                'wrote was undone, and ai/SKILLS-IN-USE.md keeps the later edit')
        after = tree_state(project)
        self.assertEqual(after.pop(ts.INVENTORY)[0], b'edited mid-write\n')
        before.pop(ts.INVENTORY)
        self.assertEqual(after, before)
        self.assertFalse((project / ts.OPERATION).exists())

    def test_an_interrupted_update_is_undone_or_completed_by_recover(self):
        for at, outcome in (('.trace-os/skills.lock.json', 'Undone'), ('.trace-os/skill-adoptions/', 'Completed')):
            with self.subTest(at=at):
                project = self.installed(name='interrupted-' + outcome.lower())
                source, first, _ = self.adopted(project)
                second = source.commit({'skills/example-skill/scripts/run.py': (b"print('two')\n", '100755')})
                before = tree_state(project)
                operation = f'{STAMP}-example-skill-update'
                with self.interrupt_at(project, lambda relative, data: relative.startswith(at)):
                    code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes',
                                                   *PUBLIC, *WHO)
                self.assertEqual((code, err), (130, f'trace-skills: interrupted; run {self.tool(project)} recover to '
                                                    f'finish or undo {operation}\n'))
                self.refused(self.run_main(project, 'retire', 'example-skill', '--yes'), 1,
                             f'an earlier operation ({operation}) did not finish; run {self.tool(project)} recover '
                             'first')
                code, out, err = self.run_main(project, 'recover')
                self.assertEqual(err, '')
                if outcome == 'Undone':
                    self.assertEqual(out, f'Undone: {operation}; the project is as it was before. Run the command '
                                          'again to update.\n')
                    self.assertEqual(tree_state(project), before)
                    self.assertEqual(self.rows(project, ts.INTAKE)[-1]['object'],
                                     f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json')
                else:
                    receipt_path = f'.trace-os/skill-adoptions/{operation}.json'
                    self.assertEqual(out, f'Completed: {operation}; receipt {receipt_path}.\n')
                    self.assertTrue(self.read(project, receipt_path)['recovered'])
                    self.assertEqual(len(self.rows(project, ts.INTAKE)), 2)
                    for path in ('.skills/example-skill/scripts/run.py',
                                 '.claude/skills/example-skill/scripts/run.py'):
                        self.assertEqual((project / path).read_bytes(), b"print('two')\n")
                        self.assertTrue(executable(project / path), path)
                self.assertFalse((project / ts.OPERATION).exists())
                self.assertEqual(self.staging(), [])
                self.assert_parity(project)
                self.assertEqual(self.run_main(project, 'recover'), (0, 'Nothing to recover.\n', ''))


# update with an adapted copy, and --accept-merged (6.5, 6.6) ------------------------

MERGE_UPSTREAM = skill_md('example-skill', 'Body.\nUpstream line \x1b[31mred.\n')
MERGE_CHANGE = {'skills/example-skill/SKILL.md': (MERGE_UPSTREAM, '100644'),
                'skills/example-skill/references/new.md': (b'New notes.\n', '100644')}
MERGE_V2 = dict(VECTOR, **{'SKILL.md': (MERGE_UPSTREAM, '100644'), 'references/new.md': (b'New notes.\n', '100644')})


class MergeTests(Fixture):
    def adapted(self, project):
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'Our line.\n')
        self.commit(project, 'Adapt example-skill')

    def test_an_adapted_copy_gets_the_three_versions_and_nothing_is_written(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        self.adapted(project)
        second = source.commit(MERGE_CHANGE, 'Upstream moves on')
        before = tree_state(project)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (1, ''), out)
        self.assertEqual(tree_state(project), before, 'update wrote into the project for an adapted copy')
        self.assertEqual(self.rows(project, ts.INTAKE)[-1]['object'],
                         f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json')
        self.assertFalse((project / ts.OPERATION).exists())
        # The staging folder and the candidate stay, because the person merges from them (3.3).
        folders = list(self.tmp.iterdir())
        self.assertEqual(len(folders), 1)
        staged, candidate = folders[0] / 'package', folders[0] / 'candidate.json'
        self.assertEqual(folder_members(staged), MERGE_V2)
        data = json.loads(candidate.read_text())
        self.assertEqual((data['kind'], data['created_by'], data['created_at'], data['for'], data['source'],
                          data['package']['tree_sha256'], data['package']['files'], data['staged']),
                         ('trace-skills-candidate', 'update', AT,
                          {'operation': 'accept-merged', 'name': 'example-skill', 'from_commit': first},
                          {'repository': source.bare.as_posix(), 'path': 'skills/example-skill',
                           'requested_ref': second, 'commit': second},
                          package_digest(MERGE_V2), manifest(MERGE_V2), staged.as_posix()))
        self.assertEqual(out, '\n'.join([
            'example-skill is adapted here; the tool does not merge. The three versions:',
            f'  original (what was adopted): .trace-os/skill-bases/{VECTOR_DIGEST}',
            "  this project's copy: .skills/example-skill/",
            f'  new upstream: {staged.as_posix()} ({source.bare.as_posix()} skills/example-skill at {second[:12]})',
            "From the original to this project's copy:",
            '  changed SKILL.md',
            '--- original/SKILL.md', '+++ copy/SKILL.md', '@@ -3,3 +3,4 @@',
            ' description: An example skill for tests.', ' ---', ' Body.', '+Our line.',
            'From the original to the new upstream:',
            '  changed SKILL.md',
            '--- original/SKILL.md', '+++ upstream/SKILL.md', '@@ -3,3 +3,4 @@',
            ' description: An example skill for tests.', ' ---', ' Body.', '+Upstream line \\x1b[31mred.',
            '  added references/new.md',
            'Nothing was written. Merge by hand into .skills/example-skill/, then record the merge: '
            f'{self.tool(project)} update example-skill --accept-merged --candidate {candidate.as_posix()}', '']))
        # The person merges by hand, then the merge is recorded as it stands, against the candidate.
        merged = MERGE_UPSTREAM + b'Our line.\n'
        (project / '.skills/example-skill/SKILL.md').write_bytes(merged)
        (project / '.skills/example-skill/references').mkdir()
        (project / '.skills/example-skill/references/new.md').write_bytes(b'New notes.\n')
        copy_members = dict(MERGE_V2, **{'SKILL.md': (merged, '100644')})
        merged_digest, tree2 = package_digest(copy_members), package_digest(MERGE_V2)
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--candidate',
                                           candidate, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        operation = f'{STAMP}-example-skill-accept-merged'
        receipt_path = f'.trace-os/skill-adoptions/{operation}.json'
        copy_definition = definition(copy_members, 'example-skill')
        definition_text = (f"definition 1 of the merged copy: {copy_definition['format']}; "
                           f"{copy_definition['licence']}; {copy_definition['handoff']}")
        old_base, new_base = f'.trace-os/skill-bases/{VECTOR_DIGEST}', f'.trace-os/skill-bases/{tree2}'
        self.assertIn('\n'.join([
            f'Merged copy: .skills/example-skill/, digest {merged_digest}',
            'From the new upstream to the merged copy:', '  changed SKILL.md',
            '--- upstream/SKILL.md', '+++ copy/SKILL.md', '@@ -4,3 +4,4 @@', ' ---', ' Body.',
            ' Upstream line \\x1b[31mred.', '+Our line.', definition_text, '',
            f'Plan: record the merged copy of example-skill against {second[:12]}',
            f'  from: {source.bare.as_posix()} skills/example-skill at {second} (requested {second})',
            f'  package digest: {tree2}',
            f'  merged copy: .skills/example-skill/ is recorded as it stands and not changed, digest {merged_digest}',
            '  merged by: not stated', '  ' + PUBLIC_AUDIENCE, '  writes, in this order:', f'    4 file(s) under {new_base}/',
            '    4 agent copies under .claude/skills/example-skill/, made from the copy as it stands',
            f'    3 file(s) of the old original removed from {old_base}/',
            '    .trace-os/install.json', '    .trace-os/skills.lock.json', '    ai/SKILLS-IN-USE.md (the inventory section)',
            f'  then: an intake row in sources/intake.jsonl, and last the receipt {receipt_path}',
            '  not tracked by Git here: none', LIMIT, '']), out)
        self.assertTrue(out.endswith('\n'.join([
            f'Recorded the merged copy of example-skill against {second[:12]}.', f'  receipt: {receipt_path}',
            '  intake: IN-002 in sources/intake.jsonl', '  agent copies: 2 written for claude',
            'Next: review and commit these files, then record the adoption against that commit:',
            f'  {self.tool(project)} link {receipt_path} --commit <sha>', 'Nothing was committed or pushed.', ''])))
        copies = '.claude/skills/example-skill'
        self.assertEqual(seen, [[*[f'{new_base}/{path}' for path in sorted(MERGE_V2)],
                                 f'{copies}/SKILL.md', f'{copies}/references/new.md',
                                 *[f'{old_base}/{path}' for path in sorted(VECTOR)],
                                 '.trace-os/install.json', ts.LOCK, ts.INVENTORY]])
        # The copy is never changed; its agent copies follow it; the new original replaces the old one.
        self.assertEqual(folder_members(project / '.skills/example-skill'), copy_members)
        self.assertEqual(folder_members(project / copies), copy_members)
        self.assertEqual(folder_members(project / new_base), MERGE_V2)
        self.assertFalse((project / old_base).exists())
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['source']['commit'], entry['upstream'], entry['adopted']),
                         (second, {'tree_sha256': tree2, 'tracked_sha256': tree2, 'snapshot': new_base},
                          {'path': '.skills/example-skill', 'tree_sha256': merged_digest, 'receipt': receipt_path,
                           'kept_existing': False}))
        receipt = self.assert_receipt(project, receipt_path, 'accept-merged', {
            'source': {'repository': source.bare.as_posix(), 'path': 'skills/example-skill', 'requested_ref': second,
                       'commit': second},
            'upstream': {'tree_sha256': tree2, 'tracked_sha256': tree2, 'snapshot': new_base,
                         'files': manifest(MERGE_V2), 'litter': []},
            'previous': {'commit': first, 'tree_sha256': VECTOR_DIGEST, 'adopted_tree_sha256': VECTOR_DIGEST},
            'copy': {'path': '.skills/example-skill', 'tree_sha256': merged_digest, 'kept_existing': False,
                     'files': manifest(copy_members)},
            'definition': copy_definition, 'ownership': OWNERSHIP, 'decided': dict(DECIDED, merged_by=None),
            'agent_copies': {'agent': 'claude', 'note': NOTE_ADAPTER, 'removed': [],
                             'written': [f'{copies}/SKILL.md', f'{copies}/references/new.md'],
                             'kept': [f'{copies}/LICENSE.txt', f'{copies}/scripts/run.py']},
            'not_tracked': [], 'limits': RECEIPT_LIMITS}, head=git(project, 'rev-parse', 'HEAD'))
        self.assertNotIn('.skills/example-skill/SKILL.md', receipt['writes'])
        # AH-231: the opening clause names the update run that printed the three versions, and when.
        row = self.assert_intake(project, receipt)
        self.assertEqual(row['checked'], (
            f"the three versions were printed by update on {AT}; the merged copy is recorded as it stands with digest "
            f"{merged_digest[:12]} (who merged it was not stated); the upstream original's files were re-checked "
            f'against commit {second[:12]}; {definition_text}'))
        self.assertEqual(row['what'], f'skill package example-skill ({source.bare.as_posix()} skills/example-skill) '
                                      f'at commit {second}, package digest {tree2}')
        self.assert_registries(project)
        self.assertEqual(self.staging(), [])
        self.assert_parity(project)
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertIn(f'  copy: adapted (1 file(s) differ from the upstream original: SKILL.md)', out.splitlines())
        # The same merge again is Current.
        self.assertEqual(self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second, '--yes',
                                       *PUBLIC)[:2],
                         (0, f'Current: example-skill is at {second[:12]}, the commit {second} resolves to; nothing '
                             'to do.\n'))

    def test_a_merge_with_no_update_before_it_says_so(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        self.adapted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (MERGE_UPSTREAM, '100644')})
        (project / '.skills/example-skill/SKILL.md').write_bytes(MERGE_UPSTREAM + b'Our line.\n')
        code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second, '--yes',
                                       *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        digest = package_digest(folder_members(project / '.skills/example-skill'))
        merged = dict(VECTOR, **{'SKILL.md': (MERGE_UPSTREAM + b'Our line.\n', '100644')})
        states = definition(merged, 'example-skill')
        expected = ("no update run preceded this; the new upstream's manifest and its difference from the merged copy "
                    f"were printed by this command; the merged copy is recorded as it stands with digest "
                    f"{digest[:12]} (who merged it was not stated); the upstream original's files were re-checked "
                    f"against commit {second[:12]}; "
                    f"definition 1 of the merged copy: {states['format']}; {states['licence']}; {states['handoff']}")
        self.assertEqual(self.rows(project, ts.INTAKE)[-1]['checked'], expected)
        self.assert_parity(project)
        # A candidate show wrote is no update run either.
        self.commit(project, 'Record the merge')
        licence = b'Apache License 2.0\nWith a notice.\n'
        third = source.commit({'skills/example-skill/LICENSE.txt': (licence, '100644')})
        (project / '.skills/example-skill/LICENSE.txt').write_bytes(licence)
        candidate = self.show(project, source)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--candidate', candidate,
                                       '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertTrue(self.rows(project, ts.INTAKE)[-1]['checked'].startswith(
            "no update run preceded this; the new upstream's manifest and its difference from the merged copy were "
            'printed by this command; '))
        self.assertEqual(self.lock(project)['skills']['example-skill']['source']['commit'], third)
        self.assert_parity(project)

    def test_an_accept_merged_without_a_terminal_writes_a_candidate_that_claims_no_three_versions(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        self.adapted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (MERGE_UPSTREAM, '100644')})
        (project / '.skills/example-skill/SKILL.md').write_bytes(MERGE_UPSTREAM + b'Our line.\n')
        before = tree_state(project)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second,
                                       *PUBLIC, *WHO)
        self.assertEqual((code, err), (1, ''), out)
        self.assertEqual(tree_state(project), before)
        last = out.rstrip('\n').splitlines()[-1]
        candidate = Path(last.split(' --candidate ')[1][:-len(' --yes')])
        data = json.loads(candidate.read_text())
        self.assertEqual((data['created_by'], data['for']['operation']), ('update', 'update'))
        code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--candidate', candidate,
                                       '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertTrue(self.rows(project, ts.INTAKE)[-1]['checked'].startswith('no update run preceded this; '))
        self.assert_parity(project)

    def test_ah248_the_merge_is_credited_to_whoever_merged_by_names(self):
        """AH-238, AH-248: --merged-by names the merger; the execution agent, which runs the tool, never does."""
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        self.adapted(project)
        copy = project / '.skills/example-skill'

        def merge(members, note):
            """One upstream change, merged into the copy by hand and committed upstream; returns the new commit."""
            commit = source.commit(members, note)
            for member, (data, _) in members.items():
                target = copy / member.split('/', 2)[2]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data + (b'Our line.\n' if member.endswith('SKILL.md') else b''))
            return commit

        def recorded(commit, *extra):
            code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', commit,
                                           '--yes', *PUBLIC, *WHO, *extra)
            self.assertEqual((code, err), (0, ''), out)
            receipt = self.read(project, self.lock(project)['skills']['example-skill']['adopted']['receipt'])
            self.commit(project, 'Record the merge')
            return out, self.rows(project, ts.INTAKE)[-1], receipt, package_digest(folder_members(copy))[:12]

        # A person's merge, recorded by an agent: the person is named, the recording agent is not.
        second = merge({'skills/example-skill/SKILL.md': (MERGE_UPSTREAM, '100644')}, 'Upstream line')
        out, row, receipt, digest = recorded(second, '--merged-by', 'Bea Merger')
        self.assertIn('  merged by: Bea Merger\n', out)
        self.assertIn(f"; the merged copy, merged by Bea Merger under {BY}'s direction, is recorded as it stands with "
                      f'digest {digest}; ', row['checked'])
        self.assertNotIn(AGENT_ID, row['checked'])
        self.assertEqual((receipt['decided']['merged_by'], receipt['decided']['execution_agent'], row['admitted_by']),
                         ('Bea Merger', AGENT_ID, BY))
        # One agent's merge, recorded by another: the merging model is named, the recording one is not.
        licence = b'Apache License 2.0\nWith a notice.\n'
        third = merge({'skills/example-skill/LICENSE.txt': (licence, '100644')}, 'A licence notice')
        out, row, receipt, digest = recorded(third, '--merged-by', 'gpt-6-astra')
        self.assertIn(f"; the merged copy, merged by gpt-6-astra under {BY}'s direction, is recorded as it stands "
                      f'with digest {digest}; ', row['checked'])
        self.assertNotIn(AGENT_ID, row['checked'])
        self.assertEqual((receipt['decided']['merged_by'], receipt['decided']['execution_agent']),
                         ('gpt-6-astra', AGENT_ID))
        # Nobody named: the row says so, and credits neither the person nor the recording agent.
        fourth = merge({'skills/example-skill/references/new.md': (b'New notes.\n', '100644')}, 'New notes')
        out, row, receipt, digest = recorded(fourth)
        self.assertIn('  merged by: not stated\n', out)
        self.assertIn(f'; the merged copy is recorded as it stands with digest {digest} (who merged it was not '
                      "stated); the upstream original's files", row['checked'])
        self.assertNotIn('merged by', row['checked'])
        self.assertNotIn(f"{BY}'s", row['checked'])
        self.assertNotIn(AGENT_ID, row['checked'])
        self.assertEqual((receipt['decided']['merged_by'], receipt['decided']['execution_agent']), (None, AGENT_ID))
        self.assert_registries(project)
        # Only accept-merged takes the flag, and only its receipts carry merged_by.
        self.refused(self.run_main(project, 'update', 'example-skill', '--ref', fourth, '--yes', '--merged-by', 'X'),
                     2, '--merged-by applies to update --accept-merged only')
        self.refused(self.run_main(project, 'update', 'example-skill', '--detach', '--yes', '--merged-by', 'X'),
                     2, '--merged-by applies to update --accept-merged only')
        self.assertNotIn('merged_by', self.read(project, f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json')
                         ['decided'])

    def test_accept_merged_refusals(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/SKILL.md': (MERGE_UPSTREAM, '100644')})
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(skill_md('merged-name'))
        before = tree_state(project)
        self.refused(self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second, '--yes',
                                   *PUBLIC), 1,
                     'the package at .skills/example-skill is now named merged-name; a renamed package is another '
                     'skill: retire example-skill, then adopt merged-name')
        self.assertEqual(tree_state(project), before)
        copy.write_bytes(b'no front matter\n')
        self.refused(self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second, '--yes',
                                   *PUBLIC), 1,
                     'the package at .skills/example-skill is refused: SKILL.md has no front matter between --- lines')
        git(project, 'checkout', '--', '.skills')
        if os.name != 'nt':
            os.symlink('SKILL.md', project / '.skills/example-skill/link.md')
            self.refused(self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second,
                                       '--yes', *PUBLIC), 1,
                         'the package at .skills/example-skill is refused: link.md is a symbolic link')
            (project / '.skills/example-skill/link.md').unlink()
        self.assertEqual(self.run_main(project, 'update', 'example-skill', '--detach', '--yes')[0], 0)
        self.refused(self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', second, '--yes',
                                   *PUBLIC), 1,
                     'example-skill is detached: updates are not offered; status reports whether its source moved')
        self.assertEqual(self.staging(), [])


# update --detach and --attach (6.7) ---------------------------------------------------

class DetachTests(Fixture):
    def test_detach_then_attach(self):
        project = self.installed()
        source, first, adopt_receipt = self.adopted(project, via_show=True)
        before = tree_state(project)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--detach')
        detach_receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-detach.json'
        self.assertEqual((code, err), (1, ''))
        self.assertEqual(out, '\n'.join([
            'Plan: detach example-skill: later updates are not offered; status keeps reporting whether its source '
            'moved.', '  writes, in this order:', '    .trace-os/skills.lock.json (detached, detached_at, detached_by)',
            '    ai/SKILLS-IN-USE.md (the inventory section)', f'  then: the receipt {detach_receipt}',
            'Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, run: '
            f'{self.tool(project)} update example-skill --detach --yes', '']))
        self.assertEqual(tree_state(project), before)
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'update', 'example-skill', '--detach', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.splitlines()[-1], 'Detached example-skill. Nothing was committed or pushed.')
        self.assertEqual(seen, [[ts.LOCK, ts.INVENTORY]])
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['detached'], entry['detached_at'], entry['detached_by'], entry['receipts'],
                          entry['adopted']['receipt']),
                         (True, AT, BY, [adopt_receipt, detach_receipt], adopt_receipt))
        receipt = self.assert_receipt(project, detach_receipt, 'detach', {
            'source': None, 'upstream': None, 'previous': None, 'copy': None, 'definition': None, 'audience': None,
            'ownership': OWNERSHIP, 'agent_copies': {'agent': 'claude', 'written': [], 'kept': [], 'removed': [],
                                                     'note': NOTE_ADAPTER},
            'modes': {}, 'not_tracked': [], 'limits': RECEIPT_LIMITS}, head=git(project, 'rev-parse', 'HEAD'))
        self.assertEqual(sorted(receipt['writes']), [ts.LOCK, ts.INVENTORY])
        self.assertEqual(receipt['records']['intake'], None)
        self.assertEqual(len(self.rows(project, ts.INTAKE)), 1)
        self.assertEqual(self.section(project), inventory(adopted=[
            f'| example-skill | {source.bare.as_posix()} `skills/example-skill` | {first[:12]} | source public | yes '
            f'| `{adopt_receipt}` |']))
        self.assert_parity(project)
        self.assertEqual(self.run_main(project, 'update', 'example-skill', '--detach', '--yes'),
                         (0, 'Already detached: example-skill; nothing to do.\n', ''))
        # Detached: nothing is offered, and status says how far behind the source is.
        second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        before = tree_state(project)
        self.refused(self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC), 1,
                     'example-skill is detached: updates are not offered; status reports whether its source moved')
        self.assertEqual(tree_state(project), before)
        code, out, err = self.run_main(project, 'status')
        self.assertIn('  detached: yes', out.splitlines())
        self.assertIn(f'  upstream: behind {second[:12]}', out.splitlines())
        # A link refuses a detach receipt: it adopts no instruction.
        self.commit(project, 'Detach')
        self.refused(self.run_main(project, 'link', detach_receipt, '--commit', 'HEAD'), 1,
                     f'{detach_receipt} records a detach, which adopts no instruction; link records adoptions, '
                     'updates and accepted merges')
        # Attach again: the person's name comes from git config when --by is not given.
        attach_receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-attach.json'
        code, out, err = self.run_main(project, 'update', 'example-skill', '--attach', '--yes')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, '\n'.join([
            f'Plan: attach example-skill: updates are offered again from {source.bare.as_posix()} '
            'skills/example-skill.', '  writes, in this order:',
            '    .trace-os/skills.lock.json (detached false; detached_at and detached_by removed)',
            '    ai/SKILLS-IN-USE.md (the inventory section)', f'  then: the receipt {attach_receipt}',
            'Attached example-skill. Nothing was committed or pushed.', '']))
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual(entry['detached'], False)
        self.assertNotIn('detached_at', entry)
        self.assertNotIn('detached_by', entry)
        self.assertEqual(entry['receipts'], [adopt_receipt, detach_receipt, attach_receipt])
        attach = self.read(project, attach_receipt)
        self.assertEqual((attach['operation'], attach['decided']['by'], attach['records']['intake']),
                         ('attach', 'Test Researcher', None))
        self.assertIn('| no |', self.section(project))
        self.assert_parity(project)
        self.assertEqual(self.run_main(project, 'update', 'example-skill', '--attach', '--yes'),
                         (0, 'Already attached: example-skill; nothing to do.\n', ''))

    def test_detached_and_adapted_are_independent(self):
        project = self.installed()
        source, first, _ = self.adopted(project, via_show=True)
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'Our line.\n')
        self.commit(project, 'Adapt')
        for flag in ('--detach', '--attach'):
            self.assertEqual(self.run_main(project, 'update', 'example-skill', flag, '--yes')[0], 0)
            self.assertEqual(copy.read_bytes(), skill_md('example-skill') + b'Our line.\n')
        source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
        code, out, err = self.run_main(project, 'update', 'example-skill', *PUBLIC)
        self.assertEqual(code, 1)
        self.assertIn('example-skill is adapted here; the tool does not merge. The three versions:', out.splitlines())


# retire (6.8) ---------------------------------------------------------------------------

class RetireTests(Fixture):
    def test_retire_keeps_the_original(self):
        project = self.installed()
        source, first, adopt_receipt = self.adopted(project)
        (project / '.skills/example-skill/notes.local').write_text('ignored notes\n')
        snapshot = f'.trace-os/skill-bases/{VECTOR_DIGEST}'
        retire_receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-retire.json'
        before = tree_state(project)
        code, out, err = self.run_main(project, 'retire', 'example-skill', *WHO)
        plan = ['Plan: retire example-skill', '  writes, in this order:',
                '    3 file(s) removed under .skills/example-skill/',
                '    3 agent copies removed under .claude/skills/example-skill/, and their entries in '
                '.trace-os/install.json',
                f'    the original under {snapshot}/ is kept', '    .trace-os/install.json',
                '    .trace-os/skills.lock.json (the entry becomes retired)',
                '    ai/SKILLS-IN-USE.md (the inventory section)',
                f'  then: the receipt {retire_receipt}; no intake row and no transition row',
                'Git history keeps every removed file once the removal is committed; files Git ignores under '
                '.skills/example-skill/ are left in place: .skills/example-skill/notes.local']
        self.assertEqual((code, err), (1, ''))
        self.assertEqual(out.splitlines()[:-1], plan)
        self.assertTrue(out.splitlines()[-1].endswith(f"run: {self.tool(project)} retire example-skill --by 'Ann "
                                                      f"Author' --words 'yes, take it' --authority 'T3 of "
                                                      f"ai/records/20261001-lifecycle' --execution-agent {AGENT_ID} "
                                                      '--yes'), out)
        self.assertEqual(tree_state(project), before)
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'retire', 'example-skill', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.splitlines(), plan + ['Retired example-skill.', f'Original kept at {snapshot}.',
                                                   'Nothing was committed or pushed.'])
        copies = '.claude/skills/example-skill'
        self.assertEqual(seen, [[*[f'.skills/example-skill/{path}' for path in sorted(VECTOR)],
                                 *[f'{copies}/{path}' for path in sorted(VECTOR)],
                                 '.trace-os/install.json', ts.LOCK, ts.INVENTORY]])
        # The copy and its agent copies are gone, the ignored file stays, the original stays.
        self.assertEqual([path.name for path in (project / '.skills/example-skill').rglob('*')], ['notes.local'])
        self.assertFalse((project / copies).exists())
        self.assertEqual(folder_members(project / snapshot), VECTOR)
        self.assertEqual([name for name in self.read(project, bs.STATE)['files'] if 'example-skill' in name], [])
        # The lock (4.4) and the receipt (5.1).
        self.assertEqual(self.lock(project)['skills']['example-skill'], {
            'manager': 'retired', 'was': 'external',
            'source': {'repository': source.bare.as_posix(), 'path': 'skills/example-skill', 'requested_ref': first,
                       'commit': first},
            'held_by': None, 'upstream': {'tree_sha256': VECTOR_DIGEST, 'snapshot': snapshot, 'dropped': False},
            'retired_at': AT, 'receipt': retire_receipt, 'receipts': [adopt_receipt, retire_receipt]})
        receipt = self.assert_receipt(project, retire_receipt, 'retire', {
            'source': {'repository': source.bare.as_posix(), 'path': 'skills/example-skill', 'requested_ref': first,
                       'commit': first},
            'upstream': {'tree_sha256': VECTOR_DIGEST, 'tracked_sha256': VECTOR_DIGEST, 'snapshot': snapshot,
                         'files': manifest(VECTOR), 'litter': []},
            'previous': None, 'copy': None, 'definition': None, 'audience': None, 'ownership': OWNERSHIP,
            'agent_copies': {'agent': 'claude', 'written': [], 'kept': [], 'note': NOTE_ADAPTER,
                             'removed': [f'{copies}/{path}' for path in sorted(VECTOR)]},
            'history': HISTORY, 'left_in_place': ['.skills/example-skill/notes.local'], 'modes': {},
            'not_tracked': [], 'limits': RECEIPT_LIMITS + [RETIRE_LIMIT]},
            head=git(project, 'rev-parse', 'HEAD'))
        self.assertEqual(receipt['records']['intake'], None)
        self.assertEqual(receipt['writes']['.skills/example-skill/scripts/run.py']['after'], None)
        # No intake row and no transition row (A10).
        self.assertEqual(len(self.rows(project, ts.INTAKE)), 1)
        self.assertFalse((project / ts.TRANSITIONS).exists())
        self.assertEqual(self.section(project), inventory(retired=[
            f'| example-skill | adopted | {source.bare.as_posix()} `skills/example-skill` at {first[:12]} | '
            f'`.trace-os/skill-bases/{VECTOR_DIGEST[:12]}...` | `{retire_receipt}` |']))
        self.assert_parity(project)
        self.assertEqual(self.run_main(project, 'retire', 'example-skill', '--yes'),
                         (0, 'Already retired: example-skill; nothing to do.\n', ''))
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertIn(f'  example-skill: was adopted from {source.bare.as_posix()} skills/example-skill at '
                      f'{first[:12]}; original kept at {snapshot}; receipt {retire_receipt}', out.splitlines())
        self.commit(project, 'Retire example-skill')
        self.refused(self.run_main(project, 'link', retire_receipt, '--commit', 'HEAD'), 1,
                     f'{retire_receipt} records a retire, which adopts no instruction; link records adoptions, '
                     'updates and accepted merges')

    def test_retire_with_drop_base(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        retire_receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-retire.json'
        snapshot = f'.trace-os/skill-bases/{VECTOR_DIGEST}'
        code, out, err = self.run_main(project, 'retire', 'example-skill', '--drop-base', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''))
        self.assertIn(f'    3 file(s) of the original removed under {snapshot}/ (--drop-base)', out.splitlines())
        self.assertEqual(out.splitlines()[-3:], ['Retired example-skill.', 'Original dropped; Git history keeps it.',
                                                 'Nothing was committed or pushed.'])
        self.assertFalse((project / snapshot).exists())
        self.assertTrue((project / ts.BASES).is_dir())
        self.assertFalse((project / '.skills/example-skill').exists())
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual(entry['upstream'], {'tree_sha256': VECTOR_DIGEST, 'snapshot': snapshot, 'dropped': True})
        receipt = self.read(project, retire_receipt)
        self.assertEqual(sorted(path for path, item in receipt['writes'].items() if path.startswith(snapshot)),
                         [f'{snapshot}/{path}' for path in sorted(VECTOR)])
        self.assertEqual(self.section(project), inventory(retired=[
            f'| example-skill | adopted | {source.bare.as_posix()} `skills/example-skill` at {first[:12]} | '
            f'no (dropped; Git history keeps it) | `{retire_receipt}` |']))
        self.assert_parity(project)
        # A later adoption of the name carries every receipt forward (4.4).
        self.commit(project, 'Retire example-skill')
        code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example-skill', '--ref', first, '--yes',
                                       *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual(entry['receipts'], [f'.trace-os/skill-adoptions/{STAMP}-example-skill-adopt.json',
                                             retire_receipt, f'.trace-os/skill-adoptions/{STAMP}-example-skill-'
                                                             'adopt-2.json'])
        self.assertEqual(folder_members(project / snapshot), VECTOR)
        self.assert_parity(project)

    def test_ah235_drop_base_leaves_an_ignored_uncommitted_file_under_the_original(self):
        """AH-235: --drop-base removes only what Git history keeps; an ignored file under the original stays."""
        project = self.installed()
        source, first, _ = self.adopted(project)
        snapshot = f'.trace-os/skill-bases/{VECTOR_DIGEST}'
        notes = project / snapshot / 'notes.local'
        notes.write_text('ignored notes, never committed\n')
        self.assertEqual(git(project, 'status', '--porcelain', '--', snapshot), '', 'the file is not ignored')
        retire_receipt = f'.trace-os/skill-adoptions/{STAMP}-example-skill-retire.json'
        code, out, err = self.run_main(project, 'retire', 'example-skill', '--drop-base', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn(f'    3 file(s) of the original removed under {snapshot}/ (--drop-base)', out.splitlines())
        self.assertIn('Git history keeps every removed file once the removal is committed; files Git ignores under '
                      f'.skills/example-skill/ and {snapshot}/ are left in place: {snapshot}/notes.local',
                      out.splitlines())
        self.assertEqual(notes.read_text(), 'ignored notes, never committed\n')
        self.assertEqual([path.name for path in (project / snapshot).rglob('*')], ['notes.local'])
        receipt = self.read(project, retire_receipt)
        self.assertEqual((receipt['history'], receipt['left_in_place']), (HISTORY, [f'{snapshot}/notes.local']))
        self.assertEqual(sorted(path for path in receipt['writes'] if path.startswith(snapshot)),
                         [f'{snapshot}/{path}' for path in sorted(VECTOR)])
        # Every file the receipt says it removed is in the commit before the retirement, so history keeps it.
        for path, entry in receipt['writes'].items():
            if entry['after'] is None:
                self.assertEqual(sha(subprocess.run(['git', 'show', f'HEAD:{path}'], cwd=project, capture_output=True,
                                                    check=True).stdout), entry['before'], path)
        self.assertTrue(self.lock(project)['skills']['example-skill']['upstream']['dropped'])
        self.assert_parity(project)

    def test_retire_refusals(self):
        project = self.installed()
        self.adopted(project)
        before = tree_state(project)
        for argv, text in (
                (('nothing',), 'no skill named nothing is adopted here'),
                (('example',), 'example is a skill the kit ships; retire it as SETUP-AGENT.md describes: delete '
                               '.skills/example/ and run python3 scripts/bootstrap.py --sync-skills')):
            self.refused(self.run_main(project, 'retire', *argv, '--yes'), 1, text)
        self.assertEqual(tree_state(project), before)
        plain = self.uninstalled()
        self.refused(self.run_main(plain, 'retire', 'own', '--yes'), 1,
                     "own is the project's own skill; this tool updates and retires only skills it adopted or "
                     'declared')
        self.refused(self.run_main(plain, 'retire', 'legacy', '--yes'), 1,
                     "legacy is the project's own skill; this tool updates and retires only skills it adopted or "
                     'declared')
        # N22: a change Git has not recorded, in the copy or the original, tracked or not.
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'Not committed.\n')
        (project / '.skills/example-skill/new.md').write_text('untracked\n')
        base = project / f'.trace-os/skill-bases/{VECTOR_DIGEST}/LICENSE.txt'
        base.write_bytes(b'Edited.\n')
        before = tree_state(project)
        self.refused(self.run_main(project, 'retire', 'example-skill', '--yes'), 1,
                     'retiring example-skill would delete changes Git has not recorded (.skills/example-skill/SKILL.md, '
                     f'.skills/example-skill/new.md, .trace-os/skill-bases/{VECTOR_DIGEST}/LICENSE.txt); commit them '
                     'first, so that history keeps them')
        self.assertEqual(tree_state(project), before)
        git(project, 'checkout', '--', '.')
        (project / '.skills/example-skill/new.md').unlink()
        # N23: an agent copy edited separately.
        agent_copy = project / '.claude/skills/example-skill/SKILL.md'
        agent_copy.write_bytes(b'Edited separately.\n')
        before = tree_state(project)
        self.refused(self.run_main(project, 'retire', 'example-skill', '--yes'), 1,
                     'a generated copy of example-skill was edited separately (.claude/skills/example-skill/SKILL.md); '
                     'merge or remove it, then run this again')
        self.assertEqual(tree_state(project), before)

    def test_an_interrupted_retire_is_undone_with_its_modes(self):
        project = self.installed()
        self.adopted(project)
        before = tree_state(project)
        operation = f'{STAMP}-example-skill-retire'
        with self.interrupt_at(project, lambda relative, data: relative == ts.LOCK):
            code, out, err = self.run_main(project, 'retire', 'example-skill', '--yes')
        self.assertEqual(code, 130)
        journal = self.read(project, ts.OPERATION)
        self.assertEqual(journal['modes_before'], {
            '.claude/skills/example-skill/scripts/run.py': '100755', '.skills/example-skill/scripts/run.py': '100755'})
        self.assertEqual((journal['intake'], journal['prune'][0]), (None, '.claude/skills/example-skill/LICENSE.txt'))
        self.assertEqual(self.run_main(project, 'recover'), (
            0, f'Undone: {operation}; the project is as it was before. Run the command again to retire.\n', ''))
        self.assertEqual(tree_state(project), before)
        self.assertTrue(executable(project / '.skills/example-skill/scripts/run.py'))
        self.assert_parity(project)

    def test_retire_a_declaration(self):
        project = self.installed()
        self.assertEqual(self.run_main(project, 'declare', 'held-skill', '--held-by', 'Alex Holder', '--yes',
                                       *WHO)[0], 0)
        declare_receipt = f'.trace-os/skill-adoptions/{STAMP}-held-skill-declare.json'
        retire_receipt = f'.trace-os/skill-adoptions/{STAMP}-held-skill-retire.json'
        code, out, err = self.run_main(project, 'retire', 'held-skill', '--drop-base', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.splitlines(), [
            'Plan: retire held-skill', '  writes, in this order:',
            '    .trace-os/skills.lock.json (the entry becomes retired)', '    ai/SKILLS-IN-USE.md (the inventory section)',
            f'  then: the receipt {retire_receipt}; no intake row and no transition row', 'Retired held-skill.',
            'Nothing was committed or pushed.'])
        self.assertEqual(self.lock(project)['skills']['held-skill'], {
            'manager': 'retired', 'was': 'outside', 'source': None, 'held_by': 'Alex Holder', 'upstream': None,
            'retired_at': AT, 'receipt': retire_receipt, 'receipts': [declare_receipt, retire_receipt]})
        self.assert_receipt(project, retire_receipt, 'retire', {
            'source': None, 'upstream': None, 'copy': None, 'history': HISTORY, 'left_in_place': [],
            'ownership': dict(OWNERSHIP, before='outside'),
            'agent_copies': {'agent': 'claude', 'written': [], 'kept': [], 'removed': [], 'note': NOTE_ADAPTER},
            'limits': RECEIPT_LIMITS + [RETIRE_LIMIT]}, head=git(project, 'rev-parse', 'HEAD'))
        self.assertEqual(self.section(project), inventory(retired=[
            f'| held-skill | declared | held by Alex Holder | not applicable | `{retire_receipt}` |']))
        self.assert_parity(project)


# declare (6.9) -------------------------------------------------------------------------

HELD = {'SKILL.md': (skill_md('held-skill', 'A private method.\n'), '100644'),
        'scripts/x.py': (b'print(1)\n', '100755')}


class StateFolderMoveTests(Fixture):
    """AH-281: a lock written before an update that moved the state folder names its originals under the earlier one.

    The update to 0.1.0-rc19 renames .trace-kit/ to .traceware/ and the update
    to 0.1.0-rc20 renames either to .trace-os/, each leaving the lock as it was,
    so every stored original is read, compared and removed where it is now.
    ROUTE is the folders the state passed through, the one the skill was adopted
    in first; the subclasses below run the same tests for each earlier name.
    """

    ROUTE = ('.trace-kit', '.trace-os')
    BASE = f'.trace-os/skill-bases/{VECTOR_DIGEST}'

    def moved(self, name):
        """A skill adopted while the state was in an earlier folder, then the folder renamed as each update does."""
        earlier = self.ROUTE[0]
        ignored = ''.join(line.replace('.trace-os/', earlier + '/') + '\n'
                          for line in IGNORED.splitlines() if line.startswith('.trace-os/'))

        def as_before_the_move(project):
            os.rename(project / '.trace-os', project / earlier)
            with (project / '.gitignore').open('a') as stream:
                stream.write(ignored)

        project = self.installed(name=name, before_commit=as_before_the_move)
        source, first, receipt = self.adopted(project)
        lock = json.loads((project / earlier / 'skills.lock.json').read_text())
        self.assertEqual(lock['skills']['example-skill']['upstream']['snapshot'],
                         f'{earlier}/skill-bases/{VECTOR_DIGEST}')
        for old, new in zip(self.ROUTE, self.ROUTE[1:]):
            os.rename(project / old, project / new)
            self.commit(project, f'An update moves the state folder from {old}/ to {new}/')
        return project, source, first

    def test_ah281_the_original_is_read_and_compared_where_it_is_after_the_move(self):
        project, source, first = self.moved('moved-compare')
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn('  copy: unchanged\n', out)
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'Our line.\n')
        self.commit(project, 'Adapt example-skill')
        second = source.commit(MERGE_CHANGE, 'Upstream moves on')
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (1, ''), out)
        lines = out.splitlines()
        self.assertIn(f'  original (what was adopted): {self.BASE}', lines)
        # The differences are made from the original's own bytes, not from its manifest alone.
        self.assertIn('+Our line.', lines)
        self.assertIn('+Upstream line \\x1b[31mred.', lines)

    def test_ah281_retire_drop_base_removes_the_moved_original_and_says_so_truly(self):
        project, source, first = self.moved('moved-retire')
        code, out, err = self.run_main(project, 'retire', 'example-skill', '--drop-base', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn(f'    3 file(s) of the original removed under {self.BASE}/ (--drop-base)', out.splitlines())
        self.assertFalse((project / self.BASE).exists())
        for old in self.ROUTE[:-1]:
            self.assertFalse((project / old).exists())
        entry = json.loads((project / '.trace-os/skills.lock.json').read_text())['skills']['example-skill']
        self.assertEqual(entry['upstream'], {'tree_sha256': VECTOR_DIGEST, 'snapshot': self.BASE, 'dropped': True})
        receipt = self.read(project, entry['receipt'])
        self.assertEqual(sorted(path for path, item in receipt['writes'].items() if item['after'] is None
                                and path.startswith(self.BASE)), [f'{self.BASE}/{path}' for path in sorted(VECTOR)])
        self.assert_parity(project)

    def test_ah281_an_update_removes_the_moved_original_it_replaces(self):
        project, source, first = self.moved('moved-update')
        second = source.commit(MERGE_CHANGE, 'Upstream moves on')
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn(f'    {len(VECTOR)} file(s) of the old original removed from {self.BASE}/', out.splitlines())
        self.assertFalse((project / self.BASE).exists())
        self.assertEqual(folder_members(project / f'.trace-os/skill-bases/{package_digest(MERGE_V2)}'), MERGE_V2)
        self.assert_parity(project)



class StateFolderMoveFromTracewareTests(StateFolderMoveTests):
    ROUTE = ('.traceware', '.trace-os')


class StateFolderTwoMovesTests(StateFolderMoveTests):
    ROUTE = ('.trace-kit', '.traceware', '.trace-os')


class DeclareTests(Fixture):
    def held(self, members=HELD, name='held-skill'):
        folder = self.home / '.claude/skills' / name
        for path, (data, mode) in members.items():
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_bytes(data)
            os.chmod(folder / path, 0o755 if mode == '100755' else 0o644)
        return folder

    def test_declare_records_an_attested_digest_and_copies_nothing(self):
        project = self.installed()
        folder = self.held()
        digest = package_digest(HELD if os.name != 'nt' else {path: (data, '100644') for path, (data, _) in
                                                              HELD.items()})
        state = (project / bs.STATE).read_bytes()
        receipt_path = f'.trace-os/skill-adoptions/{STAMP}-held-skill-declare.json'
        plan = ['Plan: declare held-skill as held outside the project by Alex Holder',
                '  path: ~/.claude/skills/held-skill', f'  digest: {digest} (attested)',
                f'  limitation: {DEFAULT_LIMITATION}', '  writes, in this order:', '    .trace-os/skills.lock.json',
                '    ai/SKILLS-IN-USE.md (the inventory section)',
                f'  then: the receipt {receipt_path}; no intake row, since nothing enters the project, and no agent '
                'copies']
        before = tree_state(project)
        code, out, err = self.run_main(project, 'declare', 'held-skill', '--held-by', 'Alex Holder', '--path', folder)
        self.assertEqual((code, err), (1, ''))
        self.assertEqual(out.splitlines(), plan + [
            'Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, run: '
            f"{self.tool(project)} declare held-skill --held-by 'Alex Holder' --path {folder.as_posix()} --yes"])
        self.assertEqual(tree_state(project), before)
        with self.transactions() as seen:
            code, out, err = self.run_main(project, 'declare', 'held-skill', '--held-by', 'Alex Holder', '--path',
                                           folder, '--yes', *WHO)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.splitlines(), plan + ['Declared held-skill, held by Alex Holder. Nothing was committed '
                                                   'or pushed.'])
        self.assertEqual(seen, [[ts.LOCK, ts.INVENTORY]])
        # The lock (4.3): the home folder written ~, the digest attested.
        self.assertEqual(self.lock(project)['skills']['held-skill'], {
            'manager': 'outside', 'held_by': 'Alex Holder', 'path': '~/.claude/skills/held-skill',
            'digest': {'tree_sha256': digest, 'basis': 'attested'}, 'limitation': DEFAULT_LIMITATION,
            'declared_by': BY, 'receipt': receipt_path, 'receipts': [receipt_path]})
        self.assertNotIn(str(self.home), (project / ts.LOCK).read_text())
        receipt = self.assert_receipt(project, receipt_path, 'declare', {
            'source': None, 'upstream': None, 'previous': None, 'copy': None, 'definition': None, 'audience': None,
            'ownership': dict(OWNERSHIP, before='free'),
            'agent_copies': {'agent': 'claude', 'written': [], 'kept': [], 'removed': [], 'note': NOTE_ADAPTER},
            'modes': {}, 'not_tracked': [], 'limits': RECEIPT_LIMITS}, head=git(project, 'rev-parse', 'HEAD'))
        self.assertEqual((receipt['records']['intake'], sorted(receipt['writes'])), (None, [ts.LOCK, ts.INVENTORY]))
        self.assertFalse((project / ts.INTAKE).exists())
        self.assertEqual((project / bs.STATE).read_bytes(), state)
        self.assertFalse((project / '.skills/held-skill').exists())
        self.assertFalse((project / '.claude/skills/held-skill').exists())
        self.assertEqual(self.section(project), inventory(relied=[
            f'| held-skill | Alex Holder | {digest[:12]} (attested) | {DEFAULT_LIMITATION} |']))
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertIn('\n'.join(['held-skill', f'  owner: outside, held by Alex Holder; digest {digest[:12]} (attested)',
                                 f'  limitation: {DEFAULT_LIMITATION}', f'  receipt: {receipt_path}']), out)
        self.assert_parity(project)
        self.assertEqual(self.run_main(project, 'declare', 'held-skill', '--held-by', 'Alex Holder', '--path', folder,
                                       '--yes'), (0, 'Already declared: held-skill; nothing to do.\n', ''))
        # The same holder with other details, or another holder: retire the declaration first (N4).
        for argv in (('--held-by', 'Alex Holder', '--limitation', 'Other words.'), ('--held-by', 'Someone Else')):
            self.refused(self.run_main(project, 'declare', 'held-skill', *argv, '--yes'), 1,
                         'held-skill is declared as held outside the project by Alex Holder; retire the declaration '
                         f'first: {self.tool(project)} retire held-skill')
        # adopt refuses the declared name with the same message (N4).
        source = self.source()
        commit = source.commit(package('held-skill', HELD))
        self.refused(self.run_main(project, 'adopt', source.bare, 'skills/held-skill', '--ref', commit, '--yes',
                                   *PUBLIC), 1,
                     'held-skill is declared as held outside the project by Alex Holder; retire the declaration '
                     f'first: {self.tool(project)} retire held-skill')

    def test_declare_without_a_path(self):
        project = self.installed('none')
        code, out, err = self.run_main(project, 'declare', 'held-skill', '--held-by', 'Alex | Holder', '--limitation',
                                       'Ask Alex\nfor a run.', '--yes')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('  path: not stated\n  digest not stated\n', out)
        entry = self.lock(project)['skills']['held-skill']
        self.assertEqual((entry['path'], entry['digest'], entry['limitation'], entry['declared_by']),
                         (None, {'tree_sha256': None, 'basis': 'not stated'}, 'Ask Alex\nfor a run.',
                          'Test Researcher'))
        receipt = self.read(project, entry['receipt'])
        self.assertEqual(receipt['agent_copies'], {'agent': 'none', 'written': [], 'kept': [], 'removed': [],
                                                   'note': NOTE_NONE})
        self.assertEqual(self.section(project), inventory(relied=[
            '| held-skill | Alex \\| Holder | not stated | Ask Alex for a run. |']))
        _, problems = check_skills.check(project)
        self.assertEqual(problems, [])

    def test_declare_refusals(self):
        project = self.installed()
        self.adopted(project)
        before = tree_state(project)
        folder = self.held()
        cases = [
            (('Bad_Name', '--held-by', 'A'), 2,
             'Bad_Name is not a valid skill name (1 to 64 lowercase letters, digits and single hyphens)'),
            (('example', '--held-by', 'A'), 1, 'example is already a skill of this project (kit); declare records only '
                                               'a skill that is not in the project'),
            (('example-skill', '--held-by', 'A'), 1, 'example-skill is already a skill of this project (external); '
                                                     'declare records only a skill that is not in the project'),
            (('held-skill', '--held-by', 'A', '--path', self.root / 'missing'), 2,
             f"--path {(self.root / 'missing').as_posix()} is not a folder holding a SKILL.md"),
            (('held-skill', '--held-by', 'A', '--path', folder / 'scripts'), 2,
             f"--path {(folder / 'scripts').as_posix()} is not a folder holding a SKILL.md"),
            (('other-skill', '--held-by', 'A', '--path', folder), 1,
             f'the skill at {folder.as_posix()} is named held-skill, not other-skill')]
        for argv, code, text in cases:
            with self.subTest(argv=argv):
                self.refused(self.run_main(project, 'declare', *argv, '--yes'), code, text)
        if os.name != 'nt':
            os.symlink('SKILL.md', folder / 'linked.md')
            self.refused(self.run_main(project, 'declare', 'held-skill', '--held-by', 'A', '--path', folder, '--yes'),
                         1, f'the package at {folder.as_posix()} is refused: linked.md is a symbolic link')
        self.assertEqual(tree_state(project), before)
        plain = self.uninstalled()
        # AH-252: for the project's own skill, N28 says how to record a personal skill that skill sends agents to.
        for name in ('own', 'legacy'):
            self.refused(self.run_main(plain, 'declare', name, '--held-by', 'A', '--yes'), 1,
                         f'{name} is already a skill of this project (project); declare records only a skill that is '
                         'not in the project; if that skill only says to use a personal copy, remove it first and '
                         'declare the personal skill; if it holds rules of its own and relies on a personal copy too, '
                         "declare the personal skill with --beside-project-skill, which keeps the project's skill as "
                         'it is')

    def test_ah260_a_project_skill_and_its_personal_namesake_are_both_recorded(self):
        """AH-260: --beside-project-skill declares a personal skill the project's own same-named skill relies on."""
        project = self.uninstalled()
        own = (project / '.skills/own/SKILL.md').read_bytes()
        receipt_path = f'.trace-os/skill-adoptions/{STAMP}-own-declare.json'
        code, out, err = self.run_main(project, 'declare', 'own', '--held-by', 'Alex Holder',
                                       '--beside-project-skill', '--yes', *WHO)
        self.assertEqual((code, err), (0, ''))
        self.assertIn("  beside: the project's own .skills/own/ stays as it is and relies on this personal skill; "
                      'nothing of it is read or changed\n', out)
        self.assertEqual((project / '.skills/own/SKILL.md').read_bytes(), own)
        entry = self.lock(project)['skills']['own']
        self.assertEqual((entry['manager'], entry['held_by'], entry['beside_project']), ('outside', 'Alex Holder', True))
        self.assertEqual(self.read(project, receipt_path)['ownership']['before'], 'project')
        self.assertEqual(self.section(project), inventory(relied=[
            f"| own (relied on by the project's own .skills/own) | Alex Holder | not stated | {DEFAULT_LIMITATION} |"]))
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertNotIn('conflict', out)
        block = out.split('\nown\n', 1)[1].split('\n\n', 1)[0].splitlines()
        self.assertEqual(block[0], '  owner: project (.skills/own/)')
        self.assertEqual(block[-3:], ['  relies on: a personal skill of the same name, held by Alex Holder; '
                                      'digest not stated', f'  limitation: {DEFAULT_LIMITATION}',
                                      f'  receipt: {receipt_path}'])
        self.assertEqual(self.run_main(project, 'declare', 'own', '--held-by', 'Alex Holder',
                                       '--beside-project-skill', '--yes'),
                         (0, 'Already declared: own; nothing to do.\n', ''))
        # A single-file project skill takes the flag too; a name the project does not hold does not (N29).
        self.assertEqual(self.run_main(project, 'declare', 'legacy', '--held-by', 'A', '--beside-project-skill',
                                       '--yes')[0], 0)
        self.refused(self.run_main(project, 'declare', 'held-skill', '--held-by', 'A', '--beside-project-skill',
                                   '--yes'), 2,
                     '--beside-project-skill needs a skill of the project named held-skill (.skills/held-skill/ or '
                     '.skills/held-skill.md); there is none, so declare it without the flag')
        # Retiring the declaration leaves the project's skill where it is.
        self.assertEqual(self.run_main(project, 'retire', 'own', '--yes')[0], 0)
        self.assertEqual((project / '.skills/own/SKILL.md').read_bytes(), own)

    def test_a_declaration_after_a_retirement_carries_the_receipts(self):
        project = self.uninstalled()
        first = f'.trace-os/skill-adoptions/{STAMP}-held-skill-declare.json'
        retired = f'.trace-os/skill-adoptions/{STAMP}-held-skill-retire.json'
        for argv in (('declare', 'held-skill', '--held-by', 'A', '--yes'), ('retire', 'held-skill', '--yes'),
                     ('declare', 'held-skill', '--held-by', 'B', '--yes')):
            self.assertEqual(self.run_main(project, *argv)[0], 0, argv)
        entry = self.lock(project)['skills']['held-skill']
        self.assertEqual((entry['held_by'], entry['receipts']),
                         ('B', [first, retired, f'.trace-os/skill-adoptions/{STAMP}-held-skill-declare-2.json']))
        receipt = self.read(project, entry['receipt'])
        self.assertEqual((receipt['ownership']['before'], receipt['agent_copies'], receipt['project']['installed']),
                         ('retired', {'agent': None, 'written': [], 'kept': [], 'removed': [], 'note': NOTE_NO_STATE},
                          False))


# What a receipt promises about the transition row (5.1) ----------------------------------

class ReceiptTests(Fixture):
    def test_ah240_receipts_that_adopt_nothing_promise_no_transition_row(self):
        """AH-240: detach, attach, retire and declare adopt no instruction, so their receipts promise no row."""
        project = self.installed()
        source, first, adopt_receipt = self.adopted(project, via_show=True)
        for argv in (('update', 'example-skill', '--detach'), ('update', 'example-skill', '--attach'),
                     ('retire', 'example-skill'), ('declare', 'held-skill', '--held-by', 'Alex Holder')):
            self.assertEqual(self.run_main(project, *argv, '--yes', *WHO)[0], 0, argv)
        lock = self.lock(project)
        receipts = lock['skills']['example-skill']['receipts'] + lock['skills']['held-skill']['receipts']
        found = {self.read(project, path)['operation']: self.read(project, path)['records']['transition']
                 for path in receipts}
        self.assertEqual(found, {'adopt': TRANSITION, 'detach': NO_TRANSITION, 'attach': NO_TRANSITION,
                                 'retire': NO_TRANSITION, 'declare': NO_TRANSITION})
        # The receipts and link agree: link refuses each of them (L2), and writes the row only for the adoption.
        self.commit(project, 'Detach, attach, retire and declare')
        for path in receipts[1:]:
            operation = self.read(project, path)['operation']
            self.refused(self.run_main(project, 'link', path, '--commit', 'HEAD'), 1,
                         f'{path} records a {operation}, which adopts no instruction; link records adoptions, '
                         'updates and accepted merges')
        self.assertFalse((project / ts.TRANSITIONS).exists())


# The operation journal (12.2, A2) -------------------------------------------------------

class JournalTests(Fixture):
    def test_the_operation_journal_lives_in_the_ignored_receipts_folder(self):
        """A2: the journal is .trace-os/receipts/skill-operation.json, which Git ignores; the folder is made and kept."""
        self.assertEqual(ts.OPERATION, '.trace-os/receipts/skill-operation.json')
        for kind in ('installed', 'uninstalled'):
            with self.subTest(project=kind):
                project = self.installed(name='journal-installed') if kind == 'installed' else \
                    self.uninstalled(name='journal-plain')
                receipts = project / '.trace-os/receipts'
                # bootstrap's own receipts are there in an installed project; a project with no install has none.
                before = sorted(path.name for path in receipts.iterdir()) if receipts.is_dir() else []
                self.assertEqual(bool(before), kind == 'installed')
                operation = f'{STAMP}-held-skill-declare'
                with self.interrupt_at(project, lambda relative, data: relative.startswith(ts.RECEIPTS + '/')):
                    code, out, err = self.run_main(project, 'declare', 'held-skill', '--held-by', 'A', '--yes')
                self.assertEqual(code, 130, err)
                self.assertEqual(json.loads((project / ts.OPERATION).read_text())['operation_id'], operation)
                self.assertEqual(git(project, 'check-ignore', ts.OPERATION), ts.OPERATION)
                self.assertNotIn('skill-operation', git(project, 'status', '--porcelain', '--untracked-files=all'))
                self.assertEqual(self.run_main(project, 'recover'), (
                    0, f'Completed: {operation}; receipt .trace-os/skill-adoptions/{operation}.json.\n', ''))
                self.assertFalse((project / ts.OPERATION).exists())
                self.assertTrue(receipts.is_dir(), 'the receipts folder was removed with the journal')
                self.assertEqual(sorted(path.name for path in receipts.iterdir()), before)


# Modes: AH-230 (a mode-only change), AH-233 (a rewritten executable), AH-234 (Windows) -----

@unittest.skipIf(os.name == 'nt', 'Windows keeps no executable bit')
class ModeTests(Fixture):
    def test_a_mode_only_change_both_ways_through_an_interruption_and_recover(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        plain = dict(VECTOR, **{'scripts/run.py': (RUN, '100644')})
        second = source.commit({'skills/example-skill/scripts/run.py': (RUN, '100644')}, 'Not executable')
        third = source.commit({'skills/example-skill/scripts/run.py': (RUN, '100755')}, 'Executable again')
        copy = project / '.skills/example-skill/scripts/run.py'
        agent_copy = project / '.claude/skills/example-skill/scripts/run.py'
        for ref, target, old, new in ((second, '100644', VECTOR_DIGEST, package_digest(plain)),
                                      (third, '100755', package_digest(plain), VECTOR_DIGEST)):
            with self.subTest(target=target):
                before_mode = executable(copy)
                code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', ref, *PUBLIC)
                self.assertIn('    0 file(s) written, 0 removed and 1 mode change(s) under .skills/example-skill/',
                              out.splitlines())
                self.assertIn('  mode scripts/run.py', out.splitlines())
                # Interrupted after the transaction, before the forward steps: the bytes are in place, the mode not.
                with self.interrupt_at(project, lambda relative, data: relative == ts.OPERATION
                                       and json.loads(data)['state'] == 'applied'):
                    code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', ref, '--yes',
                                                   *PUBLIC, *WHO)
                self.assertEqual(code, 130, err)
                journal = self.read(project, ts.OPERATION)
                self.assertEqual(journal['state'], 'applying')
                self.assertNotIn('.skills/example-skill/scripts/run.py', journal['writes'])
                self.assertEqual({path: mode for path, mode in journal['modes_after'].items()
                                  if path.endswith('example-skill/scripts/run.py')},
                                 {'.claude/skills/example-skill/scripts/run.py': target,
                                  '.skills/example-skill/scripts/run.py': target})
                self.assertEqual(journal['modes_before']['.skills/example-skill/scripts/run.py'],
                                 '100755' if before_mode else '100644')
                self.assertEqual(executable(copy), before_mode, 'the mode changed before the forward step ran')
                code, out, err = self.run_main(project, 'recover')
                self.assertEqual((code, err), (0, ''))
                self.assertTrue(out.startswith(f"Completed: {journal['operation_id']}; receipt "), out)
                for path in (copy, agent_copy):
                    self.assertEqual((path.read_bytes(), executable(path)), (RUN, target == '100755'), path)
                entry = self.lock(project)['skills']['example-skill']
                self.assertEqual(entry['upstream']['tree_sha256'], new)
                self.assertFalse((project / ts.BASES / old).exists())
                snapshot_run = project / entry['upstream']['snapshot'] / 'scripts/run.py'
                self.assertEqual(executable(snapshot_run), target == '100755')
                receipt = self.read(project, entry['adopted']['receipt'])
                self.assertTrue(receipt['recovered'])
                self.assertEqual(sorted(receipt['modes']), [] if target == '100644' else sorted([
                    '.claude/skills/example-skill/scripts/run.py', '.skills/example-skill/scripts/run.py',
                    f'.trace-os/skill-bases/{VECTOR_DIGEST}/scripts/run.py']))
                self.assert_parity(project)
                self.commit(project, f'run.py is {target}')
                self.assertTrue(git(project, 'ls-files', '-s', '--', copy.relative_to(project)).startswith(target))
                self.assertTrue(git(project, 'ls-files', '-s', '--', snapshot_run.relative_to(project))
                                .startswith(target))

    def test_ah241_a_mode_only_update_writes_no_agent_copy(self):
        """AH-241: agent_copies.written names only the copies the operation wrote; the others are listed as kept."""
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/scripts/run.py': (RUN, '100644')}, 'Not executable')
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        copies = '.claude/skills/example-skill'
        # The plan counts the copies the skill keeps; the result counts the copies written.
        self.assertIn(f'    3 agent copies under {copies}/\n', out)
        self.assertIn('  agent copies: 0 written for claude\n', out)
        receipt = self.read(project, self.lock(project)['skills']['example-skill']['adopted']['receipt'])
        self.assertEqual(receipt['agent_copies'], {'agent': 'claude', 'note': NOTE_ADAPTER, 'removed': [], 'written': [],
                                                   'kept': [f'{copies}/{path}' for path in sorted(VECTOR)]})
        self.assertEqual([path for path in receipt['writes'] if path.startswith(copies + '/')], [])
        # The mode reached the kept copy all the same, through the forward step.
        self.assertFalse(executable(project / copies / 'scripts/run.py'))
        self.assertEqual(receipt['modes'], {})
        self.assert_parity(project)

    def test_an_undone_mode_only_update_restores_the_old_original_bit(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        second = source.commit({'skills/example-skill/scripts/run.py': (RUN, '100644')})
        before = tree_state(project)
        with self.interrupt_at(project, lambda relative, data: relative == ts.LOCK):
            code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC)
        self.assertEqual(code, 130)
        journal = self.read(project, ts.OPERATION)
        self.assertEqual(journal['modes_before'][f'.trace-os/skill-bases/{VECTOR_DIGEST}/scripts/run.py'], '100755')
        self.assertEqual(self.run_main(project, 'recover')[0], 0)
        self.assertEqual(tree_state(project), before)
        self.assert_parity(project)

    def test_ah233_an_update_that_rewrites_an_executable_script_keeps_its_mode(self):
        project = self.installed()
        source, first, _ = self.adopted(project)
        changed = b"print('changed')\n"
        second = source.commit({'skills/example-skill/scripts/run.py': (changed, '100755')})
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        entry = self.lock(project)['skills']['example-skill']
        paths = ('.skills/example-skill/scripts/run.py', '.claude/skills/example-skill/scripts/run.py',
                 f"{entry['upstream']['snapshot']}/scripts/run.py")
        for path in paths:
            self.assertEqual((project / path).read_bytes(), changed, path)
            self.assertTrue(executable(project / path), f'{path} lost its execute bit (AH-233)')
        receipt = self.read(project, entry['adopted']['receipt'])
        self.assertEqual(receipt['modes'], {path: '100755' for path in paths})
        self.assertIn('.skills/example-skill/scripts/run.py', receipt['writes'])
        self.assert_parity(project)
        self.commit(project, 'Update example-skill')
        for path in paths:
            self.assertTrue(git(project, 'ls-files', '-s', '--', path).startswith('100755 '), path)

    def test_ah233_replace_kit_copy_over_an_executable_kit_file_keeps_its_mode(self):
        def executable_kit_script(project):
            os.chmod(project / '.skills/example/run.sh', 0o755)
        project = self.installed(skills={'.skills/example/run.sh': ('#!/bin/sh\necho kit\n', 'managed')},
                                 before_commit=executable_kit_script)
        self.assertTrue(executable(project / '.skills/example/run.sh'))
        library = b'#!/bin/sh\necho library\n'
        source = self.source()
        commit = source.commit(package('example', {'SKILL.md': (skill_md('example', 'Library version.\n'), '100644'),
                                                   'run.sh': (library, '100755')}))
        code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example', '--ref', commit, '--yes',
                                       '--replace-kit-copy', *PUBLIC, *WHO)
        self.assertEqual((code, err), (0, ''), out)
        for path in ('.skills/example/run.sh', '.claude/skills/example/run.sh'):
            self.assertEqual((project / path).read_bytes(), library, path)
            self.assertTrue(executable(project / path), f'{path} lost its execute bit (AH-233)')
        self.assert_parity(project)


class WindowsStagingTests(Fixture):
    """AH-234: where no execute bit is kept, the staged bytes are read with the manifest's modes."""

    def test_ah234_direct_and_candidate_adoption_of_an_executable_member(self):
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        for label in ('direct', 'candidate'):
            with self.subTest(adoption=label), mock.patch.object(ts, 'on_windows', return_value=True):
                project = self.installed(name=f'windows-{label}')
                if label == 'direct':
                    code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example-skill', '--ref',
                                                   commit, '--yes', *PUBLIC, *WHO)
                else:
                    candidate = self.show(project, source)
                    staged = Path(json.loads(candidate.read_text())['staged'])
                    self.assertFalse(executable(staged / 'scripts/run.py'), 'staged as Windows stages it')
                    code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example-skill',
                                                   '--candidate', candidate, '--yes', *PUBLIC, *WHO)
                self.assertEqual((code, err), (0, ''), out)
                # 2.5: adopt prints the limit line once, after the plan, as update does.
                self.assertIn('  not tracked by Git here: none\n' + LIMIT + '\nAdopted example-skill from ', out)
                self.assertEqual(out.count(LIMIT), 1)
                entry = self.lock(project)['skills']['example-skill']
                self.assertEqual(entry['upstream']['tree_sha256'], VECTOR_DIGEST)
                receipt = self.read(project, entry['adopted']['receipt'])
                self.assertEqual(receipt['modes'], {path: '100755' for path in (
                    '.skills/example-skill/scripts/run.py', '.claude/skills/example-skill/scripts/run.py',
                    f'.trace-os/skill-bases/{VECTOR_DIGEST}/scripts/run.py')})
                _, problems = check_skills.check(project)
                self.assertEqual(problems, [])

    def test_on_windows_the_copy_is_read_with_the_latest_receipts_modes(self):
        """2.1: with no execute bit to read, an unchanged copy holding a 100755 member is still unchanged."""
        with mock.patch.object(ts, 'on_windows', return_value=True):
            project = self.installed()
            source, first, _ = self.adopted(project)
            if os.name != 'nt':
                # Windows applies no mode, so the copy's script was written plain.
                self.assertFalse(executable(project / '.skills/example-skill/scripts/run.py'))
            code, out, err = self.run_main(project, 'status', '--offline')
            self.assertIn('  copy: unchanged', out.splitlines())
            second = source.commit({'skills/example-skill/SKILL.md': (V2_SKILL, '100644')})
            code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
            self.assertEqual((code, err), (0, ''), out)
            self.assertIn(f'Updated example-skill from {first[:12]} to {second[:12]}.', out.splitlines())
            receipt = self.read(project, self.lock(project)['skills']['example-skill']['adopted']['receipt'])
            self.assertEqual([item['mode'] for item in receipt['copy']['files']], ['100644', '100644', '100755'])
            _, problems = check_skills.check(project)
            self.assertEqual(problems, [])

    @unittest.skipIf(os.name == 'nt', 'Windows keeps no executable bit')
    def test_ah234_on_posix_the_staged_mode_is_still_checked(self):
        source = self.source()
        source.commit(package('example-skill', VECTOR))
        project = self.installed()
        candidate = self.show(project, source)
        staged = Path(json.loads(candidate.read_text())['staged'])
        os.chmod(staged / 'scripts/run.py', 0o644)
        before = tree_state(project)
        self.refused(self.run_main(project, 'adopt', source.bare, 'skills/example-skill', '--candidate', candidate,
                                   '--yes', *PUBLIC), 1,
                     "the package's bytes changed since they were shown (scripts/run.py); run show again and review it")
        self.assertEqual(tree_state(project), before)


if __name__ == '__main__':
    unittest.main()
