#!/usr/bin/env python3
"""Keep the team's large and bulky files in one shared place, out of Git, with the agent doing the work.

The person says where the files live. Two routes, the first the default:

1. A sync app folder (OneDrive, Google Drive, Dropbox, SURFdrive): the app on each computer keeps
   the folder in step, and the project links to it. Nothing to install, no cost beyond the app.

  python scripts/shared_folder.py find                       # the sync apps' folders on this machine
  python scripts/shared_folder.py connect "<folder>" --plan  # then without --plan
  python scripts/shared_folder.py status

2. A bucket (Google Cloud Storage, SURF's object store, any S3 store) through rclone, for data
   too large for a sync app. Buckets are paid and have no app that syncs them, so the agent copies:

  python scripts/shared_folder.py bucket set <remote>:<bucket>/<folder>
  python scripts/shared_folder.py pull --plan                # then without --plan; push likewise

Either way, Git records what the shared files should be, and the agent compares:

  python scripts/shared_folder.py check                      # conflict copies, online-only files, shortcuts
  python scripts/shared_folder.py inventory                  # path, size and SHA-256 of each file, for Git
  python scripts/shared_folder.py verify                     # this machine's files against that inventory
  python scripts/shared_folder.py snapshot data/survey --dataset survey-2026-10
  python scripts/shared_folder.py keep-local "workspace/exports/"
  python scripts/shared_folder.py disconnect

connect makes the project's shared path (workspace/shared, or shared_root in ai/project.json) a link
to the sync app's folder and adds the link to .gitignore. It never deletes or overwrites a file:
material already in workspace/shared moves only with --move-existing, only when no name collides,
and a name that appears in the shared folder during the move stops it rather than being replaced.
disconnect removes the link and never touches the folder it pointed to.

bucket set records the bucket in ai/project.json (no credentials: rclone keeps those in its own
configuration) and keeps a local copy in workspace/shared. pull and push run rclone copy with
--update, never sync, so nothing is deleted on either side and a newer file is never overwritten.

inventory writes sources/shared-inventory.json, which is committed; verify says which recorded files
are missing here (not synced yet, or removed), which differ, and which are new. Hashes are cached
in an ignored file by size, modification time, device and inode. An online-only file is compared by size unless
--download is given, because reading it downloads it.

snapshot copies a subfolder of the shared folder into a verified local snapshot under
workspace/snapshots/<dataset>/ (scripts/shared_snapshot.py), so an analysis reads bytes the sync app
does not change under it. Record what was taken with trace_record.py intake.

Outside material waits for a decision in incoming/ inside the shared folder (incoming_root in
ai/project.json names another folder below the project): a dataset, a delivery or a reviewer's files
stay there, outside the project's data, until trace_record.py intake records a decision to admit
them, and only then move into the data. status names the folder and counts what is in it.

The project's own Git working copy stays outside any synced folder; status says when it is not.
This script makes no network call except through rclone for a bucket, creates no account and shares
nothing: who can see the files is decided in the sync app or the bucket's permissions.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import errno
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARED = 'workspace/shared'
SNAPSHOTS = 'workspace/snapshots'
# Where outside material waits for a decision, inside the shared folder unless
# incoming_root in ai/project.json names another folder below the project (rc22).
INCOMING = 'incoming'
# Folder names the common sync clients create. A match is a hint, never a verdict.
SYNCED_MARKERS = ('surfdrive', 'dropbox', 'onedrive', 'icloud', 'mobile documents', 'google drive',
                  'googledrive', 'my drive', 'cloudstorage', 'nextcloud', 'owncloud', 'box sync', 'box')


class FolderError(Exception):
    pass


def shared_path(root: Path) -> str:
    """The project's shared path, relative to its root: shared_root in ai/project.json, when set."""
    return project_folder(root, 'shared_root') or DEFAULT_SHARED


def incoming_path(root: Path) -> str:
    """Where outside material waits for a decision, relative to the project root:
    incoming_root in ai/project.json, when set, else incoming/ inside the shared folder."""
    return project_folder(root, 'incoming_root') or shared_path(root) + '/' + INCOMING


def project_folder(root: Path, key: str) -> str | None:
    """A folder ai/project.json names under `key`, relative to the project root, or None
    when the key is absent or names no folder below the root."""
    try:
        value = json.loads((root / 'ai' / 'project.json').read_text(encoding='utf-8')).get(key)
    except (OSError, ValueError, AttributeError):
        value = None
    if isinstance(value, str) and value.strip():
        # A folder below the project root: never the root itself ('.'), never above it or
        # elsewhere (an absolute path, a drive), never .git. Judged on the text as it is used.
        text = value.strip().replace('\\', '/')
        candidate = PurePosixPath(text)
        parts = candidate.parts
        if (parts and not candidate.is_absolute() and not PureWindowsPath(text).drive
                and '..' not in parts and parts[0].lower() != '.git'):
            return '/'.join(parts)
    return None


def is_link(path: Path) -> bool:
    """A symlink, or a Windows directory junction, which Python 3.11 does not call a symlink."""
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, 'isjunction', None)
    if isjunction is not None:
        return isjunction(path)
    if os.name == 'nt' and path.exists():
        try:
            os.readlink(path)
            return True
        except (OSError, ValueError):
            return False
    return False


def link_target(path: Path) -> Path:
    target = Path(os.readlink(path))
    text = str(target)
    if text.startswith('\\\\?\\'):
        target = Path(text[4:])
    return target if target.is_absolute() else (path.parent / target)


def synced_hint(path: Path) -> str | None:
    """The sync client a path seems to sit under, from its folder names; None when nothing matches."""
    for part in Path(path).parts:
        low = part.lower()
        for marker in SYNCED_MARKERS:
            if low == marker or low.startswith(marker + ' ') or low.startswith(marker + '-') \
                    or (marker != 'box' and marker in low):
                return part
    return None


def git(root: Path, *args) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    except OSError:
        return None


def ignored(root: Path, name: str) -> bool | None:
    """Whether Git ignores the path itself (a link is a file to Git, so `dir/` rules miss it)."""
    result = git(root, 'check-ignore', '-q', '--no-index', name)
    if result is None or result.returncode not in (0, 1):
        return None
    return result.returncode == 0


def ensure_ignored(root: Path, name: str) -> str | None:
    """Add `/name` to .gitignore when Git would not ignore it; returns the line added."""
    state = ignored(root, name)
    if state is not False:
        return None
    line = '/' + name.strip('/')
    gitignore = root / '.gitignore'
    text = gitignore.read_text(encoding='utf-8') if gitignore.is_file() else ''
    if line in text.splitlines():
        return None
    with gitignore.open('a', encoding='utf-8') as stream:
        stream.write(('' if not text or text.endswith('\n') else '\n') + line + '\n')
    return line


def status(root: Path) -> dict:
    name = shared_path(root)
    link = root / name
    result = {'shared_path': name, 'project': str(root)}
    remote = shared_remote(root)
    if remote and not is_link(link):
        result['bucket'] = remote['location']
        if link.is_dir():
            result['state'] = 'bucket copy'
            result['entries'] = sum(1 for _ in link.iterdir())
        elif link.exists():
            result['state'] = 'blocked'
            result['reason'] = 'a file sits where the shared folder belongs'
        else:
            result['state'] = 'not fetched'
    elif is_link(link):
        target = link_target(link)
        result['target'] = str(target)
        if target.is_dir():
            result['state'] = 'connected'
            try:
                result['entries'] = sum(1 for _ in target.iterdir())
            except OSError as exc:
                result['state'], result['reason'] = 'unreadable', str(exc)
        else:
            result['state'] = 'broken'
            result['reason'] = 'the folder it points to is not on this machine (not synced here yet, moved or renamed)'
        result['target_synced_hint'] = synced_hint(target)
    elif link.is_dir():
        result['state'] = 'local only'
        result['entries'] = sum(1 for _ in link.iterdir())
        result['reason'] = 'a plain folder in this copy of the project; nobody else sees it'
    elif link.exists():
        result['state'] = 'blocked'
        result['reason'] = 'a file sits where the shared folder belongs'
    else:
        result['state'] = 'not connected'
    result['ignored_by_git'] = ignored(root, name)
    result['project_synced_hint'] = synced_hint(root)
    # Outside material waiting for a decision is not the project's data; status names
    # where it waits and how much is there, so it is seen before work reads the folder.
    result['incoming'] = incoming_path(root)
    if result['state'] in ('connected', 'local only', 'bucket copy'):
        result['incoming_files'] = len(incoming_files(root))
    result['next'] = next_step(result)
    return result


def next_step(result: dict) -> str:
    if result.get('project_synced_hint'):
        return ('the project itself sits in a synced folder (' + result['project_synced_hint'] + '); '
                'move the Git working copy out of it before connecting')
    state = result['state']
    if state == 'bucket copy':
        return ('run pull before using the shared files and push after adding any; then inventory '
                'if files were added')
    if state == 'not fetched':
        return ('run pull to fetch the bucket\'s files; this machine needs an rclone remote named as in '
                + result['bucket'].partition(':')[0])
    if state == 'not connected':
        return 'ask the person where the team\'s shared folder is, then run connect with that path'
    if state == 'local only':
        return ('ask the person where the team\'s shared folder is; connect --move-existing moves '
                'what is here into it')
    if state == 'broken':
        return ('ask the person to let their sync app finish, or for the folder\'s new location '
                '(connect --replace with the new path)')
    if state == 'blocked':
        return 'show the person the file at ' + result['shared_path'] + ' and ask what it is'
    if result.get('ignored_by_git') is False:
        return 'run connect again so Git ignores the link'
    waiting = result.get('incoming_files')
    if waiting:
        return ('nothing for the folder; ' + str(waiting) + ' file(s) are held in ' + result['incoming']
                + '/, outside the data: a file there moves into the data only after a check and a '
                'decision to admit it recorded with trace_record.py intake (trace_report.py says '
                'which have a decision)')
    return 'nothing; bulky files go in ' + result['shared_path'] + '/'


def inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def make_link(link: Path, target: Path) -> str:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return 'symlink'
    except OSError:
        if os.name != 'nt':
            raise
    # Windows without Developer Mode cannot make a symlink; a junction needs no privilege.
    import _winapi
    _winapi.CreateJunction(str(target), str(link))
    return 'junction'


def remove_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)  # a junction is removed as an empty directory, leaving its target alone


def connect(root: Path, path: str, create: bool = False, move_existing: bool = False,
            replace: bool = False, plan: bool = False) -> dict:
    name = shared_path(root)
    link = root / name
    remote = shared_remote(root)
    if remote:
        raise FolderError('this project keeps its shared files in a bucket (' + remote['location'] + '); '
                          'run bucket unset first if the team is moving to a sync app folder')
    target = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"').strip("'"))))
    if not target.is_absolute():
        raise FolderError('give the full path of the shared folder, not a relative one: ' + path)
    real_root, real_target = root.resolve(), target.resolve()
    if inside(real_target, real_root):
        raise FolderError('the shared folder cannot sit inside the project; it would be part of '
                          'the Git working copy')
    if inside(real_root, real_target):
        raise FolderError('the project sits inside the shared folder; move the Git working copy '
                          'out of the synced folder first')
    actions = []
    if not target.exists():
        if not create:
            raise FolderError('no folder at ' + str(target) + '; check the path with the person, '
                              'or pass --create to make it')
        actions.append('create the folder ' + str(target))
    elif not target.is_dir():
        raise FolderError('not a folder: ' + str(target))

    moving = []
    if is_link(link):
        current = link_target(link)
        if current.exists() and current.resolve() == real_target:
            result = status(root)
            added = None if plan else ensure_ignored(root, name)
            result.update(action='already connected', gitignore_added=added)
            return result
        if not replace:
            raise FolderError(name + ' already points to ' + str(current) + '; pass --replace to '
                              'point it at the new folder (nothing in either folder changes)')
        actions.append('point ' + name + ' at the new folder instead of ' + str(current))
    elif link.is_dir():
        entries = sorted(link.iterdir(), key=lambda p: p.name)
        tracked = git(root, 'ls-files', '--cached', '--', name)
        if entries and tracked is not None and tracked.returncode == 0 and tracked.stdout.strip():
            raise FolderError('Git tracks files under ' + name + ', so it cannot become the shared folder; '
                              'set shared_root in ai/project.json to a folder Git does not track')
        if entries and not move_existing:
            raise FolderError(name + ' already holds ' + str(len(entries)) + ' item(s) in this copy '
                              'of the project; ask the person, then pass --move-existing to move '
                              'them into the shared folder')
        if entries and target.is_dir():
            clash = [p.name for p in entries if (target / p.name).exists() or is_link(target / p.name)]
            if clash:
                raise FolderError('these names exist in both places, so nothing was moved: '
                                  + ', '.join(clash))
        moving = entries
        if moving:
            actions.append('move ' + str(len(moving)) + ' item(s) from ' + name + ' into the shared folder')
        actions.append('replace the empty folder ' + name + ' with a link')
    elif link.exists():
        raise FolderError('a file sits at ' + name + '; nothing was changed')
    actions.append('link ' + name + ' to ' + str(target))
    if ignored(root, name) is False:
        actions.append('add /' + name + ' to .gitignore')
    if plan:
        return {'action': 'plan', 'shared_path': name, 'target': str(target), 'steps': actions}

    if not target.exists():
        target.mkdir(parents=True)
    moved = []
    for entry in moving:
        try:
            move_into(entry, target)
        except FileExistsError:
            raise FolderError(entry.name + ' appeared in the shared folder during the move (a sync app may '
                              'have just added it), so it stayed here and nothing was replaced; moved so far: '
                              + (', '.join(moved) or 'nothing') + '. Ask the person which version to keep, '
                              'then connect again') from None
        except OSError as exc:
            raise FolderError('could not move ' + entry.name + ' (' + str(exc) + '); nothing was replaced; '
                              'moved so far: ' + (', '.join(moved) or 'nothing')) from None
        moved.append(entry.name)
    if is_link(link):
        remove_link(link)
    elif link.is_dir():
        link.rmdir()
    kind = make_link(link, target)
    added = ensure_ignored(root, name)
    result = status(root)
    result.update(action='connected', link_kind=kind, moved=[p.name for p in moving], gitignore_added=added)
    return result


def same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a, follow_symlinks=False).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def rename_no_replace(source: Path, destination: Path) -> None:
    """Rename, never replacing what is at the destination; raises FileExistsError if anything is there.

    A sync app can put a coauthor's file at the destination at any moment, so a check before
    os.replace is not enough. macOS (renamex_np, RENAME_EXCL) and Linux (renameat2,
    RENAME_NOREPLACE) refuse atomically, and Windows' os.rename refuses by itself. Elsewhere,
    or on a file system without the call, the name is claimed with mkdir or a hard link, which
    fail when anything is there; a file system without hard links stops the move instead.
    """
    if os.name == 'nt':
        os.rename(source, destination)
        return
    call = None
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
            call = lambda a, b: libc.renamex_np(a, b, ctypes.c_uint(0x4))
        elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
            call = lambda a, b: libc.renameat2(-100, a, -100, b, ctypes.c_uint(1))  # AT_FDCWD
    except (ImportError, OSError):
        call = None
    if call is not None:
        if call(os.fsencode(source), os.fsencode(destination)) == 0:
            return
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(code, os.strerror(code), str(destination))
        if code not in (errno.EINVAL, errno.ENOSYS, getattr(errno, 'ENOTSUP', -1), getattr(errno, 'EOPNOTSUPP', -1)):
            raise OSError(code, os.strerror(code), str(destination))
    # Without an exclusive rename, claim the name with calls that fail when anything is there:
    # a folder by mkdir, then its entries one by one; a file by a hard link, then the old name goes.
    if source.is_dir() and not source.is_symlink():
        os.mkdir(destination)
        for child in sorted(source.iterdir(), key=lambda p: p.name):
            rename_no_replace(child, destination / child.name)
        source.rmdir()
    else:
        os.link(source, destination, follow_symlinks=False)
        os.unlink(source)


def move_into(entry: Path, target: Path) -> None:
    """Move one entry into the shared folder without replacing anything already there."""
    destination = target / entry.name
    if same_device(entry, target):
        rename_no_replace(entry, destination)
        return
    # Another device: copy into a staging folder this run creates, under a name nobody else
    # holds, then rename the copy into place; only that staging folder is ever removed.
    is_tree = entry.is_dir() and not entry.is_symlink()
    staging = Path(tempfile.mkdtemp(prefix='.trace-os-moving-', dir=target))
    try:
        temp = staging / entry.name
        if is_tree:
            shutil.copytree(entry, temp, symlinks=True)
        else:
            shutil.copy2(entry, temp, follow_symlinks=False)
        rename_no_replace(temp, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if is_tree:
        shutil.rmtree(entry)
    else:
        entry.unlink()


def disconnect(root: Path) -> dict:
    name = shared_path(root)
    link = root / name
    if not is_link(link):
        raise FolderError(name + ' is not a link to a shared folder; nothing was changed')
    target = link_target(link)
    remove_link(link)
    return {'action': 'disconnected', 'shared_path': name, 'was': str(target),
            'note': 'the shared folder and its files are unchanged'}


def snapshot(root: Path, subpath: str, dataset: str) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import shared_snapshot as ss
    name = shared_path(root)
    base = root / name
    if not base.is_dir():
        raise FolderError('the shared folder is not connected on this machine; run status')
    source = base / subpath if subpath not in ('', '.') else base
    if not inside(source.resolve(), base.resolve()):
        raise FolderError('the snapshot source must be inside the shared folder')
    if not source.is_dir():
        raise FolderError('no folder at ' + name + '/' + subpath)
    if not dataset or not all(c.isalnum() or c in '-_.' for c in dataset) or dataset.startswith('.'):
        raise FolderError('name the dataset with letters, digits, dots, dashes or underscores')
    added = ensure_ignored(root, SNAPSHOTS)
    store = root / SNAPSHOTS / dataset
    manifest = ss.create_manifest(source.resolve(), dataset)
    try:
        result = ss.materialize(source.resolve(), manifest, store)
    except ss.SnapshotError as exc:
        raise FolderError('snapshot: ' + str(exc)) from None
    result.update(dataset=dataset, source=name + '/' + subpath.strip('/') if subpath not in ('', '.') else name,
                  files=len(manifest['files']), bytes=sum(e['size'] for e in manifest['files']),
                  gitignore_added=added)
    return result


def keep_local(root: Path, patterns: list[str]) -> dict:
    gitignore = root / '.gitignore'
    text = gitignore.read_text(encoding='utf-8') if gitignore.is_file() else ''
    lines = text.splitlines()
    added, tracked = [], {}
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern or pattern.startswith('#'):
            raise FolderError('empty or comment pattern: ' + repr(pattern))
        if pattern not in lines:
            lines.append(pattern)
            added.append(pattern)
        # The ignore rule's own matching (exports/ also matches analysis/exports/), not a pathspec's.
        listed = git(root, 'ls-files', '-z', '--cached', '--ignored', '--exclude=' + pattern)
        if listed is not None and listed.returncode == 0 and listed.stdout.strip('\0'):
            tracked[pattern] = [name for name in listed.stdout.split('\0') if name]
    if added:
        gitignore.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'action': 'keep-local', 'added': added, 'already_tracked': tracked,
            'note': ('files Git already tracks stay tracked; removing them from the index '
                     '(git rm --cached) is the person\'s decision') if tracked else ''}


# ---------------------------------------------------------------------------
# Finding the sync app's folders, so the person need not know the path.

def find_folders(home: Path | None = None, environ=None) -> list[dict]:
    """The sync apps' folders on this machine, from their documented places; nothing is created."""
    home = Path.home() if home is None else Path(home)
    env = os.environ if environ is None else environ
    found, seen = [], set()

    def add(provider, path):
        path = Path(path)
        try:
            ok = path.is_dir()
            key = os.path.realpath(path) if ok else None
        except OSError:
            ok = False
        # macOS links ~/Google Drive and ~/OneDrive - <org> to ~/Library/CloudStorage,
        # so one folder is listed once, under the name found first.
        if ok and key not in seen:
            seen.add(key)
            found.append({'provider': provider, 'path': str(path)})

    for variable, provider in (('OneDriveCommercial', 'OneDrive (work or school)'),
                               ('OneDriveConsumer', 'OneDrive (personal)'), ('OneDrive', 'OneDrive')):
        if env.get(variable):
            add(provider, env[variable])
    cloud = home / 'Library' / 'CloudStorage'  # macOS File Provider locations
    for entry in sorted(cloud.iterdir()) if cloud.is_dir() else []:
        low = entry.name.lower()
        if low.startswith('googledrive'):
            add('Google Drive', entry / 'My Drive')
            add('Google Drive (shared drives)', entry / 'Shared drives')
        elif low.startswith('onedrive'):
            add('OneDrive', entry)
        elif low.startswith('dropbox'):
            add('Dropbox', entry)
        elif low.startswith('box'):
            add('Box', entry)
        else:
            add(entry.name.split('-')[0], entry)
    for entry in sorted(home.iterdir()) if home.is_dir() else []:
        name = entry.name
        if name == 'OneDrive' or name.startswith('OneDrive - ') or name.startswith('OneDrive-'):
            add('OneDrive', entry)
        elif name == 'Dropbox' or name.startswith('Dropbox ('):
            add('Dropbox', entry)
        elif name.lower().startswith('surfdrive'):
            add('SURFdrive', entry)
        elif name in ('ownCloud', 'Nextcloud'):
            add(name, entry)
        elif name in ('Google Drive', 'GoogleDrive'):
            add('Google Drive', entry / 'My Drive' if (entry / 'My Drive').is_dir() else entry)
    add('Google Drive', '/Volumes/GoogleDrive/My Drive')  # older Google Drive for desktop on macOS
    if os.name == 'nt':
        for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ':
            add('Google Drive', f'{letter}:\\My Drive')
            add('Google Drive (shared drives)', f'{letter}:\\Shared drives')
    return found


def find(root: Path) -> dict:
    folders = find_folders()
    return {'action': 'find', 'found': [f"{f['provider']}: {f['path']}" for f in folders],
            'next': ('show the person these places and ask which folder (or which new subfolder) is '
                     'this project\'s; connect only the one they confirm') if folders else
                    ('no sync app folder found; ask the person which app their team uses, and '
                     'whether it is installed and signed in on this computer')}


# ---------------------------------------------------------------------------
# What is in the shared folder: sync trouble, a recorded inventory, and a comparison with it.

INVENTORY = 'sources/shared-inventory.json'
HASH_CACHE = 'workspace/.shared-hash-cache.json'
NOISE_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini', '.dropbox', '.dropbox.attr', 'Icon\r'}
NOISE_PREFIXES = ('~$', '.~lock.', '.sync_', '._')
NOISE_SUFFIXES = ('.tmp', '.partial', '.crdownload', '.download', '.part', '.icloud')
SHORTCUT_SUFFIXES = ('.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform', '.gmap', '.gsite', '.gjam', '.gnote')
CONFLICT_MARKS = (re.compile(r'conflicted copy', re.I),          # Dropbox, Nextcloud
                  re.compile(r'_conflict-\d{8}-\d{6}'),          # ownCloud, SURFdrive
                  re.compile(r'[\(\[]conflict[\)\]]', re.I))
# A sibling's name plus one of these is a possible conflict copy: OneDrive appends the
# computer's name (name-DESKTOP-1AB2C3D.ext), Google Drive a number (name (1).ext).
COPY_SUFFIX = re.compile(r'^(?:-[A-Z0-9][A-Z0-9-]{2,}| \(\d+\))$')
NOISE_EXCLUDES = ['.DS_Store', 'Thumbs.db', 'desktop.ini', '~$*', '.~lock.*', '*.tmp', '*.partial',
                  '*.crdownload', '._*']


def is_noise(name: str) -> bool:
    return name in NOISE_NAMES or name.startswith(NOISE_PREFIXES) or name.endswith(NOISE_SUFFIXES)


def online_only(info: os.stat_result) -> bool:
    """A placeholder whose bytes are still in the cloud: reading it would download it.

    Windows marks such files RECALL_ON_DATA_ACCESS, RECALL_ON_OPEN or OFFLINE; macOS File
    Provider marks them SF_DATALESS. Neither path was run on those systems here.
    """
    attributes = getattr(info, 'st_file_attributes', 0) or 0
    if attributes & (0x00400000 | 0x00040000 | 0x00001000):
        return True
    return bool((getattr(info, 'st_flags', 0) or 0) & 0x40000000)


def shared_base(root: Path) -> Path:
    base = root / shared_path(root)
    if not base.is_dir():
        raise FolderError('the shared folder is not on this machine; run status')
    return base


def walk(base: Path, unreadable: list | None = None):
    """(relative posix path, stat) for every regular file, not following links.

    What cannot be read (a folder that cannot be listed, a file that cannot be read) goes into
    `unreadable`, so a caller never takes a partial scan for the whole folder.
    """
    def note(path):
        if unreadable is not None:
            unreadable.append(Path(os.path.relpath(path, base)).as_posix() if path else '.')

    for directory, dirs, files in os.walk(base, followlinks=False, onerror=lambda exc: note(exc.filename)):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d)))
        for name in sorted(files):
            full = os.path.join(directory, name)
            try:
                info = os.stat(full, follow_symlinks=False)
            except OSError:
                note(full)
                continue
            if not os.path.islink(full):
                yield Path(full).relative_to(base).as_posix(), info


def incoming_files(root: Path) -> list[str]:
    """The files waiting in the incoming folder, by the path the project names them, the sync
    clients' housekeeping files left out; empty when the folder is not on this machine."""
    name = incoming_path(root)
    base = root / name
    if not base.is_dir():
        return []
    return [name + '/' + path for path, _ in walk(base) if not is_noise(path.rsplit('/', 1)[-1])]


def conflict_of(path: str, siblings: set) -> str | None:
    name = path.rsplit('/', 1)[-1]
    if any(mark.search(name) for mark in CONFLICT_MARKS):
        return 'a sync conflict copy'
    stem, dot, suffix = name.rpartition('.')
    if not dot or not stem:
        stem, suffix = name, ''
    folder = path.rsplit('/', 1)[0] + '/' if '/' in path else ''
    for cut in range(1, len(stem)):
        if COPY_SUFFIX.match(stem[cut:]):
            original = folder + stem[:cut] + ('.' + suffix if suffix else '')
            if original in siblings:
                return 'possibly a conflict copy of ' + original
    return None


def check(root: Path, limit: int = 50) -> dict:
    """Report sync trouble in the shared folder. Changes nothing."""
    base = shared_base(root)
    unreadable = []
    files = list(walk(base, unreadable))
    names = {path for path, _ in files}
    result = {'action': 'check', 'files': 0, 'bytes': 0, 'online_only': [], 'conflict_copies': [],
              'google_shortcuts': [], 'temporary': [], 'unreadable': unreadable}
    for path, info in files:
        name = path.rsplit('/', 1)[-1]
        if is_noise(name):
            result['temporary'].append(path)
            continue
        result['files'] += 1
        result['bytes'] += info.st_size
        if name.endswith(SHORTCUT_SUFFIXES):
            result['google_shortcuts'].append(path)
        if online_only(info):
            result['online_only'].append(path)
        why = conflict_of(path, names)
        if why:
            result['conflict_copies'].append(f'{path}: {why}')
    for key in ('online_only', 'conflict_copies', 'google_shortcuts', 'temporary', 'unreadable'):
        result[key + '_count'] = len(result[key])
        result[key] = result[key][:limit]
    steps = []
    if result['unreadable']:
        steps.append('some items could not be read, so this report leaves them out; ask the person about '
                     'access to them')
    if result['conflict_copies']:
        steps.append('show the person the conflict copies and ask which version to keep; delete nothing yourself')
    if result['online_only']:
        steps.append('the online-only files download when read; before an analysis, ask the person to mark '
                     'the project folder "Always keep on this device" (OneDrive) or "Available offline" '
                     '(Google Drive)')
    if result['google_shortcuts']:
        steps.append('Google Docs, Sheets and Slides are links, not files; ask the person to export the ones '
                     'an analysis needs (for example as .csv or .docx)')
    result['next'] = '; '.join(steps) or 'nothing'
    return result


def read_cache(root: Path) -> dict:
    try:
        data = json.loads((root / HASH_CACHE).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def file_sha256(full: Path, info: os.stat_result, cache: dict, key: str) -> str:
    # Size and modification time alone would let a different file with the same two (after
    # connect --replace, say) pass as the recorded one; the device and inode tie the digest to
    # this very file, and a sync app that rewrites a file gives it a new inode.
    stamp = [info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino]
    cached = cache.get(key)
    if isinstance(cached, list) and len(cached) == len(stamp) + 1 and cached[:-1] == stamp:
        return cached[-1]
    digest = hashlib.sha256()
    with open(full, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    cache[key] = stamp + [digest.hexdigest()]
    return digest.hexdigest()


def write_cache(root: Path, cache: dict) -> None:
    ensure_ignored(root, HASH_CACHE)
    target = root / HASH_CACHE
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.' + str(os.getpid()))
    temp.write_text(json.dumps(cache), encoding='utf-8')
    os.replace(temp, target)


def scan(root: Path, download: bool) -> tuple[dict, list, list]:
    """{path: {size, sha256}} for the shared folder's files, the online-only ones left unread, and
    what could not be read at all."""
    base = shared_base(root)
    cache, rows, skipped, unreadable = read_cache(root), {}, [], []
    for path, info in walk(base, unreadable):
        name = path.rsplit('/', 1)[-1]
        if is_noise(name):
            continue
        if online_only(info) and not download:
            rows[path] = {'size': info.st_size, 'sha256': None}
            skipped.append(path)
            continue
        try:
            rows[path] = {'size': info.st_size, 'sha256': file_sha256(base / path, info, cache, path)}
        except OSError:
            unreadable.append(path)
    write_cache(root, cache)
    return rows, skipped, unreadable


def inventory(root: Path, download: bool = False) -> dict:
    """Write sources/shared-inventory.json: each file's path, size and SHA-256, for Git."""
    target = root / INVENTORY
    previous = None
    try:
        previous = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    rows, skipped, unreadable = scan(root, download)
    if unreadable:
        # A partial scan would quietly drop what it could not read from the record.
        raise FolderError('could not read ' + str(len(unreadable)) + ' item(s) in the shared folder ('
                          + ', '.join(unreadable[:10]) + '), so the inventory was not written; ask the person '
                          'about access to them, then run inventory again')
    recorded = {}
    if isinstance(previous, dict) and isinstance(previous.get('files'), list):
        recorded = {row.get('path'): row for row in previous['files'] if isinstance(row, dict)}
    kept = []
    for path in skipped:
        # An online-only file was not read; keep the digest already recorded for it while its size is unchanged.
        row = recorded.get(path)
        if row and row.get('sha256') and row.get('size') == rows[path]['size']:
            rows[path]['sha256'] = row['sha256']
            kept.append(path)
    data = {'schema_version': 1, 'shared_path': shared_path(root),
            'recorded_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            'files': [{'path': p, **rows[p]} for p in sorted(rows)]}
    remote = shared_remote(root)
    if remote:
        data['bucket'] = remote['location']
    if isinstance(previous, dict) and previous.get('files') == data['files']:
        return {'action': 'inventory', 'inventory': INVENTORY, 'files': len(rows), 'changed': False,
                'next': 'nothing; the recorded inventory already matches the shared folder'}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')
    return {'action': 'inventory', 'inventory': INVENTORY, 'files': len(rows), 'changed': True,
            'bytes': sum(r['size'] for r in rows.values()),
            'not_hashed_online_only': [p for p in skipped if p not in kept][:50],
            'kept_recorded_digest_online_only': kept[:50],
            'next': 'commit ' + INVENTORY + ' so coauthors\' agents can check their copies against it'}


def verify(root: Path, download: bool = False, limit: int = 50) -> dict:
    """Compare this machine's shared folder with the inventory Git holds. Changes nothing."""
    try:
        recorded = json.loads((root / INVENTORY).read_text(encoding='utf-8'))
        expected = {row['path']: row for row in recorded['files']}
    except (OSError, ValueError, KeyError, TypeError):
        raise FolderError('no readable ' + INVENTORY + '; run inventory first') from None
    rows, skipped, unreadable = scan(root, download)
    missing = sorted(set(expected) - set(rows))
    new = sorted(set(rows) - set(expected))
    changed, unread = [], []
    for path in sorted(set(expected) & set(rows)):
        want, have = expected[path], rows[path]
        if want.get('size') != have['size']:
            changed.append(path)
        elif have['sha256'] is None or want.get('sha256') is None:
            unread.append(path)
        elif want['sha256'] != have['sha256']:
            changed.append(path)
    result = {'action': 'verify', 'inventory_recorded_at': recorded.get('recorded_at'),
              'matching': len(expected) - len(missing) - len(changed) - len(unread),
              'missing_here': missing[:limit], 'missing_here_count': len(missing),
              'changed': changed[:limit], 'changed_count': len(changed),
              'not_in_inventory': new[:limit], 'not_in_inventory_count': len(new),
              'size_only': unread[:limit], 'size_only_count': len(unread),
              'unreadable': unreadable[:limit], 'unreadable_count': len(unreadable)}
    steps = []
    if unreadable:
        steps.append('some items could not be read, so files under them may be listed as missing; ask the '
                     'person about access to them')
    if missing:
        steps.append('files the inventory lists are not here: the sync app may still be downloading, or '
                     'someone removed them; for a bucket, run pull')
    if changed:
        steps.append('files differ from the recorded version: ask the person which version is right '
                     'before using them')
    if new:
        steps.append('files are here that the inventory does not list: run inventory and commit it if '
                     'they belong to the project')
    if unread:
        steps.append('some files are online-only and were compared by size alone; --download compares '
                     'their bytes')
    result['next'] = '; '.join(steps) or 'nothing; this machine has the recorded files'
    return result


# ---------------------------------------------------------------------------
# A bucket (Google Cloud Storage, SURF's object store, any S3 store) through rclone.

def read_project(root: Path) -> dict:
    try:
        data = json.loads((root / 'ai' / 'project.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def shared_remote(root: Path) -> dict | None:
    value = read_project(root).get('shared_remote')
    return value if isinstance(value, dict) and isinstance(value.get('location'), str) else None


def rclone_path() -> str:
    found = shutil.which(os.environ.get('TRACE_OS_RCLONE', 'rclone'))
    if not found:
        raise FolderError('rclone is not installed; install it (macOS: brew install rclone; Windows: '
                          'winget install Rclone.Rclone; Linux: rclone.org/install, since distribution packages are often old)')
    return found


def run_rclone(*args) -> subprocess.CompletedProcess:
    return subprocess.run([rclone_path(), *args], capture_output=True, text=True)


def remotes() -> dict:
    """{name: type} for the rclone remotes this machine knows, environment-defined ones included."""
    done = run_rclone('listremotes', '--long')
    if done.returncode != 0:
        raise FolderError('rclone listremotes failed: ' + done.stderr.strip()[-300:])
    found = {}
    for line in done.stdout.splitlines():
        name, _, kind = line.partition(':')
        if name.strip():
            found[name.strip()] = kind.strip()
    return found


def remote_set(root: Path, location: str, note: str | None = None) -> dict:
    name, colon, path = location.partition(':')
    if not colon or not name or not path.strip('/'):
        raise FolderError('give the bucket as <rclone remote>:<bucket>/<folder>, for example '
                          'team-bucket:our-project-data/study')
    known = remotes()
    if name not in known:
        raise FolderError(f'this machine has no rclone remote called {name}; help the person run '
                          f'"rclone config" and name the new remote {name}, then try again')
    link = root / shared_path(root)
    if is_link(link):
        raise FolderError('the shared path is linked to a sync app folder; disconnect it first, so the '
                          'project does not use two systems at once')
    project = read_project(root)
    project['shared_remote'] = {'location': location, 'type': known[name]}
    if note:
        project['shared_remote']['note'] = note
    target = root / 'ai' / 'project.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(project, indent=2) + '\n', encoding='utf-8')
    link.mkdir(parents=True, exist_ok=True)
    added = ensure_ignored(root, shared_path(root))
    return {'action': 'bucket set', 'location': location, 'type': known[name], 'gitignore_added': added,
            'next': 'commit ai/project.json so coauthors use the same bucket; each coauthor names their '
                    f'rclone remote {name}; then run pull'}


def remote_unset(root: Path) -> dict:
    project = read_project(root)
    if 'shared_remote' not in project:
        raise FolderError('no bucket is set for this project')
    was = project.pop('shared_remote')
    (root / 'ai' / 'project.json').write_text(json.dumps(project, indent=2) + '\n', encoding='utf-8')
    return {'action': 'bucket unset', 'was': was.get('location'),
            'note': 'the bucket and the local copy are unchanged'}


def compare(source: str, destination: str) -> dict:
    """rclone check --combined: only in source (+), only in destination (-), different (*)."""
    done = run_rclone('check', source, destination, '--combined', '-', *excludes())
    if done.returncode not in (0, 1):
        raise FolderError('rclone check failed: ' + done.stderr.strip()[-400:])
    out = {'+': [], '-': [], '*': [], '!': [], '=': []}
    for line in done.stdout.splitlines():
        if len(line) > 2 and line[0] in out and line[1] == ' ':
            out[line[0]].append(line[2:])
    return out


def excludes() -> list:
    flags = []
    for pattern in NOISE_EXCLUDES:
        flags += ['--exclude', pattern]
    return flags


def transfer(root: Path, direction: str, plan: bool, limit: int = 50) -> dict:
    remote = shared_remote(root)
    if not remote:
        raise FolderError('no bucket is set for this project; a sync app folder needs no pull or push')
    local = root / shared_path(root)
    if is_link(local):
        raise FolderError('the shared path is a link to a sync app folder, not a bucket copy')
    local.mkdir(parents=True, exist_ok=True)
    bucket = remote['location']
    source, destination = (bucket, str(local)) if direction == 'pull' else (str(local), bucket)
    before = compare(source, destination)
    result = {'action': direction + (' plan' if plan else ''), 'bucket': bucket,
              'to_copy': (before['+'] + before['*'])[:limit], 'to_copy_count': len(before['+']) + len(before['*']),
              'only_at_destination': before['-'][:limit], 'only_at_destination_count': len(before['-']),
              'errors': before['!'][:limit]}
    if plan:
        result['next'] = (f'read the plan to the person, then run {direction}' if result['to_copy_count']
                          else 'nothing to copy')
        return result
    done = run_rclone('copy', source, destination, '--update', *excludes())
    if done.returncode != 0:
        raise FolderError('rclone copy failed: ' + done.stderr.strip()[-400:])
    after = compare(source, destination)
    result['copied_count'] = result['to_copy_count'] - len(after['+']) - len(after['*'])
    result['kept_newer_at_destination'] = after['*'][:limit]
    result['never_deleted'] = ('nothing was deleted on either side; files only at the destination stay, '
                               'and a newer file at the destination is kept')
    steps = []
    if after['*']:
        steps.append('some files are newer at the destination and were kept; ask the person which version is right')
    if direction == 'push' or before['+']:
        steps.append('run inventory and commit it, so coauthors can check their copies')
    result['next'] = '; '.join(steps) or 'nothing'
    return result


def render(result: dict) -> str:
    rows = []
    for key, value in result.items():
        if value in (None, '', [], {}):
            continue
        if isinstance(value, list):
            value = '\n  - ' + '\n  - '.join(map(str, value))
        elif isinstance(value, dict):
            value = json.dumps(value)
        rows.append(f'{key.replace("_", " ")}: {value}')
    return '\n'.join(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', type=Path, default=argparse.SUPPRESS,
                        help='project root (default: this script\'s project)')
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='print the result as JSON')
    parser.set_defaults(project=ROOT, json=False)
    for action in common._actions:
        parser._add_action(action)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', parents=[common], help='say whether the shared folder is connected on this machine')
    p = sub.add_parser('connect', parents=[common], help='link the project\'s shared path to the person\'s shared folder')
    p.add_argument('path', help='the shared folder, as the person gave it')
    p.add_argument('--create', action='store_true', help='make the folder when it does not exist')
    p.add_argument('--move-existing', action='store_true', help='move what the project holds there into it')
    p.add_argument('--replace', action='store_true', help='point an existing link at a different folder')
    p.add_argument('--plan', action='store_true', help='say what would change, change nothing')
    sub.add_parser('disconnect', parents=[common], help='remove the link; the shared folder is untouched')
    p = sub.add_parser('snapshot', parents=[common], help='take a verified local snapshot of a subfolder of the shared folder')
    p.add_argument('subpath', help='the subfolder, relative to the shared folder ("." for all of it)')
    p.add_argument('--dataset', required=True, help='a short name for what the snapshot holds')
    p = sub.add_parser('keep-local', parents=[common], help='keep paths out of Git by adding them to .gitignore')
    p.add_argument('patterns', nargs='+')
    sub.add_parser('find', parents=[common], help='list the sync apps\' folders on this machine')
    sub.add_parser('check', parents=[common], help='report conflict copies, online-only files and shortcuts')
    for name, text in (('inventory', 'record each shared file\'s path, size and SHA-256 in ' + INVENTORY),
                       ('verify', 'compare this machine\'s shared folder with ' + INVENTORY)):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument('--download', action='store_true', help='read online-only files too, which downloads them')
    p = sub.add_parser('bucket', parents=[common], help='use a bucket through rclone instead of a sync app folder')
    p.add_argument('step', choices=('set', 'unset'))
    p.add_argument('location', nargs='?', help='<rclone remote>:<bucket>/<folder>, for set')
    p.add_argument('--note', help='what the bucket is, for coauthors (no credentials)')
    for name in ('pull', 'push'):
        p = sub.add_parser(name, parents=[common], help=('copy new files from the bucket here' if name == 'pull'
                                                         else 'copy new files from here to the bucket')
                                                         + '; never deletes')
        p.add_argument('--plan', action='store_true', help='say what would be copied, copy nothing')
    args = parser.parse_args(argv)
    root = args.project.resolve()
    try:
        if args.command == 'status':
            result = status(root)
        elif args.command == 'connect':
            result = connect(root, args.path, args.create, args.move_existing, args.replace, args.plan)
        elif args.command == 'disconnect':
            result = disconnect(root)
        elif args.command == 'snapshot':
            result = snapshot(root, args.subpath, args.dataset)
        elif args.command == 'keep-local':
            result = keep_local(root, args.patterns)
        elif args.command == 'find':
            result = find(root)
        elif args.command == 'check':
            result = check(root)
        elif args.command == 'inventory':
            result = inventory(root, args.download)
        elif args.command == 'verify':
            result = verify(root, args.download)
        elif args.command == 'bucket':
            if args.step == 'set':
                if not args.location:
                    raise FolderError('bucket set needs <rclone remote>:<bucket>/<folder>')
                result = remote_set(root, args.location, args.note)
            else:
                result = remote_unset(root)
        else:
            result = transfer(root, args.command, args.plan)
    except (OSError, FolderError) as exc:
        print('shared folder: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
