#!/usr/bin/env python3
"""Plan and apply a starter locally. No remote operations or human acceptance."""
from __future__ import annotations
import argparse
import contextlib
from contextlib import contextmanager
import datetime as dt
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
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
import uuid
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
AGENTS = {'claude': '.claude/skills', 'codex': '.agents/skills', 'cline': '.cline/skills'}
# The installation state's folder, then its earlier names, newest first: .traceware/ in 0.1.0-rc19 and
# .trace-kit/ up to 0.1.0-rc18. A project keeps its state in an earlier folder until the update to a release
# whose manifest names a newer one (install_state_dir) moves it there. Every reader here looks in STATE_DIR
# first, then in each earlier folder in turn.
STATE_DIR = '.trace-os'
EARLIER_STATE_DIRS = ('.traceware', '.trace-kit')
STATE_DIRS = (STATE_DIR, *EARLIER_STATE_DIRS)
# The names below are in STATE_DIR; in_layout() gives a project's own, which differ before the move.
STATE = STATE_DIR + '/install.json'
JOURNAL = STATE_DIR + '/transaction.json'
UPDATE_PENDING = STATE_DIR + '/pending-update.json'
PLAN_PENDING = STATE_DIR + '/pending-plan.json'
UPDATE_HISTORY = STATE_DIR + '/update-history.jsonl'
RECEIPT = STATE_DIR + '/receipts/latest.json'
# The release archive's one top folder, then the ones earlier releases used (0.1.0-rc19, then up to
# 0.1.0-rc18), which a chain still reads.
ARCHIVE_ROOTS = ('trace-os', 'traceware', 'trace-kit-starter')
# Managed instruction-section markers, <!-- trace-os:begin id -->, then their earlier forms, newest first:
# traceware: in 0.1.0-rc19 and trace-kit: up to 0.1.0-rc18.
MARKER = '<!-- trace-os:'
MARKERS = (MARKER, '<!-- traceware:', '<!-- trace-kit:')
# The key of ai/project.json that holds a project's overrides, then its earlier names, newest first.
PROJECT_KEYS = ('trace_os', 'traceware', 'trace_kit')
IGNORE_FILE = '.gitignore'
# Files an operating system drops into any folder; never a skill's content (AH-172).
OS_FILES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}


class SetupError(Exception):
    pass


class Interrupted(KeyboardInterrupt):
    """SIGTERM or SIGHUP, raised like Ctrl-C so an update restores what it applied (AH-168)."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


@contextmanager
def interruptible():
    """While this is open, SIGTERM and SIGHUP raise Interrupted instead of ending the process.

    An agent's command timeout or a closed terminal otherwise stops the process
    between two writes, with no chance to restore the previous installation or to
    stop the check it started (AH-168). Signal handlers belong to the main thread,
    so elsewhere this changes nothing, and a signal the caller ignores (nohup)
    stays ignored.
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


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return (json.dumps(data, sort_keys=True, indent=2) + '\n').encode()


def relative(name):
    if not isinstance(name, str) or '\\' in name or ':' in name:
        raise SetupError('invalid package path')
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in ('', '.', '..') for p in name.split('/')):
        raise SetupError('package path must be a confined relative path')
    return name


def confined(root, name):
    relative(name)
    path = root / name
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise SetupError(f'symlink is not supported: {name}')
        if component == root:
            break
    if root.resolve() not in path.resolve().parents:
        raise SetupError('path escapes target')
    return path


def load(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise SetupError(f'{path.name} must be a JSON object')
    return data


def state_dir(target):
    """The folder holding this project's installation state: .trace-os/, or an earlier name (STATE_DIRS).

    The folder holding install.json wins, newest name first, so a stray empty
    folder of another name changes nothing; with none holding it, an existing
    folder (an unfinished first setup's), and with none, .trace-os/.
    """
    target = Path(target)
    for name in STATE_DIRS:
        if (target / name / 'install.json').is_file():
            return name
    for name in STATE_DIRS:
        if (target / name).is_dir():
            return name
    return STATE_DIR


def in_layout(target, name):
    """`name`, a path under .trace-os/, where this project keeps it now."""
    folder = state_dir(target)
    if folder == STATE_DIR or not name.startswith(STATE_DIR + '/'):
        return name
    return folder + name[len(STATE_DIR):]


def recorded_path(target, name):
    """A state path a journal, a pending update or the history recorded, read where it is now.

    Such a path names the folder the state was in when it was written. After a
    move, or two in a chain, the same file sits under another folder's name, so
    a path that is missing as recorded is read in the first folder that holds it.
    """
    if (target / name).exists():
        return name
    for old in STATE_DIRS:
        if name.startswith(old + '/'):
            for new in STATE_DIRS:
                if new != old and (target / (new + name[len(old):])).exists():
                    return new + name[len(old):]
    return name


def newer_state_dir(folder, than):
    """Whether `folder` is a later state-folder name than `than`; both must be known names."""
    return folder in STATE_DIRS and than in STATE_DIRS and STATE_DIRS.index(folder) < STATE_DIRS.index(than)


def planned_move(manifest, target):
    """The state-folder move an update to this release makes, or None.

    A release moves the state to the folder its manifest names as its
    install_state_dir when the project keeps it under an earlier name: the hop
    to 0.1.0-rc19 moves .trace-kit/ to .traceware/, the hop to this release
    moves either earlier folder to .trace-os/. A release that names no folder,
    or one the project has already passed, leaves it where that release's own
    tools look for it.
    """
    folder, current = manifest.get('install_state_dir'), state_dir(target)
    if newer_state_dir(folder, current):
        return {'from': current, 'to': folder}
    return None


def move_clash(target, source, destination):
    """Why `source` cannot move to `destination`, or None: names both folders hold (a bare lock aside)."""
    there = target / destination
    if there.is_symlink() or (there.exists() and not there.is_dir()):
        return f'{destination} exists and is not a folder'
    if not there.is_dir():
        return None
    held = sorted(child.name for child in (target / source).iterdir()
                  if child.name != 'setup.lock' and (there / child.name).exists())
    return f"both {source}/ and {destination}/ hold {', '.join(held)}" if held else None


def move_state_folder(target, source, destination):
    """Move the installation state from one folder to the other; how it moved, or None when nothing did.

    One rename moves the whole folder, so no reader sees the state split
    between the two; on macOS and Linux the setup lock goes with it, still
    held, and a process that resolves the new folder meets the same lock.
    Windows keeps the folder of an open file in place, so there the entries
    move one by one, install.json last, and the held lock file stays behind for
    drop_leftover_lock() once it is released. A move that stopped part-way
    resumes here, since the two folders then hold different names.
    """
    src, dst = target / source, target / destination
    if src.is_symlink() or not src.is_dir():
        return None
    clash = move_clash(target, source, destination)
    if clash:
        raise SetupError(f'the installation state cannot move from {source}/ to {destination}/: {clash}; '
                         'keep one of each and run the command again')
    if not dst.exists() or [child.name for child in dst.iterdir()] in ([], ['setup.lock']):
        try:
            if dst.exists():
                (dst / 'setup.lock').unlink(missing_ok=True)
                dst.rmdir()
            os.rename(src, dst)
            return 'renamed'
        except OSError:
            pass
    dst.mkdir(exist_ok=True)
    for child in sorted(src.iterdir(), key=lambda path: (path.name == 'install.json', path.name)):
        if child.name != 'setup.lock':
            os.rename(child, dst / child.name)
    try:
        src.rmdir()
    except OSError:
        return 'moved entry by entry; the held lock file is removed once released'
    return 'moved entry by entry'


def drop_leftover_lock(target):
    """Remove a state folder that holds nothing but a released lock file, left by an entry-by-entry move."""
    for name in STATE_DIRS:
        folder = target / name
        if name != state_dir(target) and folder.is_dir() and not folder.is_symlink() \
                and [child.name for child in folder.iterdir()] == ['setup.lock']:
            with contextlib.suppress(OSError):
                (folder / 'setup.lock').unlink()
                folder.rmdir()


def finish_state_move(target):
    """Make a move an update to the installed release recorded but did not complete, or None.

    An update finalizes in the folder it started in and moves the folder after,
    so an update stopped in between leaves the new release installed under
    an earlier folder name. Every tool still reads it there; the next locked
    command moves it, once no transaction or update is pending.
    """
    current = state_dir(target)
    if current == STATE_DIR or any(
            (target / current / name).exists() for name in ('transaction.json', 'pending-update.json')):
        return None
    try:
        installed = load(target / 'starter-manifest.json')
        state = load(target / current / 'install.json')
    except (OSError, ValueError, SetupError):
        return None
    folder = installed.get('install_state_dir')
    if not newer_state_dir(folder, current) or state.get('version') != installed.get('version'):
        return None
    how = move_state_folder(target, current, folder)
    return {'from': current, 'to': folder, 'how': how} if how else None


def state_ignore(folder):
    """A pattern for an ignore rule's mention of any state-folder name earlier than `folder`."""
    earlier = STATE_DIRS[STATE_DIRS.index(folder) + 1:] if folder in STATE_DIRS else EARLIER_STATE_DIRS
    return re.compile(r'(?<![^/!*])(?:' + '|'.join(re.escape(name) for name in earlier) + r')(?=/|$)')


def with_state_ignores(data, move):
    """The project's ignore file with the new folder's line after each earlier folder's line it lacks.

    The project owns its .gitignore once it has edited it, so no line of it
    changes: each rule naming an earlier folder (.traceware/ or .trace-kit/)
    keeps its place and gains the same rule for the new one directly after it,
    once. Returns the bytes and the lines added.
    """
    lines = data.decode('utf-8', 'surrogateescape').splitlines(keepends=True)
    present = {line.strip() for line in lines}
    result, added = [], []
    ignore = state_ignore(move['to'])
    for line in lines:
        pattern = line.strip()
        converted = None
        if pattern and not pattern.startswith('#') and ignore.search(pattern):
            converted = ignore.sub(move['to'], pattern)
        if converted and converted not in present and converted not in added:
            ending = line[len(line.rstrip('\r\n')):]
            result.append(line if ending else line + '\n')
            result.append(converted + (ending or '\n'))
            added.append(converted)
        else:
            result.append(line)
    return ''.join(result).encode('utf-8', 'surrogateescape'), added


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def check_executor(executor):
    """One model identifier, recorded exactly as given.

    An optional bracketed suffix is part of it: Claude Code reports ids such
    as claude-opus-5-5[1m]. Whitespace and control characters stay out, since
    the id ends up in a commit trailer and the update history.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*(\[[A-Za-z0-9._-]+\])?', executor):
        raise SetupError('execution agent must be one identifier')


def release_identity(manifest, archive_sha256=None):
    result = {'version': manifest['version'], 'source_commit': manifest.get('source_commit'),
              'manifest_sha256': digest(encoded(manifest))}
    if archive_sha256:
        result['archive_sha256'] = archive_sha256
    return result


def marked_range(data, item):
    start, end = item['start'].encode(), item['end'].encode()
    if data.count(start) != 1 or data.count(end) != 1:
        raise SetupError('instruction section markers must occur exactly once: ' + item['id'])
    first = data.index(start)
    last = data.index(end, first) + len(end)
    if last < len(data) and data[last:last + 1] == b'\n':
        last += 1
    return first, last


def earlier_markers(item):
    """The section's markers as earlier releases wrote them, newest first.

    A release's own markers use one of MARKERS (a chain applies earlier
    releases' manifests too); each form older than that one is returned,
    newest first, the two earlier forms for this release's own.
    """
    forms = [marker for marker in MARKERS if item['start'].startswith(marker)]
    if not forms:
        return []
    own = forms[0]
    return [dict(item, start=item['start'].replace(own, marker, 1), end=item['end'].replace(own, marker, 1))
            for marker in MARKERS[MARKERS.index(own) + 1:]]


def marker_name(item):
    """The word of a section's markers: trace-os, traceware or trace-kit."""
    return item['start'][len('<!-- '):].split(':', 1)[0]


def convert_markers(section, item, old):
    """A section held under the earlier markers `old`, with the release's markers in their place."""
    return section.replace(old['start'].encode(), item['start'].encode(), 1) \
        .replace(old['end'].encode(), item['end'].encode(), 1)


def verify_release_archive(source, archive, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
        raise SetupError('expected release SHA-256 must be 64 hexadecimal characters')
    archive = archive.resolve()
    if not archive.is_file():
        raise SetupError('release archive does not exist')
    actual = digest(archive.read_bytes())
    if actual != expected.lower():
        raise SetupError('release archive SHA-256 does not match the independently supplied digest')
    try:
        with zipfile.ZipFile(archive) as zipped:
            names = [name for name in zipped.namelist() if name.endswith('/starter-manifest.json')]
            if len(names) != 1 or names[0] not in [root + '/starter-manifest.json' for root in ARCHIVE_ROOTS]:
                raise SetupError('release archive must contain one trace-os/starter-manifest.json '
                                 '(traceware/ in 0.1.0-rc19, trace-kit-starter/ up to 0.1.0-rc18)')
            archived = zipped.read(names[0])
    except zipfile.BadZipFile as exc:
        raise SetupError('release archive is not a readable ZIP') from exc
    local = (source / 'starter-manifest.json').read_bytes()
    if archived != local:
        raise SetupError('unpacked release manifest does not match the verified archive')
    return actual


def instruction_overrides(target):
    path = target / 'ai/project.json'
    if not path.is_file():
        return {}
    project = load(path)
    # The project's own file, never rewritten by an update: the trace_os key, else the
    # key an earlier release documented, newest first (traceware in 0.1.0-rc19, trace_kit before).
    key = next((name for name in PROJECT_KEYS if name in project), PROJECT_KEYS[0])
    trace = project.get(key, {})
    overrides = trace.get('instruction_overrides', {}) if isinstance(trace, dict) else {}
    if not isinstance(overrides, dict):
        raise SetupError(f'ai/project.json {key}.instruction_overrides must be an object')
    return overrides


def supported_upgrade(manifest, old, target, profile, agent):
    matches = [item for item in manifest.get('supported_upgrades', [])
               if item.get('from_version') == old.get('version')
               and item.get('to_version') == manifest.get('version')]
    if len(matches) != 1:
        raise SetupError(f"unsupported release pair: {old.get('version')} -> {manifest.get('version')}; a project "
                         'more than one release behind updates with --update-chain and --release-store '
                         '(SETUP-AGENT.md, Update)')
    item = matches[0]
    if profile not in item.get('profiles', []) or agent not in item.get('agents', []):
        raise SetupError('this release pair does not support the installed profile or agent adapter')
    if old.get('schema_version') != item.get('install_state_schema'):
        raise SetupError('installation-state schema migration is not supported by this release pair')
    project = load(target / 'ai/project.json')
    if project.get('schema_version') != item.get('project_schema'):
        raise SetupError('project schema migration is not supported by this release pair')
    from_record = item.get('from_record_schema', item.get('record_schema'))
    to_record = item.get('to_record_schema', item.get('record_schema'))
    old_record = old.get('record_schema_version', from_record)
    if old_record != from_record:
        raise SetupError('record schema migration is not supported by this release pair')
    compatibility = manifest.get('compatibility', {})
    expected = {'install_state_schema': item.get('install_state_schema'),
                'project_schema': item.get('project_schema'),
                'record_schema': to_record}
    if any(compatibility.get(key) != value for key, value in expected.items()):
        raise SetupError('release compatibility metadata changes a schema in the supported pair')
    return item


def seeded_prefix(current, recorded):
    """How many leading bytes of an instruction file are still the seed a release installed (AH-171).

    An update never rewrites a seeded file, so a project that started on an
    earlier release still holds that release's bytes where a migration's legacy
    baseline expects the release just before it: an untouched rc2 install
    reaches rc4 to rc5 with rc2's `ai/SKILLS-IN-USE.md`, not rc4's. The
    installation state records the digest of the seed it installed. When the file
    begins with bytes of that digest, ending at a line end or at the end of the
    file, those bytes are the kit's own and whatever follows is the project's.
    """
    if not isinstance(recorded, dict) or recorded.get('policy') != 'seed' or recorded.get('canonical'):
        return None
    running, length = hashlib.sha256(), 0
    for line in current.splitlines(keepends=True):
        running.update(line)
        length += len(line)
        if running.hexdigest() == recorded.get('sha256'):
            return length
    return None


def reconcile_sections(manifest, desired, old, target):
    overrides = instruction_overrides(target)
    by_path = {}
    status, conflicts = [], []
    for item in manifest.get('instruction_sections', []):
        name = relative(item['path'])
        if name not in desired:
            raise SetupError('instruction section names a file outside the selected profile')
        release_file = desired[name]['data']
        first, last = marked_range(release_file, item)
        release_section = release_file[first:last]
        if digest(release_section) != item.get('sha256') or len(release_section) != item.get('size'):
            raise SetupError('instruction section differs from the release manifest: ' + item['id'])
        if name not in by_path and not confined(target, name).is_file():
            # AH-173: a deleted instruction file is a named conflict, not a file error
            # that stops the plan before anything is reported.
            if 'missing-instruction:' + name not in conflicts:
                conflicts.append('missing-instruction:' + name)
            status.append({'id': item['id'], 'path': name, 'action': 'unresolved',
                           'reason': 'the instruction file is missing',
                           'hint': f'restore {name} (git checkout HEAD -- {name}), then update again'})
            continue
        current = by_path.setdefault(name, confined(target, name).read_bytes())
        previous = old.get('instruction_sections', {}).get(item['id'])
        action, installed, baseline, converted = None, current, None, None
        if previous:
            try:
                begin, finish = marked_range(current, item)
            except SetupError:
                # Installed by an earlier release: the same section under its earlier markers.
                for old_markers in earlier_markers(item):
                    try:
                        begin, finish = marked_range(current, old_markers)
                    except SetupError:
                        continue
                    converted = old_markers
                    break
                else:
                    conflicts.append('instruction:' + item['id'])
                    status.append({'id': item['id'], 'path': name, 'action': 'unresolved',
                                   'reason': 'installed section markers are absent or ambiguous'})
                    continue
            current_section = current[begin:finish]
            current_sha = digest(current_section)
            if current_sha == previous.get('release_sha256'):
                installed = current[:begin] + release_section + current[finish:]
                action = 'update'
            else:
                override = overrides.get(item['id'], {})
                token = str(item['version'])
                compatible = isinstance(override, dict) and token in override.get('compatible_with', [])
                basis = override.get('basis') if isinstance(override, dict) else None
                if not compatible or not isinstance(basis, str) or not basis.strip():
                    conflicts.append('instruction:' + item['id'])
                    status.append({'id': item['id'], 'path': name, 'action': 'unresolved',
                                   'reason': 'locally edited section has no compatibility declaration'})
                    continue
                action = 'retain-compatible-override'
                release_sha = item['sha256']
                if converted:
                    # The project's text stays; only its two marker lines change.
                    current_section = convert_markers(current_section, item, converted)
                    by_path[name] = current[:begin] + current_section + current[finish:]
                status.append({'id': item['id'], 'path': name, 'version': item['version'],
                               'action': action, 'release_sha256': release_sha,
                               'current_sha256': digest(current_section), 'compatibility_basis': basis.strip()})
                if converted:
                    status[-1]['markers'] = f'{marker_name(converted)}: markers became {marker_name(item)}: markers'
                continue
        else:
            legacy = [entry for entry in item.get('legacy', [])
                      if entry.get('from_version') == old.get('version')]
            if len(legacy) != 1 or legacy[0].get('mode') != 'prefix':
                conflicts.append('instruction:' + item['id'])
                status.append({'id': item['id'], 'path': name, 'action': 'unresolved',
                               'reason': 'no unique legacy section baseline for the installed release'})
                continue
            entry = legacy[0]
            prefix = current[:entry.get('size', -1)]
            if len(prefix) != entry.get('size') or digest(prefix) != entry.get('sha256'):
                # AH-171: only for a file that holds no bounded section yet, as the
                # legacy prefix itself assumes.
                sectioned = any(row['path'] == name for row in status) or any(
                    isinstance(row, dict) and row.get('path') == name
                    for row in old.get('instruction_sections', {}).values())
                length = None if sectioned else seeded_prefix(current, old.get('files', {}).get(name))
                if length is None:
                    conflicts.append('instruction:' + item['id'])
                    status.append({'id': item['id'], 'path': name, 'action': 'unresolved',
                                   'reason': 'legacy shared instruction prefix was edited or is unknown'})
                    continue
                prefix = current[:length]
                baseline = 'the seed an earlier release installed, unedited'
            installed = release_section + current[len(prefix):]
            action = 'adopt-bounded-section'
        by_path[name] = installed
        status.append({'id': item['id'], 'path': name, 'version': item['version'],
                       'action': action, 'release_sha256': item['sha256'],
                       'current_sha256': item['sha256']})
        if baseline:
            status[-1]['baseline'] = baseline
        if converted:
            status[-1]['markers'] = f'{marker_name(converted)}: markers became {marker_name(item)}: markers'
    return by_path, status, conflicts


def file_hash(path):
    if path.exists() and not path.is_file():
        raise SetupError(f'expected a regular file: {path.name}')
    return digest(path.read_bytes()) if path.exists() else None


def skill_of(name):
    """The skill id a canonical or generated skill path belongs to, else None."""
    for root in ('.skills',) + tuple(AGENTS.values()):
        if name.startswith(root + '/') and '/' in name[len(root) + 1:]:
            return name[len(root) + 1:].split('/', 1)[0]
    return None


def git_ignored(target, names):
    """The names Git ignores in this project. Tracked files are never among them."""
    if not names or not (target / '.git').exists():
        return set()
    try:
        run = subprocess.run(['git', 'check-ignore', '-z', '--stdin'], cwd=target, capture_output=True,
                             input='\0'.join(names).encode('utf-8'))
    except OSError:
        return set()
    if run.returncode not in (0, 1):
        return set()
    return {name for name in run.stdout.decode('utf-8', 'replace').split('\0') if name}


def skill_litter(target, names, shipped=()):
    """Names that are operating-system files or ignored by Git, so never skill content (AH-172).

    A file a release shipped is never litter, whatever an ignore rule says.
    """
    names = [name for name in names if name not in shipped]
    litter = {name for name in names if PurePosixPath(name).name in OS_FILES
              or PurePosixPath(name).name.startswith('._')}
    return litter | git_ignored(target, [name for name in names if name not in litter])


def skill_files(target, shipped=()):
    """The canonical skill files on disk, without operating-system files or files Git ignores."""
    root = target / '.skills'
    if not root.is_dir():
        return []
    names = sorted(path.relative_to(target).as_posix() for path in root.rglob('*') if path.is_file())
    litter = skill_litter(target, names, shipped)
    return [name for name in names if name not in litter]


def retired_skills(manifest, old):
    """Shipped skills this project retired, followed through the renames of this update (AH-172)."""
    retired = old.get('retired_skills', []) if old else []
    if not isinstance(retired, list) or not all(isinstance(value, str) for value in retired):
        raise SetupError('installation state carries an invalid retired_skills list')
    retired = set(retired)
    if old and old.get('version') != manifest.get('version'):
        for item in manifest.get('renames', []):
            if isinstance(item, dict) and item.get('from_version') == old.get('version') \
                    and skill_of(str(item.get('from', ''))) in retired and skill_of(str(item.get('to', ''))):
                retired.add(skill_of(item['to']))
    return retired


def renamed_files(manifest, old, desired, target, agent, retired=()):
    """Old package paths this release pair renames, and the copies it can remove.

    Without this, a renamed skill leaves its old canonical file and its old
    generated agent copy in place, so the project holds both names and the
    installed set no longer matches the release.

    A group is removed only when every copy still holds the bytes the
    installation recorded. A locally edited copy keeps the whole group, and its
    paths join the conflict list, so the update stays pending and names them.
    That is what an edited copy of a skill this release does not rename already
    does, and deleting somebody's edit is the one outcome a rollback cannot
    undo. Nothing is written and nothing is removed until the person resolves
    the named path.
    """
    removals, retained = {}, []
    for item in manifest.get('renames', []):
        if not isinstance(item, dict):
            raise SetupError('invalid rename entry')
        if item.get('from_version') != old.get('version'):
            continue
        if skill_of(str(item.get('from', ''))) in retired:
            continue  # AH-172: a retired skill stays retired under its new name
        source, destination = relative(item['from']), relative(item['to'])
        if destination not in desired:
            raise SetupError('a rename names a destination outside this release: ' + destination)
        group = [source]
        if source.startswith('.skills/') and agent in AGENTS:
            group.append(AGENTS[agent] + '/' + source[len('.skills/'):])
        recorded = old.get('files', {})
        # A path the installation recorded is part of the group even when it is
        # no longer on disk, so its entry leaves the state with the rest.
        known = [name for name in group
                 if name in recorded or file_hash(confined(target, name)) is not None]
        edited = [name for name in known
                  if file_hash(confined(target, name)) != recorded.get(name, {}).get('sha256')
                  and file_hash(confined(target, name)) is not None]
        if edited:
            retained.append({'from': source, 'to': destination, 'paths': group,
                             'edited': sorted(edited),
                             'reason': 'a locally edited copy keeps this rename pending; '
                                       'merge or remove it, then update again'})
            continue
        for name in known:
            removals[name] = None
    return removals, retained


def dropped_files(old, desired, target, handled):
    """Managed files a release installed that this release no longer ships.

    The installation state tells them apart from the project's own entries:
    only a release writes a managed entry without a `canonical` key, and a
    generated agent copy came with a release when its canonical file did. A
    copy --sync-skills made of the project's own skill names a canonical file
    the state does not hold, so it stays, as do seeded files, which the project
    owns once installed. An entry stays recorded until an update drops it, so
    this also reaches a file an earlier release stopped shipping.

    An unmodified or already missing file is removed with its entry, inside the
    update's transaction, so a failed check or a rollback restores it. An
    edited one is a conflict, as an edited managed file this release changes is.
    """
    recorded = old.get('files', {})

    def shipped(name):
        entry = recorded.get(name, {})
        return entry.get('policy') == 'managed' and not entry.get('canonical')

    removals, edited = {}, []
    for name, entry in recorded.items():
        if name in desired or name in handled:
            continue
        if not (shipped(name) or (entry.get('canonical') and shipped(entry['canonical']))):
            continue
        current = file_hash(confined(target, name))
        if current is None or current == entry.get('sha256'):
            removals[name] = None
        else:
            edited.append(name)
    return removals, edited


def update_payload(summary, desired):
    """The bytes to write for each planned change; None removes that path."""
    return {name: (None if action == 'remove' else desired[name]['data'])
            for name, action in summary['changes'].items()}


def prune_empty_skill_dirs(target, names):
    """Remove only the directory a renamed or dropped skill file leaves behind."""
    roots = ('.skills',) + tuple(AGENTS.values())
    pruned = []
    for name in sorted(names, key=lambda value: -value.count('/')):
        parent = PurePosixPath(name).parent
        while str(parent) != '.' and any(str(parent) == root or str(parent).startswith(root + '/')
                                         for root in roots):
            path = confined(target, str(parent))
            if not path.is_dir() or any(path.iterdir()):
                break
            path.rmdir()
            pruned.append(str(parent))
            parent = parent.parent
    return pruned


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bundle(source, target, profile, agent):
    manifest = load(source / 'starter-manifest.json')
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('files'), dict):
        raise SetupError('unsupported starter manifest')
    desired = {}
    installed_here = source == target and (target / in_layout(target, STATE)).is_file()
    # AH-221: in place, a retired skill's files were the source and are gone;
    # leaving them out here is what plan() would do with them anyway.
    gone = set(load(target / in_layout(target, STATE)).get('retired_skills') or []) if installed_here else set()
    for name, item in manifest['files'].items():
        parts = PurePosixPath(name).parts
        if len(parts) > 2 and parts[0] == '.skills' and parts[1] in gone and not (source / name).exists():
            continue
        if isinstance(item, dict) and item.get('policy') == 'release':
            # In the archive, never in a project: the kit's LICENSE, since a project's own licence is
            # its owner's to choose (0.1.0-rc19).
            continue
        if not isinstance(item, dict) or item.get('policy') not in ('managed', 'seed'):
            raise SetupError('invalid file entry')
        if item.get('profile') not in ('core', 'academic'):
            raise SetupError('invalid file profile')
        if item['profile'] == 'academic' and profile == 'core':
            continue
        if PurePosixPath(name).parts[0] in ('.git', *STATE_DIRS) or name in ('ai/project.json', 'starter-manifest.json'):
            raise SetupError('package entry targets reserved setup state')
        path = confined(source, name)
        data = path.read_bytes()
        if digest(data) != item.get('sha256') or len(data) != item.get('size'):
            if not (installed_here and item['policy'] == 'seed'):
                raise SetupError(f'starter content differs from its manifest: {name}; use a fresh archive')
        desired[name] = dict(item, data=data)
    raw = (source / 'starter-manifest.json').read_bytes()
    desired['starter-manifest.json'] = {'sha256': digest(raw), 'data': raw, 'policy': 'managed'}
    if agent != 'none':
        for name, item in list(desired.items()):
            if name.startswith('.skills/'):
                target_name = AGENTS[agent] + '/' + name[len('.skills/'):]
                desired[target_name] = dict(item, policy='managed', canonical=name,
                                            sha256=digest(item['data']))
    return manifest, desired


def git(root, *args, check=True):
    run = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True,
                         encoding='utf-8', errors='replace')
    if check and run.returncode:
        raise SetupError(run.stderr.strip() or 'Git command failed')
    return run.stdout.strip()


def executable_names(names):
    """Package files Git runs only when their mode allows it: the shell scripts."""
    return sorted(name for name in names
                  if name.startswith('scripts/') and name.endswith('.sh') and name.count('/') == 1)


def mark_executable(target, names, posix=os.name != 'nt'):
    """Restore the executable bits a ZIP or a plain copy did not carry. A no-op on Windows."""
    matched = [name for name in executable_names(names) if confined(target, name).is_file()]
    if not posix:
        return {'files': matched, 'mode': None,
                'reason': 'file modes are not applied on this platform; run the shell scripts through bash'}
    for name in matched:
        os.chmod(confined(target, name), 0o755)
    return {'files': matched, 'mode': '0755'}


def git_state(target):
    probe = target if target.exists() else target.parent
    while not probe.exists():
        probe = probe.parent
    top = git(probe, 'rev-parse', '--show-toplevel', check=False)
    if not top:
        return False, None
    if Path(top).resolve() != target:
        raise SetupError('target is inside another Git repository; choose a separate directory')
    return True, git(target, 'rev-parse', '--verify', 'HEAD', check=False) or None


def in_step(target, agent, desired, name, current):
    """False only for a canonical skill file whose generated copy holds other bytes (AH-122)."""
    if not name.startswith('.skills/') or agent not in AGENTS:
        return True
    copy = AGENTS[agent] + '/' + name[len('.skills/'):]
    return copy not in desired or file_hash(confined(target, copy)) == current


def plan(source, target, profile='academic', agent='claude'):
    if target.is_symlink():
        raise SetupError('target must not be a symlink')
    source, target = source.resolve(), target.resolve()
    if source != target and source in target.parents:
        raise SetupError('target must be outside the starter directory')
    manifest, desired = bundle(source, target, profile, agent)
    state_name = in_layout(target, STATE)
    old = load(confined(target, state_name)) if (target / state_name).exists() else {}
    if old and (old.get('schema_version') != 1 or not isinstance(old.get('files'), dict)):
        raise SetupError('unsupported installation state')
    if old and (old.get('profile'), old.get('agent')) != (profile, agent):
        raise SetupError('profile or agent changes need a separate reviewed migration')
    upgrade = bool(old and old.get('version') != manifest.get('version'))
    unfinished = pending_update_state(target)
    if upgrade:
        try:
            supported_upgrade(manifest, old, target, profile, agent)
        except SetupError as exc:
            if unfinished:  # AH-168: the installation state may be an unfinished candidate's
                raise SetupError(f"{exc}; an update to {unfinished.get('to', 'a new release')} "
                                 'did not finish, so run --recover first') from None
            raise
    # AH-172: a shipped skill the project retired is neither installed nor recorded again.
    retired = retired_skills(manifest, old)
    if retired:
        desired = {name: item for name, item in desired.items() if skill_of(name) not in retired}
    changes, kept, conflicts, expected, kept_edits = {}, [], [], {}, []
    section_paths = {item['path'] for item in manifest.get('instruction_sections', [])}
    section_status = []
    reconciled, removals, renamed_retained, dropped = {}, {}, [], {}
    move, unmoved = (planned_move(manifest, target) if upgrade else None), None
    if move:
        # The state is in an earlier folder (.traceware/ or .trace-kit/); this update moves it to the folder
        # its release names once it succeeds.
        move['entries'] = sorted(child.name for child in (target / move['from']).iterdir()
                                 if child.name != 'setup.lock')
        clash = move_clash(target, move['from'], move['to'])
        if clash:
            conflicts.append('state-folder:' + move['to'] + '/')
            move['clash'] = clash + '; keep one of each, then update again'
    elif old and not upgrade and newer_state_dir(manifest.get('install_state_dir'), state_dir(target)):
        unmoved = {'from': state_dir(target), 'to': manifest['install_state_dir'],
                   'pending': 'the update to this release finished without moving the folder; the next '
                              '--apply, --update-chain, --sync-skills or --recover moves it'}
    if upgrade:
        reconciled, section_status, section_conflicts = reconcile_sections(manifest, desired, old, target)
        conflicts.extend(section_conflicts)
        removals, renamed_retained = renamed_files(manifest, old, desired, target, agent, retired)
        conflicts.extend(name for row in renamed_retained for name in row['edited'])
        handled = set(removals).union(*(row['paths'] for row in renamed_retained))
        dropped, dropped_edited = dropped_files(old, desired, target, handled)
        conflicts.extend(dropped_edited)
    for name, item in desired.items():
        current = file_hash(confined(target, name))
        before = old.get('files', {}).get(name, {}).get('sha256')
        expected[name] = current
        if upgrade and name in section_paths:
            if any(row['path'] == name and row['action'] == 'unresolved' for row in section_status):
                continue
            planned = reconciled[name]
            desired[name]['data'] = planned
            if digest(planned) == current:
                kept.append(name)
            else:
                changes[name] = 'instruction-update'
            continue
        if current == item['sha256']:
            kept.append(name)
        elif current is None:
            changes[name] = 'create'
        elif item['policy'] == 'seed' and old:
            kept.append(name)
        elif move and name == IGNORE_FILE and before is not None and current != before:
            # The project edited its ignore file, so it owns it: the update adds the
            # new folder's rule after each earlier folder's rule and changes no line of it. The
            # state keeps the kit's digest, as for a kept edit, so a later release that
            # changes the file still finds the edit.
            data, added = with_state_ignores(confined(target, name).read_bytes(), move)
            move['ignore_file'] = {'path': name, 'added': added,
                                   'kept': 'the project edited it; no line of it changed'}
            if added:
                desired[name] = dict(item, data=data)
                changes[name] = 'state-folder-ignores'
        elif upgrade and (item.get('canonical') in kept_edits or (
                'canonical' not in item and before == item['sha256']
                and in_step(target, agent, desired, name, current))):
            # AH-122: the project changed a managed file this release ships as the kit
            # installed it, so there is nothing to merge: the file stays as the project
            # has it, unwritten, and the state keeps the kit's digest, so a release that
            # does change the file still finds the edit. A generated skill copy follows
            # its canonical file, which is kept only when the copy already matches it.
            kept_edits.append(name)
        elif before is not None and current == before:
            changes[name] = 'update'
        else:
            conflicts.append(name)
    for name in [*removals, *dropped]:
        changes[name] = 'remove'
        expected[name] = file_hash(confined(target, name))
    # AH-175: the installation state belongs to the planned snapshot too.
    expected[state_name] = file_hash(confined(target, state_name))
    # Project epochs are initialized once and are never reset on upgrades.
    if not old and (target / 'ai/project.json').exists():
        conflicts.append('ai/project.json')
    repository, head = git_state(target)
    # AH-178: an untouched copy in a held rename leaves once the rename resumes;
    # the group is explained under renamed.retained, not kept on purpose.
    held = {name for row in renamed_retained for name in row['paths']}
    summary = {'schema_version': 1, 'version': manifest['version'], 'profile': profile,
               'agent': agent, 'target': str(target), 'changes': changes,
               'preserved': kept, 'kept_project_edits': sorted(kept_edits),
               'conflicts': sorted(conflicts),
               'mode': 'upgrade' if upgrade else ('rerun' if old else 'install'),
               'from_version': old.get('version') if old else None,
               'instruction_sections': section_status,
               'expected_before': expected,
               'renamed': {'removed': sorted(removals), 'retained': renamed_retained},
               'retired_skills': sorted(retired),
               'obsolete_preserved': sorted(set(old.get('files', {})) - set(desired) - set(changes)
                                            - set(conflicts) - held),
               'git': 'retain existing history and index' if head else 'initialize local Git history',
               # AH-168: an unfinished update is pending recovery as much as a transaction is.
               'pending_recovery': (target / in_layout(target, JOURNAL)).exists() or unfinished is not None,
               'pending_update': unfinished}
    if move:
        if IGNORE_FILE in changes and 'ignore_file' not in move:
            move['ignore_file'] = {'path': IGNORE_FILE, 'replaced': "the kit's lines, unedited, by this release's"}
        summary['state_folder'] = move
    if unmoved:
        summary['state_folder_pending'] = unmoved
    return summary, desired, old, repository, head


@contextmanager
def locked(target):
    folder = state_dir(target)
    directory = confined(target, folder)
    directory.mkdir(parents=True, exist_ok=True)
    lock = confined(target, folder + '/setup.lock')
    with lock.open('a+b') as stream:
        # Use byte zero as one stable lock region on Windows. The byte is setup
        # state and carries no ownership claim; the OS releases the lock when
        # the stream or process closes.
        if msvcrt is not None:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'\0')
                stream.flush()
        try:
            stream.seek(0)
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise SetupError('this Python build has no supported file-locking module')
        except (BlockingIOError, OSError):
            raise SetupError('another setup process owns this target') from None
        try:
            yield
        finally:
            stream.seek(0)
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def recover(target):
    path = confined(target, in_layout(target, JOURNAL))
    if not path.exists():
        return False
    journal = load(path)
    if journal.get('schema_version') != 1 or not isinstance(journal.get('entries'), dict):
        raise SetupError(f'invalid recovery journal; inspect {state_dir(target)}/backups/')
    for name, entry in journal['entries'].items():
        current = file_hash(confined(target, name))
        if current not in (entry['before'], entry['after']):
            raise SetupError(f'recovery would overwrite a later edit: {name}')
        if entry['before'] is not None:
            backup = confined(target, recorded_path(target, entry['backup']))
            if file_hash(backup) != entry['before']:
                raise SetupError('recovery backup digest mismatch')
    for name, entry in journal['entries'].items():
        destination = confined(target, name)
        if entry['before'] is None:
            destination.unlink(missing_ok=True)
        else:
            write_atomic(destination, confined(target, recorded_path(target, entry['backup'])).read_bytes())
    # AH-174: a restored file is written anew, so the shell scripts get their mode back.
    mark_executable(target, journal['entries'])
    path.unlink()
    return True


def transaction(target, updates, expected=None):
    run_id = uuid.uuid4().hex
    entries = {}
    for name, data in updates.items():
        path = confined(target, name)
        before = file_hash(path)
        if expected is not None and name in expected and before != expected[name]:
            raise SetupError(f'file changed since the setup plan: {name}')
        backup_name = f'{state_dir(target)}/backups/{run_id}/{name}'
        if before is not None:
            write_atomic(confined(target, backup_name), path.read_bytes())
        entries[name] = {'before': before, 'after': digest(data) if data is not None else None,
                         'backup': backup_name}
    journal = in_layout(target, JOURNAL)
    write_atomic(confined(target, journal), encoded({'schema_version': 1, 'entries': entries}))
    try:
        for name, data in updates.items():
            # A cooperative lock does not authorize overwriting a human edit made since planning.
            if file_hash(confined(target, name)) != entries[name]['before']:
                raise SetupError(f'file changed while applying setup: {name}')
            if data is None:
                confined(target, name).unlink(missing_ok=True)
            else:
                write_atomic(confined(target, name), data)
    except Exception:
        recover(target)
        raise
    (target / journal).unlink()


def initialize_history(target, existing_head, names, executor):
    project = confined(target, 'ai/project.json')
    if project.exists():
        return
    if existing_head is None:
        if not (target / '.git').exists():
            git(target, 'init', '-q')
        # Only package files are staged. Existing untracked material stays untouched.
        git(target, 'add', '--', *sorted(names), in_layout(target, STATE))
        # The committed tree carries the shell scripts' mode on every platform, so
        # a later clone on macOS or Linux checks them out executable even when
        # this machine keeps no file modes.
        executables = [name for name in executable_names(names) if (target / name).is_file()]
        if executables:
            git(target, 'update-index', '--chmod=+x', '--', *executables)
        git(target, 'commit', '-m', 'Initialize research workflow starter\n\nExecution-agent: ' + executor)
        existing_head = git(target, 'rev-parse', 'HEAD')
    data = {'schema_version': 1, 'policy_epoch': existing_head, 'accept_epoch': existing_head,
            'custom_bibliography': False, 'shared_root': 'workspace/shared'}
    transaction(target, {'ai/project.json': encoded(data)})


def instruction_state(manifest, summary, desired, old=None):
    if old and summary.get('mode') == 'rerun' and old.get('instruction_sections'):
        return old['instruction_sections']
    rows = {row['id']: row for row in summary.get('instruction_sections', [])
            if row.get('action') != 'unresolved'}
    result = {}
    for item in manifest.get('instruction_sections', []):
        row = rows.get(item['id'])
        if row is None:
            first, last = marked_range(desired[item['path']]['data'], item)
            current = digest(desired[item['path']]['data'][first:last])
            row = {'current_sha256': current, 'action': 'release'}
        result[item['id']] = {'path': item['path'], 'version': item['version'],
                              'release_sha256': item['sha256'],
                              'current_sha256': row['current_sha256'],
                              'status': 'override' if row['action'] == 'retain-compatible-override' else 'release'}
        if row.get('compatibility_basis'):
            result[item['id']]['compatibility_basis'] = row['compatibility_basis']
    return result


def installation_state(manifest, summary, desired, old, profile, agent, archive_sha256=None):
    release = release_identity(manifest, archive_sha256)
    previous = old.get('release') if old else None
    # AH-170: a rerun of the release an update installed keeps the archive digest the
    # update verified; dropping it changed install.json and rollback then refused.
    if archive_sha256 is None and summary.get('mode') == 'rerun' and isinstance(previous, dict) \
            and all(previous.get(key) == value for key, value in release.items()):
        release = previous
    state = {'schema_version': 1, 'version': summary['version'], 'profile': profile, 'agent': agent,
             'record_schema_version': manifest.get('compatibility', {}).get('record_schema', 1),
             'release': release,
             'instruction_sections': instruction_state(manifest, summary, desired, old),
             'files': {name: {k: value for k, value in item.items()
                              if k in ('sha256', 'policy', 'canonical')}
                       for name, item in desired.items()}}
    # AH-122: a kept project edit keeps its recorded entry, the kit's digest (for a
    # synced skill copy, the digest --sync-skills recorded).
    for name in summary['preserved'] + summary['obsolete_preserved'] + summary.get('kept_project_edits', []):
        if name in old.get('files', {}) and (name not in desired or
                file_hash(confined(Path(summary['target']), name)) != desired[name]['sha256']):
            state['files'][name] = old['files'][name]
    if summary.get('retired_skills'):
        state['retired_skills'] = summary['retired_skills']
    return state


def append_event_bytes(path, event):
    prior = path.read_bytes() if path.exists() else b''
    return prior + (json.dumps(event, sort_keys=True) + '\n').encode()


def record_pending_plan(target, summary, manifest):
    plan_name, history_name = in_layout(target, PLAN_PENDING), in_layout(target, UPDATE_HISTORY)
    pending_path = confined(target, plan_name)
    previous = load(pending_path) if pending_path.exists() else None
    history = confined(target, history_name)
    updates = {}
    if previous and previous.get('to', {}).get('version') != manifest['version']:
        updates[history_name] = append_event_bytes(history, {
            'schema_version': 1, 'event': 'plan-superseded', 'recorded_at': now(),
            'from': previous.get('from'), 'to': previous.get('to')})
    event = {'schema_version': 1, 'event': 'plan-pending', 'recorded_at': now(),
             'from': {'version': summary.get('from_version')},
             'to': release_identity(manifest), 'conflicts': summary['conflicts'],
             'instruction_sections': summary.get('instruction_sections', [])}
    existing = updates.get(history_name, history.read_bytes() if history.exists() else b'')
    updates[history_name] = existing + (json.dumps(event, sort_keys=True) + '\n').encode()
    updates[plan_name] = encoded(event)
    transaction(target, updates)


def begin_update(target, updates, state, summary, manifest, archive_sha256, executor):
    """Put the whole candidate in place, installation state included, then check it.

    The state file used to be held back until the checks passed. That left the
    project internally inconsistent while they ran, with the new release's files
    beside the previous release's record of them, and the installed-skill check
    read that record: a release that renames a skill failed its own post-update
    check because the state still named the file the update had just removed.
    The candidate is now applied whole, so the check tests what a success would
    leave behind, and a failure restores files and state together from the
    verified backups.
    """
    run_id = uuid.uuid4().hex
    combined = dict(updates)
    combined[in_layout(target, STATE)] = encoded(state)
    planned = summary.get('expected_before', {})
    for name in combined:
        # AH-175: an edit saved between the plan and this write is refused, as
        # --apply refuses it, rather than overwritten and left only in a backup.
        if name in planned and file_hash(confined(target, name)) != planned[name]:
            raise SetupError('file changed since the update plan: ' + name)
    entries = {}
    for name, data in combined.items():
        path = confined(target, name)
        if path.exists() and not path.is_file():
            raise SetupError(f'expected a regular file: {path.name}')
        # AH-175: the baseline is the planned hash, never a fresh read, and the backup
        # holds exactly those bytes, so an edit saved after the check above is refused
        # here, before anything is written, and every write below is compared with it.
        before = planned[name] if name in planned else file_hash(path)
        saved = path.read_bytes() if path.is_file() else None
        if (digest(saved) if saved is not None else None) != before:
            raise SetupError('file changed since the update plan: ' + name)
        backup = f'{state_dir(target)}/update-backups/{run_id}/{name}'
        if saved is not None:
            write_atomic(confined(target, backup), saved)
        entries[name] = {'before': before, 'after': digest(data) if data is not None else None,
                         'backup': backup, 'applied_before_checks': True}
    pending = {'schema_version': 1, 'run_id': run_id, 'started_at': now(),
               'execution_agent': executor,
               'from': {'version': summary['from_version']},
               'to': release_identity(manifest, archive_sha256),
               'entries': entries, 'changes': summary['changes'],
               'kept_project_edits': summary.get('kept_project_edits', []),
               'instruction_sections': summary.get('instruction_sections', [])}
    if summary.get('state_folder'):
        # The move comes after the update is final; until then every path here is in the old folder.
        pending['state_folder'] = {key: summary['state_folder'][key] for key in ('from', 'to')}
    write_atomic(confined(target, in_layout(target, UPDATE_PENDING)), encoded(pending))
    try:
        for name, data in combined.items():
            if file_hash(confined(target, name)) != entries[name]['before']:
                raise SetupError('file changed while applying update: ' + name)
            if data is None:
                confined(target, name).unlink(missing_ok=True)
            else:
                write_atomic(confined(target, name), data)
    except BaseException as exc:
        stop_update(target, exc)
        raise
    return pending


def update_completed(target, pending):
    """True when the history holds this run's success and every file still has its updated bytes."""
    history = confined(target, in_layout(target, UPDATE_HISTORY))
    if not history.is_file():
        return False
    completed = any(json.loads(line).get('run_id') == pending.get('run_id')
                    and json.loads(line).get('status') == 'succeeded'
                    for line in history.read_text(encoding='utf-8').splitlines() if line.strip())
    return completed and all(file_hash(confined(target, name)) == entry['after']
                             for name, entry in pending['entries'].items())


def pending_update_state(target):
    """What a pending-update marker describes, read without changing anything (AH-168, AH-169).

    An update that stopped before it finished leaves its candidate installed and
    install.json naming the new release, so the plan has to say so rather than
    read the candidate as a completed installation.
    """
    path = confined(target, in_layout(target, UPDATE_PENDING))
    if not path.exists():
        return None
    try:
        pending = load(path)
        if pending.get('schema_version') != 1 or not isinstance(pending.get('entries'), dict):
            raise ValueError('invalid pending update')
        completed = update_completed(target, pending)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SetupError):
        folder = state_dir(target)
        return {'state': f'unreadable; inspect {folder}/pending-update.json and {folder}/update-backups/'}
    return {'run_id': pending.get('run_id'), 'started_at': pending.get('started_at'),
            'from': (pending.get('from') or {}).get('version'), 'to': (pending.get('to') or {}).get('version'),
            'state': ('completed; only its marker is left, which --recover clears' if completed else
                      'unfinished; --recover, --update, --update-chain, --apply or --rollback '
                      'restores the previous installation first')}


# The update-history fields for a candidate a later run found and restored.
FOUND_LATER = {'status': 'interrupted',
               'cause': 'the update stopped before it finished; a later run restored the previous installation'}


def recover_update(target, record=True):
    """Restore an unfinished update's candidate, or clear the marker of a completed one.

    Returns False when no update is pending, otherwise what was found and done,
    keeping the two cases apart (AH-169): a completed update whose marker merely
    survived stays installed, and an unfinished candidate is restored from the
    verified backups. A restored candidate is recorded in the update history
    (AH-168): with `record` True as an update a later run found stopped, with a
    dict as those event fields, and not at all with False, for a caller that
    records its own event.
    """
    path = confined(target, in_layout(target, UPDATE_PENDING))
    if not path.exists():
        return False
    pending = load(path)
    entries = pending.get('entries')
    if pending.get('schema_version') != 1 or not isinstance(entries, dict):
        raise SetupError(f'invalid pending update; inspect {state_dir(target)}/update-backups/')
    found = {'run_id': pending.get('run_id'), 'from': (pending.get('from') or {}).get('version'),
             'to': (pending.get('to') or {}).get('version')}
    if update_completed(target, pending):
        path.unlink()
        confined(target, in_layout(target, PLAN_PENDING)).unlink(missing_ok=True)
        return dict(found, outcome='the update had completed; only its leftover marker was cleared')
    for name, entry in entries.items():
        current = file_hash(confined(target, name))
        allowed = {entry['before']}
        if entry.get('applied_before_checks'):
            allowed.add(entry['after'])
        if current not in allowed:
            raise SetupError('update recovery would overwrite a later edit: ' + name)
        if entry['before'] is not None and \
                file_hash(confined(target, recorded_path(target, entry['backup']))) != entry['before']:
            raise SetupError('update recovery backup digest mismatch: ' + name)
    for name, entry in entries.items():
        destination = confined(target, name)
        if entry['before'] is None:
            destination.unlink(missing_ok=True)
        else:
            write_atomic(destination, confined(target, recorded_path(target, entry['backup'])).read_bytes())
    # AH-174: the restored scripts are written anew, so they get their mode back.
    mark_executable(target, entries)
    if record:
        history = confined(target, in_layout(target, UPDATE_HISTORY))
        event = {'schema_version': 1, 'event': 'update', 'recorded_at': now(),
                 'run_id': pending.get('run_id'), 'started_at': pending.get('started_at'),
                 'from': pending.get('from'), 'to': pending.get('to'),
                 'execution_agent': pending.get('execution_agent'), 'checks': [],
                 'restored': 'the previous installation, from the verified backups'}
        event.update(FOUND_LATER if record is True else record)
        # Written before the marker goes: a stop in between records the event
        # twice rather than not at all.
        write_atomic(history, append_event_bytes(history, event))
    path.unlink()
    return dict(found, outcome='the update had not finished; the previous installation was restored')


def stop_update(target, exc, checks=None):
    """An update stopped part-way: restore what it applied and record why (AH-168).

    Covers Ctrl-C, SIGTERM and SIGHUP (raised as Interrupted) and an error while
    the candidate was applied, checked or finalized. An interrupted closing
    transaction is undone first; after that, an update that had completed keeps
    its installation, and a failed check that already restored and recorded
    itself leaves nothing to do.
    """
    if (target / in_layout(target, JOURNAL)).exists():
        recover(target)
    cause = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
    return recover_update(target, record={
        'status': 'failed' if isinstance(exc, Exception) else 'interrupted',
        'cause': cause, 'checks': checks or []})


def stop_process(process, windows=os.name == 'nt'):
    """End a check and every process it started: its whole process group (AH-168).

    The check ending on SIGTERM says nothing about a process it started that
    ignores it, so after the grace period the group gets SIGKILL either way; a
    group already gone is the outcome wanted. Windows has no process groups to
    signal, so there `taskkill /T /F` ends the check's process tree while the
    check is still its root, a branch not yet run on Windows.
    """
    try:
        if windows:
            try:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)],
                               capture_output=True, check=False)
            except OSError:
                pass
            if process.poll() is None:
                process.kill()
            process.wait()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait()
    finally:
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


def command_check(command, cwd, label):
    # AH-168: the check runs in a process group of its own, so an interrupted
    # update stops it and everything it started rather than leave them running.
    options = {'start_new_session': True} if os.name != 'nt' else {}
    with interruptible():
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                   **options)
        try:
            stdout, stderr = process.communicate()
        except BaseException:
            stop_process(process)
            raise
    # AH-250: the tails can cut a failing test's name, so unittest's own
    # FAIL:/ERROR: headers are read from the whole output.
    failed = sorted({line.strip() for line in (stdout + '\n' + stderr).splitlines()
                     if line.startswith(('FAIL: ', 'ERROR: '))})
    return {'name': label, 'command': command, 'exit_code': process.returncode,
            'status': 'pass' if process.returncode == 0 else 'fail',
            'failed_tests': failed,
            'stdout_tail': stdout[-2000:], 'stderr_tail': stderr[-2000:]}


def preflight_release(source):
    return command_check([sys.executable, '-X', 'utf8', '-I', '-S', '-m', 'unittest',
                          'discover', '-s', 'tests', '-p', 'test_*.py'], source,
                         'release portable tests')


def preflight_refusal(target, preflight):
    """Keep the refused preflight's output, and say which tests failed (AH-250).

    The log goes to the ignored receipts folder, so no project file changes.
    """
    message = 'release preflight failed before project writes'
    failed = preflight.get('failed_tests') or []
    if failed:
        message += ': ' + '; '.join(failed)
    elif preflight.get('exit_code') is not None:
        message += f" (exit {preflight['exit_code']}, no failing test named)"
    if target.is_dir() and not target.is_symlink():
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        log = target / f'{state_dir(target)}/receipts/update-preflight-{stamp}.json'
        try:
            write_atomic(log, encoded(dict(preflight, recorded_at=now())))
            message += '; output kept in ' + log.relative_to(target).as_posix()
        except OSError as error:
            message += f'; its output could not be kept ({error.strerror or error})'
    return message


def check_updated_project(target, run_id):
    checker = target / 'scripts/check_workflow.py'
    if not checker.is_file():
        raise SetupError('updated project has no scripts/check_workflow.py')
    report = target / f'{state_dir(target)}/receipts/update-{run_id}-workflow.json'
    return command_check([sys.executable, str(checker), '--out', str(report)], target,
                         'updated project workflow')


def finalize_update(target, pending, state, checks):
    state_name, history_name = in_layout(target, STATE), in_layout(target, UPDATE_HISTORY)
    history = confined(target, history_name)
    event = {'schema_version': 1, 'event': 'update', 'status': 'succeeded',
             'recorded_at': now(), 'run_id': pending['run_id'], 'from': pending['from'],
             'to': pending['to'], 'changes': pending['changes'],
             'kept_project_edits': pending.get('kept_project_edits', []),
             'execution_agent': pending.get('execution_agent'),
             'instruction_sections': pending['instruction_sections'], 'checks': checks,
             'entries': pending['entries']}
    if pending.get('state_folder'):
        # Written before the folder moves; a rollback moves it back before it restores anything.
        event['state_folder'] = pending['state_folder']
    updates = {state_name: encoded(state), history_name: append_event_bytes(history, event)}
    expected = {state_name: pending['entries'][state_name]['after'], history_name: file_hash(history)}
    transaction(target, updates, expected)
    confined(target, in_layout(target, UPDATE_PENDING)).unlink()
    confined(target, in_layout(target, PLAN_PENDING)).unlink(missing_ok=True)
    return event


def fail_update(target, pending, checks):
    recover_update(target, record=False)
    history = confined(target, in_layout(target, UPDATE_HISTORY))
    event = {'schema_version': 1, 'event': 'update', 'status': 'failed',
             'recorded_at': now(), 'run_id': pending['run_id'], 'from': pending['from'],
             'to': pending['to'], 'execution_agent': pending.get('execution_agent'),
             'checks': checks}
    write_atomic(history, append_event_bytes(history, event))
    return event


def apply(source, target, profile='academic', agent='claude', executor='starter-bootstrap'):
    if target.is_symlink():
        raise SetupError('target must not be a symlink')
    source, target = source.resolve(), target.resolve()
    check_executor(executor)
    summary, desired, old, repository, head = plan(source, target, profile, agent)
    if summary['mode'] == 'upgrade':
        raise SetupError('version changes require --update with a verified release archive and digest')
    if summary['conflicts'] and not summary['pending_recovery']:
        raise SetupError('conflicting files: ' + ', '.join(summary['conflicts']))
    # Check author configuration before creating a new repository or writing files.
    if head is None:
        probe = target if target.exists() else source
        identity = git(probe, 'var', 'GIT_AUTHOR_IDENT', check=False)
        if not identity:
            raise SetupError('configure your Git user.name and user.email, then rerun setup')
        if repository:
            staged = git(target, 'diff', '--cached', '--name-only').splitlines()
            for name in staged:
                expected = old.get('files', {}).get(name, {}).get('sha256')
                if name == in_layout(target, STATE) and old:
                    expected = digest(encoded(old))
                raw = subprocess.run(['git', 'show', ':' + name], cwd=target, capture_output=True)
                if expected is None or raw.returncode or digest(raw.stdout) != expected:
                    raise SetupError('unborn repository has unrelated staged work; commit it before setup')
    target.mkdir(parents=True, exist_ok=True)
    with locked(target):
        recovered = recover(target)
        # AH-168: an unfinished update is restored before anything is replanned, so
        # this apply cannot rewrite the candidate's state and strand its recovery.
        update_recovered = recover_update(target)
        moved = finish_state_move(target)
        summary, desired, old, repository, head = plan(source, target, profile, agent)
        if summary['mode'] == 'upgrade':
            raise SetupError(('an unfinished update was restored first; ' if update_recovered else '')
                             + 'version changes require --update with a verified release archive and digest')
        if summary['conflicts']:
            raise SetupError('conflicting files: ' + ', '.join(summary['conflicts']))
        updates = update_payload(summary, desired)
        manifest = load(source / 'starter-manifest.json')
        state = installation_state(manifest, summary, desired, old, profile, agent)
        state_name = in_layout(target, STATE)
        old_bytes = (target / state_name).read_bytes() if (target / state_name).exists() else None
        summary['expected_before'][state_name] = digest(old_bytes) if old_bytes is not None else None
        if encoded(state) != old_bytes:
            updates[state_name] = encoded(state)
        if updates:
            transaction(target, updates, summary['expected_before'])
        executable = mark_executable(target, desired)
        initialize_history(target, head, desired, executor)
        global_skills = []
        if agent in AGENTS:
            for skill in sorted((target / '.skills').iterdir()):
                if skill.is_dir() and (Path.home() / AGENTS[agent] / skill.name).exists():
                    global_skills.append(skill.name)
        receipt = dict(summary, applied=True, recovered=recovered, update_recovered=update_recovered,
                       executable=executable,
                       generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                       python=sys.version.split()[0], git_version=git(target, '--version'),
                       global_skill_names_present=global_skills,
                       remaining=['Run the portable tests and workflow check.',
                                  'Review and commit ai/project.json and any setup changes.',
                                  'Model discovery and permissions require a recorded rehearsal.'])
        if moved:
            receipt['state_folder'] = moved
            receipt['remaining'].insert(0, moved_message(moved))
        write_atomic(target / in_layout(target, RECEIPT), encoded(receipt))
    drop_leftover_lock(target)
    return receipt


def pending_message(summary):
    """Why an update stays pending, in words the person can act on."""
    # AH-173: a missing instruction file needs restoring, not moving or deleting.
    hints = {'missing-instruction:' + row['path']: row['hint']
             for row in summary.get('instruction_sections', []) if row.get('hint')}
    changed = [name for name in summary['conflicts'] if name not in hints]
    parts = [name + '; ' + hint for name, hint in sorted(hints.items())]
    if changed:
        parts.insert(0, ', '.join(changed) + '; each was changed in this project since the kit installed it:'
                     ' move the change to a file of your own or delete it, then update again'
                     ' (SETUP-AGENT.md, Conflicts)')
    return 'update is pending: ' + '; '.join(parts)


def moved_message(moved):
    """What a receipt's remaining steps say about a state folder that moved."""
    return (f"The installation state moved from {moved['from']}/ to {moved['to']}/ ({moved.get('how', 'renamed')}): "
            f"stage both folders (git add -A {moved['from']} {moved['to']}) so Git records the tracked files "
            'as renamed, and commit them with the update.')


def move_after_update(target, folder):
    """Move the state folder once the update is final; the move, or why it waits.

    Windows keeps the held lock file's folder in place, so there the entries
    move one by one and the lock file goes once released. A move that fails
    leaves the new release installed in the old folder, where every tool still
    reads it, and the next locked command finishes it (finish_state_move).
    """
    try:
        how = move_state_folder(target, folder['from'], folder['to'])
    except (OSError, SetupError) as exc:
        return dict(folder, how=None, waiting=f'{type(exc).__name__}: {exc}; the next --apply, '
                    '--update-chain, --sync-skills or --recover moves it')
    return dict(folder, how=how)


def update(source, target, profile, agent, archive, expected_sha256, executor='starter-bootstrap'):
    if target.is_symlink():
        raise SetupError('target must not be a symlink')
    source, target = source.resolve(), target.resolve()
    check_executor(executor)
    archive_sha256 = verify_release_archive(source, archive, expected_sha256)
    preflight = preflight_release(source)
    manifest = load(source / 'starter-manifest.json')
    if preflight['status'] != 'pass':
        raise SetupError(preflight_refusal(target, preflight))
    with locked(target), interruptible():
        if (target / in_layout(target, JOURNAL)).exists():
            recover(target)
        recovered = recover_update(target)
        finished = finish_state_move(target)
        summary, desired, old, repository, head = plan(source, target, profile, agent)
        if not old or summary['mode'] != 'upgrade':
            raise SetupError('--update requires a supported version change from an installed project')
        if summary['conflicts']:
            record_pending_plan(target, summary, manifest)
            raise SetupError(pending_message(summary))
        state = installation_state(manifest, summary, desired, old, profile, agent, archive_sha256)
        updates = update_payload(summary, desired)
        pending = begin_update(target, updates, state, summary, manifest, archive_sha256, executor)
        checks = [preflight]
        # AH-168: from here until the update is final, a Ctrl-C, a SIGTERM or an
        # error restores the previous installation and records the attempt, so no
        # unchecked candidate is left behind looking like a completed update.
        try:
            # Before the check, so it reads the tree a success would leave. Restoring
            # a file recreates its directory, so a failed update loses nothing.
            pruned = prune_empty_skill_dirs(target, summary['renamed']['removed'])
            prune_empty_skill_dirs(target, [name for name, action in summary['changes'].items()
                                            if action == 'remove'])
            # AH-174: the check sees the modes a success would leave.
            executable = mark_executable(target, desired)
            try:
                postflight = check_updated_project(target, pending['run_id'])
            except Exception as exc:
                postflight = {'name': 'updated project workflow', 'status': 'error',
                              'error': f'{type(exc).__name__}: {exc}'}
                fail_update(target, pending, [preflight, postflight])
                raise SetupError('updated project check could not run; restored the last successful installation') from exc
            checks = [preflight, postflight]
            if postflight['status'] != 'pass':
                fail_update(target, pending, checks)
                raise SetupError('updated project checks failed; restored the last successful installation')
            event = finalize_update(target, pending, state, checks)
        except BaseException as exc:
            stop_update(target, exc, checks)
            raise
        # The update is final in the folder it started in; only now does the folder move.
        moved = move_after_update(target, summary['state_folder']) if summary.get('state_folder') else None
    drop_leftover_lock(target)
    if moved:
        summary['state_folder'] = dict(summary['state_folder'], **moved)
    renamed = dict(summary['renamed'], pruned_directories=pruned)
    remaining = [f'Review and commit the update changes and {in_layout(target, UPDATE_HISTORY)}.',
                 'Public release discovery and publisher authentication are not included.']
    if renamed['removed']:
        remaining.append('Renamed skills: the old ids are gone and the new ones installed. '
                         'Update any project text that still names the old ids.')
    if moved and moved.get('how'):
        remaining.insert(0, moved_message(moved))
    elif moved:
        remaining.insert(0, f"The installation state stays in {moved['from']}/ for now: {moved['waiting']}.")
    folder = summary.get('state_folder') or {}
    ignore = folder.get('ignore_file') or {}
    if ignore.get('added'):
        remaining.append(f"{ignore['path']}: the project's own file kept every line; the {folder['to']}/ rule "
                         'was added after each earlier folder\'s rule: ' + ', '.join(ignore['added']) + '.')
    converted = sorted({row['path'] for row in summary.get('instruction_sections', []) if row.get('markers')})
    if converted:
        how = '; '.join(sorted({row['markers'] for row in summary['instruction_sections'] if row.get('markers')}))
        remaining.append('The managed sections in ' + ', '.join(converted) + ' now open and close with the '
                         "release's markers (" + how + ').')
    receipt = dict(summary, applied=True, recovered=recovered, update=event, renamed=renamed,
                   executable=executable, generated_at=now(),
                   python=sys.version.split()[0], git_version=git(target, '--version'),
                   remaining=remaining)
    if finished:
        receipt['state_folder_finished'] = finished
    write_atomic(target / in_layout(target, RECEIPT), encoded(receipt))
    return receipt


def rollback_update(target, executor='starter-bootstrap', recovered=False):
    """Undo the latest completed update, from its verified backups.

    The update that moved the state folder recorded its files under the old
    folder's name, so its rollback first moves the folder back, then restores
    them; a rollback that cannot go through moves it forward again.
    """
    target = target.resolve()
    history_path = confined(target, in_layout(target, UPDATE_HISTORY))
    if not history_path.is_file():
        raise SetupError('no completed update history to roll back')
    rows = [json.loads(line) for line in history_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    rolled = {row.get('update_run_id') for row in rows if row.get('event') == 'rollback'}
    candidates = [row for row in rows if row.get('event') == 'update'
                  and row.get('status') == 'succeeded' and row.get('run_id') not in rolled]
    if not candidates:
        raise SetupError('no unrolled completed update exists')
    event = candidates[-1]
    state = load(confined(target, in_layout(target, STATE)))
    if state.get('version') != event.get('to', {}).get('version'):
        raise SetupError('active installation does not match the latest completed update')
    folder = event.get('state_folder') if isinstance(event.get('state_folder'), dict) else {}
    back = folder if folder.get('from') in STATE_DIRS and folder.get('to') in STATE_DIRS \
        and folder['from'] != folder['to'] and state_dir(target) == folder['to'] else None

    def now_at(name):
        """Where a file the update recorded under the old folder is until the folder moves back."""
        if back and name.startswith(back['from'] + '/'):
            return back['to'] + name[len(back['from']):]
        return name

    updates, expected = {}, {}
    for name, entry in event['entries'].items():
        current = file_hash(confined(target, now_at(name)))
        if current != entry['after']:
            raise SetupError('rollback would overwrite a later edit: ' + name)
        expected[name] = current
        if entry['before'] is None:
            updates[name] = None
        else:
            backup = confined(target, now_at(recorded_path(target, entry['backup'])))
            if file_hash(backup) != entry['before']:
                raise SetupError('rollback backup is unavailable or changed: ' + name)
            updates[name] = backup.read_bytes()
    rollback = {'schema_version': 1, 'event': 'rollback', 'recorded_at': now(),
                'update_run_id': event['run_id'], 'from': event['to'], 'to': event['from'],
                'execution_agent': executor}
    if back:
        rollback['state_folder'] = {'from': back['to'], 'to': back['from']}
    history_bytes, history_hash = append_event_bytes(history_path, rollback), file_hash(history_path)
    if back:
        move_state_folder(target, back['to'], back['from'])
    history_name = in_layout(target, UPDATE_HISTORY)
    updates[history_name] = history_bytes
    expected[history_name] = history_hash
    try:
        transaction(target, updates, expected)
    except BaseException:
        if back:
            move_state_folder(target, back['from'], back['to'])
        raise
    # AH-174: restored and recreated scripts are written anew, so they get their mode back.
    executable = mark_executable(target, updates)
    receipt = {'schema_version': 1, 'rolled_back': event['run_id'],
               'active_version': event['from'].get('version'), 'recorded_at': rollback['recorded_at'],
               'executable': executable,
               'remaining': ['Review and commit the rollback and update-history entry.']}
    if back:
        receipt['state_folder'] = rollback['state_folder']
        receipt['remaining'].insert(0, f"The installation state moved back from {back['to']}/ to {back['from']}/, "
                                       'where the restored release reads it; stage both folders '
                                       f"(git add -A {back['from']} {back['to']}) with the rollback.")
    if recovered:
        # AH-169: reported apart from the rollback, which undid the latest completed update.
        receipt['recovered_update'] = recovered
    write_atomic(target / in_layout(target, RECEIPT), encoded(receipt))
    return receipt


def rollback(target, executor='starter-bootstrap'):
    """Roll back the latest completed update, as SETUP-AGENT.md says.

    An update that stopped before it finished is restored first and reported
    separately in the receipt and the history (AH-169): restoring it is not the
    rollback, and a completed update whose marker merely survived is not
    restored at all.
    """
    target = target.resolve()
    try:
        with locked(target):
            if (target / in_layout(target, JOURNAL)).exists():
                recover(target)
            recovered = recover_update(target)
            try:
                return rollback_update(target, executor, recovered)
            except SetupError as exc:
                if recovered:
                    raise SetupError(f"{recovered['outcome']} (update to {recovered['to']}, recorded in "
                                     f'{in_layout(target, UPDATE_HISTORY)}); nothing more was rolled back: {exc}') from None
                raise
    finally:
        drop_leftover_lock(target)


def release_digests(manifest):
    """The archive digests of earlier releases, as this release's manifest pins them.

    The person supplies one digest through a channel they trust, the newest
    archive's. Every earlier archive the chain needs is then checked against
    the digest this manifest carries for it, so the trust runs from that one
    digest down the chain rather than from a checksum found beside a file.
    """
    rows = manifest.get('releases', [])
    if not isinstance(rows, list):
        raise SetupError('the release manifest carries an invalid releases list')
    pinned = {}
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('archive_sha256', ''))):
            raise SetupError('the release manifest carries an invalid release entry')
        version = row.get('version')
        if not isinstance(version, str) or not version or version in pinned or version == manifest.get('version'):
            raise SetupError('a release entry is missing its version, repeats one or names this release')
        pinned[version] = row['archive_sha256']
    return pinned


def chain_path(manifest, installed):
    """The nominated pairs from the installed version to this release, in order.

    Each pair is one the manifest nominates and one a rehearsal verified. The
    chain is those pairs walked one after another; it is not a new path.
    """
    newest = manifest.get('version')
    pairs = {}
    for item in manifest.get('supported_upgrades', []):
        if isinstance(item, dict):
            pairs.setdefault(item.get('from_version'), []).append(item.get('to_version'))
    path, version, seen = [], installed, set()
    while version != newest:
        if version in seen:
            raise SetupError('the nominated pairs loop at ' + str(version))
        seen.add(version)
        following = pairs.get(version, [])
        if len(following) != 1:
            raise SetupError(f'no unique nominated pair leads on from {version}, '
                             f'so no chain reaches {newest} from the installed release')
        path.append((version, following[0]))
        version = following[0]
    return path


def store_archives(store):
    """Every ZIP in the release store, by digest. Names are not trusted; bytes are."""
    store = store.resolve()
    if store.is_symlink() or not store.is_dir():
        raise SetupError('release store is not a directory')
    found = {}
    for path in sorted(store.iterdir()):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == '.zip':
            found.setdefault(digest(path.read_bytes()), path)
    return found


def unpack_release(archive, workdir):
    """Unpack one release archive into a fresh directory and return its starter root.

    The archive's one top folder is trace-os/, or an earlier release's
    (traceware/ in 0.1.0-rc19, trace-kit-starter/ up to 0.1.0-rc18), which a
    chain still applies.
    """
    with zipfile.ZipFile(archive) as zipped:
        tops = set()
        for info in zipped.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or not name.parts or name.parts[0] not in ARCHIVE_ROOTS \
                    or any(part in ('..', '') for part in name.parts):
                raise SetupError('release archive names a path outside trace-os/ (traceware/ in '
                                 '0.1.0-rc19, trace-kit-starter/ up to 0.1.0-rc18): ' + info.filename)
            tops.add(name.parts[0])
        if len(tops) != 1:
            raise SetupError('release archive must hold one top folder')
        zipped.extractall(workdir)
    root = workdir / tops.pop()
    if not (root / 'starter-manifest.json').is_file():
        raise SetupError('release archive carries no manifest')
    return root


def run_hop(root, target, profile, agent, archive, expected, executor):
    """Apply one nominated pair: this release's updater over that release's own archive.

    The archive supplies the manifest and the files, which the pair's rehearsal
    verified; the updater is this release's, as a migration runner is the
    current engine over historical steps. The rc3 and rc4 updaters read the
    previous installation state during their own postflight, so under an agent
    adapter a hop that added a skill failed its check and rolled itself back
    (rc5 corrected it); running each hop with its own updater would replay
    that on every install older than rc5.
    """
    try:
        receipt = update(root, target, profile, agent, archive, expected, executor)
    except SetupError as exc:
        return {'source': str(root), 'exit_code': 2, 'receipt': None, 'error': str(exc)}
    return {'source': str(root), 'exit_code': 0, 'receipt': receipt, 'error': None}


def update_chain(source, target, profile, agent, archive, expected_sha256, store,
                 executor='starter-bootstrap'):
    """Walk the nominated pairs from the installed release to this one.

    Every hop is the verified single update, this release's updater over
    that release's own archive, with the archive's release preflight, the
    project's postflight, a transaction and a history event of its own. The
    chain adds the walk, nothing else. It first restores an update left
    unfinished, then resolves every archive before the first hop and refuses
    to start when one is missing (AH-222); a hop that stops (a conflict, a failed check) ends the
    chain there, with the earlier hops finalized and the installed version
    saying where the project stands. Running the same command again resumes
    from that version.
    """
    if target.is_symlink():
        raise SetupError('target must not be a symlink')
    source, target = source.resolve(), target.resolve()
    check_executor(executor)
    archive = archive.resolve()
    newest_sha256 = verify_release_archive(source, archive, expected_sha256)
    manifest = load(source / 'starter-manifest.json')
    newest = manifest.get('version')

    def installed_version():
        # Read where the state is now: a hop to a release naming a newer folder moves it there.
        path = confined(target, in_layout(target, STATE))
        return load(path).get('version') if path.is_file() else None

    if not confined(target, in_layout(target, STATE)).is_file():
        raise SetupError('--update-chain requires an installed project; use --apply for a first installation')
    # AH-168: an update that stopped before it finished left its candidate's
    # version in install.json; restore it before reading where the project stands.
    with locked(target):
        if (target / in_layout(target, JOURNAL)).exists():
            recover(target)
        recovered = recover_update(target)
        finished = finish_state_move(target)
    drop_leftover_lock(target)
    installed = installed_version()
    if installed == newest:
        receipt = {'schema_version': 1, 'mode': 'update-chain', 'status': 'already current',
                   'from': installed, 'to': newest, 'hops': [], 'not_started': [],
                   'installed': installed, 'recovered': recovered, 'exit_code': 0, 'generated_at': now(),
                   'remaining': [moved_message(finished)] if finished else []}
        if finished:
            receipt['state_folder_finished'] = finished
        write_atomic(target / in_layout(target, RECEIPT), encoded(receipt))
        return receipt
    path = chain_path(manifest, installed)
    pinned = release_digests(manifest)
    archives = store_archives(store) if store is not None else {}
    plan, missing = [], []
    for from_version, to_version in path:
        if to_version == newest:
            plan.append({'from': from_version, 'to': to_version, 'archive': str(archive),
                         'archive_sha256': newest_sha256})
            continue
        sha256 = pinned.get(to_version)
        if sha256 is None:
            missing.append(f'{to_version}: this release pins no digest for it')
        elif sha256 not in archives:
            where = 'no --release-store was given' if store is None else 'no archive in the store has that digest'
            missing.append(f'{to_version}: expected sha256 {sha256}, {where}')
        else:
            plan.append({'from': from_version, 'to': to_version, 'archive': str(archives[sha256]),
                         'archive_sha256': sha256})
    if missing:
        raise SetupError('the chain cannot start, no release was applied; ' + '; '.join(missing))
    hops, status, exit_code = [], 'completed', 0
    for hop in plan:
        workdir = Path(tempfile.mkdtemp(prefix='trace-os-hop-'))
        try:
            root = unpack_release(Path(hop['archive']), workdir)
            result = run_hop(root, target, profile, agent, Path(hop['archive']),
                             hop['archive_sha256'], executor)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        installed_after = installed_version()
        record = dict(hop, **result, installed_after=installed_after)
        if result['exit_code'] != 0 or installed_after != hop['to']:
            record['status'] = 'stopped'
            hops.append(record)
            status, exit_code = 'stopped', result['exit_code'] or 2
            break
        record['status'] = 'succeeded'
        hops.append(record)
    not_started = [{'from': hop['from'], 'to': hop['to']} for hop in plan[len(hops):]]
    remaining = [f'Review and commit the update changes and {in_layout(target, UPDATE_HISTORY)}; '
                 'each hop wrote its own event there.']
    if status == 'stopped':
        remaining.insert(0, f"The chain stopped at {hops[-1]['from']} to {hops[-1]['to']}; "
                            'resolve what that hop reported, then run the same command again '
                            'to resume from the installed release.')
    if any(hop.get('receipt', {}) and hop['receipt'].get('renamed', {}).get('removed') for hop in hops):
        remaining.append('Renamed skills: the old ids are gone and the new ones installed. '
                         'Update any project text that still names the old ids.')
    # What the hop that moved the state folder said about the move, the ignore file and the markers.
    for hop in hops:
        if (hop.get('receipt') or {}).get('state_folder'):
            remaining += [line for line in hop['receipt'].get('remaining', [])
                          if line.startswith(('The installation state', IGNORE_FILE, 'The managed sections'))]
    receipt = {'schema_version': 1, 'mode': 'update-chain', 'status': status, 'updater': manifest.get('version'),
               'from': installed, 'to': newest, 'profile': profile, 'agent': agent,
               'target': str(target), 'recovered': recovered, 'hops': hops, 'not_started': not_started,
               'installed': installed_version(),
               'exit_code': exit_code, 'generated_at': now(),
               'python': sys.version.split()[0], 'remaining': remaining}
    if finished:
        receipt['state_folder_finished'] = finished
        receipt['remaining'].insert(0, moved_message(finished))
    write_atomic(target / in_layout(target, RECEIPT), encoded(receipt))
    return receipt


def sync_skills(target):
    """Install the selected agent's copies of the canonical skills, and retire removed ones.

    AH-172: a canonical skill removed from .skills/ has its unchanged generated
    copy removed with it, and a skill a release shipped is recorded as retired
    in the installation state, so a later --apply or --update does not install
    it again. A generated copy edited separately is protected: nothing is
    written and the path is named. Operating-system files and files Git ignores
    are not skill content, and an entry an earlier sync made for one is dropped
    from the state, leaving the file itself alone.
    """
    with locked(target):
        if (target / in_layout(target, JOURNAL)).exists():
            raise SetupError('unfinished transaction; run --recover before syncing skills')
        if (target / in_layout(target, UPDATE_PENDING)).exists():
            # AH-168: rewriting the state now would strand the update's recovery.
            raise SetupError('an update did not finish; run --recover before syncing skills')
        finish_state_move(target)
        state = load(confined(target, in_layout(target, STATE)))
        agent = state['agent']
        files = state['files']
        shipped = {name for name, item in files.items()
                   if name.startswith('.skills/') and item.get('policy') == 'managed' and not item.get('canonical')}
        generated = {name: item['canonical'] for name, item in files.items() if item.get('canonical')}
        names = set(skill_files(target, shipped))
        litter = skill_litter(target, sorted(set(generated.values()) - names), shipped)
        missing = (shipped | set(generated.values())) - names - litter
        updates, expected, removed = {}, {}, []
        for name, source in sorted(generated.items()):
            if source not in litter and source not in missing:
                continue
            if source in missing:
                current = file_hash(confined(target, name))
                if current not in (None, files[name].get('sha256')):
                    raise SetupError("a removed skill's generated copy was edited separately; "
                                     'merge or remove it, then sync again: ' + name)
                if current is not None:
                    updates[name], expected[name] = None, current
                    removed.append(name)
            del files[name]
        for name in missing & shipped:
            del files[name]
        retired = sorted({skill_of(name) for name in missing & shipped} - {skill_of(name) for name in names})
        if retired:
            state['retired_skills'] = sorted(set(state.get('retired_skills', [])) | set(retired))
        if agent not in AGENTS:
            if not (missing or litter):
                raise SetupError('no skill-discovery adapter selected')
        else:
            for source in sorted(names):
                name = AGENTS[agent] + '/' + source[len('.skills/'):]
                item = files.get(name, {})
                current = file_hash(confined(target, name))
                data = confined(target, source).read_bytes()
                if current not in (item.get('sha256'), digest(data)):
                    raise SetupError('generated skill was edited separately: ' + name)
                expected[name] = current
                if current != digest(data):
                    updates[name] = data
                files[name] = {'sha256': digest(data), 'policy': 'managed', 'canonical': source}
        synced = len([name for name, data in updates.items() if data is not None])
        updates[in_layout(target, STATE)] = encoded(state)
        transaction(target, updates, expected)
        prune_empty_skill_dirs(target, removed)
    drop_leftover_lock(target)
    return {'synced': synced, 'agent': agent, 'removed': removed, 'retired': retired}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=SOURCE)
    parser.add_argument('--profile', choices=['academic', 'core'], default='academic')
    parser.add_argument('--agent', choices=[*AGENTS, 'none'], default='claude')
    parser.add_argument('--execution-agent', default='starter-bootstrap')
    parser.add_argument('--release-archive', type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--release-store', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--plan', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--update', action='store_true')
    action.add_argument('--update-chain', action='store_true')
    action.add_argument('--rollback', action='store_true')
    action.add_argument('--sync-skills', action='store_true')
    action.add_argument('--recover', action='store_true')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or later is required; choose that interpreter explicitly')
    if args.target.is_symlink():
        parser.error('target must not be a symlink')
    target = args.target.resolve()
    try:
        if args.recover:
            with locked(target):
                result = {'transaction_recovered': recover(target),
                          'update_recovered': recover_update(target),
                          'state_folder_finished': finish_state_move(target)}
            drop_leftover_lock(target)
        elif args.rollback:
            result = rollback(target, args.execution_agent)
        elif args.update:
            if args.release_archive is None or args.expected_sha256 is None:
                raise SetupError('--update requires --release-archive and --expected-sha256')
            result = update(SOURCE, target, args.profile, args.agent,
                            args.release_archive, args.expected_sha256, args.execution_agent)
        elif args.update_chain:
            if args.release_archive is None or args.expected_sha256 is None:
                raise SetupError('--update-chain requires --release-archive and --expected-sha256, '
                                 'and --release-store for every hop but the last')
            result = update_chain(SOURCE, target, args.profile, args.agent, args.release_archive,
                                  args.expected_sha256, args.release_store, args.execution_agent)
            print(json.dumps(result, indent=2))
            return result['exit_code']
        elif args.sync_skills:
            result = sync_skills(target)
        elif args.apply:
            result = apply(SOURCE, target, args.profile, args.agent, args.execution_agent)
        else:
            result = plan(SOURCE, target, args.profile, args.agent)[0]
        print(json.dumps(result, indent=2))
        return 2 if result.get('conflicts') else 0
    except PermissionError as exc:
        print('setup: ' + str(exc) + '; keep the permission boundary. Use the normal permission process or the explicit-instruction --agent none route.', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, SetupError) as exc:
        print('setup: ' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        # AH-168: an update restores what it applied before this is reached.
        print('setup: interrupted; run --plan to see whether anything is left pending', file=sys.stderr)
        return 128 + getattr(exc, 'signum', signal.SIGINT)


if __name__ == '__main__':
    sys.exit(main())
