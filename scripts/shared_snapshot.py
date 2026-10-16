#!/usr/bin/env python3
"""Publish verified local input snapshots. No network or research-acceptance records.

Keep the dataset source directory and the snapshot store separate.
Create the manifest outside the source, then materialize and verify it:

  python scripts/shared_snapshot.py manifest --source /path/to/input --dataset sample-v1 --out /path/to/sample-v1.json
  python scripts/shared_snapshot.py materialize --source /path/to/input --manifest /path/to/sample-v1.json --store /path/to/snapshots
  python scripts/shared_snapshot.py verify --store /path/to/snapshots

Use the returned data directory as TRACE_OS_SHARED_ROOT for one run (TRACEWARE_SHARED_ROOT and
TRACE_KIT_SHARED_ROOT, its earlier names, are still read where TRACE_OS_SHARED_ROOT is unset).
Keep it unchanged throughout the run and verify it again before reuse.
Selection is a local atomic pointer update; there is no background synchronization.
Each completed snapshot has a manifest and verified file bytes; a failed copy leaves the previous selection intact.
The writer lock is cooperative and may remain after a forced termination; inspect the process and staging directories before removing an abandoned .snapshot.lock.
This is not a distributed lock or an operating-system security boundary.
Remote SURFdrive, Dropbox, OneDrive/SharePoint and Google Cloud adapters are not included.
A remote download can supply the input directory once it is fully available offline, but that provider has not thereby passed the snapshot contract.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata
import uuid


class SnapshotError(Exception):
    pass


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def name_ok(name):
    if not isinstance(name, str) or not name or name.startswith('/'):
        raise SnapshotError('expected a relative file path')
    for part in name.split('/'):
        if (not part or part in ('.', '..') or part[-1] in ' .'
                or unicodedata.normalize('NFC', part) != part
                or any(ord(c) < 32 or c in '<>:"\\|?*' for c in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', part.split('.')[0])):
            raise SnapshotError('nonportable file path: ' + name)
    return name


def confined(root, name):
    name_ok(name)
    path = root / name
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise SnapshotError('symlink is not permitted: ' + name)
        if part == root:
            break
    if root.resolve() not in path.resolve().parents:
        raise SnapshotError('path escapes its root')
    return path


def paths_ok(names):
    seen, full = {}, set()
    for name in names:
        name_ok(name)
        if name in full:
            raise SnapshotError('duplicate path: ' + name)
        full.add(name)
        parts = name.split('/')
        for i in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:i])
            folded = prefix.casefold()
            if folded in seen and seen[folded] != prefix:
                raise SnapshotError('case-colliding paths')
            seen[folded] = prefix
    if any('/'.join(name.split('/')[:i]) in full
           for name in full for i in range(1, len(name.split('/')))):
        raise SnapshotError('a file path is also used as a directory')


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise SnapshotError('input must be a regular directory')
    files, names = [], []
    def unreadable(error):
        raise error
    for directory, directories, leaves in os.walk(root, followlinks=False, onerror=unreadable):
        for item in directories + leaves:
            path = Path(directory) / item
            name = path.relative_to(root).as_posix()
            confined(root, name)
            names.append(name)
            if path.is_file():
                files.append(name)
            elif not path.is_dir():
                raise SnapshotError('special input file: ' + name)
    # Check directory collisions too, while allowing ancestors listed by os.walk.
    seen = {}
    for name in names:
        name_ok(name)
        if name.casefold() in seen and seen[name.casefold()] != name:
            raise SnapshotError('case-colliding input paths')
        seen[name.casefold()] = name
    paths_ok(files)
    return sorted(files)


def file_digest(path):
    h, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
            size += len(block)
    return size, h.hexdigest()


def validate(manifest):
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1 or not isinstance(manifest.get('dataset'), str)
            or not manifest['dataset'].strip() or not isinstance(manifest.get('files'), list)
            or not manifest['files']):
        raise SnapshotError('invalid or empty dataset manifest')
    names = []
    for entry in manifest['files']:
        if (not isinstance(entry, dict) or type(entry.get('size')) is not int or entry['size'] < 0
                or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise SnapshotError('invalid file size or digest')
        names.append(entry.get('path'))
    paths_ok(names)
    return manifest


def create_manifest(source, dataset='dataset'):
    source = Path(source)
    files = []
    for name in inventory(source):
        size, checksum = file_digest(confined(source, name))
        files.append({'path': name, 'size': size, 'sha256': checksum})
    return validate({'schema_version': 1, 'dataset': dataset, 'files': files})


def check_data(root, manifest):
    validate(manifest)
    if inventory(root) != sorted(e['path'] for e in manifest['files']):
        raise SnapshotError('file inventory differs from the manifest')
    for entry in manifest['files']:
        if file_digest(confined(root, entry['path'])) != (entry['size'], entry['sha256']):
            raise SnapshotError('file digest or size differs: ' + entry['path'])


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def snapshot(store, identifier):
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier):
        raise SnapshotError('invalid snapshot identifier')
    root = confined(store, 'snapshots/' + identifier)
    manifest = validate(read_json(confined(root, 'manifest.json')))
    if sha(canonical(manifest)) != identifier:
        raise SnapshotError('snapshot manifest digest differs from its identifier')
    check_data(confined(root, 'data'), manifest)
    return {'schema_version': 1, 'snapshot': identifier, 'data': str(root / 'data')}


def verify_store(store):
    store = Path(store)
    if store.is_symlink():
        raise SnapshotError('store must not be a symlink')
    store = store.resolve()
    pointer = read_json(confined(store, 'current.json'))
    if not isinstance(pointer, dict) or type(pointer.get('schema_version')) is not int or pointer['schema_version'] != 1:
        raise SnapshotError('invalid current snapshot pointer')
    return snapshot(store, pointer.get('snapshot'))


@contextmanager
def locked(store):
    lock = confined(store, '.snapshot.lock')
    token = uuid.uuid4().hex.encode()
    try:
        with lock.open('xb') as stream:
            stream.write(token)
    except FileExistsError:
        raise SnapshotError('snapshot writer lock exists; inspect the writer before removing an abandoned lock') from None
    try:
        yield
    finally:
        if lock.is_file() and not lock.is_symlink() and lock.read_bytes() == token:
            lock.unlink()


def materialize(source, manifest, store):
    source, store = Path(source), Path(store)
    if source.is_symlink() or store.is_symlink():
        raise SnapshotError('source and store must not be symlinks')
    source, store = source.resolve(), store.resolve()
    if source == store or source in store.parents or store in source.parents:
        raise SnapshotError('source and snapshot store must be separate directories')
    validate(manifest)
    identifier = sha(canonical(manifest))
    # Complete the source inventory check before any store writes.
    if inventory(source) != sorted(e['path'] for e in manifest['files']):
        raise SnapshotError('source inventory differs from the manifest')
    store.mkdir(parents=True, exist_ok=True)
    with locked(store):
        destination = confined(store, 'snapshots/' + identifier)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.staging-', dir=store))
        pointer_temp = store / ('.current-' + uuid.uuid4().hex + '.json')
        try:
            data_root = stage / 'data'
            data_root.mkdir()
            for entry in manifest['files']:
                target = confined(data_root, entry['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(confined(source, entry['path']), target)
            check_data(data_root, manifest)
            (stage / 'manifest.json').write_bytes(canonical(manifest) + b'\n')
            if destination.exists():
                snapshot(store, identifier)  # An existing object is never replaced.
            else:
                stage.rename(destination)
            result = snapshot(store, identifier)
            with pointer_temp.open('xb') as stream:
                stream.write(canonical({'schema_version': 1, 'snapshot': identifier}) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pointer_temp, confined(store, 'current.json'))
            return result
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            pointer_temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('manifest'); p.add_argument('--source', type=Path, required=True)
    p.add_argument('--dataset', required=True); p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('materialize'); p.add_argument('--source', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True); p.add_argument('--store', type=Path, required=True)
    p = sub.add_parser('verify'); p.add_argument('--store', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'manifest':
            if args.out.resolve() == args.source.resolve() or args.source.resolve() in args.out.resolve().parents:
                raise SnapshotError('write the manifest outside its source directory')
            result = create_manifest(args.source, args.dataset)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open('xb') as stream:
                stream.write(canonical(result) + b'\n')
        elif args.command == 'materialize':
            result = materialize(args.source, read_json(args.manifest), args.store)
        else:
            result = verify_store(args.store)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, SnapshotError) as exc:
        print('snapshot: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
