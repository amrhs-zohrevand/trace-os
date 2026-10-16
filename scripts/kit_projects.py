#!/usr/bin/env python3
"""Find the TRACE projects under the folders you name, plan a release for all of them, update them.

  python scripts/kit_projects.py list --root ~/Developer
  python scripts/kit_projects.py plan --root ~/Developer \\
      --release-archive trace-os-0.1.0-rcN.zip --sha256-file trace-os-0.1.0-rcN.zip.sha256
  python scripts/kit_projects.py update --root ~/Developer --all \\
      --release-archive trace-os-0.1.0-rcN.zip --sha256-file trace-os-0.1.0-rcN.zip.sha256

A project is a Git working tree with .trace-os/install.json (or the same file in an
earlier folder, .traceware/ or .trace-kit/, which is found too), which says the
release, profile and agent it has. plan and update verify the archive's digest
once, unpack it once and run that release's own scripts/bootstrap.py on each
project: --plan to show what would change, then --update, or --update-chain
for a project more than one release behind (with --release-store, or
--download for the archives the releases page carries, 0.1.0-rc24 and
later). update prints the plan first and applies only after a
confirmation: --yes, or an answer at the terminal. This script writes no
project file itself and never commits or pushes; each updated project is left
for its person to review and commit.

--download fetches the release with the GitHub CLI (gh) when it is installed
and signed in. Without gh, download the archive and its .sha256 file from the
releases page by hand.

Ctrl-C, or SIGTERM or SIGHUP, while a project updates is passed on to that
project's bootstrap.py, which restores what it applied; update waits for it
and reports the project as restored, or as recovery incomplete with the
--recover command to run, and attempts no further project.

Exit codes: 0 all good; 1 something needs a person (a conflict, a failed
update, a project that cannot be updated as it stands, an update nobody
confirmed); 2 a usage or environment error, such as a digest that does not
match; 128 plus the signal number (130 for Ctrl-C) when interrupted.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

REPOSITORY = 'amrhs-zohrevand/trace-os'
RELEASES_PAGE = f'https://github.com/{REPOSITORY}/releases'
# The installation state's folder, then its earlier names, newest first: .traceware/ in 0.1.0-rc19 and
# .trace-kit/ up to 0.1.0-rc18. A project keeps an earlier folder until an update to a release naming a newer
# one moves it, so every path below is read in the newest folder first, then in each earlier one.
STATE_DIRS = ('.trace-os', '.traceware', '.trace-kit')
STATE = '.trace-os/install.json'
UPDATE_HISTORY = '.trace-os/update-history.jsonl'
# What bootstrap.py leaves when a setup or an update stopped before it finished.
UNFINISHED = ('.trace-os/transaction.json', '.trace-os/pending-update.json')
# The release archive's one top folder: trace-os/, or traceware/ in 0.1.0-rc19, or trace-kit-starter/ up to
# 0.1.0-rc18.
ARCHIVE_ROOTS = ('trace-os', 'traceware', 'trace-kit-starter')
SKIP_NAMES = {'node_modules', '__pycache__', 'site-packages', 'venv'}
# Folders a sync client owns. SETUP-AGENT.md asks for working copies outside them,
# so they are not searched, and the listing names the ones it passed over.
CLOUD_NAMES = {'dropbox', 'google drive', 'googledrive', 'my drive', 'icloud drive',
               'iclouddrive', 'mobile documents', 'cloudstorage', 'box', 'box sync',
               'pcloud drive', 'surfdrive', 'nextcloud', 'owncloud'}
# How long an interrupted bootstrap.py is given to stop and restore its project before
# it is ended (review 2026-09-30, finding 1). Its own stop takes seconds: up to five
# for a running check to end, then the restore from its backups.
RECOVERY_WAIT = 120
# The repository's history begins at 0.1.0-rc24: the candidates before it are withdrawn and not on its
# releases page, so a chain from an earlier release takes their archives from a store the person names.
BY_HAND = (f'Download the release archive and its .sha256 file (trace-os-<version>.zip) from {RELEASES_PAGE}, '
           'which carries 0.1.0-rc24 and later, then pass --release-archive <zip> --sha256-file <zip>.sha256, '
           'or --expected-sha256 with a digest from a channel you trust. The earlier candidates are withdrawn '
           'and not on that page: name a folder that holds the archives a chain needs with --release-store.')
NOT_FOUND = ('No TRACE project found (a Git working tree with .trace-os/install.json, or the same file in '
             '.traceware/ or .trace-kit/ before 0.1.0-rc20).')


class Stop(Exception):
    """End the run with a message and an exit code (2 unless stated)."""

    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


class Interrupted(KeyboardInterrupt):
    """Ctrl-C, or SIGTERM or SIGHUP raised like it, with what an interrupted bootstrap.py did.

    Review 2026-09-30, finding 1: run_bootstrap re-raises an interruption as this
    once the bootstrap.py it runs has returned, or has been ended because the
    wait ran out (`expired`).
    """

    def __init__(self, signum, exit_code=None, expired=False, message=''):
        super().__init__(signal.Signals(signum).name)
        self.signum, self.exit_code, self.expired, self.message = signum, exit_code, expired, message


@contextlib.contextmanager
def interruptible():
    """While this is open, SIGTERM and SIGHUP raise Interrupted, as Ctrl-C raises KeyboardInterrupt.

    Review 2026-09-30, finding 1: an agent's command timeout or a closed terminal
    is then passed on to a running bootstrap.py and waited for, as Ctrl-C is. As
    in bootstrap.py, a signal the caller ignores stays ignored, and on Windows or
    off the main thread nothing changes.
    """
    names = [name for name in ('SIGTERM', 'SIGHUP') if hasattr(signal, name)
             and signal.getsignal(getattr(signal, name)) is not signal.SIG_IGN]
    if threading.current_thread() is not threading.main_thread() or os.name == 'nt':
        yield
        return

    def stop(signum, frame):
        raise Interrupted(signum)

    previous = {name: signal.signal(getattr(signal, name), stop) for name in names}
    try:
        yield
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)


def say(text):
    """A line on stderr that a closed terminal cannot turn into an error mid-wait."""
    with contextlib.suppress(OSError, ValueError):
        print(text, file=sys.stderr, flush=True)


def shown(path):
    return Path(path).as_posix()


def sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            hasher.update(block)
    return hasher.hexdigest()


def located(project, name):
    """`name`, a path under .trace-os/, where this project keeps it: the newest folder first, then each earlier one."""
    project = Path(project)
    for folder in STATE_DIRS:
        if (project / folder / 'install.json').is_file():
            return folder + name[len(STATE_DIRS[0]):]
    return name


def git(path, *args):
    """Run a read-only Git command; None when it fails. No optional locks, so no index refresh is written."""
    try:
        run = subprocess.run(['git', '--no-optional-locks', *args], cwd=path, capture_output=True,
                             text=True, encoding='utf-8', errors='replace')
    except OSError:
        return None
    return run.stdout if run.returncode == 0 else None


# Discovery ---------------------------------------------------------------

def is_cloud(folder):
    name = folder.name.casefold()
    return name in CLOUD_NAMES or name.startswith(('onedrive', 'dropbox (', 'googledrive-'))


def discover(roots, depth):
    """Every TRACE project at most `depth` folders below a root, and the cloud folders passed over.

    A folder holding .git and .trace-os/install.json (or the same file in
    .traceware/ or .trace-kit/, before 0.1.0-rc20) is a project and is not
    searched further. Hidden folders, symlinks, dependency folders and sync
    folders are not entered; a root itself is searched whatever its name.
    """
    found, passed = set(), set()
    home_library = Path.home() / 'Library'
    for root in roots:
        root = root.expanduser()
        if not root.is_dir():
            raise Stop(f'--root is not a directory: {shown(root)}')
        stack = [(root.resolve(), 0)]
        while stack:
            folder, level = stack.pop()
            if (folder / '.git').exists() and (folder / located(folder, STATE)).is_file():
                found.add(folder)
                continue
            if level >= depth:
                continue
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_symlink() or not child.is_dir():
                        continue
                except OSError:
                    continue
                if child.name.startswith('.') or child.name in SKIP_NAMES or child == home_library:
                    continue
                if is_cloud(child):
                    passed.add(child)
                    continue
                stack.append((child, level + 1))
    return sorted(found), sorted(passed)


def read_project(path):
    """What the project says about itself: install state, working tree, unfinished operations."""
    row = {'path': shown(path), 'version': None, 'profile': None, 'agent': None,
           'dirty': None, 'changed_files': 0, 'untracked_files': 0,
           'unfinished': [located(path, name) for name in UNFINISHED if (path / located(path, name)).exists()],
           'state': located(path, STATE), 'error': None}
    try:
        state = json.loads((path / row['state']).read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('not an object')
    except (OSError, ValueError):
        row['error'] = f"{row['state']} is missing or unreadable"
        return row
    row.update(version=state.get('version'), profile=state.get('profile'), agent=state.get('agent'))
    top = git(path, 'rev-parse', '--show-toplevel')
    if top is None or Path(top.strip()).resolve() != path:
        row['error'] = 'not the top of its own Git working tree'
        return row
    status = git(path, 'status', '--porcelain', '--untracked-files=normal')
    if status is None:
        row['error'] = 'git status failed'
        return row
    lines = [line for line in status.splitlines() if line.strip()]
    row['untracked_files'] = sum(line.startswith('??') for line in lines)
    row['changed_files'] = len(lines) - row['untracked_files']
    unborn = git(path, 'rev-parse', '--verify', '--quiet', 'HEAD') is None
    # Tracked changes make a tree dirty; untracked files are counted and shown but
    # do not, since the updater never overwrites a file the project put in its way.
    row['dirty'] = bool(row['changed_files']) or unborn
    if unborn:
        row['changed_files'] = row['changed_files'] or len(lines)
    return row


def tree_text(row):
    if row['dirty'] is None:
        return 'unknown'
    text = f"uncommitted ({row['changed_files']})" if row['dirty'] else 'clean'
    if row['untracked_files']:
        text += f", {row['untracked_files']} untracked"
    return text


# Releases ----------------------------------------------------------------

def version_key(version):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?', str(version or ''))
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch)), (int(rc) if rc else None)


def nominated_path(manifest, installed):
    """The nominated pairs from `installed` to the release, in order; None when none leads there.

    Only for the listing and the plan. bootstrap.py --update-chain walks the
    same pairs itself and is what decides.
    """
    newest = manifest.get('version')
    pairs = {}
    for item in manifest.get('supported_upgrades') or []:
        if isinstance(item, dict):
            pairs.setdefault(item.get('from_version'), []).append(item.get('to_version'))
    path, version, seen = [], installed, set()
    while version != newest:
        following = pairs.get(version, [])
        if version in seen or len(following) != 1:
            return None
        seen.add(version)
        path.append((version, following[0]))
        version = following[0]
    return path


def standing(row, release):
    """How the installed release stands to the given one: (kind, hops, words)."""
    installed, version = row['version'], release['version']
    if not installed:
        return 'unknown', None, f"{row.get('state', STATE)} names no version"
    if installed == version:
        return 'current', [], 'current'
    manifest = release.get('manifest')
    if manifest is not None:
        hops = nominated_path(manifest, installed)
        if hops is None:
            return 'no-path', None, (f'{version} nominates no upgrade path from {installed}; '
                                     'the project may be newer or outside the nominated pairs')
    else:
        old, new = version_key(installed), version_key(version)
        if old is None or new is None or old[0] != new[0] or None in (old[1], new[1]):
            return 'unknown', None, f'{installed} and {version} cannot be compared by version number'
        if old[1] > new[1]:
            return 'no-path', None, f'{installed} is newer than {version}'
        hops = [(None, None)] * (new[1] - old[1])
    if len(hops) == 1:
        return 'behind-one', hops, 'one release behind: --update applies'
    return 'behind-chain', hops, f'{len(hops)} releases behind: the chain (--update-chain)'


def read_sidecar(path):
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as exc:
        raise Stop(f'cannot read the digest file {shown(path)}: {exc.strerror or exc}') from None
    token = text.split()[0] if text.split() else ''
    if not re.fullmatch(r'[0-9a-fA-F]{64}', token):
        raise Stop(f'{shown(path)} does not begin with a SHA-256 digest')
    return token.lower()


def archive_manifest(archive):
    try:
        with zipfile.ZipFile(archive) as zipped:
            names = [name for name in zipped.namelist() if name.endswith('starter-manifest.json')]
            if len(names) != 1 or names[0] not in [root + '/starter-manifest.json' for root in ARCHIVE_ROOTS]:
                raise Stop('release archive must contain one trace-os/starter-manifest.json '
                           '(traceware/ in 0.1.0-rc19, trace-kit-starter/ up to 0.1.0-rc18)')
            manifest = json.loads(zipped.read(names[0]).decode('utf-8'))
    except (zipfile.BadZipFile, OSError) as exc:
        raise Stop(f'release archive is not a readable ZIP: {shown(archive)}') from exc
    except ValueError as exc:
        raise Stop('the release manifest is not valid JSON') from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get('version'), str):
        raise Stop('the release manifest names no version')
    return manifest


def unpack(archive, workdir):
    """Unpack the release once; every path must stay under its one top folder (trace-os/, or
    traceware/ in 0.1.0-rc19, or trace-kit-starter/ up to 0.1.0-rc18)."""
    with zipfile.ZipFile(archive) as zipped:
        tops = set()
        for info in zipped.infolist():
            name = info.filename
            parts = name.rstrip('/').split('/')
            if '\\' in name or name.startswith('/') or ':' in parts[0] or parts[0] not in ARCHIVE_ROOTS \
                    or any(part in ('', '.', '..') for part in parts):
                raise Stop('release archive names a path outside trace-os/ (traceware/ in 0.1.0-rc19, '
                           'trace-kit-starter/ up to 0.1.0-rc18): ' + name)
            tops.add(parts[0])
        if len(tops) != 1:
            raise Stop('release archive must hold one top folder')
        zipped.extractall(workdir)
    root = workdir / tops.pop()
    if not (root / 'scripts' / 'bootstrap.py').is_file():
        raise Stop('the release archive carries no scripts/bootstrap.py')
    return root


def gh_command():
    gh = shutil.which('gh')
    if gh is None:
        raise Stop('--download needs the GitHub CLI (gh), which is not installed. ' + BY_HAND)
    try:
        signed_in = subprocess.run([gh, 'auth', 'status'], capture_output=True).returncode == 0
    except OSError:
        signed_in = False
    if not signed_in:
        raise Stop('--download needs gh signed in (gh auth login). ' + BY_HAND)
    return gh


def gh_run(gh, *args):
    run = subprocess.run([gh, *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if run.returncode:
        raise Stop(f"gh {' '.join(args[:2])} failed: {run.stderr.strip() or run.stdout.strip()}. " + BY_HAND)
    return run.stdout


def newest_tag(gh):
    rows = json.loads(gh_run(gh, 'release', 'list', '-R', REPOSITORY, '--limit', '50',
                             '--json', 'tagName,isDraft,createdAt') or '[]')
    rows = [row for row in rows if not row.get('isDraft')]
    if not rows:
        raise Stop(f'{REPOSITORY} lists no release. ' + BY_HAND)
    return max(rows, key=lambda row: row.get('createdAt', ''))['tagName']


def download_archive(gh, version, directory, sidecar=True):
    """Fetch one release's archive, and its .sha256 file, under whatever name the release carries it.

    From 0.1.0-rc20 the archive is trace-os-<version>.zip; earlier releases keep
    the names they were published under, which changed more than once, so the
    release's one .zip is fetched by pattern, into a folder of its own, and moved
    into `directory`.
    """
    landing = Path(tempfile.mkdtemp(prefix='.download-', dir=directory))
    try:
        patterns = ['-p', '*.zip'] + (['-p', '*.zip.sha256'] if sidecar else [])
        gh_run(gh, 'release', 'download', version, '-R', REPOSITORY, *patterns, '-D', str(landing), '--clobber')
        found = sorted(landing.glob('*.zip'))
        if len(found) != 1:
            raise Stop(f'the release {version} carries {len(found)} .zip archives, not one. ' + BY_HAND)
        archive = directory / found[0].name
        os.replace(found[0], archive)
        if sidecar:
            digest_file = landing / (found[0].name + '.sha256')
            if not digest_file.is_file():
                raise Stop(f'the release {version} carries no {found[0].name}.sha256. ' + BY_HAND)
            os.replace(digest_file, directory / digest_file.name)
    finally:
        shutil.rmtree(landing, ignore_errors=True)
    return archive


def resolve_release(args, workdir, required):
    """The release named on the command line, verified when a digest is given; None when none is."""
    if args.download and args.release_archive:
        raise Stop('give --release-archive or --download, not both')
    if args.sha256_file and args.expected_sha256:
        raise Stop('give --expected-sha256 or --sha256-file, not both')
    expected = args.expected_sha256.lower() if args.expected_sha256 else None
    if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise Stop('--expected-sha256 must be 64 hexadecimal characters')
    if args.sha256_file:
        expected = read_sidecar(args.sha256_file.expanduser())
    release = {'download_dir': None}
    if args.download:
        gh = gh_command()
        version = args.release or newest_tag(gh)
        # review 2026-09-30, finding 3: absolute, since bootstrap.py later reads it as the release
        # store from the project's folder, not the caller's.
        directory = (args.download_dir.expanduser().resolve() if args.download_dir else workdir / 'downloads')
        directory.mkdir(parents=True, exist_ok=True)
        archive = download_archive(gh, version, directory)
        published = read_sidecar(directory / (archive.name + '.sha256'))
        if expected is not None and expected != published:
            raise Stop('the digest you gave does not match the release\'s own .sha256 file; '
                       'nothing was planned or applied')
        verified_by = ('matches the digest you gave' if expected else
                       "matches the release's own .sha256 file, fetched with the archive; that detects "
                       'a damaged download, not who published it (SETUP-AGENT.md)')
        expected = expected or published
        release.update(download_dir=directory, gh=gh)
    elif args.release_archive:
        archive = args.release_archive.expanduser()
        if not archive.is_file():
            raise Stop(f'release archive does not exist: {shown(archive)}')
        verified_by = 'matches the digest you gave'
    else:
        if required:
            raise Stop('plan and update need --release-archive with --expected-sha256 or --sha256-file, '
                       'or --download. ' + BY_HAND)
        if args.release:
            return dict(release, version=args.release, archive=None, sha256=None, manifest=None,
                        verified_by='compared by version number only')
        return None
    archive = archive.resolve()
    if expected is None:
        if required:
            raise Stop('the archive\'s digest is needed: --expected-sha256 or --sha256-file. ' + BY_HAND)
        actual, verified_by = sha256_file(archive), 'not verified: no digest given'
    else:
        actual = sha256_file(archive)
        if actual != expected:
            raise Stop(f'release archive SHA-256 is {actual}, not the digest given ({expected}); '
                       'nothing was planned or applied')
    manifest = archive_manifest(archive)
    if args.release and manifest['version'] != args.release:
        raise Stop(f"the archive is release {manifest['version']}, not {args.release}")
    return dict(release, version=manifest['version'], archive=archive, sha256=actual,
                manifest=manifest, verified_by=verified_by)


def release_text(release):
    if release['archive'] is None:
        return f"Release {release['version']} ({release['verified_by']})"
    return (f"Release {release['version']}: {shown(release['archive'])}\n"
            f"  sha256 {release['sha256']} ({release['verified_by']})")


# Planning ----------------------------------------------------------------

def output(stream):
    stream.seek(0)
    return stream.read().decode('utf-8', errors='replace').replace('\r\n', '\n')


def run_bootstrap(release_root, project, arguments):
    """Run the release's own bootstrap.py on one project: (exit code, JSON output or None, message).

    Review 2026-09-30, finding 1: bootstrap.py runs in a process group of its
    own, so an interruption reaches it once, from this script. Ctrl-C, SIGTERM or
    SIGHUP here is passed on to it, this script waits for it to stop and restore
    what it applied (settle), and the interruption is then re-raised as
    Interrupted. Its output goes to files, so the wait cannot fill a pipe. On
    Windows the console delivers Ctrl-C to bootstrap.py as well, so nothing is
    passed on there and only the wait applies; that branch has not been run on
    Windows.
    """
    command = [sys.executable, str(release_root / 'scripts' / 'bootstrap.py'), '--target', str(project),
               *arguments]
    options = {'start_new_session': True} if os.name != 'nt' else {}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, cwd=project, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   **options)
        try:
            process.wait()
        except KeyboardInterrupt as exc:
            signum = getattr(exc, 'signum', signal.SIGINT)
            expired = settle(process, signum, project)
            raise Interrupted(signum, process.returncode, expired, output(err).strip()) from None
        stdout, message = output(out), output(err).strip()
    try:
        data = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        data = None
    message = message[len('setup: '):] if message.startswith('setup: ') else message
    return process.returncode, (data if isinstance(data, dict) else None), message


def settle(process, signum, project):
    """Pass an interruption on to bootstrap.py and wait for it to return: True when the wait ran out.

    Review 2026-09-30, finding 1: bootstrap.py stops a running check and restores
    the project before it returns, which can take several seconds, so it is not
    ended before it returns unless RECOVERY_WAIT runs out. A further interruption
    here does not shorten the wait.
    """
    if os.name != 'nt' and process.poll() is None:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signum)  # its group, as a terminal's Ctrl-C would reach it
    say(f'kit-projects: interrupted ({signal.Signals(signum).name}); waiting up to {RECOVERY_WAIT} seconds '
        f'for bootstrap.py to stop and restore what it applied in {shown(project)}.')
    deadline = time.monotonic() + RECOVERY_WAIT
    while True:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
            return False
        except subprocess.TimeoutExpired:
            break
        except KeyboardInterrupt:
            say(f'kit-projects: still waiting for bootstrap.py in {shown(project)}; it is ended only if it has '
                f'not returned {RECOVERY_WAIT} seconds after the first interruption.')
    say(f'kit-projects: bootstrap.py in {shown(project)} had not returned after {RECOVERY_WAIT} seconds; '
        'ending it.')
    with contextlib.suppress(OSError):
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    process.wait()
    return True


def pinned_digests(manifest):
    return {row['version']: row['archive_sha256'] for row in manifest.get('releases') or []
            if isinstance(row, dict) and isinstance(row.get('version'), str)
            and isinstance(row.get('archive_sha256'), str)}


def store_digests(store):
    found = {}
    for path in sorted(store.iterdir()):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == '.zip':
            found.setdefault(sha256_file(path), path)
    return found


def resolve_store(args, release, rows):
    """The release store the chain projects need, downloading their archives when --download was given."""
    earlier = sorted({to for row in rows if row['status'] == 'chain' for _, to in row['hop_pairs'][:-1]})
    if not earlier:
        return None, {}
    if args.release_store is not None:
        store = args.release_store.expanduser()
        if not store.is_dir():
            raise Stop(f'--release-store is not a directory: {shown(store)}')
        store = store.resolve()
    elif release.get('download_dir') is not None:
        store = release['download_dir']
        present = store_digests(store)
        pinned = pinned_digests(release['manifest'])
        for version in earlier:
            if pinned.get(version) and pinned[version] not in present:
                try:
                    download_archive(release['gh'], version, store, sidecar=False)
                except Stop as exc:
                    print(f'kit-projects: {exc}', file=sys.stderr)
    else:
        return None, {}
    return store, store_digests(store)


def plan_project(row, release, allow_dirty):
    """Classify one project against the release and, for one release behind, run the release's own plan."""
    row.update(status=None, reason=None, conflicts=[], kept_project_edits=[], changes=None,
               hop_pairs=[], planned=None)
    if row['error']:
        row.update(status='cannot', reason=row['error'])
        return row
    if row['unfinished']:
        row.update(status='cannot', reason=(
            'an earlier setup or update did not finish here (' + ', '.join(row['unfinished']) + '); run '
            'python scripts/bootstrap.py --target . --recover in the project, then plan again '
            '(SETUP-AGENT.md, Recovery)'))
        return row
    kind, hops, words = standing(row, release)
    if kind == 'current':
        row['status'] = 'current'
        return row
    if kind in ('no-path', 'unknown'):
        row.update(status='cannot', reason=words)
        return row
    if not row['profile'] or not row['agent']:
        row.update(status='cannot', reason=f"{row['state']} names no profile or agent")
        return row
    row['hop_pairs'] = hops
    if kind == 'behind-chain':
        row['status'] = 'chain'
    else:
        code, data, message = run_bootstrap(release['root'], Path(row['path']),
                                            ['--profile', row['profile'], '--agent', row['agent'], '--plan'])
        if data is None:
            row.update(status='cannot', reason=message or f'bootstrap.py --plan exited {code}')
        elif data.get('pending_recovery'):
            row.update(status='cannot', reason='an earlier setup or update did not finish here; run '
                       'python scripts/bootstrap.py --target . --recover in the project, then plan again')
        elif data.get('conflicts'):
            row.update(status='conflicts', conflicts=list(data['conflicts']),
                       kept_project_edits=list(data.get('kept_project_edits') or []))
            hints = [item['hint'] for item in data.get('instruction_sections') or []
                     if isinstance(item, dict) and item.get('hint')]
            row['reason'] = '; '.join(hints + [
                'each named file was changed in this project since the kit installed it: move the change '
                'to a file of your own or delete it, then plan again (SETUP-AGENT.md, Conflicts)'])
        elif data.get('mode') != 'upgrade':
            row.update(status='cannot', reason=f"the release's plan reads {data.get('mode')!r}, not an upgrade")
        else:
            row.update(status='update', changes=len(data.get('changes') or {}),
                       kept_project_edits=list(data.get('kept_project_edits') or []))
    if row['dirty'] and row['status'] in ('update', 'chain') and not allow_dirty:
        row.update(planned=row['status'], status='cannot', reason=(
            f"uncommitted changes ({row['changed_files']} tracked file{'' if row['changed_files'] == 1 else 's'}); "
            'commit them first so the '
            'update stays reviewable on its own, or pass --allow-dirty'))
    return row


def check_chain(row, release, store, present):
    """A chain project needs every earlier archive, matched by the digest the release pins."""
    if row['status'] != 'chain':
        return row
    earlier = [to for _, to in row['hop_pairs'][:-1]]
    pinned = pinned_digests(release['manifest'])
    missing = []
    for version in earlier:
        digest = pinned.get(version)
        if digest is None:
            missing.append(f'{version} (this release pins no digest for it)')
        elif digest not in present:
            missing.append(version)
    if store is None:
        row.update(status='cannot', reason=(
            f"{len(row['hop_pairs'])} releases behind: the chain needs the earlier release archives; "
            'give --release-store DIR holding them, or --download'))
    elif missing:
        row.update(status='cannot', reason=f'the release store {shown(store)} lacks ' + ', '.join(missing))
    return row


def plan_text(row):
    status = row['status']
    if status == 'current':
        return 'current'
    if status == 'update':
        text = f"would update to the release: {row['changes']} files change"
        if row['kept_project_edits']:
            text += f"; project edits kept: {', '.join(row['kept_project_edits'])}"
        return text
    if status == 'chain':
        steps = ' -> '.join([row['hop_pairs'][0][0]] + [to for _, to in row['hop_pairs']])
        return (f"would update through {len(row['hop_pairs'])} releases ({steps}); the chain plans each "
                'step as it reaches it and stops at the first that conflicts or fails')
    if status == 'conflicts':
        text = 'conflicts: ' + ', '.join(row['conflicts'])
        return text + (f"; {row['reason']}" if row['reason'] else '')
    return 'cannot: ' + (row['reason'] or 'unknown')


def print_rows(rows, columns, stream=None):
    stream = stream or sys.stdout
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(columns) - 1)]
    for row in rows:
        cells = [str(cell).ljust(width) for cell, width in zip(row, widths)] + [str(row[-1])]
        print('  ' + '  '.join(cells).rstrip(), file=stream)


def summarize(counts):
    return ', '.join(f'{number} {label}' for label, number in counts if number)


# Commands ----------------------------------------------------------------

def selected_projects(args, update=False):
    explicit = []
    for path in args.project or []:
        path = path.expanduser()
        if not path.is_dir():
            raise Stop(f'--project is not a directory: {shown(path)}')
        explicit.append(path.resolve())
    if update:
        if not args.all and not explicit:
            raise Stop('update needs the projects named: --project DIR for each, or --all with --root')
        if args.all and not args.root:
            raise Stop('--all updates every project found under --root; give --root DIR')
    elif not args.root and not explicit:
        raise Stop('name where to look: --root DIR, or --project DIR')
    search = args.root and (not update or args.all)
    found, passed = discover(args.root, args.depth) if search else ([], [])
    return sorted(set(found) | set(explicit)), passed


def command_list(args, workdir):
    release = resolve_release(args, workdir, required=False)
    projects, passed = selected_projects(args)
    rows = [read_project(path) for path in projects]
    for row in rows:
        if release is not None:
            kind, hops, words = standing(row, release)
            row['release'] = {'kind': kind, 'hops': len(hops) if hops else 0, 'text': words}
    if args.json:
        print(json.dumps({'release': release and {k: (shown(v) if isinstance(v, Path) else v)
                                                  for k, v in release.items() if k in
                                                  ('version', 'archive', 'sha256', 'verified_by')},
                          'projects': rows, 'passed_over': [shown(path) for path in passed]}, indent=2))
        return 0
    if release is not None:
        print(release_text(release))
    if not rows:
        print(NOT_FOUND)
    else:
        header = ['PROJECT', 'VERSION', 'PROFILE', 'AGENT', 'TREE'] + (['RELEASE'] if release else [])
        table = [header]
        for row in rows:
            cells = [row['path'], row['version'] or '?', row['profile'] or '?', row['agent'] or '?',
                     row['error'] or tree_text(row)]
            if release is not None:
                cells.append(row['release']['text'])
            table.append(cells)
        print_rows(table, header)
    if passed:
        print('Sync folders not searched: ' + ', '.join(shown(path) for path in passed))
    return 0


def build_plan(args, workdir, update=False):
    release = resolve_release(args, workdir, required=True)
    release['root'] = unpack(release['archive'], workdir / 'release')
    projects, passed = selected_projects(args, update)
    rows = [plan_project(read_project(path), release, args.allow_dirty) for path in projects]
    store, present = resolve_store(args, release, rows)
    rows = [check_chain(row, release, store, present) for row in rows]
    return release, rows, passed, store


def plan_counts(rows):
    count = {key: sum(row['status'] == key for row in rows)
             for key in ('update', 'chain', 'current', 'conflicts', 'cannot')}
    return [('would update', count['update']), ('would update through the chain', count['chain']),
            ('current', count['current']), ('with conflicts', count['conflicts']),
            ('cannot be updated as they stand', count['cannot'])]


def print_plan(release, rows, passed, stream):
    print(release_text(release), file=stream)
    if not rows:
        print(NOT_FOUND, file=stream)
    else:
        table = [['PROJECT', 'INSTALLED', 'TREE', 'PLAN']]
        table += [[row['path'], row['version'] or '?', tree_text(row), plan_text(row)] for row in rows]
        print_rows(table, table[0], stream)
        print('Plan: ' + summarize(plan_counts(rows)) + '.', file=stream)
    if passed:
        print('Sync folders not searched: ' + ', '.join(shown(path) for path in passed), file=stream)


def json_row(row):
    return {key: value for key, value in row.items() if key != 'hop_pairs'} | {
        'hops': [f'{a} -> {b}' for a, b in row.get('hop_pairs') or []]}


def command_plan(args, workdir):
    release, rows, passed, _ = build_plan(args, workdir)
    if args.json:
        print(json.dumps({'release': public(release), 'projects': [json_row(row) for row in rows],
                          'passed_over': [shown(path) for path in passed]}, indent=2))
    else:
        print_plan(release, rows, passed, sys.stdout)
    return 1 if any(row['status'] in ('conflicts', 'cannot') for row in rows) else 0


def public(release):
    return {'version': release['version'], 'archive': shown(release['archive']),
            'sha256': release['sha256'], 'verified_by': release['verified_by']}


def confirmed(args, count, version, stream):
    if args.yes:
        return True, None
    if not sys.stdin or not sys.stdin.isatty():
        return False, ('Nothing was applied: no terminal to confirm on. Show the plan above to the person; '
                       'when they agree, run the same command with --yes.')
    stream.write(f'Apply {version} to the {count} project(s) marked to apply? '
                 'Nothing is committed or pushed. [y/N] ')
    stream.flush()
    answer = sys.stdin.readline().strip().casefold()
    if answer in ('y', 'yes'):
        return True, None
    return False, 'Nothing was applied: not confirmed.'


def apply_one(row, release, store, args):
    project = Path(row['path'])
    arguments = ['--profile', row['profile'], '--agent', row['agent'],
                 '--release-archive', str(release['archive']), '--expected-sha256', release['sha256']]
    if args.execution_agent:
        arguments += ['--execution-agent', args.execution_agent]
    chain = row['status'] == 'chain'
    arguments += ['--release-store', str(store), '--update-chain'] if chain else ['--update']
    code, data, message = run_bootstrap(release['root'], project, arguments)
    try:
        # Where the update left it: an update to a release naming a newer state folder moves it there.
        installed = json.loads((project / located(project, STATE)).read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        installed = None
    result = {'path': row['path'], 'from': row['version'], 'to': release['version'], 'installed_after': installed,
              'exit_code': code, 'changes': None, 'kept_project_edits': [], 'remaining': []}
    if code == 0 and data is not None and installed == release['version']:
        if chain:
            result['changes'] = sum(len((hop.get('receipt') or {}).get('changes') or {})
                                    for hop in data.get('hops') or [])
        else:
            result['changes'] = len(data.get('changes') or {})
            result['kept_project_edits'] = list(data.get('kept_project_edits') or [])
        result.update(outcome='updated', remaining=list(data.get('remaining') or []))
        moved = data.get('state_folder') or next((hop['receipt']['state_folder'] for hop in data.get('hops') or []
                                                  if (hop.get('receipt') or {}).get('state_folder')), None)
        if isinstance(moved, dict) and moved.get('how'):
            result['state_folder'] = f"its installation state moved from {moved['from']}/ to {moved['to']}/"
        return result
    if chain and data is not None:
        stopped = [hop for hop in data.get('hops') or [] if hop.get('status') == 'stopped']
        why = stopped[-1].get('error') if stopped else None
        result.update(outcome='failed', reason=(f"the chain stopped at {stopped[-1]['from']} to "
                                                f"{stopped[-1]['to']}: {why}") if stopped else
                      (message or 'the chain did not finish'), remaining=list(data.get('remaining') or []))
        return result
    result.update(outcome='failed', reason=message or f'bootstrap.py exited {code}')
    return result


def history_events(project):
    """The update history, one dict per event; a line that is not a JSON object reads as {}."""
    try:
        lines = (project / located(project, UPDATE_HISTORY)).read_text(encoding='utf-8').splitlines()
    except (OSError, ValueError):
        return []
    events = []
    for line in lines:
        if line.strip():
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            events.append(event if isinstance(event, dict) else {})
    return events


def after_interruption(row, release, seen, exc):
    """What an interrupted update left, read from the project once bootstrap.py returned.

    Review 2026-09-30, finding 1: restored when no unfinished marker is left and
    the update history records the stop after the `seen` events it held before;
    recovery incomplete, with the command that completes it, when a marker is
    left or bootstrap.py had to be ended.
    """
    project = Path(row['path'])
    try:
        installed = json.loads((project / located(project, STATE)).read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError, AttributeError):
        installed = None
    left = [located(project, name) for name in UNFINISHED if (project / located(project, name)).exists()]
    events = [event for event in history_events(project)[seen:] if event.get('event') == 'update']
    status = events[-1].get('status') if events else None
    expired = getattr(exc, 'expired', False)
    now = f'; installed now: {installed}' if installed else ''
    result = {'path': row['path'], 'from': row['version'], 'to': release['version'], 'installed_after': installed,
              'exit_code': getattr(exc, 'exit_code', None), 'changes': None, 'kept_project_edits': [],
              'remaining': []}
    if expired or left:
        said = getattr(exc, 'message', '')
        said = said[len('setup: '):] if said.startswith('setup: ') else said
        why = (f'bootstrap.py had not returned {RECOVERY_WAIT} seconds after the interruption and was ended'
               if expired else
               'bootstrap.py returned without finishing its recovery' + (f' ({said})' if said else ''))
        return dict(result, outcome='recovery incomplete', reason=(
            why + (f" ({', '.join(left)} left)" if left else '') + now + '; run python scripts/bootstrap.py '
            f"--target . --recover in {row['path']}, then plan again (SETUP-AGENT.md, Recovery)"))
    if status in ('interrupted', 'failed'):
        return dict(result, outcome='restored', reason=(
            'interrupted; bootstrap.py restored the installation the update started from and recorded the '
            f'stop in {located(project, UPDATE_HISTORY)}' + now))
    if status == 'succeeded':
        return dict(result, outcome='interrupted', reason=(
            'interrupted after bootstrap.py completed an update' + now + '; review it and commit it as usual'
            + ('' if installed == release['version'] else ', then run update again to continue')))
    return dict(result, outcome='interrupted', reason='interrupted before bootstrap.py applied anything' + now)


def result_text(result):
    if result['outcome'] == 'updated':
        project = Path(result['path'])
        text = (f"updated {result['from']} -> {result['to']} ({result['changes']} files); review it with "
                f"git -C {result['path']} status and git diff, then commit it with "
                f"{located(project, UPDATE_HISTORY)} as the project's own rules allow; "
                f"{located(project, '.trace-os/receipts/latest.json')} lists every change")
        if result.get('state_folder'):
            text += '; ' + result['state_folder']
        if result['kept_project_edits']:
            text += '; project edits kept: ' + ', '.join(result['kept_project_edits'])
        return text
    if result['outcome'] == 'failed':
        where = f"; installed now: {result['installed_after']}" if result.get('installed_after') else ''
        return f"failed: {result['reason']}{where}"
    return f"{result['outcome']}: {result['reason']}"


def command_update(args, workdir):
    release, rows, passed, store = build_plan(args, workdir, update=True)
    out = sys.stderr if args.json else sys.stdout
    print_plan(release, rows, passed, out)
    # Walk the projects in order: apply the clean ones, stop at the first that
    # conflicts (here, or when its update runs) unless --keep-going.
    order, stop_at = [], None
    for row in rows:
        if row['status'] in ('update', 'chain') and stop_at is None:
            order.append(row)
        elif row['status'] == 'conflicts' and stop_at is None and not args.keep_going:
            stop_at = row
    results, refusal = [], None
    if order:
        names = ', '.join(row['path'] for row in order)
        extra = (f"; it stops at {stop_at['path']}, whose plan names conflicts, so the projects after it are "
                 'not attempted (--keep-going continues past it)') if stop_at else ''
        print(f'To apply: {names}{extra}.', file=out)
        ok, refusal = confirmed(args, len(order), release['version'], out)
        if not ok:
            print(refusal, file=out)
    else:
        print('Nothing to apply.', file=out)
    stopped, interrupted = None, None
    for row in rows:
        entry = {'path': row['path'], 'from': row['version'], 'to': release['version']}
        if row['status'] == 'current':
            results.append(dict(entry, outcome='current', reason='already on this release'))
        elif refusal is not None and row in order:
            results.append(dict(entry, outcome='not applied', reason='not confirmed'))
        elif stopped is not None:
            results.append(dict(entry, outcome='not attempted', reason=stopped))
        elif row['status'] == 'conflicts':
            results.append(dict(entry, outcome='needs you', reason=plan_text(row)))
            if not args.keep_going:
                stopped = f"the run stopped at {row['path']}"
        elif row['status'] == 'cannot':
            results.append(dict(entry, outcome='skipped', reason=row['reason']))
        else:
            # review 2026-09-30, finding 2: the tree is read again just before its update, so a
            # project that became dirty while the confirmation waited, or while earlier projects
            # updated, is not updated without --allow-dirty either.
            tree = None if args.allow_dirty else read_project(Path(row['path']))
            if tree is not None and tree['dirty'] is not False:
                changed = tree['changed_files']
                results.append(dict(entry, outcome='skipped', reason=(
                    f"uncommitted changes appeared after the plan ({changed} tracked "
                    f"file{'' if changed == 1 else 's'}); not updated without --allow-dirty: commit them, "
                    'then run update again') if tree['dirty'] else
                    f"its working tree could not be read again before the update: {tree['error']}"))
                continue
            print(f"Updating {row['path']} ({row['version']} -> {release['version']}) ...", file=out, flush=True)
            seen = len(history_events(Path(row['path'])))
            try:
                result = apply_one(row, release, store, args)
            except KeyboardInterrupt as exc:
                # review 2026-09-30, finding 1: reported from the project itself once its
                # bootstrap.py has returned; no further project is attempted.
                interrupted, result = exc, after_interruption(row, release, seen, exc)
            results.append(result)
            if interrupted is not None:
                stopped = f"the run was interrupted at {row['path']}"
            elif result['outcome'] != 'updated' and not args.keep_going:
                stopped = f"the run stopped at {row['path']}"
    outcomes = [result['outcome'] for result in results]
    counts = [('updated', outcomes.count('updated')), ('restored', outcomes.count('restored')),
              ('recovery incomplete', outcomes.count('recovery incomplete')),
              ('interrupted', outcomes.count('interrupted')), ('failed', outcomes.count('failed')),
              ('need you', outcomes.count('needs you')), ('skipped', outcomes.count('skipped')),
              ('not attempted', outcomes.count('not attempted')),
              ('not applied', outcomes.count('not applied')), ('current', outcomes.count('current'))]
    if args.json:
        print(json.dumps({'release': public(release), 'plan': [json_row(row) for row in rows],
                          'results': results}, indent=2))
    else:
        print_rows([['PROJECT', 'RESULT']] + [[result['path'], result_text(result)] for result in results],
                   ['PROJECT', 'RESULT'])
        print('Result: ' + (summarize(counts) or 'no project') + '. Nothing was committed or pushed.')
    if interrupted is not None:
        return 128 + getattr(interrupted, 'signum', signal.SIGINT)
    return 0 if all(outcome in ('updated', 'current') for outcome in outcomes) else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n\n'.join(__doc__.split('\n\n')[1:]))
    commands = parser.add_subparsers(dest='command', required=True)

    def common(sub, required):
        sub.add_argument('--root', type=Path, action='append',
                         help='a folder to search for TRACE projects (repeatable)')
        sub.add_argument('--project', type=Path, action='append', help='one project folder (repeatable)')
        sub.add_argument('--depth', type=int, default=3, help='folders below a root to search (default 3)')
        sub.add_argument('--json', action='store_true', help='machine-readable output')
        sub.add_argument('--release-archive', type=Path, help='the release ZIP')
        sub.add_argument('--expected-sha256', help='the archive digest, from a channel you trust')
        sub.add_argument('--sha256-file', type=Path, help="the release's .sha256 file")
        sub.add_argument('--download', action='store_true',
                         help=f'fetch the release from {REPOSITORY} with gh (optional)')
        sub.add_argument('--release', help='a release version such as 0.1.0-rc17: which one to download, '
                         'or, for list without an archive, the one to compare with')
        sub.add_argument('--download-dir', type=Path, help='keep downloaded archives here (default: removed)')
        if required:
            sub.add_argument('--release-store', type=Path,
                             help='a folder of earlier release archives, for projects more than one release behind')
            sub.add_argument('--allow-dirty', action='store_true',
                             help='plan and update projects with uncommitted changes too')

    common(commands.add_parser('list', help='find the projects and say where each stands (read-only)'), False)
    common(commands.add_parser('plan', help="run the release's own plan on each project (read-only)"), True)
    update = commands.add_parser('update', help='plan, confirm, then update each clean project')
    common(update, True)
    update.add_argument('--all', action='store_true', help='every project found under --root')
    update.add_argument('--yes', action='store_true',
                        help='the person has seen the plan and agreed; apply without asking')
    update.add_argument('--keep-going', action='store_true',
                        help='continue past a project that conflicts or fails')
    update.add_argument('--execution-agent', help='the model id of an agent running this, for the update history')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or later is required')
    if args.depth < 0:
        parser.error('--depth must be 0 or more')
    handler = {'list': command_list, 'plan': command_plan, 'update': command_update}[args.command]
    try:
        with interruptible(), tempfile.TemporaryDirectory(prefix='trace-os-projects-') as scratch:
            return handler(args, Path(scratch).resolve())
    except Stop as exc:
        print(f'kit-projects: {exc}', file=sys.stderr)
        return exc.code
    except KeyboardInterrupt as exc:
        # review 2026-09-30, finding 1: update reports an interrupted update itself, once its
        # bootstrap.py has returned; an interruption that reaches here found none running.
        say('kit-projects: interrupted while no update was running; run plan again to see where each '
            'project stands.')
        return 128 + getattr(exc, 'signum', signal.SIGINT)


if __name__ == '__main__':
    sys.exit(main())
