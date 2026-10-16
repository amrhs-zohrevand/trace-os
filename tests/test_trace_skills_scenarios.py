"""Scenario tests for scripts/trace_skills.py, written from its contract alone.

The contract is scripts/trace-skills-contract.md (version 1, with the answers of
its section 14). Each test method is one scenario of section 5 of
ai/skills-library-plan.md, run end to end through the tool's public behaviour:
`trace_skills.main(argv)` with `--project`, exit codes, the catalogue's messages,
file bytes and modes, the lock and receipt fields, and the registries read with
`trace_record.validate_intake` and `validate_transitions`. Nothing here imports
the builders' own test module.

Fixtures follow section 13: temporary folders only, a bare Git source per test
(special entries made with `git hash-object` and `update-index`), a fixture
release applied with `bootstrap.apply`, HOME and the Git environment patched,
`GIT_ALLOW_PROTOCOL=file` so no network address can be fetched, and no
credential anywhere. Each kind of fixture (a source holding a given series
of commits, an installed or uninstalled project, and an installed project
with the test-vector package adopted and committed) is made once per run of
this module, by those steps, and every test that asks for it gets its own
copy: a copied Git repository is a whole repository, so the tests stay
independent of each other and of their order.

`--project` comes before the command, because the contract's `<tool>` form
(section 0) is `python3 <script> --project <project>` followed by the command.

Commands that a later builder adds (`update`, `--accept-merged`, `--detach`,
`--attach`, `retire`, `declare`) are exercised by methods that begin with
`self.require(command, *flags)`, the equivalent of a skipUnless on builder D. It
skips the method while the command is not in the tool's parser
(`main([command, '--help'])` does not exit 0 naming the flag), or while the
command, run on a throwaway repository with those flags and a name nobody
adopted, answers with the core build's placeholder `<command> is not in this
build yet`. Any other answer, right or wrong, runs the scenario, so a landed
command is tested and its defects are reported rather than skipped.

Run from the kit's repository root:

    python3 -X utf8 -m unittest starter/files/tests/test_trace_skills_scenarios.py -v

and in an exported project from its root, as the portable suite runs it.
"""
import atexit
import contextlib
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
# A19: in the kit's source tree, trace_record.py and check_skill_handoff.py live
# in the repository's scripts/, not beside the tool; an exported project has
# them all in scripts/.
REPO_SCRIPTS = TESTS.parents[2] / 'scripts' if (TESTS.parents[1].name == 'starter'
                                                and not (SCRIPTS / 'trace_record.py').is_file()) else None
sys.path.insert(0, str(SCRIPTS))
if REPO_SCRIPTS is not None and REPO_SCRIPTS.is_dir():
    sys.path.append(str(REPO_SCRIPTS))
import bootstrap as bs  # noqa: E402
import check_skills  # noqa: E402
import trace_record  # noqa: E402
try:
    import check_skill_handoff  # noqa: E402
except ImportError:  # the state then reads "handoff not checked"
    check_skill_handoff = None
TOOL_FILE = SCRIPTS / 'trace_skills.py'
if TOOL_FILE.is_file():
    import trace_skills  # noqa: E402
else:
    trace_skills = None

ENVIRONMENT = {'GIT_AUTHOR_NAME': 'Test Researcher', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
               'GIT_COMMITTER_NAME': 'Test Researcher', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
               'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'}
IGNORED = ''.join(name + '\n' for name in (
    '__pycache__/', '.trace-os/setup.lock', '.trace-os/transaction.json', '.trace-os/backups/',
    '.trace-os/pending-update.json', '.trace-os/pending-plan.json', '.trace-os/update-backups/',
    '.trace-os/receipts/', '.trace-os/skill-operation.json'))
# Section 13's list, plus record_privacy.py: trace_story.add_commands imports it whenever
# trace_record.py starts, so intake and transition fail without it.
COPIED_SCRIPTS = ('trace_record.py', 'trace_story.py', 'input_provenance.py', 'record_privacy.py',
                  'trace_defaults.py', 'check_skill_handoff.py', 'trace_skills.py', 'bootstrap.py', 'check_skills.py')

BY = 'Test Researcher'
EMAIL = 'test@example.invalid'
WORDS = 'yes, adopt it'
AUTHORITY = 'T3 of ai/records/20261001-fixture'
AGENT_ID = 'claude-opus-5-5'
WHO = ['--by', BY, '--words', WORDS, '--authority', AUTHORITY, '--execution-agent', AGENT_ID]
PUBLIC = ['--audience', 'source=public']

LIMIT = ('Limit: definition 1 reads the skill\'s text; it does not establish that a method is valid, '
         'that its scripts are safe, or that any agent discovers the skill.')
RECEIPT_LIMITS = ('This receipt records what the tool wrote and what it was told; it is not acceptance, '
                  'and it does not establish that the method is valid or that its scripts are safe.',
                  'The intake row names this receipt by its path; this receipt names the row by its id and digest.')
NOTE_ADAPTER = 'copies of the members Git does not ignore, as scripts/bootstrap.py --sync-skills makes them'
NOTE_NONE = 'agent none: no copies; agents read .skills/ directly'
NOTE_UNINSTALLED = 'no adapter state here'


def skill_md(name, body='Body.\n', description='An example skill for tests.'):
    return f'---\nname: {name}\ndescription: {description}\n---\n{body}'.encode('utf-8')


RUN = b"print('ok')\n"
# The package of the contract's test vector (4.6): its digests are fixed values.
VECTOR = {'LICENSE.txt': (b'Apache License 2.0\n', '100644'),
          'SKILL.md': (skill_md('example-skill'), '100644'),
          'scripts/run.py': (RUN, '100755')}
VECTOR_DIGEST = '00c6ea65a3bcf3089fb6c48e684604dc26207fac72c61276d41c0d291e5396d3'
VECTOR_TRACKED = 'c28c830a561645ef6da9382caefb0072a7ebf60f20fff0a9efa5c4dceec31324'

ROUTING_BEGIN, ROUTING_END = '<!-- trace-os:begin skill-routing -->', '<!-- trace-os:end skill-routing -->'
SEED_SKILLS_IN_USE = f'# Skills in use\n\n{ROUTING_BEGIN}\nRoute research tasks to the skills below.\n{ROUTING_END}\n'
KIT_SKILL = skill_md('example', 'Kit body.\n', "The kit's example skill.")
INVENTORY_HEAD = ('<!-- trace-skills:begin inventory -->\n'
                  '## Skills from outside this project\n\n'
                  "Written by the kit's skill tool (scripts/trace_skills.py in an installed project) from "
                  '.trace-os/skills.lock.json; change it with that tool, not by hand.\n\n')
ADOPTED_HEADER = '| Skill id | Source | Commit | Audience | Detached | Receipt |\n| --- | --- | --- | --- | --- | --- |\n'
RELIED_HEADER = '| Skill id | Held by | Digest | Limitation |\n| --- | --- | --- | --- |\n'
RETIRED_HEADER = ('| Skill id | Was | Source or holder | Original kept | Receipt |\n'
                  '| --- | --- | --- | --- | --- |\n')
DEFAULT_LIMITATION = ('Not in the project: co-authors cannot read or check this method; '
                      'a method run records the digest it read.')


def inventory(adopted=(), relied=(), retired=()):
    """The inventory section of 3.2, byte for byte, from its rows."""
    def part(title, header, rows):
        return f'### {title}\n\n' + (header + ''.join(row + '\n' for row in rows) if rows else 'None.\n')
    return (INVENTORY_HEAD
            + part('Adopted from a source', ADOPTED_HEADER, adopted) + '\n'
            + part('Relied on, not in the project', RELIED_HEADER, relied) + '\n'
            + part('Retired', RETIRED_HEADER, retired)
            + '<!-- trace-skills:end inventory -->\n')


def git(path, *args, data=None):
    run = subprocess.run(['git', *map(str, args)], cwd=path, capture_output=True, input=data, check=False)
    if run.returncode:
        raise AssertionError(f'git {" ".join(map(str, args))} failed: {run.stderr.decode("utf-8", "replace")}')
    return run.stdout.decode('utf-8', 'replace').strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def executable(path):
    return os.name != 'nt' and bool(path.stat().st_mode & 0o100)


def litter(name):
    return name in bs.OS_FILES or name.startswith('._')


def package_digest(members):
    """Section 4.6, written again here so the tool's digest is checked against an independent one."""
    lines = ''.join(f'{mode} {len(data)} {sha(data)} {path}\n' for path, (data, mode) in sorted(members.items()))
    return sha(lines.encode('ascii'))


def manifest(members):
    return [{'path': path, 'mode': mode, 'size': len(data), 'sha256': sha(data)}
            for path, (data, mode) in sorted(members.items())]


def folder_members(folder):
    """A folder on disk read as members: bytes and the mode its owner-execute bit gives."""
    return {path.relative_to(folder).as_posix(): (path.read_bytes(), '100755' if executable(path) else '100644')
            for path in sorted(folder.rglob('*')) if path.is_file() and not litter(path.name)}


def package(name, members, root='skills'):
    return {f'{root}/{name}/{path}': value for path, value in members.items()}


def tree(root):
    """Every file's bytes and execute bit, and every symlink's target, outside Git and bootstrap's scratch."""
    def skipped(rel):
        parts = rel.split('/')
        return (parts[0] == '.git' or '__pycache__' in parts or rel == '.trace-os/setup.lock'
                or rel.startswith('.trace-os/backups/') or rel == '.trace-os/backups')
    found = {}
    for folder, dirs, files in os.walk(root):
        base = Path(folder)
        for name in list(dirs):
            rel = (base / name).relative_to(root).as_posix()
            if skipped(rel):
                dirs.remove(name)
            elif (base / name).is_symlink():
                found[rel] = ('symlink', os.readlink(base / name))
        for name in files:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if skipped(rel):
                continue
            found[rel] = ('symlink', os.readlink(path)) if path.is_symlink() else (path.read_bytes(), executable(path))
    return found


def kit_script(name):
    for folder in (SCRIPTS, REPO_SCRIPTS):
        if folder is not None and (folder / name).is_file():
            return folder / name
    raise AssertionError(f'{name} is neither in {SCRIPTS} nor in the repository\'s scripts/')


@contextlib.contextmanager
def working_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@functools.lru_cache(maxsize=None)
def landed(command, option):
    """True when the tool parses `<command> --help` and its help names `option`."""
    if trace_skills is None:
        return False
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = trace_skills.main([command, '--help'])
    except SystemExit as exc:
        code = 0 if exc.code is None else exc.code
    except Exception:  # noqa: BLE001 - any failure to parse means the command has not landed
        return False
    return code == 0 and option in out.getvalue()


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
        folder = Path(tempfile.mkdtemp(prefix='test-trace-skills-scenarios-')).resolve()
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
    """A bare repository served from a path, and the work tree that pushes to it (section 13).

    The repositories are made with the first commit. A source changes only through commit(), so the commits
    it holds name it: a history this run has made before is a copy of what it made.
    """

    def __init__(self, root):
        self.root = root
        self.bare = root / 'src.git'
        self.work = root / 'work'
        self.history = ()

    def commit(self, members, message='Change the package'):
        """Stage each member by blob id and mode, without the file system; None removes one."""
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


class Kit:
    """Fixture releases, cut as a folder with a manifest and an archive holding that manifest."""

    def __init__(self, root):
        self.root = root
        self.previous = None
        self.upgrades = []

    def cut(self, version, contents):
        source = self.root / ('release-' + version)
        files = {}
        for name, (text, policy) in contents.items():
            data = text if isinstance(text, bytes) else text.encode('utf-8')
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[name] = {'sha256': bs.digest(data), 'size': len(data), 'policy': policy, 'profile': 'core'}
        sections = []
        for ident, name in (('core-workflow', 'AGENTS.md'), ('skill-routing', 'ai/SKILLS-IN-USE.md')):
            data = (source / name).read_bytes()
            spec = {'id': ident, 'path': name, 'version': 1,
                    'start': f'<!-- trace-os:begin {ident} -->', 'end': f'<!-- trace-os:end {ident} -->'}
            first, last = bs.marked_range(data, spec)
            spec.update(sha256=bs.digest(data[first:last]), size=last - first, legacy=[])
            sections.append(spec)
        if self.previous:
            self.upgrades.append({'from_version': self.previous, 'to_version': version,
                                  'profiles': ['academic', 'core'], 'agents': ['claude', 'codex', 'cline', 'none'],
                                  'install_state_schema': 1, 'project_schema': 1, 'record_schema': 1})
        data = {'schema_version': 1, 'version': version, 'source_commit': '0' * 40,
                'compatibility': {'install_state_schema': 1, 'project_schema': 1, 'record_schema': 1},
                'supported_upgrades': list(self.upgrades), 'renames': [], 'releases': [],
                'instruction_sections': sections, 'files': files}
        (source / 'starter-manifest.json').write_bytes(bs.encoded(data))
        archive = self.root / f'trace-os-{version}.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.write(source / 'starter-manifest.json', 'trace-os/starter-manifest.json')
        self.previous = version
        return source, archive, bs.digest(archive.read_bytes())


def release_contents(two=False):
    """Release one, or release two with a changed script, kit skill and routing section."""
    routing = 'Route research tasks to the skills below, as release two says.' if two else \
        'Route research tasks to the skills below.'
    return {'AGENTS.md': ('<!-- trace-os:begin core-workflow -->\ninstructions\n<!-- trace-os:end core-workflow -->\n',
                          'seed'),
            '.gitignore': (IGNORED, 'managed'),
            'scripts/example.py': ('print(2)\n' if two else 'print(1)\n', 'managed'),
            '.skills/example/SKILL.md': (skill_md('example', 'Kit body, release two.\n' if two else 'Kit body.\n',
                                                  "The kit's example skill."), 'managed'),
            'ai/SKILLS-IN-USE.md': (f'# Skills in use\n\n{ROUTING_BEGIN}\n{routing}\n{ROUTING_END}\n', 'seed')}


@unittest.skipIf(trace_skills is None, 'scripts/trace_skills.py is not here')
class TraceSkillsScenarios(unittest.TestCase):
    maxDiff = None

    # ----------------------------------------------------------------- fixtures

    def setUp(self):
        # Every test works in the same folder, emptied before it, so a fixture made in an earlier test
        # and copied here names the paths it would name had this test made it.
        self.root = run_folder() / 'test'
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        # Each project installed() made, until a fixture or the test acts on it: its key and its state.
        self.fresh = {}
        self.home = self.root / 'home'
        self.home.mkdir()
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        for patch in (mock.patch.dict(os.environ, dict(ENVIRONMENT, HOME=str(self.home))),
                      # 3.3: every staging folder lands here, outside every project.
                      mock.patch.object(tempfile, 'tempdir', str(self.tmp)),
                      mock.patch.object(trace_skills, 'observe_audience', return_value='not checked: test',
                                        create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        kit = Kit(self.root / 'kit')
        self.release1 = kit.cut('test-1', release_contents())
        self.release2 = kit.cut('test-2', release_contents(two=True))
        self.marker = self.root / 'EXECUTED'

    def require(self, command, *flags):
        """Skip until builder D's command is in the parser and answers with something other than a placeholder."""
        shown = ' '.join((command, *flags))
        if not landed(command, flags[0] if flags else command):
            self.skipTest(f'{shown}: not in the parser yet (builder D has not landed)')
        # The tool's answer does not change within a run, so the probe runs once per command, as landed() does.
        placeholders = _RUN.setdefault('placeholder', {})
        if (command, flags) not in placeholders:
            probe = Path(tempfile.mkdtemp(prefix='probe-', dir=self.root))
            git(probe, 'init', '-q')
            extra = ['--held-by', 'Nobody'] if command == 'declare' else []
            code, out, err = self.run_main(probe, command, 'no-such-skill', *flags, *extra, '--yes')
            placeholders[(command, flags)] = 'is not in this build yet' in err
        if placeholders[(command, flags)]:
            self.skipTest(f'{shown}: a placeholder in this build (builder D has not landed)')

    def source(self, label='library'):
        return Source(self.root / 'sources' / label)

    def add_scripts(self, target):
        (target / 'scripts').mkdir(exist_ok=True)
        for name in COPIED_SCRIPTS:
            shutil.copyfile(kit_script(name), target / 'scripts' / name)

    def installed(self, agent='claude', name=None):
        """An installed project (section 13): the fixture release applied, the tools copied in, all committed."""
        target = self.root / (name or 'project-' + agent)

        def make():
            bs.apply(self.release1[0], target, 'academic', agent, AGENT_ID)
            self.add_scripts(target)
            git(target, 'config', 'user.name', BY)
            git(target, 'config', 'user.email', EMAIL)
            self.commit(target, 'Record the setup and the tools')
        # Bootstrap's setup receipt names the project's folder.
        made(('installed', agent), (target,), make, states_its_path=True)
        self.fresh[target] = (('installed', agent), self.state(target))
        return target

    def uninstalled(self, name='plain', symlink=False):
        """A project with no install state, like the kit's development repository itself (section 13)."""
        target = self.root / name
        linked = symlink and os.name != 'nt'

        def make():
            target.mkdir()
            git(target, 'init', '-q', '-b', 'main')
            git(target, 'config', 'user.name', BY)
            git(target, 'config', 'user.email', EMAIL)
            files = {'README.md': b'# A project\n', '.gitignore': IGNORED.encode(),
                     '.skills/own/SKILL.md': skill_md('own', 'Our own method.\n', 'The project\'s own skill.'),
                     '.skills/legacy.md': b'# legacy\n\nAn older single-file skill.\n',
                     '.skills/example-skill/SKILL.md': VECTOR['SKILL.md'][0] + b'Our own line.\n',
                     '.skills/example-skill/LICENSE.txt': VECTOR['LICENSE.txt'][0]}
            for name, data in files.items():
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(data)
            self.add_scripts(target)
            if linked:
                (target / '.claude').mkdir()
                os.symlink('../.skills', target / '.claude/skills')
            self.commit(target, 'A project with its own skills')
        made(('uninstalled', linked), (target,), make)
        return target

    def state(self, project):
        """What a test can change in a project, its repository and this test's own folders, for telling that
        nothing did."""
        return tuple(signature(folder) for folder in (project, self.home, self.tmp))

    @staticmethod
    def commit(target, message='Record the operation'):
        git(target, 'add', '-A')
        git(target, 'commit', '-q', '--allow-empty', '-m', message)
        return git(target, 'rev-parse', 'HEAD')

    def run_main(self, project, *argv):
        out, err = io.StringIO(), io.StringIO()
        args = ['--project', str(project), *[str(arg) for arg in argv]]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, 'stdin', io.StringIO('')):
            try:
                code = trace_skills.main(args)
            except SystemExit as exc:
                code = 0 if exc.code is None else exc.code
            except KeyboardInterrupt:
                code = 'main let KeyboardInterrupt escape instead of exiting 128 plus the signal number (E16)'
        return code, out.getvalue(), err.getvalue()

    def adopt(self, project, source, path, ref, *extra):
        return self.run_main(project, 'adopt', source.bare, path, '--ref', ref, '--yes', *PUBLIC, *WHO, *extra)

    def adopt_candidate(self, project, source, path, candidate, *extra):
        return self.run_main(project, 'adopt', source.bare, path, '--candidate', candidate, '--yes',
                             *PUBLIC, *WHO, *extra)

    def show(self, project, source, path, ref='main'):
        code, out, err = self.run_main(project, 'show', source.bare, path, '--ref', ref)
        self.assertEqual(code, 0, err)
        lines = [line for line in out.splitlines() if line.startswith('Candidate: ')]
        self.assertEqual(len(lines), 1, out)
        return Path(lines[0][len('Candidate: '):])

    def tool(self, project):
        return f'python3 {TOOL_FILE.resolve()} --project {project}'

    @staticmethod
    def message(text):
        return 'trace-skills: ' + text + '\n'

    def assert_refused(self, result, code, text):
        self.assertEqual((result[0], result[2]), (code, self.message(text)), result[1])

    @staticmethod
    def lock(project):
        return json.loads((project / '.trace-os/skills.lock.json').read_text(encoding='utf-8'))

    @staticmethod
    def read_json(project, name):
        return json.loads((project / name).read_text(encoding='utf-8'))

    @staticmethod
    def rows(project, name):
        path = project / name
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] \
            if path.is_file() else []

    @staticmethod
    def journal(project):
        return json.loads((project / '.trace-os/receipts/skill-operation.json').read_text(encoding='utf-8'))

    @staticmethod
    def receipts(project):
        folder = project / '.trace-os/skill-adoptions'
        return sorted(path.relative_to(project).as_posix() for path in folder.glob('*.json')) \
            if folder.is_dir() else []

    def interrupt_at(self, project, match):
        """Raise KeyboardInterrupt at the first atomic write `match(path, data)` selects (section 13)."""
        real = trace_skills.bootstrap.write_atomic

        def wrapper(path, data):
            try:
                rel = Path(path).resolve().relative_to(project.resolve()).as_posix()
            except ValueError:
                rel = None
            if rel is not None and match(rel, data):
                raise KeyboardInterrupt
            return real(path, data)
        return mock.patch.object(trace_skills.bootstrap, 'write_atomic', wrapper)

    def handoff(self, text, name):
        if check_skill_handoff is None:
            return 'handoff not checked: scripts/check_skill_handoff.py is not here'
        absent = check_skill_handoff.absent_signals(text, name)
        return 'handoff stated' if not absent else 'handoff not stated: ' + ', '.join(absent)

    def assert_equivalent(self, project):
        """Section 9: check_skills clean and, with an adapter, sync_skills a no-op that leaves the state as it was."""
        state_path = project / '.trace-os/install.json'
        before = state_path.read_bytes()
        _, problems = check_skills.check(project)
        self.assertEqual(problems, [], 'check_skills reports a problem after the operation')
        state = json.loads(before)
        if state['agent'] in bs.AGENTS:
            result = bs.sync_skills(project)
            self.assertEqual((result['synced'], result['removed'], result['retired']), (0, [], []),
                             'bootstrap.sync_skills is not a no-op after the operation')
            self.assertEqual(state_path.read_bytes(), before, 'sync_skills rewrote .trace-os/install.json')

    def assert_members(self, folder, members):
        """A folder holds exactly these members' bytes, and on POSIX their modes too."""
        found = folder_members(folder)
        self.assertEqual({path: data for path, (data, _) in found.items()},
                         {path: data for path, (data, _) in members.items()}, folder)
        if os.name != 'nt':
            self.assertEqual(found, members, folder)

    def assert_writes_in_place(self, project, receipt):
        """Every path the receipt says it wrote holds the bytes it says, or is absent when it says removed."""
        for name, entry in receipt['writes'].items():
            path = project / name
            self.assertEqual(sha(path.read_bytes()) if path.is_file() else None, entry['after'], name)

    def update_kit(self, project, agent='claude'):
        """Update to release two with bootstrap.update; the postflight is check_skills.check (section 9)."""
        source, archive, digest = self.release2

        def postflight(target, run_id):
            _, problems = check_skills.check(target)
            return {'name': 'updated project workflow', 'status': 'fail' if problems else 'pass',
                    'exit_code': 1 if problems else 0, 'problems': problems}
        passed = {'name': 'release portable tests', 'status': 'pass', 'exit_code': 0}
        with mock.patch.object(bs, 'preflight_release', return_value=passed), \
                mock.patch.object(bs, 'check_updated_project', side_effect=postflight):
            return bs.update(source, project, 'academic', agent, archive, digest, AGENT_ID)

    def adopted_vector(self, project, source=None, via_show=False):
        """The test-vector package adopted into `project` and committed; returns (source, commit, receipt)."""
        source = source or self.source()

        def make():
            commit = source.commit(package('example-skill', VECTOR), 'The example skill')
            if via_show:
                result = self.adopt_candidate(project, source, 'skills/example-skill',
                                              self.show(project, source, 'skills/example-skill'))
            else:
                result = self.adopt(project, source, 'skills/example-skill', commit)
            self.assertEqual(result[0], 0, result[2])
            receipt = self.lock(project)['skills']['example-skill']['adopted']['receipt']
            self.commit(project, 'Adopt example-skill')
            return commit, receipt
        fresh = self.fresh.pop(project, None)
        if fresh is not None and not source.history and fresh[1] == self.state(project):
            # Into a project as installed() made it, from a new source: this run adopts there once, and copies
            # the result after. The records name the source's folder, and the compiled modules the project's,
            # so both are in the key.
            key = ('adopted', fresh[0], project.name, source.root.relative_to(self.root).as_posix(), via_show,
                   trace_skills.on_windows())
            commit, receipt = made(key, (project, source.root), make, replace=True)
            source.history = ((tuple(sorted(package('example-skill', VECTOR).items())), 'The example skill'),)
        else:
            commit, receipt = make()
        return source, commit, receipt

    # ---------------------------------------------------------------- scenarios

    def test_interrupted_adoption_at_each_boundary_then_recover(self):
        """Section 13, Interruptions: undone before the receipt's step, completed at it; recover twice."""
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        boundaries = (
            ('before the copy', lambda rel, data: rel.startswith('.skills/example-skill/'), 'Undone'),
            ('after the copy', lambda rel, data: rel.startswith('.trace-os/skill-bases/'), 'Undone'),
            ('before the lock', lambda rel, data: rel == '.trace-os/skills.lock.json', 'Undone'),
            ('before the receipt', lambda rel, data: rel.startswith('.trace-os/skill-adoptions/'), 'Completed'))
        for number, (label, match, outcome) in enumerate(boundaries):
            with self.subTest(boundary=label):
                project = self.installed(name=f'interrupted-{number}')
                before = tree(project)
                with self.interrupt_at(project, match):
                    code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
                self.assertEqual(code, 130, err)
                operation = self.journal(project)['operation_id']
                self.assertRegex(operation, r'^\d{8}T\d{6}Z-example-skill-adopt(-\d+)?$')
                self.assertEqual(err, self.message(
                    f'interrupted; run {self.tool(project)} recover to finish or undo {operation}'))
                code, out, err = self.run_main(project, 'recover')
                receipt = f'.trace-os/skill-adoptions/{operation}.json'
                if outcome == 'Undone':
                    self.assertEqual((code, out, err), (0, f'Undone: {operation}; the project is as it was '
                                                           'before. Run the command again to adopt.\n', ''))
                    self.assertEqual(tree(project), before, 'a file is not back at its plan-time bytes or mode')
                    self.assertEqual(self.rows(project, 'sources/intake.jsonl'), [])
                    self.assertEqual(self.receipts(project), [])
                else:
                    self.assertEqual((code, out, err), (0, f'Completed: {operation}; receipt {receipt}.\n', ''))
                    self.assertEqual(self.receipts(project), [receipt])
                    written = self.read_json(project, receipt)
                    self.assertTrue(written['recovered'])
                    self.assertEqual(len(self.rows(project, 'sources/intake.jsonl')), 1)
                    self.assertEqual(written['records']['intake']['id'],
                                     self.rows(project, 'sources/intake.jsonl')[0]['id'])
                    self.assert_writes_in_place(project, written)
                    self.assertEqual((project / '.skills/example-skill/SKILL.md').read_bytes(),
                                     VECTOR['SKILL.md'][0])
                self.assertFalse((project / '.trace-os/receipts/skill-operation.json').exists())
                self.assertFalse((project / '.trace-os/transaction.json').exists())
                self.assertEqual(list(self.tmp.glob('trace-skills-*')), [], 'a staging folder was left behind')
                self.assert_equivalent(project)
                self.assertEqual(self.run_main(project, 'recover'), (0, 'Nothing to recover.\n', ''))
                # 12.5: a re-run is idempotent, whichever way the operation ended.
                code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
                self.assertEqual(code, 0, err)
                if outcome == 'Completed':
                    self.assertIn(f'Already adopted: example-skill from {source.bare} skills/example-skill '
                                  f'at {commit[:12]}; nothing to do.', out.splitlines())
                    self.assertEqual(self.receipts(project), [receipt])
                else:
                    self.assertEqual(len(self.receipts(project)), 1)
                self.assertEqual(len(self.rows(project, 'sources/intake.jsonl')), 1)
                self.assert_equivalent(project)
        with self.subTest(boundary='the intake row fails'):
            project = self.installed(name='intake-fails')
            with mock.patch.object(trace_skills, 'run_trace_record', return_value=(1, '', 'simulated failure')):
                code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
            self.assertEqual(code, 2, err)
            journal = self.journal(project)
            operation = journal['operation_id']
            self.assertEqual(journal['state'], 'applied')
            self.assertTrue(err.startswith(self.message('the files were written, but the intake row failed (')[:-1]),
                            err)
            self.assertTrue(err.endswith(f'); run {self.tool(project)} recover to finish {operation}\n'), err)
            self.assertEqual(self.receipts(project), [])
            code, out, err = self.run_main(project, 'recover')
            self.assertEqual((code, err), (0, ''))
            self.assertEqual(out, f'Completed: {operation}; receipt .trace-os/skill-adoptions/{operation}.json.\n')
            self.assertEqual(len(self.rows(project, 'sources/intake.jsonl')), 1)
            self.assertFalse((project / '.trace-os/receipts/skill-operation.json').exists())
            self.assert_equivalent(project)

    def test_kit_update_over_an_adopted_skill(self):
        """An adopted skill and its agent copies survive a kit update, whose postflight check_skills passes."""
        project = self.installed()
        source, commit, receipt = self.adopted_vector(project)
        skill_files = {name: data for name, data in tree(project).items()
                       if name.startswith(('.skills/example-skill/', '.claude/skills/example-skill/',
                                           '.trace-os/skill-bases/', '.trace-os/skill-adoptions/'))}
        lock_bytes = (project / '.trace-os/skills.lock.json').read_bytes()
        state_before = self.read_json(project, '.trace-os/install.json')
        section = (project / 'ai/SKILLS-IN-USE.md').read_text(encoding='utf-8')
        section = section[section.index('<!-- trace-skills:begin inventory -->'):]
        result = self.update_kit(project)
        self.assertEqual([check['status'] for check in result['update']['checks']], ['pass', 'pass'])
        self.assertEqual(self.read_json(project, '.trace-os/install.json')['version'], 'test-2')
        after = tree(project)
        self.assertEqual({name: after.get(name) for name in skill_files}, skill_files)
        self.assertEqual((project / '.trace-os/skills.lock.json').read_bytes(), lock_bytes)
        state = self.read_json(project, '.trace-os/install.json')
        copies = {name: entry for name, entry in state_before['files'].items()
                  if name.startswith('.claude/skills/example-skill/')}
        self.assertEqual(len(copies), 3)
        self.assertEqual({name: state['files'].get(name) for name in copies}, copies)
        text = (project / 'ai/SKILLS-IN-USE.md').read_text(encoding='utf-8')
        self.assertIn('as release two says', text, 'the kit update did not reach its routing section')
        self.assertTrue(text.endswith(section), 'the kit update changed the inventory section')
        self.assert_equivalent(project)
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertEqual(code, 0, err)
        self.assertIn('  copy: unchanged', out.splitlines())

    def test_kit_update_rollback_after_an_earlier_adoption_is_clean(self):
        """12.6: an adoption made before the update is part of what the update backed up."""
        project = self.installed()
        self.adopted_vector(project)
        before = tree(project)
        self.update_kit(project)
        self.commit(project, 'Take the kit update')
        bs.rollback(project, AGENT_ID)
        self.assertEqual(self.read_json(project, '.trace-os/install.json')['version'], 'test-1')
        after = tree(project)
        for name in [name for name in before if not name.startswith(('.trace-os/update', '.trace-os/receipts/'))]:
            self.assertEqual(after.get(name), before[name], name)
        self.assert_equivalent(project)

    def test_adoption_after_a_kit_update_makes_its_rollback_refuse(self):
        """12.6: an adoption after a kit update changes install.json, so that update's rollback refuses."""
        project = self.installed()
        self.update_kit(project)
        self.commit(project, 'Take the kit update')
        self.adopted_vector(project)
        self.assert_equivalent(project)
        before = tree(project)
        with self.assertRaises(bs.SetupError) as caught:
            bs.rollback(project, AGENT_ID)
        self.assertEqual(str(caught.exception), 'rollback would overwrite a later edit: .trace-os/install.json')
        self.assertEqual(tree(project), before)

    def test_edited_agent_copy_is_refused(self):
        """N23: a generated copy edited separately stops the operation, and nothing is written."""
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        kit_commit = source.commit(package('example', {'SKILL.md': (skill_md('example', 'Library body.\n'), '100644'),
                                                       'LICENSE.txt': VECTOR['LICENSE.txt']}))
        project = self.installed()
        stray = project / '.claude/skills/example-skill/SKILL.md'
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b'an edit nobody synced\n')
        before = tree(project)
        result = self.adopt(project, source, 'skills/example-skill', commit)
        self.assert_refused(result, 1, 'a generated copy of example-skill was edited separately '
                                       '(.claude/skills/example-skill/SKILL.md); merge or remove it, then run this again')
        self.assertEqual(tree(project), before)
        stray.unlink()
        kit_copy = project / '.claude/skills/example/SKILL.md'
        kit_copy.write_bytes(kit_copy.read_bytes() + b'An edit to the generated copy.\n')
        before = tree(project)
        result = self.adopt(project, source, 'skills/example', kit_commit, '--replace-kit-copy')
        self.assert_refused(result, 1, 'a generated copy of example was edited separately '
                                       '(.claude/skills/example/SKILL.md); merge or remove it, then run this again')
        self.assertEqual(tree(project), before)

    def test_update_refuses_an_edited_agent_copy(self):
        """N23 for update: the edited generated copy is named and nothing is written."""
        self.require('update')
        project = self.installed()
        source, commit, _ = self.adopted_vector(project)
        newer = source.commit({'skills/example-skill/SKILL.md': (skill_md('example-skill', 'Body, revised.\n'),
                                                                 '100644')})
        copy = project / '.claude/skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'An edit to the generated copy.\n')
        before = tree(project)
        result = self.run_main(project, 'update', 'example-skill', '--ref', newer, '--yes', *PUBLIC, *WHO)
        self.assert_refused(result, 1, 'a generated copy of example-skill was edited separately '
                                       '(.claude/skills/example-skill/SKILL.md); merge or remove it, then run this again')
        self.assertEqual(tree(project), before)

    def test_agent_none_writes_no_copies(self):
        """Section 9 under --agent none: no copy; install.json changes only for --replace-kit-copy."""
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        project = self.installed('none')
        state = (project / '.trace-os/install.json').read_bytes()
        code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
        self.assertEqual(code, 0, err)
        self.assertIn('  agent copies: none (agent none)', out.splitlines())
        for root in ('.claude', '.agents', '.cline'):
            self.assertFalse((project / root).exists(), root)
        self.assertEqual((project / '.trace-os/install.json').read_bytes(), state)
        receipt = self.read_json(project, self.lock(project)['skills']['example-skill']['adopted']['receipt'])
        self.assertEqual(receipt['agent_copies'], {'agent': 'none', 'written': [], 'kept': [], 'removed': [],
                                                   'note': NOTE_NONE})
        self.assertNotIn('.trace-os/install.json', receipt['writes'])
        self.assert_equivalent(project)
        kit_commit = source.commit(package('example', {'SKILL.md': (skill_md('example', 'Library body.\n'), '100644')}))
        code, out, err = self.adopt(project, source, 'skills/example', kit_commit, '--replace-kit-copy')
        self.assertEqual(code, 0, err)
        state = self.read_json(project, '.trace-os/install.json')
        self.assertEqual(state.get('retired_skills'), ['example'])
        self.assertNotIn('.skills/example/SKILL.md', state['files'])
        for root in ('.claude', '.agents', '.cline'):
            self.assertFalse((project / root).exists(), root)
        self.assert_equivalent(project)

    def test_ignored_member_is_listed_as_not_tracked(self):
        """4.6 and 3.1: a member the project ignores is written, listed under not_tracked, left out of the copy's digest."""
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        project = self.installed()
        with (project / '.gitignore').open('a', encoding='utf-8') as stream:
            stream.write('run.py\n')
        self.commit(project, 'Ignore run.py')
        code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
        self.assertEqual(code, 0, err)
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['upstream']['tree_sha256'], entry['upstream']['tracked_sha256'],
                          entry['adopted']['tree_sha256']), (VECTOR_DIGEST, VECTOR_TRACKED, VECTOR_TRACKED))
        receipt = self.read_json(project, entry['adopted']['receipt'])
        self.assertEqual(receipt['not_tracked'], sorted(['.skills/example-skill/scripts/run.py',
                                                         f'.trace-os/skill-bases/{VECTOR_DIGEST}/scripts/run.py']))
        self.assertEqual(receipt['upstream']['files'], manifest(VECTOR))
        tracked = {name: value for name, value in VECTOR.items() if name != 'scripts/run.py'}
        self.assertEqual(receipt['copy']['files'], manifest(tracked))
        self.assertEqual((project / '.skills/example-skill/scripts/run.py').read_bytes(), RUN)
        self.assertFalse((project / '.claude/skills/example-skill/scripts/run.py').exists())
        self.assertEqual(receipt['agent_copies']['written'], ['.claude/skills/example-skill/LICENSE.txt',
                                                              '.claude/skills/example-skill/SKILL.md'])
        self.assert_equivalent(project)
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertEqual(code, 0, err)
        self.assertIn('  copy: unchanged', out.splitlines())

    def test_package_whose_skill_md_would_be_ignored_is_refused(self):
        """E18: a SKILL.md the project's Git ignores would not reach its history."""
        source = self.source()
        commit = source.commit(package('ignored-skill', {'SKILL.md': (skill_md('ignored-skill'), '100644'),
                                                         'LICENSE.txt': VECTOR['LICENSE.txt']}))
        project = self.installed()
        with (project / '.gitignore').open('a', encoding='utf-8') as stream:
            stream.write('/.skills/ignored-skill/\n')
        self.commit(project, 'Ignore one skill folder')
        before = tree(project)
        result = self.adopt(project, source, 'skills/ignored-skill', commit)
        self.assert_refused(result, 1, 'Git ignores .skills/ignored-skill/SKILL.md in this project, so it would '
                                       'not reach the project\'s history; change that ignore rule first')
        self.assertEqual(tree(project), before)

    @unittest.skipIf(os.name == 'nt', 'Windows keeps no executable bit')
    def test_executable_script_keeps_its_mode(self):
        """A 100755 member stays executable in the copy, the original and the agent copy, and in Git."""
        project = self.installed()
        source, commit, receipt_path = self.adopted_vector(project)
        receipt = self.read_json(project, receipt_path)
        paths = ('.skills/example-skill/scripts/run.py', f'.trace-os/skill-bases/{VECTOR_DIGEST}/scripts/run.py',
                 '.claude/skills/example-skill/scripts/run.py')
        self.assertEqual(receipt['modes'], {name: '100755' for name in paths})
        for name in paths:
            self.assertTrue(executable(project / name), name)
            self.assertTrue(git(project, 'ls-files', '-s', '--', name).startswith('100755 '), name)
        for name in ('.skills/example-skill/SKILL.md', '.claude/skills/example-skill/LICENSE.txt'):
            self.assertFalse(executable(project / name), name)
        run_entry = [item for item in receipt['upstream']['files'] if item['path'] == 'scripts/run.py']
        self.assertEqual(run_entry, [{'path': 'scripts/run.py', 'mode': '100755', 'size': len(RUN), 'sha256': sha(RUN)}])
        self.assert_equivalent(project)

    def test_script_only_upstream_change(self):
        """A change to scripts/run.py alone changes the package digest and leaves SKILL.md's digest as it was."""
        project = self.installed()
        source, first, _ = self.adopted_vector(project, via_show=True)
        self.assert_equivalent(project)
        second = source.commit({'skills/example-skill/scripts/run.py': (b"print('changed')\n", '100755')})
        code, out, err = self.run_main(project, 'status')
        self.assertEqual(code, 0, err)
        self.assertIn(f'  upstream: behind {second[:12]}', out.splitlines())
        candidate = json.loads(self.show(project, source, 'skills/example-skill').read_text(encoding='utf-8'))
        self.assertEqual(candidate['source']['commit'], second)
        files = {item['path']: item for item in candidate['package']['files']}
        self.assertEqual(files['SKILL.md']['sha256'], sha(VECTOR['SKILL.md'][0]))
        self.assertEqual(files['scripts/run.py']['sha256'], sha(b"print('changed')\n"))
        changed = dict(VECTOR, **{'scripts/run.py': (b"print('changed')\n", '100755')})
        self.assertEqual(candidate['package']['tree_sha256'], package_digest(changed))
        self.assertNotEqual(candidate['package']['tree_sha256'], VECTOR_DIGEST)

    def test_script_only_update_keeps_skill_md(self):
        """The update of a script-only change rewrites the script and leaves SKILL.md and its copy alone."""
        self.require('update')
        project = self.installed()
        source, first, _ = self.adopted_vector(project, via_show=True)
        skill_before = (project / '.skills/example-skill/SKILL.md').read_bytes()
        changed_run = b"print('changed')\n"
        second = source.commit({'skills/example-skill/scripts/run.py': (changed_run, '100755')})
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn(f'Updated example-skill from {first[:12]} to {second[:12]}.', out.splitlines())
        self.assertIn('changed scripts/run.py', [line.strip() for line in out.splitlines()])
        self.assertEqual((project / '.skills/example-skill/SKILL.md').read_bytes(), skill_before)
        self.assertEqual((project / '.skills/example-skill/scripts/run.py').read_bytes(), changed_run)
        self.assertEqual((project / '.claude/skills/example-skill/scripts/run.py').read_bytes(), changed_run)
        entry = self.lock(project)['skills']['example-skill']
        changed = dict(VECTOR, **{'scripts/run.py': (changed_run, '100755')})
        self.assertEqual((entry['source']['commit'], entry['upstream']['tree_sha256']), (second, package_digest(changed)))
        self.assertFalse((project / '.trace-os/skill-bases' / VECTOR_DIGEST / 'SKILL.md').exists())
        receipt = self.read_json(project, entry['adopted']['receipt'])
        self.assertEqual(receipt['operation'], 'update')
        self.assertEqual(receipt['previous']['commit'], first)
        self.assertNotIn('.skills/example-skill/SKILL.md', receipt['writes'])
        rows = self.rows(project, 'sources/intake.jsonl')
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]['checked'].startswith(
            f'the whole new package (3 files, {sum(len(d) for d, _ in changed.values())} bytes) and its difference '
            f'from {first[:12]} were printed before writing and the person confirmed it (--yes)'), rows[1]['checked'])
        self.assertEqual(trace_record.validate_intake(rows)[0], [])
        self.assert_equivalent(project)

    def test_stale_candidate_is_refused(self):
        """C5: bytes changed after show, at the plan and under the lock, are refused and nothing is written."""
        source = self.source()
        source.commit(package('example-skill', VECTOR))
        project = self.installed()
        before = tree(project)
        # C1: no terminal, so the plan is shown, a candidate written, and nothing applied.
        code, out, err = self.run_main(project, 'adopt', source.bare, 'skills/example-skill', '--ref', 'main',
                                       *PUBLIC, *WHO)
        self.assertEqual(code, 1, err)
        last = out.rstrip('\n').splitlines()[-1]
        self.assertTrue(last.startswith('Nothing was applied: no terminal to confirm on. Show the plan above to the '
                                        'person; when they agree, run: '), last)
        found = re.search(r' --candidate (\S+) --yes$', last)
        self.assertIsNotNone(found, last)
        candidate = Path(found.group(1))
        data = json.loads(candidate.read_text(encoding='utf-8'))
        self.assertEqual((data['kind'], data['created_by'], data['for']['operation']),
                         ('trace-skills-candidate', 'adopt', 'adopt'))
        self.assertEqual(tree(project), before)
        staged = Path(data['staged'])
        (staged / 'SKILL.md').write_bytes((staged / 'SKILL.md').read_bytes() + b'Changed after it was shown.\n')
        result = self.adopt_candidate(project, source, 'skills/example-skill', candidate)
        self.assert_refused(result, 1, 'the package\'s bytes changed since they were shown (SKILL.md); '
                                       'run show again and review it')
        self.assertEqual(tree(project), before)
        # The same, with the change made once the setup lock is taken (12.3, step 2).
        candidate = self.show(project, source, 'skills/example-skill')
        staged = Path(json.loads(candidate.read_text(encoding='utf-8'))['staged'])
        self.assertFalse(staged.resolve().is_relative_to(project), 'the staged copy is inside the project')
        real_locked = trace_skills.bootstrap.locked

        @contextlib.contextmanager
        def changing(target):
            (staged / 'SKILL.md').write_bytes(b'Changed under the lock.\n')
            with real_locked(target):
                yield
        with mock.patch.object(trace_skills.bootstrap, 'locked', changing):
            result = self.adopt_candidate(project, source, 'skills/example-skill', candidate)
        self.assert_refused(result, 1, 'the package\'s bytes changed since they were shown (SKILL.md); '
                                       'run show again and review it')
        self.assertEqual(tree(project), before)
        self.assertFalse((project / '.trace-os/receipts/skill-operation.json').exists())

    def test_kit_owned_name_without_and_with_replace_kit_copy(self):
        """N5, N6, then --replace-kit-copy: retired_skills extended, and a kit update leaves the name alone."""
        source = self.source()
        members = {'SKILL.md': (skill_md('example', 'Library body.\n', 'The library\'s example skill.'), '100644'),
                   'LICENSE.txt': VECTOR['LICENSE.txt']}
        commit = source.commit(package('example', members))
        project = self.installed()
        before = tree(project)
        result = self.adopt(project, source, 'skills/example', commit)
        self.assert_refused(result, 1, 'example is a skill the kit ships; adopting it would leave two copies under '
                                       'one name. Pass --replace-kit-copy to remove the kit\'s copy and keep the '
                                       'kit\'s updater from installing it again, or choose another package')
        self.assertEqual(tree(project), before)
        kit_file = project / '.skills/example/SKILL.md'
        kit_file.write_bytes(kit_file.read_bytes() + b'A local edit.\n')
        result = self.adopt(project, source, 'skills/example', commit, '--replace-kit-copy')
        self.assert_refused(result, 1, 'the kit\'s copy of example was edited here (.skills/example/SKILL.md); '
                                       '--replace-kit-copy removes only an unedited kit copy. Move the edit to a '
                                       'file of your own, then run this again')
        git(project, 'checkout', '--', '.skills/example/SKILL.md')
        self.assertEqual(tree(project), before)
        code, out, err = self.adopt(project, source, 'skills/example', commit, '--replace-kit-copy')
        self.assertEqual(code, 0, err)
        for name, (data, _) in members.items():
            self.assertEqual((project / '.skills/example' / name).read_bytes(), data, name)
            self.assertEqual((project / '.claude/skills/example' / name).read_bytes(), data, name)
        state = self.read_json(project, '.trace-os/install.json')
        self.assertEqual(state.get('retired_skills'), ['example'])
        self.assertNotIn('.skills/example/SKILL.md', state['files'])
        self.assertEqual(state['files']['.claude/skills/example/SKILL.md'],
                         {'sha256': sha(members['SKILL.md'][0]), 'policy': 'managed',
                          'canonical': '.skills/example/SKILL.md'})
        entry = self.lock(project)['skills']['example']
        self.assertTrue(entry['replaced_kit_copy'])
        receipt = self.read_json(project, entry['adopted']['receipt'])
        self.assertEqual(receipt['ownership'], {'before': 'kit', 'replaced_kit_copy': True,
                                                'kit_files_removed': ['.skills/example/SKILL.md'],
                                                'retired_skills_added': ['example']})
        self.assert_equivalent(project)
        self.commit(project, 'Take example from the library')
        adopted = {name: data for name, data in tree(project).items()
                   if name.startswith(('.skills/example/', '.claude/skills/example/'))}
        self.update_kit(project)
        after = tree(project)
        self.assertEqual({name: after.get(name) for name in adopted}, adopted,
                         'the kit update installed its own example over the adopted one')
        self.assertEqual(self.read_json(project, '.trace-os/install.json').get('retired_skills'), ['example'])
        self.assert_equivalent(project)

    def test_duplicate_name_of_an_adopted_skill(self):
        """N2 for another commit of the same source, N3 for another source, Already adopted for the same one."""
        project = self.installed()
        source, commit, _ = self.adopted_vector(project)
        self.assert_equivalent(project)
        code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
        self.assertEqual(code, 0, err)
        self.assertIn(f'Already adopted: example-skill from {source.bare} skills/example-skill at {commit[:12]}; '
                      'nothing to do.', out.splitlines())
        newer = source.commit({'skills/example-skill/SKILL.md': (skill_md('example-skill', 'Revised.\n'), '100644')})
        before = tree(project)
        result = self.adopt(project, source, 'skills/example-skill', newer)
        self.assert_refused(result, 1, f'example-skill is adopted from this source at {commit[:12]}; '
                                       'use update example-skill to move it')
        other = self.source('other')
        other_commit = other.commit(package('example-skill', VECTOR, root='methods'))
        result = self.adopt(project, other, 'methods/example-skill', other_commit)
        self.assert_refused(result, 1, f'example-skill is adopted from another source ({source.bare} '
                                       f'skills/example-skill); retire it first: {self.tool(project)} '
                                       'retire example-skill')
        self.assertEqual(tree(project), before)

    def test_legacy_single_file_skill_is_refused(self):
        """N8: .skills/legacy.md holds the name legacy."""
        source = self.source()
        commit = source.commit(package('legacy', {'SKILL.md': (skill_md('legacy'), '100644')}))
        project = self.uninstalled()
        before = tree(project)
        result = self.adopt(project, source, 'skills/legacy', commit)
        self.assert_refused(result, 1, 'a single-file skill .skills/legacy.md has this name; turn it into '
                                       '.skills/legacy/SKILL.md or remove it first')
        self.assertEqual(tree(project), before)

    def test_project_folder_kept_with_keep_existing(self):
        """N7, N9, N10, N11, then --keep-existing connects the project's copy without touching it (7.4)."""
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        other_commit = source.commit(package('other-skill', {'SKILL.md': (skill_md('other-skill'), '100644')}))
        project = self.uninstalled(symlink=True)
        before = tree(project)
        result = self.adopt(project, source, 'skills/example-skill', commit)
        self.assert_refused(result, 1, 'example-skill already exists in this project (.skills/example-skill/), '
                                       'written here or of unknown origin; pass --keep-existing to keep it and '
                                       'record this package as its upstream original, or move it first')
        result = self.adopt(project, source, 'skills/example-skill', commit, '--replace-kit-copy', '--keep-existing')
        self.assert_refused(result, 2, '--replace-kit-copy and --keep-existing exclude each other')
        result = self.adopt(project, source, 'skills/other-skill', other_commit, '--replace-kit-copy')
        self.assert_refused(result, 1, '--replace-kit-copy: other-skill is not a skill the kit ships here; '
                                       'nothing to replace')
        result = self.adopt(project, source, 'skills/other-skill', other_commit, '--keep-existing')
        self.assert_refused(result, 1, '--keep-existing: there is no .skills/other-skill/ here to keep')
        self.assertEqual(tree(project), before)
        code, out, err = self.adopt(project, source, 'skills/example-skill', commit, '--keep-existing')
        self.assertEqual(code, 0, err)
        after = tree(project)
        kept = {name: value for name, value in before.items() if name.startswith('.skills/example-skill/')}
        self.assertEqual({name: value for name, value in after.items()
                          if name.startswith('.skills/example-skill/')}, kept)
        existing = folder_members(project / '.skills/example-skill')
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual(entry['adopted']['kept_existing'], True)
        self.assertEqual(entry['adopted']['tree_sha256'], package_digest(existing))
        self.assertEqual(entry['upstream']['tree_sha256'], VECTOR_DIGEST)
        self.assert_members(project / '.trace-os/skill-bases' / VECTOR_DIGEST, VECTOR)
        receipt = self.read_json(project, entry['adopted']['receipt'])
        self.assertEqual(receipt['ownership']['before'], 'project')
        self.assertTrue(receipt['copy']['kept_existing'])
        self.assertEqual(receipt['agent_copies'], {'agent': None, 'written': [], 'kept': [], 'removed': [],
                                                   'note': NOTE_UNINSTALLED})
        self.assertFalse((project / '.trace-os/install.json').exists())
        for root in ('.agents', '.cline'):
            self.assertFalse((project / root).exists(), root)
        if os.name != 'nt':
            self.assertTrue((project / '.claude/skills').is_symlink())
        self.assertEqual((project / 'ai/SKILLS-IN-USE.md').read_text(encoding='utf-8'), inventory(adopted=[
            f'| example-skill | {source.bare} `skills/example-skill` | {commit[:12]} | source public | no | '
            f'`{entry["adopted"]["receipt"]}` |']))
        self.assertEqual(trace_record.validate_intake(self.rows(project, 'sources/intake.jsonl'))[0], [])
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertEqual(code, 0, err)
        self.assertTrue(any(line.startswith('  copy: adapted (') for line in out.splitlines()), out)

    def test_offline_clone_reads_the_copy_and_its_original(self):
        """A clone keeps the copy and the untouched original, readable with no source reachable."""
        project = self.installed()
        source, commit, receipt = self.adopted_vector(project, via_show=True)
        code, out, err = self.run_main(project, 'status')
        self.assertEqual(code, 0, err)
        self.assertIn('  upstream: current', out.splitlines())
        clone = self.root / 'clone'
        git(self.root, 'clone', '-q', project, clone)
        source.bare.rename(source.root / 'gone.git')
        entry = self.lock(clone)['skills']['example-skill']
        self.assert_members(clone / entry['upstream']['snapshot'], VECTOR)
        if os.name != 'nt':
            self.assertEqual(package_digest(folder_members(clone / entry['upstream']['snapshot'])),
                             entry['upstream']['tree_sha256'])
            self.assertEqual(package_digest(folder_members(clone / '.skills/example-skill')),
                             entry['adopted']['tree_sha256'])
        _, problems = check_skills.check(clone)
        self.assertEqual(problems, [])
        code, out, err = self.run_main(clone, 'status', '--offline')
        self.assertEqual((code, err), (0, ''))
        lines = out.splitlines()
        self.assertEqual(lines[0], 'Skills in clone (trace_skills.py status, contract 1)')
        for line in ('example-skill',
                     f'  owner: external, from {source.bare} skills/example-skill at {commit[:12]} (requested main)',
                     '  copy: unchanged', '  detached: no', '  upstream: unknown (not fetched (--offline))',
                     f'  receipt: {receipt}', '  owner: kit', LIMIT):
            self.assertIn(line, lines)
        code, out, err = self.run_main(clone, 'status')
        self.assertEqual(code, 0, err)
        self.assertTrue(any(line.startswith('  upstream: unknown (fetch failed: ') for line in out.splitlines()), out)

    def test_intake_commit_link_and_later_run_discovery(self):
        """The records of an adoption, its commit, link, and a later record found by later_run_candidates."""
        source = self.source()
        commit = source.commit(package('example-skill', VECTOR))
        project = self.installed()
        head = git(project, 'rev-parse', 'HEAD')
        seed = (project / 'ai/SKILLS-IN-USE.md').read_bytes()
        code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
        self.assertEqual(code, 0, err)
        entry = self.lock(project)['skills']['example-skill']
        receipt_path = entry['adopted']['receipt']
        operation = Path(receipt_path).stem
        self.assertRegex(operation, r'^\d{8}T\d{6}Z-example-skill-adopt$')
        lines = out.splitlines()
        first = f'Adopted example-skill from {source.bare} skills/example-skill at {commit[:12]}.'
        self.assertIn(first, lines)
        tail = lines[lines.index(first):]
        self.assertEqual(tail, [
            f'Adopted example-skill from {source.bare} skills/example-skill at {commit[:12]}.',
            f'  receipt: {receipt_path}', '  intake: IN-001 in sources/intake.jsonl',
            '  agent copies: 3 written for claude',
            'Next: review and commit these files, then record the adoption against that commit:',
            f'  {self.tool(project)} link {receipt_path} --commit <sha>', 'Nothing was committed or pushed.'])
        definition = {'version': 1, 'format': 'format valid', 'licence': 'licence stated',
                      'handoff': self.handoff(VECTOR['SKILL.md'][0].decode('utf-8'), 'example-skill')}
        snapshot = f'.trace-os/skill-bases/{VECTOR_DIGEST}'
        # The lock (4.1, 4.2), byte for byte in bootstrap.encoded form.
        lock_bytes = (project / '.trace-os/skills.lock.json').read_bytes()
        self.assertEqual(lock_bytes, bs.encoded(json.loads(lock_bytes)))
        self.assertEqual(self.lock(project)['schema_version'], 1)
        audience = entry['source'].pop('audience')
        self.assertEqual((audience['declared'], audience['declared_by']), ('public', BY))
        entry.pop('project_audience')
        self.assertEqual(entry, {
            'manager': 'external',
            'source': {'repository': str(source.bare), 'path': 'skills/example-skill',
                       'requested_ref': commit, 'commit': commit},
            'upstream': {'tree_sha256': VECTOR_DIGEST, 'tracked_sha256': VECTOR_DIGEST, 'snapshot': snapshot},
            'adopted': {'path': '.skills/example-skill', 'tree_sha256': VECTOR_DIGEST,
                        'receipt': receipt_path, 'kept_existing': False},
            'detached': False, 'replaced_kit_copy': False, 'receipts': [receipt_path]})
        # The receipt (5.1).
        receipt_bytes = (project / receipt_path).read_bytes()
        self.assertEqual(receipt_bytes, bs.encoded(json.loads(receipt_bytes)))
        receipt = json.loads(receipt_bytes)
        self.assertEqual(self.receipts(project), [receipt_path])
        expected = {'schema_version': 1, 'kind': 'trace-skills-receipt', 'contract': 1, 'operation': 'adopt',
                    'operation_id': operation, 'name': 'example-skill', 'recovered': False,
                    'previous': None, 'definition': definition, 'not_tracked': [],
                    'source': {'repository': str(source.bare), 'path': 'skills/example-skill',
                               'requested_ref': commit, 'commit': commit},
                    'upstream': {'tree_sha256': VECTOR_DIGEST, 'tracked_sha256': VECTOR_DIGEST,
                                 'snapshot': snapshot, 'files': manifest(VECTOR), 'litter': []},
                    'copy': {'path': '.skills/example-skill', 'tree_sha256': VECTOR_DIGEST,
                             'kept_existing': False, 'files': manifest(VECTOR)},
                    'ownership': {'before': 'free', 'replaced_kit_copy': False, 'kit_files_removed': [],
                                  'retired_skills_added': []},
                    'agent_copies': {'agent': 'claude', 'removed': [], 'kept': [], 'note': NOTE_ADAPTER,
                                     'written': [f'.claude/skills/example-skill/{name}' for name in sorted(VECTOR)]},
                    'modes': {name: '100755' for name in ('.skills/example-skill/scripts/run.py',
                                                          f'{snapshot}/scripts/run.py',
                                                          '.claude/skills/example-skill/scripts/run.py')},
                    'decided': {'by': BY, 'words': WORDS, 'authority': AUTHORITY, 'execution_agent': AGENT_ID,
                                'confirmed': '--yes'}}
        self.assertEqual({key: receipt.get(key) for key in expected}, expected)
        self.assertEqual(receipt['tool']['sha256'], sha(TOOL_FILE.read_bytes()))
        self.assertEqual(receipt['project'], {'installed': True, 'kit_version': 'test-1', 'agent': 'claude',
                                              'head': head})
        self.assertEqual((receipt['audience']['comparison'], receipt['audience']['share_with_project']),
                         ('source public; the copy widens nothing', {'required': False, 'given': False}))
        for limit in RECEIPT_LIMITS:
            self.assertIn(limit, receipt['limits'])
        self.assertEqual(list(receipt['writes']), sorted(receipt['writes']))
        self.assertEqual(receipt['writes']['ai/SKILLS-IN-USE.md']['before'], sha(seed))
        self.assertEqual(receipt['writes']['.skills/example-skill/SKILL.md']['before'], None)
        self.assert_writes_in_place(project, receipt)
        self.assertEqual(receipt['records']['transition'], 'not yet: link writes it after the commit')
        # The intake row (10.1), and the receipt naming it by id and line digest.
        rows = self.rows(project, 'sources/intake.jsonl')
        self.assertEqual(trace_record.validate_intake(rows)[0], [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        line = (project / 'sources/intake.jsonl').read_bytes().split(b'\n')[0]
        self.assertEqual(receipt['records']['intake'], {'id': 'IN-001', 'registry': 'sources/intake.jsonl',
                                                        'row_sha256': sha(line)})
        total = sum(len(data) for data, _ in VECTOR.values())
        self.assertEqual({key: row.get(key) for key in ('id', 'what', 'origin', 'admitted_by', 'checked',
                                                       'used_for', 'authority', 'decision', 'object')}, {
            'id': 'IN-001',
            'what': f'skill package example-skill ({source.bare} skills/example-skill) at commit {commit}, '
                    f'package digest {VECTOR_DIGEST}',
            'origin': f'{source.bare} skills/example-skill at commit {commit}, requested as {commit}',
            'admitted_by': BY,
            'checked': f'the whole package (3 files, {total} bytes) was printed before writing and the person '
                       'confirmed it (--yes); every file\'s SHA-256 and the package digest were re-checked just '
                       f'before writing; definition 1: format valid; licence stated; {definition["handoff"]}; '
                       'audience: source public; the copy widens nothing',
            'used_for': 'a standing instruction for the agents in this project, at .skills/example-skill/',
            'authority': AUTHORITY, 'decision': 'admit', 'object': receipt_path})
        self.assertNotIn('object_sha256', row)
        # The inventory (3.2), appended after the seed and outside the routing block.
        self.assertEqual((project / 'ai/SKILLS-IN-USE.md').read_text(encoding='utf-8'),
                         seed.decode('utf-8') + '\n' + inventory(adopted=[
                             f'| example-skill | {source.bare} `skills/example-skill` | {commit[:12]} | '
                             f'source public | no | `{receipt_path}` |']))
        self.assert_equivalent(project)
        # link (6.10): refusals first, then the row, then idempotence.
        self.assert_refused(self.run_main(project, 'link', receipt_path, '--commit', 'feedfacecafe'), 2,
                            'not a commit in this repository: feedfacecafe')
        self.assert_refused(self.run_main(project, 'link', receipt_path, '--commit', head), 1,
                            f'commit {head[:12]} does not contain {receipt_path} as it is here; '
                            'name the commit that recorded this operation')
        self.assert_refused(self.run_main(project, 'link', '.trace-os/install.json', '--commit', head), 2,
                            '.trace-os/install.json is not a receipt this tool wrote')
        recorded = self.commit(project, 'Adopt example-skill\n\nExecution-agent: ' + AGENT_ID)
        code, out, err = self.run_main(project, 'link', receipt_path, '--commit', recorded)
        self.assertEqual((code, err), (0, ''))
        self.assertIn(f'Linked {receipt_path} to {recorded[:12]}: TR-001 in sources/transitions.jsonl.',
                      out.splitlines())
        transitions = self.rows(project, 'sources/transitions.jsonl')
        self.assertEqual(trace_record.validate_transitions(transitions)[0], [])
        self.assertEqual(len(transitions), 1)
        skill_digest = sha(VECTOR['SKILL.md'][0])
        self.assertEqual({key: transitions[0].get(key) for key in (
            'id', 'kind', 'object', 'object_sha256', 'checks', 'by', 'authority', 'decision', 'destination',
            'commits', 'later_run')}, {
            'id': 'TR-001', 'kind': 'default-adoption', 'object': '.skills/example-skill/SKILL.md',
            'object_sha256': skill_digest, 'checks': receipt_path, 'by': BY, 'authority': AUTHORITY,
            'decision': 'adopt',
            'destination': '.skills/example-skill/ (a standing instruction for the agents in this project)',
            'commits': [recorded], 'later_run': 'nothing stated'})
        self.assertEqual((project / receipt_path).read_bytes(), receipt_bytes, 'link edited the receipt')
        code, out, err = self.run_main(project, 'link', receipt_path, '--commit', recorded)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Already linked: TR-001 (sources/transitions.jsonl); nothing written.', out.splitlines())
        self.assertEqual(len(self.rows(project, 'sources/transitions.jsonl')), 1)
        # A later record produced under the adopted instruction is found, not judged.
        record = project / 'ai/records/20261002-fixture-later-run'
        record.mkdir(parents=True)
        (record / 'evidence.json').write_text(json.dumps({'instructions': {
            'files': {'.skills/example-skill/SKILL.md': skill_digest}, 'base_commit': recorded}}), encoding='utf-8')
        with working_directory(project):
            found = trace_record.later_run_candidates(transitions[0])
        self.assertIn('  produced under this version of it: 20261002-fixture-later-run', found)

    def test_untrusted_package_is_never_executed(self):
        """Rule 1: a package is read as data; a script that would leave a marker if run never does."""
        payload = f'open({str(self.marker)!r}, "a").write("executed\\n")\n'.encode('utf-8')
        members = {'SKILL.md': (skill_md('untrusted-skill'), '100644'),
                   'scripts/run.py': (payload, '100755'), '__init__.py': (payload, '100644'),
                   'conftest.py': (payload, '100644'), 'setup.py': (payload, '100644'),
                   'tests/test_untrusted.py': (payload, '100644'),
                   'scripts/install.sh': (f'#!/bin/sh\necho executed >> {self.marker}\n'.encode('utf-8'), '100755')}
        source = self.source()
        commit = source.commit(package('untrusted-skill', members))
        project = self.installed()
        code, out, err = self.run_main(project, 'list', source.bare, '--ref', 'main')
        self.assertEqual(code, 0, err)
        self.assertIn('  skills/untrusted-skill: untrusted-skill', out.splitlines())
        self.assertIn('Nothing was copied into any project.', out.splitlines())
        self.assertFalse(self.marker.exists(), 'list ran a package file')
        self.show(project, source, 'skills/untrusted-skill')
        self.assertFalse(self.marker.exists(), 'show ran a package file')
        code, out, err = self.adopt(project, source, 'skills/untrusted-skill', commit)
        self.assertEqual(code, 0, err)
        self.assertFalse(self.marker.exists(), 'adopt ran a package file')
        for path, (data, _) in members.items():
            self.assertEqual((project / '.skills/untrusted-skill' / path).read_bytes(), data, path)
        code, out, err = self.run_main(project, 'status')
        self.assertEqual(code, 0, err)
        self.assert_equivalent(project)
        self.assertFalse(self.marker.exists(), 'a package file was run')

    def test_credentials_are_refused_and_never_written(self):
        """S1 and S2 refuse an address before any fetch; no lock, receipt, candidate or journal holds a secret."""
        token = 'ghp_FAKE0000000000000000000000000000000000'
        secret = 'trace-test-secret-value-0000'
        with mock.patch.dict(os.environ, {'GH_TOKEN': token, 'GITHUB_TOKEN': token, 'TRACE_TEST_SECRET': secret}):
            project = self.installed()
            source = self.source()
            commit = source.commit(package('example-skill', VECTOR))
            before = tree(project)
            s1 = ('the repository address carries a user name and password, a token, a query or a fragment; '
                  'give it without them and let Git\'s credential helper supply access')
            for address in (f'https://user:{token}@example.invalid/library.git',
                            f'https://{token}@example.invalid/library.git',
                            f'https://example.invalid/library.git?access_token={token}',
                            'https://example.invalid/library.git#main',
                            f'ssh://git:{token}@example.invalid/library.git'):
                with self.subTest(address=address):
                    self.assert_refused(self.run_main(project, 'list', address), 2, s1)
                    self.assert_refused(self.run_main(project, 'adopt', address, 'skills/example-skill', '--ref',
                                                      commit, '--yes', *PUBLIC, *WHO), 2, s1)
            # "--" lets the address reach the tool, which refuses it before Git sees it as an option.
            self.assert_refused(self.run_main(project, 'list', '--', '--upload-pack=touch-a-file'), 2,
                                'a repository address cannot begin with "-"')
            self.assertEqual(tree(project), before)
            for address in ('ssh://git@example.invalid/library.git', 'git@example.invalid:library.git'):
                with self.subTest(accepted=address):
                    code, out, err = self.run_main(project, 'list', address)
                    self.assertEqual(code, 2, err)
                    self.assertTrue(err.startswith(self.message(f'could not fetch {address}: ')[:-1]), err)
            written = []
            real = trace_skills.bootstrap.write_atomic

            def capture(path, data):
                written.append((str(path), bytes(data)))
                return real(path, data)
            candidate = self.show(project, source, 'skills/example-skill')
            with mock.patch.object(trace_skills.bootstrap, 'write_atomic', capture):
                code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
            self.assertEqual(code, 0, err)
            entry = self.lock(project)['skills']['example-skill']
            files = {'candidate': candidate.read_bytes(),
                     'lock': (project / '.trace-os/skills.lock.json').read_bytes(),
                     'receipt': (project / entry['adopted']['receipt']).read_bytes()}
            files.update({f'operation journal, write {index}': data for index, (path, data) in enumerate(written)
                          if path.endswith('skill-operation.json')})
            self.assertTrue(any(name.startswith('operation journal') for name in files),
                            'the operation journal was not written with bootstrap.write_atomic')
            for name, data in files.items():
                text = data.decode('utf-8')
                for value in (token, secret, str(self.home)):
                    self.assertNotIn(value, text, name)
            for document in (json.loads(files['lock'])['skills']['example-skill'], json.loads(files['receipt']),
                             json.loads(files['candidate'])):
                self.assertNotIn('@', document['source']['repository'])
            self.assert_equivalent(project)

    # -------------------------------------- scenarios that need builder D's commands

    def test_hand_merge_recorded_with_accept_merged(self):
        """6.5 and 6.6: an adapted copy gets three versions and no write; the person's merge is recorded as it stands."""
        self.require('update', '--accept-merged')
        project = self.installed()
        source, first, _ = self.adopted_vector(project, via_show=True)
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'Our line.\n')
        git(project, 'commit', '-qam', 'Adapt example-skill')
        upstream_skill = skill_md('example-skill', 'Body.\nUpstream line.\n')
        second = source.commit({'skills/example-skill/SKILL.md': (upstream_skill, '100644')})
        before = tree(project)
        code, out, err = self.run_main(project, 'update', 'example-skill', *PUBLIC, *WHO)
        self.assertEqual(code, 1, err)
        lines = out.splitlines()
        self.assertIn('example-skill is adapted here; the tool does not merge. The three versions:', lines)
        self.assertIn(f'  original (what was adopted): .trace-os/skill-bases/{VECTOR_DIGEST}', lines)
        self.assertIn('  this project\'s copy: .skills/example-skill/', lines)
        last = out.rstrip('\n').splitlines()[-1]
        prefix = (f'Nothing was written. Merge by hand into .skills/example-skill/, then record the merge: '
                  f'{self.tool(project)} update example-skill --accept-merged --candidate ')
        self.assertTrue(last.startswith(prefix), last)
        candidate_path = Path(last[len(prefix):])
        candidate = json.loads(candidate_path.read_text(encoding='utf-8'))
        self.assertEqual((candidate['created_by'], candidate['for']['operation'], candidate['for']['from_commit']),
                         ('update', 'accept-merged', first))
        self.assertEqual(tree(project), before, 'update wrote into the project for an adapted copy')
        merged = upstream_skill + b'Our line.\n'
        copy.write_bytes(merged)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--candidate',
                                       candidate_path, '--yes', *PUBLIC, *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn(f'Recorded the merged copy of example-skill against {second[:12]}.', out.splitlines())
        self.assertEqual(copy.read_bytes(), merged, 'accept-merged changed the copy')
        self.assertEqual((project / '.claude/skills/example-skill/SKILL.md').read_bytes(), merged)
        upstream = dict(VECTOR, **{'SKILL.md': (upstream_skill, '100644')})
        entry = self.lock(project)['skills']['example-skill']
        merged_digest = package_digest(folder_members(project / '.skills/example-skill'))
        self.assertEqual((entry['source']['commit'], entry['upstream']['tree_sha256'], entry['adopted']['tree_sha256']),
                         (second, package_digest(upstream), merged_digest))
        self.assert_members(project / entry['upstream']['snapshot'], upstream)
        self.assertFalse((project / '.trace-os/skill-bases' / VECTOR_DIGEST / 'SKILL.md').exists())
        receipt = self.read_json(project, entry['adopted']['receipt'])
        self.assertEqual((receipt['operation'], receipt['previous']['commit'], receipt['previous']['tree_sha256']),
                         ('accept-merged', first, VECTOR_DIGEST))
        rows = self.rows(project, 'sources/intake.jsonl')
        self.assertEqual(trace_record.validate_intake(rows)[0], [])
        self.assertTrue(rows[-1]['checked'].startswith(
            f'the three versions were printed by update on {candidate["created_at"]}; the merged copy is recorded as '
            f'it stands with digest {merged_digest[:12]} (who merged it was not stated); the upstream original\'s '
            f'files were re-checked against commit {second[:12]}; definition 1 of the merged copy: '), rows[-1]['checked'])
        self.assert_equivalent(project)
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertTrue(any(line.startswith('  copy: adapted (') for line in out.splitlines()), out)
        # A merge recorded with --ref and no update run before it says so (AH-231).
        self.commit(project, 'Record the merge')
        licence = b'Apache License 2.0\nWith a notice.\n'
        third = source.commit({'skills/example-skill/LICENSE.txt': (licence, '100644')})
        (project / '.skills/example-skill/LICENSE.txt').write_bytes(licence)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--accept-merged', '--ref', third,
                                       '--yes', *PUBLIC, *WHO)
        self.assertEqual(code, 0, err)
        rows = self.rows(project, 'sources/intake.jsonl')
        merged_digest = package_digest(folder_members(project / '.skills/example-skill'))
        self.assertTrue(rows[-1]['checked'].startswith(
            'no update run preceded this; the new upstream\'s manifest and its difference from the merged copy '
            f'were printed by this command; the merged copy is recorded as it stands with digest '
            f'{merged_digest[:12]} (who merged it was not stated); the upstream original\'s files were re-checked '
            f'against commit {third[:12]}; definition 1 of the merged copy: '), rows[-1]['checked'])
        self.assert_equivalent(project)

    def test_detach_then_attach(self):
        """6.7: detached and adapted are independent; a detached copy is reported behind and offered nothing."""
        self.require('update', '--detach')
        self.require('update', '--attach')
        project = self.installed()
        source, first, adopt_receipt = self.adopted_vector(project, via_show=True)
        intake = len(self.rows(project, 'sources/intake.jsonl'))
        code, out, err = self.run_main(project, 'update', 'example-skill', '--detach', '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn('Detached example-skill. Nothing was committed or pushed.', out.splitlines())
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['detached'], entry['detached_by']), (True, BY))
        self.assertIn('detached_at', entry)
        detach_receipt = self.read_json(project, entry['receipts'][-1])
        self.assertEqual((detach_receipt['operation'], detach_receipt['records']['intake']), ('detach', None))
        self.assertEqual(len(self.rows(project, 'sources/intake.jsonl')), intake)
        self.assert_equivalent(project)
        code, out, err = self.run_main(project, 'update', 'example-skill', '--detach', '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn('Already detached: example-skill; nothing to do.', out.splitlines())
        second = source.commit({'skills/example-skill/SKILL.md': (skill_md('example-skill', 'Revised.\n'), '100644')})
        before = tree(project)
        result = self.run_main(project, 'update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        self.assert_refused(result, 1, 'example-skill is detached: updates are not offered; '
                                       'status reports whether its source moved')
        self.assertEqual(tree(project), before)
        code, out, err = self.run_main(project, 'status')
        self.assertIn('  detached: yes', out.splitlines())
        self.assertIn(f'  upstream: behind {second[:12]}', out.splitlines())
        code, out, err = self.run_main(project, 'update', 'example-skill', '--attach', '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn('Attached example-skill. Nothing was committed or pushed.', out.splitlines())
        entry = self.lock(project)['skills']['example-skill']
        self.assertFalse(entry['detached'])
        self.assertNotIn('detached_at', entry)
        self.assertNotIn('detached_by', entry)
        self.assertEqual(entry['receipts'][0], adopt_receipt)
        self.assertEqual(len(entry['receipts']), 3)
        self.assert_equivalent(project)

    def test_retire_keeps_the_original_and_a_new_adoption_carries_its_receipts(self):
        """6.8 without --drop-base: copies and agent copies go, the original and the receipts stay."""
        self.require('retire')
        project = self.installed()
        source, commit, adopt_receipt = self.adopted_vector(project)
        copy = project / '.skills/example-skill/SKILL.md'
        copy.write_bytes(copy.read_bytes() + b'Not committed.\n')
        result = self.run_main(project, 'retire', 'example-skill', '--yes', *WHO)
        self.assert_refused(result, 1, 'retiring example-skill would delete changes Git has not recorded '
                                       '(.skills/example-skill/SKILL.md); commit them first, so that history keeps them')
        git(project, 'checkout', '--', '.skills/example-skill/SKILL.md')
        intake = len(self.rows(project, 'sources/intake.jsonl'))
        code, out, err = self.run_main(project, 'retire', 'example-skill', '--yes', *WHO)
        self.assertEqual(code, 0, err)
        snapshot = f'.trace-os/skill-bases/{VECTOR_DIGEST}'
        self.assertEqual(out.rstrip('\n').splitlines()[-3:], ['Retired example-skill.',
                                                              f'Original kept at {snapshot}.',
                                                              'Nothing was committed or pushed.'])
        self.assertFalse((project / '.skills/example-skill').exists())
        self.assertFalse((project / '.claude/skills/example-skill').exists())
        self.assert_members(project / snapshot, VECTOR)
        state = self.read_json(project, '.trace-os/install.json')
        self.assertEqual([name for name in state['files'] if 'example-skill' in name], [])
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['manager'], entry['was'], entry['held_by'], entry['upstream']['dropped']),
                         ('retired', 'external', None, False))
        receipt = self.read_json(project, entry['receipt'])
        self.assertEqual(receipt['operation'], 'retire')
        self.assertEqual(receipt['history'], 'Git history keeps every file this operation removed, '
                                             'once the removal is committed.')
        self.assertIn('A file that was never committed is not kept anywhere.', receipt['limits'])
        retire_receipt = entry['receipt']
        self.assertEqual(entry['receipts'], [adopt_receipt, retire_receipt])
        self.assertEqual(len(self.rows(project, 'sources/intake.jsonl')), intake)
        text = (project / 'ai/SKILLS-IN-USE.md').read_text(encoding='utf-8')
        self.assertTrue(any(line.startswith('| example-skill | adopted | ') for line in text.splitlines()), text)
        self.assert_equivalent(project)
        code, out, err = self.run_main(project, 'retire', 'example-skill', '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn('Already retired: example-skill; nothing to do.', out.splitlines())
        self.commit(project, 'Retire example-skill')
        code, out, err = self.adopt(project, source, 'skills/example-skill', commit)
        self.assertEqual(code, 0, err)
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual(entry['manager'], 'external')
        self.assertEqual(entry['receipts'], [adopt_receipt, retire_receipt, entry['adopted']['receipt']])
        self.assertEqual(self.read_json(project, entry['adopted']['receipt'])['ownership']['before'], 'retired')
        self.assert_equivalent(project)

    def test_retire_with_drop_base(self):
        """6.8 with --drop-base: the original goes too, and the lock says it was dropped."""
        self.require('retire', '--drop-base')
        project = self.installed()
        self.adopted_vector(project)
        code, out, err = self.run_main(project, 'retire', 'example-skill', '--drop-base', '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.rstrip('\n').splitlines()[-3:], ['Retired example-skill.',
                                                              'Original dropped; Git history keeps it.',
                                                              'Nothing was committed or pushed.'])
        bases = project / '.trace-os/skill-bases'
        # AH-242: the folder's README.md is no original, and stays.
        self.assertEqual([path.relative_to(bases).as_posix() for path in bases.rglob('*') if path.is_file()]
                         if bases.is_dir() else [], ['README.md'])
        self.assertTrue(self.lock(project)['skills']['example-skill']['upstream']['dropped'])
        self.assert_equivalent(project)

    def test_declare_records_an_attested_digest(self):
        """6.9: a skill held outside the project is recorded with the holder's digest; nothing is copied."""
        self.require('declare')
        project = self.installed()
        held = self.home / '.claude/skills/private-skill'
        held.mkdir(parents=True)
        (held / 'SKILL.md').write_bytes(skill_md('private-skill', 'A private method.\n'))
        state = (project / '.trace-os/install.json').read_bytes()
        result = self.run_main(project, 'declare', 'example', '--held-by', 'Alex Holder', '--yes', *WHO)
        self.assert_refused(result, 1, 'example is already a skill of this project (kit); '
                                       'declare records only a skill that is not in the project')
        code, out, err = self.run_main(project, 'declare', 'private-skill', '--held-by', 'Alex Holder',
                                       '--path', held, '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn('Declared private-skill, held by Alex Holder. Nothing was committed or pushed.',
                      out.splitlines())
        digest = package_digest(folder_members(held))
        entry = self.lock(project)['skills']['private-skill']
        self.assertEqual({key: entry.get(key) for key in ('manager', 'held_by', 'path', 'digest', 'limitation',
                                                          'declared_by')}, {
            'manager': 'outside', 'held_by': 'Alex Holder', 'path': '~/.claude/skills/private-skill',
            'digest': {'tree_sha256': digest, 'basis': 'attested'}, 'limitation': DEFAULT_LIMITATION,
            'declared_by': BY})
        receipt = self.read_json(project, entry['receipt'])
        self.assertEqual((receipt['operation'], receipt['source'], receipt['copy'], receipt['records']['intake']),
                         ('declare', None, None, None))
        self.assertFalse((project / '.skills/private-skill').exists())
        self.assertFalse((project / '.claude/skills/private-skill').exists())
        self.assertEqual((project / '.trace-os/install.json').read_bytes(), state)
        self.assertEqual(self.rows(project, 'sources/intake.jsonl'), [])
        self.assertIn(f'| private-skill | Alex Holder | {digest[:12]} (attested) | {DEFAULT_LIMITATION} |',
                      (project / 'ai/SKILLS-IN-USE.md').read_text(encoding='utf-8').splitlines())
        code, out, err = self.run_main(project, 'status', '--offline')
        self.assertIn(f'  owner: outside, held by Alex Holder; digest {digest[:12]} (attested)', out.splitlines())
        self.assert_equivalent(project)
        code, out, err = self.run_main(project, 'declare', 'private-skill', '--held-by', 'Alex Holder',
                                       '--path', held, '--yes', *WHO)
        self.assertEqual(code, 0, err)
        self.assertIn('Already declared: private-skill; nothing to do.', out.splitlines())

    @unittest.skipIf(os.name == 'nt', 'Windows keeps no executable bit')
    def test_mode_only_upstream_change_reaches_the_copy_through_the_forward_step(self):
        """AH-230 (12.2, 12.4): a mode change with identical bytes is applied by the forward step and undone by recover."""
        self.require('update')
        source = self.source()
        project = self.installed()
        source, first, _ = self.adopted_vector(project, source)
        second = source.commit({'skills/example-skill/scripts/run.py': (RUN, '100644')}, 'run.py is no longer executable')
        third = source.commit({'skills/example-skill/scripts/run.py': (RUN, '100755')}, 'run.py is executable again')
        copy = project / '.skills/example-skill/scripts/run.py'
        agent_copy = project / '.claude/skills/example-skill/scripts/run.py'
        old_base = project / '.trace-os/skill-bases' / VECTOR_DIGEST / 'scripts/run.py'
        plain = dict(VECTOR, **{'scripts/run.py': (RUN, '100644')})
        update = ('update', 'example-skill', '--ref', second, '--yes', *PUBLIC, *WHO)
        # Interrupted inside the transaction: undone, and modes_before puts the removed original's bit back.
        with self.interrupt_at(project, lambda rel, data: rel == '.trace-os/skills.lock.json'):
            code, out, err = self.run_main(project, *update)
        self.assertEqual(code, 130, err)
        operation = self.journal(project)['operation_id']
        self.assertEqual(self.run_main(project, 'recover'),
                         (0, f'Undone: {operation}; the project is as it was before. '
                             'Run the command again to update.\n', ''))
        for path in (copy, agent_copy, old_base):
            self.assertTrue(executable(path), path)
        self.assertEqual(old_base.read_bytes(), RUN)
        self.assert_equivalent(project)
        # Interrupted after the transaction, before the forward steps: recover applies the target mode.
        with self.interrupt_at(project, lambda rel, data: rel == '.trace-os/receipts/skill-operation.json'
                               and json.loads(data).get('state') == 'applied'):
            code, out, err = self.run_main(project, *update)
        self.assertEqual(code, 130, err)
        operation = self.journal(project)['operation_id']
        self.assertTrue(executable(copy), 'the mode changed before the forward step ran')
        code, out, err = self.run_main(project, 'recover')
        self.assertEqual((code, out, err), (0, f'Completed: {operation}; receipt '
                                               f'.trace-os/skill-adoptions/{operation}.json.\n', ''))
        for path in (copy, agent_copy):
            self.assertFalse(executable(path), path)
        self.assertEqual(copy.read_bytes(), RUN)
        entry = self.lock(project)['skills']['example-skill']
        self.assertEqual((entry['source']['commit'], entry['upstream']['tree_sha256']), (second, package_digest(plain)))
        self.assertFalse(executable(project / entry['upstream']['snapshot'] / 'scripts/run.py'))
        self.assertFalse(old_base.exists())
        receipt = self.read_json(project, entry['adopted']['receipt'])
        self.assertTrue(receipt['recovered'])
        self.assertEqual(receipt['modes'], {})
        self.assert_equivalent(project)
        self.commit(project, 'run.py is no longer executable')
        self.assertTrue(git(project, 'ls-files', '-s', '--', copy.relative_to(project)).startswith('100644 '))
        # The reverse, run through.
        code, out, err = self.run_main(project, 'update', 'example-skill', '--ref', third, '--yes', *PUBLIC, *WHO)
        self.assertEqual(code, 0, err)
        for path in (copy, agent_copy):
            self.assertTrue(executable(path), path)
        self.assert_equivalent(project)
        self.commit(project, 'run.py is executable again')
        self.assertTrue(git(project, 'ls-files', '-s', '--', copy.relative_to(project)).startswith('100755 '))


if __name__ == '__main__':
    unittest.main()
