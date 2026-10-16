#!/usr/bin/env python3
"""Bring skill packages from Git repositories into this project, keep the untouched original, and record each step.

  python3 scripts/trace_skills.py list <repository> [--ref REF]
  python3 scripts/trace_skills.py show <repository> <path> [--ref REF]
  python3 scripts/trace_skills.py adopt <repository> <path> [--ref REF | --candidate FILE] [--yes]
  python3 scripts/trace_skills.py status [--offline]
  python3 scripts/trace_skills.py update <name> [--ref REF | --candidate FILE] [--yes]
  python3 scripts/trace_skills.py update <name> --accept-merged [--candidate FILE | --ref COMMIT] [--merged-by NAME] [--yes]
  python3 scripts/trace_skills.py update <name> --detach | --attach [--yes]
  python3 scripts/trace_skills.py retire <name> [--drop-base] [--yes]
  python3 scripts/trace_skills.py declare <name> --held-by NAME [--path PATH] [--limitation TEXT]
                                   [--beside-project-skill] [--yes]
  python3 scripts/trace_skills.py link .trace-os/skill-adoptions/<operation id>.json --commit <sha>
  python3 scripts/trace_skills.py recover

A package is a folder holding a SKILL.md in a Git repository, at one commit.
The tool reads it as data: it fetches into a bare repository in a temporary
folder outside the project and reads every file with git cat-file, so nothing
in a package is ever run, imported or checked out. list and show print what a
source holds; show also writes a candidate file naming the exact bytes it
printed. adopt prints the whole package and a plan, applies after a yes (at
the terminal, or --yes once the person has agreed), and writes, as one
transaction: the project's copy under .skills/<name>/, the untouched original
under .trace-os/skill-bases/, the agent copies, the lock, and the inventory
section of ai/SKILLS-IN-USE.md; then an intake row, and last a receipt. It
never commits or pushes. update reviews a new upstream like a first adoption
and applies it to a copy nobody adapted; for an adapted copy it prints the
three versions and writes nothing, and --accept-merged records the person's
hand merge as it stands. --detach stops offering updates and --attach offers
them again. retire removes the copy and its agent copies and keeps the
original unless --drop-base; declare records a skill a person runs from their
own setup, copying nothing. link records the default-adoption transition once
the person has committed an adoption. status reports where every skill stands
and writes nothing. recover finishes or undoes an operation that was
interrupted.

The interface, the files and the recovery rules are in
scripts/trace-skills-contract.md (contract version 1).

Exit codes: 0 done, nothing to do, or a report; 1 a person is needed (a
refusal, a stale candidate, an apply nobody confirmed); 2 a usage or
environment error; 128 plus the signal number when interrupted.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import urllib.parse

HERE = Path(__file__).resolve().parent
# The tool and bootstrap.py ship side by side; a test imports this module from
# elsewhere, so the folder is put on the path only when it is not there yet.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import bootstrap  # noqa: E402  (called through the module, so a test can patch one function)

CONTRACT_VERSION = 1
# The project's state folder, .trace-os/, or an earlier name (.traceware/ in 0.1.0-rc19, .trace-kit/ before)
# in a project no update has moved yet. use_state_folder() points the names below at the project's own when a command opens it.
STATE_FOLDER = bootstrap.STATE_DIR
STATE = bootstrap.STATE
JOURNAL = bootstrap.JOURNAL
UPDATE_PENDING = bootstrap.UPDATE_PENDING
LOCK = '.trace-os/skills.lock.json'
BASES = '.trace-os/skill-bases'
RECEIPTS = '.trace-os/skill-adoptions'
# The operation journal lives in .trace-os/receipts/, which every install's .gitignore already ignores and
# where bootstrap writes only its own named receipts (A2). write_atomic makes the folder when it is missing,
# and nothing here ever removes it: only the journal file goes.
OPERATION = '.trace-os/receipts/skill-operation.json'
INVENTORY_BEGIN = '<!-- trace-skills:begin inventory -->'
INVENTORY_END = '<!-- trace-skills:end inventory -->'
ROUTING_BEGIN = '<!-- trace-os:begin skill-routing -->'
ROUTING_END = '<!-- trace-os:end skill-routing -->'
# The kit's routing block as earlier releases marked it, newest first, until an update converts the markers:
# traceware: in 0.1.0-rc19, trace-kit: up to 0.1.0-rc18.
EARLIER_ROUTING = (('<!-- traceware:begin skill-routing -->', '<!-- traceware:end skill-routing -->'),
                   ('<!-- trace-kit:begin skill-routing -->', '<!-- trace-kit:end skill-routing -->'))
INVENTORY = 'ai/SKILLS-IN-USE.md'
INTAKE = 'sources/intake.jsonl'
TRANSITIONS = 'sources/transitions.jsonl'
TRACE_RECORD = 'scripts/trace_record.py'
# Where a personal skill can sit on this machine; .codex/skills is where the
# Codex CLI kept them when the contract was written (A13).
SHADOW_ROOTS = tuple(bootstrap.AGENTS.values()) + ('.codex/skills',)
GIT_TIMEOUT = 300
GH_TIMEOUT = 30
REGULAR = ('100644', '100755')
SPEC_KEYS = ('name', 'description', 'license', 'compatibility', 'metadata', 'allowed-tools')
WINDOWS_NAMES = ({'con', 'prn', 'aux', 'nul'} | {f'com{n}' for n in range(1, 10)}
                 | {f'lpt{n}' for n in range(1, 10)})
CONTROL_FILES = ('.gitignore', '.gitattributes', '.gitmodules')
NAME_RE = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*')
PART_RE = re.compile(r'[A-Za-z0-9._-]+')
HEX40 = re.compile(r'[0-9a-fA-F]{40}')
LIMIT = ("Limit: definition 1 reads the skill's text; it does not establish that a method is valid, "
         'that its scripts are safe, or that any agent discovers the skill.')
PARITY_LIMIT = ('Limit: file parity between .skills/ and the agent copies is checked by '
                'scripts/check_skills.py, not here.')
RECEIPT_LIMITS = ['This receipt records what the tool wrote and what it was told; it is not acceptance, '
                  'and it does not establish that the method is valid or that its scripts are safe.',
                  'The intake row names this receipt by its path; this receipt names the row by its id '
                  'and digest.']
TRANSITION_NOTE = 'not yet: link writes it after the commit'
# AH-240: link refuses every other operation's receipt (L2), so those receipts promise no row.
TRANSITION_NONE = 'not applicable: no instruction was adopted by this operation'
LINKED = ('adopt', 'update', 'accept-merged')
# AH-242: the originals are whole packages holding a SKILL.md, so an agent that searches the tree finds them;
# this note, beside them and outside every snapshot, says what they are.
BASES_NOTE = f'{BASES}/README.md'
BASES_NOTE_TEXT = (b'The folders here are the untouched originals of skills adopted with scripts/trace_skills.py, '
                   b'kept for comparison.\n'
                   b"They are not skills to follow; the project's skills are under .skills/.\n")
INVENTORY_HEADER = ("Written by the kit's skill tool (scripts/trace_skills.py in an installed project) from "
                    '.trace-os/skills.lock.json; change it with that tool, not by hand.')
# AH-237: what the skills pilot observed, and what it did not rehearse; AH-257: each personal folder's line
# states the observation of the client that reads that folder, and a folder no client was rehearsed for says so.
SHADOW_OBSERVED = {
    '.claude/skills': ("Claude Code 2.1.280 was observed to load the personal copy instead of the project's "
                       '(the skills pilot, 2026-10-01); other clients and later versions were not rehearsed'),
    '.codex/skills': ('Codex 0.154.0 was observed to list personal skills only (the skills pilot, 2026-10-01); '
                      'other clients and later versions were not rehearsed')}
SHADOW_UNREHEARSED = 'no client was rehearsed for this folder'
# AH-251: where the setup skill's template route put a skill's scripts, which status does not compare.
# AH-259: moving them breaks every command that still names the old folder, so the advice says to change those.
KEPT_OUTSIDE_NOTE = ("the project holds {folder}/, where the template route put this skill's scripts; the package "
                     'keeps its scripts under .skills/{name}/scripts/, and the two are not compared. To move them '
                     "into the package's folder, change every command in .skills/{name}/ that names {folder}/ to "
                     'the new path in the same step and run one of them before connecting; or keep them where they '
                     'are and know that status compares only .skills/{name}/')
# AH-255: the kit's files outside .skills/ that share a skill's name are found by a name match only; the kit's
# meeting example names its helper differently.
KIT_COMPANION_NAMES = {'meeting-and-idea-intake': ('meeting_quotes',)}
# AH-252: the advice N28 adds when the name is the project's own skill; AH-260: a skill holding the project's own
# rules that also relies on a same-named personal skill keeps both, with --beside-project-skill.
N28_PROJECT = ('if that skill only says to use a personal copy, remove it first and declare the personal skill; '
               'if it holds rules of its own and relies on a personal copy too, declare the personal skill with '
               '--beside-project-skill, which keeps the project\'s skill as it is')
# AH-244: the attributes besides the line-ending ones that let Git's clean step change a file's bytes.
CONVERTING = ('filter', 'ident', 'working-tree-encoding')
NOTE_ADAPTER = 'copies of the members Git does not ignore, as scripts/bootstrap.py --sync-skills makes them'
NOTE_NONE = 'agent none: no copies; agents read .skills/ directly'
NOTE_NO_STATE = 'no adapter state here'
PERMISSION_ADVICE = ('keep the permission boundary. Use the normal permission process or the '
                     'explicit-instruction --agent none route.')
RANK = {'private': 0, 'collaborators': 1, 'public': 2}
GITHUB_FORMS = [re.compile(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?'),
                re.compile(r'git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?'),
                re.compile(r'ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?')]
DEFAULT_LIMITATION = ('Not in the project: co-authors cannot read or check this method; '
                      'a method run records the digest it read.')
RETIRE_HISTORY = 'Git history keeps every file this operation removed, once the removal is committed.'
RETIRE_LIMIT = 'A file that was never committed is not kept anywhere.'

# Every refusal and error, worded as the contract's catalogue words it (6.0).
CATALOGUE = {
    'E1': ("{project} is not the top of a Git working tree; run this from the project's root, or pass --project", 2),
    'E2': ('Git is not installed or not on PATH', 2),
    'E3': ('cannot read {state}/skills.lock.json ({reason}); restore it from Git: '
           'git checkout HEAD -- {state}/skills.lock.json', 2),
    'E4': ('{state}/skills.lock.json has schema_version {version}; this tool reads version 1', 2),
    'E5': ('cannot read {state}/install.json ({reason})', 2),
    'E6': ('another setup process owns this target; wait for it to finish, then run this again', 2),
    'E7': ('an earlier operation ({operation_id}) did not finish; run {tool} recover first', 1),
    'E8': ('an unfinished setup transaction is pending ({state}/transaction.json); run {tool} recover first', 1),
    'E9': ('a kit update did not finish; run python3 scripts/bootstrap.py --recover first', 1),
    'E10': ('the temporary folder {dir} is inside the project; set TMPDIR to a folder outside it', 2),
    'E11': ('scripts/trace_record.py is not in this project, so the {row} row cannot be recorded; '
            'nothing was written', 2),
    'E12': ('execution agent must be one identifier', 2),
    'E13': ('writing failed ({error}); everything this operation wrote was undone', 2),
    'E14': ('{path} changed after the plan was shown; nothing was written. Run the command again for a new plan', 1),
    'E15': ('{path} changed while this operation was writing; everything else it wrote was undone, and {path} '
            'keeps the later edit', 1),
    'E16': ('interrupted; run {tool} recover to finish or undo {operation_id}', 130),
    'E17': ('the files were written, but {step} failed ({error}); run {tool} recover to finish {operation_id}', 2),
    'E18': ("Git ignores {path} in this project, so it would not reach the project's history; "
            'change that ignore rule first', 1),
    'E19': ('Git would rewrite {path} when it is committed (a line-ending rule or a filter in .gitattributes, '
            'or core.autocrlf), so its recorded digest would not survive a clone; exclude {folder} from '
            'conversion in .gitattributes, for example "{folder}/** -text", then run this again', 1),
    'E20': ('symlink is not supported: {path}', 2),
    'E21': ('--merged-by applies to update --accept-merged only', 2),
    'I1': ('the inventory markers in ai/SKILLS-IN-USE.md are not one begin and one end, in that order, '
           "outside the kit's skill-routing block; fix them by hand, then run this again", 1),
    'S1': ('the repository address carries a user name and password, a token, a query or a fragment; '
           "give it without them and let Git's credential helper supply access", 2),
    'S2': ('a repository address cannot begin with "-"', 2),
    'S3': ('the source has no branch or tag named {ref}', 2),
    'S4': ('{ref} names both a branch and a tag in the source; pass refs/heads/{ref} or refs/tags/{ref}', 2),
    'S5': ('could not fetch {repository}: {error}', 2),
    'S6': ('the source does not serve commit {commit}', 2),
    'S7': ('there is no package folder {path} at commit {c12}', 2),
    'S8': ('the package path must be a relative path inside the repository, with no empty, "." or ".." '
           'part: {path}', 2),
    'P': ('the package at {path} is refused: {reason}', 1),
    'N2': ('{name} is adopted from this source at {c12}; use update {name} to move it', 1),
    'N3': ('{name} is adopted from another source ({repository} {path}); retire it first: {tool} retire {name}', 1),
    'N4': ('{name} is declared as held outside the project by {held_by}; retire the declaration first: '
           '{tool} retire {name}', 1),
    'N5': ("{name} is a skill the kit ships; adopting it would leave two copies under one name. Pass "
           "--replace-kit-copy to remove the kit's copy and keep the kit's updater from installing it again, "
           'or choose another package', 1),
    'N6': ("the kit's copy of {name} was edited here ({path}); --replace-kit-copy removes only an unedited kit "
           'copy. Move the edit to a file of your own, then run this again', 1),
    'N7': ('{name} already exists in this project (.skills/{name}/), written here or of unknown origin; pass '
           '--keep-existing to keep it and record this package as its upstream original, or move it first', 1),
    'N8': ('a single-file skill .skills/{name}.md has this name; turn it into .skills/{name}/SKILL.md or '
           'remove it first', 1),
    'N9': ('--replace-kit-copy: {name} is not a skill the kit ships here; nothing to replace', 1),
    'N10': ('--keep-existing: there is no .skills/{name}/ here to keep', 1),
    'N11': ('--replace-kit-copy and --keep-existing exclude each other', 2),
    'N12': ('no skill named {name} is adopted here', 1),
    'N13': ("{name} is a skill the kit ships; the kit's updater updates it (SETUP-AGENT.md, Update)", 1),
    'N13r': ('{name} is a skill the kit ships; retire it as SETUP-AGENT.md describes: delete .skills/{name}/ and '
             'run python3 scripts/bootstrap.py --sync-skills', 1),
    'N14': ("{name} is the project's own skill; this tool updates and retires only skills it adopted or declared", 1),
    'N15': ('{name} is declared, not in the project; there is nothing to update', 1),
    'N16': ('{name} is retired; adopt it again to bring it back', 1),
    'N17': ('{name} is detached: updates are not offered; status reports whether its source moved', 1),
    'N18': ('the package at {path} is now named {x}; a renamed package is another skill: retire {name}, '
            'then adopt {x}', 1),
    'N19': ('{name} is not a valid skill name (1 to 64 lowercase letters, digits and single hyphens)', 2),
    'N20': ('the skill at {path} is named {x}, not {name}', 1),
    'N21': ('--path {path} is not a folder holding a SKILL.md', 2),
    'N22': ('retiring {name} would delete changes Git has not recorded ({paths}); commit them first, so that '
            'history keeps them', 1),
    'N23': ('a generated copy of {name} was edited separately ({path}); merge or remove it, then run this again', 1),
    'N24': ('the upstream original under {snapshot} was edited ({path}); it is the record of what was adopted. '
            'Restore it with git checkout HEAD -- {path}, then run this again', 1),
    'N25': ('{name} is pinned to commit {c12}; name the ref to update to with --ref', 2),
    'N26': ('the copy .skills/{name}/ is not here; retire {name}, or adopt it again', 1),
    'N27': ('{name} is a {manager} entry; {operation} applies to adopted skills only', 1),
    'N28': ('{name} is already a skill of this project ({owner}); declare records only a skill that is not in '
            'the project', 1),
    'N29': ('--beside-project-skill needs a skill of the project named {name} (.skills/{name}/ or .skills/{name}.md); '
            'there is none, so declare it without the flag', 2),
    'C1': ('Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, '
           'run: {command} --candidate {file} --yes', 1),
    # C1 for a command that fetches nothing (detach, attach, retire, declare), which has no candidate to name.
    'C1n': ('Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, '
            'run: {command} --yes', 1),
    'C2': ('Nothing was applied: not confirmed.', 1),
    'C3': ('--yes applies what the person saw: pass the --candidate file that show, adopt or update wrote, '
           'or --ref with a full commit id', 1),
    'C4': ('the candidate is for {repository} {path} at {c12}, which does not match this command '
           '({command_repository} {command_path}, ref {ref})', 1),
    'C5': ("the package's bytes changed since they were shown ({member}); run show again and review it", 1),
    'C6': ('{file} is not a trace-skills candidate file', 2),
    'A1': ('the source ({source}) is not known to be public, and the project is {project}: copying it here '
           'lets everyone who can read the project read it. If the person wants that, pass '
           '--share-with-project, with their words in --words', 1),
    'A2': ('--audience takes source=<value> or project=<value>, where the value is private, collaborators '
           'or public', 2),
    'L1': ('{receipt} is not a receipt this tool wrote', 2),
    'L2': ('{receipt} records a {operation}, which adopts no instruction; link records adoptions, updates and '
           'accepted merges', 1),
    'L3': ('not a commit in this repository: {commit}', 2),
    'L4': ('commit {c12} does not contain {receipt} as it is here; name the commit that recorded this operation', 1),
    'L5': ('commit {c12} holds no .skills/{name}/SKILL.md', 1),
    'L6': ('{receipt} is already linked to {commits} by {tr}; a transition row is not amended', 1),
    'R1': ("the pending setup transaction is not this tool's (it names {path}); "
           'run python3 scripts/bootstrap.py --recover', 1),
    'R2': ('recovery would overwrite a later edit: {path}', 1),
    'R3': ('the operation journal {state}/receipts/skill-operation.json cannot be read ({reason}); inspect it and '
           '{state}/backups/ before removing it', 2),
}

# The reasons a package is refused, printed after "is refused: " (P1 to P14).
REASONS = {
    'P1': '{member} is a symbolic link',
    'P1b': '{member} is not a regular file',
    'P2': '{member} is a submodule',
    'P3': '{member} uses characters outside A-Z, a-z, 0-9, ".", "_", "-" and "/"',
    'P4': '{member} and {other} differ only in letter case',
    'P5': "{member} is a Git control file, which would change how this project's Git treats files",
    'P6': '{member} uses a name Windows reserves',
    'P7': 'it has no SKILL.md',
    'P8': 'SKILL.md is not UTF-8 text',
    'P9': 'SKILL.md has no front matter between --- lines',
    'P10': 'SKILL.md front matter line {line} is not readable',
    'P11': 'SKILL.md front matter key {key} appears twice',
    'P12': 'SKILL.md names no skill',
    'P13': 'its name {name} is not 1 to 64 lowercase letters, digits and single hyphens',
    'P14': 'its name {name} differs from its folder {folder}',
}


class Stop(Exception):
    """End the command with one of the catalogue's messages and its exit code."""

    def __init__(self, ident, advice=None, **fields):
        template, code = CATALOGUE[ident]
        text = template.format(**dict({'state': STATE_FOLDER}, **fields))
        super().__init__(text + ('; ' + advice if advice else ''))
        self.ident, self.code, self.fields = ident, code, fields
        # C1 and C2 answer the confirmation exchange, so they go to stdout without the prefix.
        self.stdout = ident in ('C1', 'C1n', 'C2')


class Done(Exception):
    """Nothing to do: print the line on stdout and exit 0."""


class Refused(Exception):
    """A package refusal (P1 to P14), carrying its id and its reason as printed."""

    def __init__(self, ident, **fields):
        super().__init__(REASONS[ident].format(**fields))
        self.ident, self.reason, self.fields = ident, str(self), fields


class FrontMatterError(Exception):
    """SKILL.md's front matter cannot be read: `reason` is no front matter, not readable or duplicate key <k>."""

    def __init__(self, line, reason):
        super().__init__(reason if line is None else f'line {line}: {reason}')
        self.line, self.reason = line, reason


# Conventions (section 0) ---------------------------------------------------

def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def on_windows():
    """True where the file system keeps no execute bit, so modes come from a manifest (2.1); a test seam."""
    return os.name == 'nt'


def stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def quoted(parts):
    return ' '.join(shlex.quote(str(part)) for part in parts)


def first_line(data):
    text = data.decode('utf-8', 'replace') if isinstance(data, bytes) else str(data)
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[0] if lines else 'Git gave no message'


def inside(project, path):
    path = Path(path).resolve()
    return path == project or project in path.parents


def escape(text):
    """Package text as a terminal may print it: control and bidirectional characters made visible (6.2)."""
    out = []
    for char in text:
        code = ord(char)
        if char == '\r':
            out.append('\\r')
        elif char in '\t\n':
            out.append(char)
        elif code < 0x20 or code == 0x7f or 0x80 <= code <= 0x9f:
            out.append(f'\\x{code:02x}')
        elif 0x202a <= code <= 0x202e or 0x2066 <= code <= 0x2069:
            out.append(f'\\u{code:04x}')
        else:
            out.append(char)
    return ''.join(out)


def printable(data):
    """The member's text, escaped, or None when it is not UTF-8 text or holds a NUL."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return None if '\0' in text else escape(text)


def file_hash(project, name):
    return bootstrap.file_hash(bootstrap.confined(project, name))


def file_mode(project, name):
    """100755 when the owner-execute bit is set, 100644 otherwise, None when the path holds no file."""
    path = bootstrap.confined(project, name)
    if not path.is_file():
        return None
    return '100755' if path.stat().st_mode & stat.S_IXUSR else '100644'


# The package digest (4.6) --------------------------------------------------

def package_digest(files):
    """SHA-256 over the sorted lines `<mode> <size> <sha256> <path>`, one per member."""
    lines = ''.join(f"{item['mode']} {item['size']} {item['sha256']} {item['path']}\n"
                    for item in sorted(files, key=lambda item: item['path']))
    return hashlib.sha256(lines.encode('ascii')).hexdigest()


def manifest(members):
    return [{key: item[key] for key in ('path', 'mode', 'size', 'sha256')} for item in members]


def member(path, mode, data):
    return {'path': path, 'mode': mode, 'size': len(data), 'sha256': sha256(data), 'data': data}


# The SKILL.md front matter reader (2.3) ------------------------------------

TOP_LINE = re.compile(r'([A-Za-z0-9_-]+):(.*)')
MAP_LINE = re.compile(r'( +)([A-Za-z0-9_.-]+):(.*)')
BLOCK_MARKS = ('|', '|-', '|+', '>', '>-', '>+')


def unquote(rest):
    """The inside of a quoted value, or None when the quote does not close on its line."""
    if len(rest) < 2 or rest[-1] != rest[0]:
        return None
    inner = rest[1:-1]
    if rest[0] == "'":
        return inner.replace("''", "'")
    out, index = [], 0
    while index < len(inner):
        if inner[index] == '\\' and index + 1 < len(inner) and inner[index + 1] in '"\\':
            out.append(inner[index + 1])
            index += 2
        else:
            out.append(inner[index])
            index += 1
    return ''.join(out)


def read_front_matter(text):
    """The front matter of a SKILL.md as a dict of strings (and one dict for a mapping value).

    It reads the small subset every kit skill uses, with no YAML library, so a
    plain value may hold ": " as the kit's descriptions do.
    """
    if text.startswith('\ufeff'):
        text = text[1:]
    lines = [line[:-1] if line.endswith('\r') else line for line in text.split('\n')]
    if lines[0].rstrip(' \t') != '---':
        raise FrontMatterError(None, 'no front matter')
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip(' \t') == '---'), None)
    if end is None:
        raise FrontMatterError(None, 'no front matter')

    def blank(line):
        return line.strip() == ''

    def comment(line):
        return line.lstrip(' ').startswith('#')

    def tabbed(index):
        line = lines[index]
        if '\t' in line[:len(line) - len(line.lstrip(' \t'))]:
            raise FrontMatterError(index + 1, 'not readable')

    def indented_after(index):
        """The indented lines that continue a key, skipping blank and comment lines, and where they stop."""
        found = []
        while index < end:
            line = lines[index]
            if blank(line) or (comment(line) and line.startswith(' ')):
                index += 1
                continue
            tabbed(index)
            if not line.startswith(' '):
                break
            found.append(index)
            index += 1
        return found, index

    result, index = {}, 1
    while index < end:
        line = lines[index]
        if blank(line) or comment(line):
            index += 1
            continue
        tabbed(index)
        match = TOP_LINE.fullmatch(line)
        if line.startswith(' ') or not match:
            raise FrontMatterError(index + 1, 'not readable')
        key, rest = match.group(1), match.group(2).strip(' ')
        if key in result:
            raise FrontMatterError(index + 1, 'duplicate key ' + key)
        index += 1
        if rest in BLOCK_MARKS:
            block = []
            while index < end and (blank(lines[index]) or lines[index].startswith(' ')):
                if not blank(lines[index]):
                    tabbed(index)
                block.append(lines[index])
                index += 1
            first = next((line for line in block if not blank(line)), '')
            width = len(first) - len(first.lstrip(' '))
            body = [line[width:] if line[:width].strip() == '' else line.lstrip(' ') for line in block]
            if rest.startswith('|'):
                value = '\n'.join(body).strip()
            else:
                value = ' '.join(line for line in body if line.strip()).strip()
            result[key] = value
        elif rest == '':
            found, index = indented_after(index)
            first = MAP_LINE.fullmatch(lines[found[0]]) if found else None
            if first:
                width, mapping = len(first.group(1)), {}
                for position in found:
                    row = MAP_LINE.fullmatch(lines[position])
                    if not row or len(row.group(1)) != width:
                        raise FrontMatterError(position + 1, 'not readable')
                    value = row.group(3).strip(' ')
                    if value[:1] in ('"', "'"):
                        value = unquote(value)
                        if value is None:
                            raise FrontMatterError(position + 1, 'not readable')
                    mapping[row.group(2)] = value
                result[key] = mapping
            else:
                result[key] = ' '.join(lines[position].strip() for position in found)
        elif rest[0] in ('"', "'"):
            value = unquote(rest)
            if value is None:
                raise FrontMatterError(index, 'not readable')
            result[key] = value
        else:
            found, index = indented_after(index)
            result[key] = ' '.join([rest] + [lines[position].strip() for position in found])
    return result


# Definition 1 (2.5) --------------------------------------------------------

_HANDOFF = []


def handoff_module():
    """check_skill_handoff from the tool's folder, or, in the kit's source tree, from the repository's scripts/ (A19)."""
    if _HANDOFF:
        return _HANDOFF[0]
    folders = [HERE]
    if HERE.parent.name == 'files' and HERE.parents[1].name == 'starter':
        folders.append(HERE.parents[2] / 'scripts')
    module = None
    for folder in folders:
        path = folder / 'check_skill_handoff.py'
        if path.is_file():
            try:
                spec = importlib.util.spec_from_file_location('check_skill_handoff', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                break
            except Exception:  # an unreadable checker is reported as "not checked", never fatal
                module = None
    _HANDOFF.append(module)
    return module


def front_reason(error):
    if error.reason == 'no front matter':
        return 'no front matter between --- lines'
    if error.reason == 'not readable':
        return f'front matter line {error.line} is not readable'
    return f"front matter key {error.reason[len('duplicate key '):]} appears twice"


def definition(members, folder):
    """The three reported states of definition 1, and the front matter notes; nothing here refuses."""
    paths = {item['path']: item for item in members}
    reasons, notes, text, front = [], [], None, None
    if 'SKILL.md' not in paths:
        reasons.append('no SKILL.md')
    else:
        try:
            text = paths['SKILL.md']['data'].decode('utf-8')
        except UnicodeDecodeError:
            reasons.append('SKILL.md is not UTF-8 text')
    if text is not None:
        try:
            front = read_front_matter(text)
        except FrontMatterError as exc:
            reasons.append(front_reason(exc))
    if front is not None:
        name = front.get('name')
        if name in (None, ''):
            reasons.append('no name')
        else:
            if not isinstance(name, str) or not NAME_RE.fullmatch(name) or len(name) > 64:
                reasons.append(f'name {name} is not 1 to 64 lowercase letters, digits and single hyphens')
            if name != folder:
                reasons.append(f'name {name} differs from the folder {folder}')
        description = front.get('description')
        if not isinstance(description, str) or description == '':
            reasons.append('no description')
        elif len(description) > 1024:
            reasons.append('description is longer than 1024 characters')
        if 'compatibility' in front and not (isinstance(front['compatibility'], str)
                                             and 1 <= len(front['compatibility']) <= 500):
            reasons.append('compatibility is not 1 to 500 characters')
        if 'metadata' in front and not isinstance(front['metadata'], dict):
            reasons.append('metadata is not a mapping of strings')
        if 'license' in front and not (isinstance(front['license'], str) and front['license']):
            reasons.append('license is empty')
        extra = [key for key in front if key not in SPEC_KEYS]
        if extra:
            notes.append('front matter keys not in the specification: ' + ', '.join(extra))
    licence_stated = ('LICENSE.txt' in paths or 'LICENSE' in paths
                      or (front is not None and isinstance(front.get('license'), str) and front.get('license')))
    module = handoff_module()
    if module is None:
        handoff = 'handoff not checked: scripts/check_skill_handoff.py is not here'
    else:
        absent = module.absent_signals(text or '', folder)
        handoff = module.STATED if not absent else f'{module.NOT_STATED}: ' + ', '.join(absent)
    states = {'version': 1,
              'format': 'format valid' if not reasons else 'format invalid: ' + '; '.join(reasons),
              'licence': 'licence stated' if licence_stated else 'licence not stated',
              'handoff': handoff}
    return states, notes, front


def definition_line(states):
    return f"definition 1: {states['format']}; {states['licence']}; {states['handoff']}"


# The package (section 2) ---------------------------------------------------

class Package:
    """One package's members, read as data, with what definition 1 reports about them."""

    def __init__(self, path, name, members, litter):
        self.path, self.name = path, name
        self.members = sorted(members, key=lambda item: item['path'])
        self.litter = sorted(litter)
        self.states, self.notes, self.front = definition(self.members, name)

    @property
    def files(self):
        return manifest(self.members)

    @property
    def tree_sha256(self):
        return package_digest(self.files)

    @property
    def size(self):
        return sum(item['size'] for item in self.members)


def is_litter(path):
    final = PurePosixPath(path).name
    return final in bootstrap.OS_FILES or final.startswith('._')


def screen(entries):
    """Steps 1 to 7 of 2.2 over a package's entries, each over every entry in path order; returns members and litter."""
    entries = sorted(entries, key=lambda entry: entry['path'])
    litter = [entry['path'] for entry in entries if is_litter(entry['path'])]
    kept = [entry for entry in entries if not is_litter(entry['path'])]
    for entry in kept:
        if entry['mode'] == '120000':
            raise Refused('P1', member=entry['path'])
        if entry['mode'] == '160000':
            raise Refused('P2', member=entry['path'])
        if entry['mode'] not in REGULAR:
            raise Refused('P1b', member=entry['path'])
    for entry in kept:
        if any(part in ('.', '..') or not PART_RE.fullmatch(part) for part in entry['path'].split('/')):
            raise Refused('P3', member=entry['path'])
    seen = {}
    for entry in kept:
        folded = entry['path'].casefold()
        if folded in seen:
            raise Refused('P4', member=seen[folded], other=entry['path'])
        seen[folded] = entry['path']
    for entry in kept:
        parts = entry['path'].split('/')
        if parts[-1].casefold() in CONTROL_FILES or any(part.casefold() == '.git' for part in parts):
            raise Refused('P5', member=entry['path'])
    for entry in kept:
        if any(part.split('.')[0].casefold() in WINDOWS_NAMES or part.endswith('.')
               for part in entry['path'].split('/')):
            raise Refused('P6', member=entry['path'])
    if 'SKILL.md' not in {entry['path'] for entry in kept}:
        raise Refused('P7')
    return [member(entry['path'], entry['mode'], entry['data']) for entry in kept], litter


def adoptable(path, name, entries):
    """The package, once 2.2 and 2.4 pass; Refused names the first reason otherwise."""
    members, litter = screen(entries)
    text = next(item['data'] for item in members if item['path'] == 'SKILL.md')
    try:
        text = text.decode('utf-8')
    except UnicodeDecodeError:
        raise Refused('P8') from None
    try:
        front = read_front_matter(text)
    except FrontMatterError as exc:
        if exc.reason == 'no front matter':
            raise Refused('P9') from None
        if exc.reason == 'not readable':
            raise Refused('P10', line=exc.line) from None
        raise Refused('P11', key=exc.reason[len('duplicate key '):]) from None
    named = front.get('name')
    if named in (None, ''):
        raise Refused('P12')
    if not isinstance(named, str) or not NAME_RE.fullmatch(named) or len(named) > 64:
        raise Refused('P13', name=named)
    if named != name:
        raise Refused('P14', name=named, folder=name)
    return Package(path, name, members, litter)


def disk_entries(root, recorded=None):
    """Every entry under a folder on disk; a symlink reads as 120000 and any other special file as 'other'.

    Symlinked folders are not entered, and a special file is never opened, so
    a FIFO cannot block the read. On Windows, which keeps no execute bit, the
    mode is the one the latest receipt recorded (2.1).
    """
    entries = []
    for folder, folders, names in os.walk(root, followlinks=False):
        base = Path(folder)
        for name in sorted(folders):
            if (base / name).is_symlink():
                entries.append({'path': (base / name).relative_to(root).as_posix(), 'mode': '120000', 'data': None})
        folders[:] = [name for name in folders if not (base / name).is_symlink()]
        for name in names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                entries.append({'path': relative, 'mode': '120000', 'data': None})
            elif not stat.S_ISREG(info.st_mode):
                entries.append({'path': relative, 'mode': 'other', 'data': None})
            else:
                if on_windows():
                    mode = (recorded or {}).get(relative, '100644')
                else:
                    mode = '100755' if info.st_mode & stat.S_IXUSR else '100644'
                entries.append({'path': relative, 'mode': mode, 'data': path.read_bytes()})
    return sorted(entries, key=lambda entry: entry['path'])


def copy_entries(project, folder, recorded=None):
    """A folder in the project as package entries, leaving out the files Git ignores there."""
    root = project / folder
    if not root.is_dir():
        return []
    entries = disk_entries(root, recorded)
    regular = [folder + '/' + entry['path'] for entry in entries
               if entry['mode'] in REGULAR and not is_litter(entry['path'])]
    ignored = bootstrap.git_ignored(project, regular)
    return [entry for entry in entries if folder + '/' + entry['path'] not in ignored]


def disk_members(project, folder, recorded=None):
    """The regular, non-litter files Git does not ignore under a folder: the copy as the digest sees it."""
    return [member(entry['path'], entry['mode'], entry['data'])
            for entry in copy_entries(project, folder, recorded)
            if entry['mode'] in REGULAR and not is_litter(entry['path'])]


# Fetching (6.0) --------------------------------------------------------------

def scp_like(address):
    head = address.split('/', 1)[0]
    return ':' in head and len(head.split(':', 1)[0]) > 1


def check_address(address):
    """The address after S1 and S2; a local path is made absolute."""
    if address.startswith('-'):
        raise Stop('S2')
    if '://' in address:
        parts = urllib.parse.urlsplit(address)
        if parts.password is not None or parts.query or parts.fragment or '?' in address or '#' in address:
            raise Stop('S1')
        if parts.scheme.lower() not in ('ssh', 'git+ssh', 'ssh+git') and parts.username is not None:
            raise Stop('S1')
        return address
    if scp_like(address):
        # git@host:path is how SSH is written; a colon before that @ would carry a password.
        head = address.split('/', 1)[0]
        if '?' in address or '#' in address or ('@' in head and ':' in head.split('@', 1)[0]):
            raise Stop('S1')
        return address
    return Path(address).expanduser().resolve().as_posix()


def is_local(address):
    return address.startswith('file://') or ('://' not in address and not scp_like(address))


def source_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    if 'GIT_SSH_COMMAND' not in env and 'GIT_SSH' not in env:
        env['GIT_SSH_COMMAND'] = 'ssh -o BatchMode=yes'
    return env


def source_git(args, cwd=None, data=None):
    """A Git command on the source: no prompt, no ext protocol, a timeout; never a credential copied anywhere."""
    try:
        return subprocess.run(['git', '-c', 'protocol.ext.allow=never', *args], cwd=cwd, env=source_env(),
                              capture_output=True, input=data, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, b'', f'timed out after {GIT_TIMEOUT} seconds'.encode())


def remote_refs(repository, *args, pattern=None):
    run = source_git(['ls-remote', *args, repository] + ([pattern] if pattern else []))
    if run.returncode:
        raise Stop('S5', repository=repository, error=first_line(run.stderr))
    refs = {}
    for line in run.stdout.decode('utf-8', 'replace').splitlines():
        if line.startswith('ref:') or '\t' not in line:
            continue
        oid, name = line.split('\t', 1)
        refs[name] = oid
    return refs


def resolve(repository, ref):
    """The requested ref and the commit it names now (step 3 of Fetching)."""
    if ref is None:
        refs = remote_refs(repository, '--symref', pattern='HEAD')
        # A bare source made without -b names a branch that does not exist, and lists no HEAD line.
        if 'HEAD' not in refs:
            raise Stop('S3', ref='HEAD')
        return 'HEAD', refs['HEAD']
    if HEX40.fullmatch(ref):
        return ref, ref.lower()
    refs = remote_refs(repository)
    if ref.startswith('refs/'):
        found = refs.get(ref + '^{}') or refs.get(ref)
        if not found:
            raise Stop('S3', ref=ref)
        return ref, found
    branch = refs.get('refs/heads/' + ref)
    tag = refs.get('refs/tags/' + ref + '^{}') or refs.get('refs/tags/' + ref)
    if branch and tag:
        raise Stop('S4', ref=ref)
    if not (branch or tag):
        raise Stop('S3', ref=ref)
    return ref, branch or tag


def new_temp(project):
    folder = Path(tempfile.mkdtemp(prefix='trace-skills-')).resolve()
    if inside(project, folder):
        shutil.rmtree(folder, ignore_errors=True)
        raise Stop('E10', dir=folder.as_posix())
    return folder


def fetch_commit(tmp, repository, commit):
    """A bare repository in the temporary folder holding the commit, or S5 or S6."""
    bare = tmp / 'source.git'
    run = source_git(['init', '--bare', '-q', str(bare)])
    if run.returncode:
        raise Stop('S5', repository=repository, error=first_line(run.stderr))
    run = source_git(['-C', str(bare), 'fetch', '-q', '--no-tags', '--depth=1', repository, commit])
    if run.returncode:
        run = source_git(['-C', str(bare), 'fetch', '-q', '--no-tags', repository,
                          '+refs/heads/*:refs/remotes/source/heads/*', '+refs/tags/*:refs/remotes/source/tags/*'])
        if run.returncode:
            raise Stop('S5', repository=repository, error=first_line(run.stderr))
    if source_git(['-C', str(bare), 'cat-file', '-e', commit + '^{commit}']).returncode:
        raise Stop('S6', commit=commit)
    return bare


def tree_entries(bare, commit, path=None):
    """Every entry of the commit's tree (under `path` when given) as path, mode, kind and object id."""
    args = ['-C', str(bare), 'ls-tree', '-r', '-z', '--full-tree', commit]
    if path is not None:
        args += ['--', path]
    run = source_git(args)
    if run.returncode:
        raise Stop('S6', commit=commit)
    entries = []
    for record in run.stdout.split(b'\0'):
        if not record:
            continue
        meta, name = record.split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split(' ')
        entries.append({'path': name.decode('utf-8', 'surrogateescape'), 'mode': mode, 'kind': kind, 'oid': oid})
    return entries


def read_blobs(bare, oids):
    """The bytes of each blob, read with git cat-file --batch: raw, with no filter or attribute applied."""
    oids = sorted(set(oids))
    if not oids:
        return {}
    run = source_git(['-C', str(bare), 'cat-file', '--batch'], data=('\n'.join(oids) + '\n').encode())
    if run.returncode:
        raise Stop('S5', repository=str(bare), error=first_line(run.stderr))
    out, position, blobs = run.stdout, 0, {}
    for oid in oids:
        end = out.index(b'\n', position)
        header = out[position:end].decode('ascii').split(' ')
        size = int(header[2])
        blobs[oid] = out[end + 1:end + 1 + size]
        position = end + 1 + size + 1
    return blobs


def package_entries(bare, entries, folder):
    """The entries under a package folder, relative to it, with each regular file's bytes."""
    prefix = folder + '/'
    chosen = [entry for entry in entries if entry['path'].startswith(prefix)]
    blobs = read_blobs(bare, [entry['oid'] for entry in chosen
                              if entry['mode'] in REGULAR and not is_litter(entry['path'])])
    return [{'path': entry['path'][len(prefix):], 'mode': entry['mode'], 'data': blobs.get(entry['oid'])}
            for entry in chosen]


def check_package_path(path):
    try:
        bootstrap.relative(path)
    except bootstrap.SetupError:
        raise Stop('S8', path=path) from None
    return path


class Fetched:
    """A package read from its source, with where its bytes are staged and whether this run made that folder."""

    def __init__(self, repository, path, requested_ref, commit, package, staged, owned, candidate=None):
        self.repository, self.path, self.requested_ref, self.commit = repository, path, requested_ref, commit
        self.package, self.staged, self.owned, self.candidate = package, staged, owned, candidate
        self.candidate_file = None

    @property
    def staging(self):
        """The folder removed once an operation completes: the trace-skills- folder, or the staged bytes alone."""
        parent = self.staged.parent
        if self.staged.name == 'package' and parent.name.startswith('trace-skills-'):
            return parent
        return self.staged

    def source(self):
        return {'repository': self.repository, 'path': self.path, 'requested_ref': self.requested_ref,
                'commit': self.commit}

    def discard(self):
        """Remove a temporary folder this run made and nothing will apply."""
        if self.owned:
            shutil.rmtree(self.staging, ignore_errors=True)


def stage(folder, package):
    """Write the members under <tmp>/package, outside the project, with the executable bit for 100755."""
    root = folder / 'package'
    for item in package.members:
        path = root / item['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item['data'])
        if item['mode'] == '100755' and not on_windows():
            os.chmod(path, 0o755)
    return root


def fetch_package(project, repository, path, ref):
    """Fetch, read and validate one package, and stage its bytes (S1 to S8, then P)."""
    repository = check_address(repository)
    check_package_path(path)
    tmp = new_temp(project)
    try:
        requested, commit = resolve(repository, ref)
        bare = fetch_commit(tmp, repository, commit)
        entries = tree_entries(bare, commit, path)
        if not any(entry['path'].startswith(path + '/') for entry in entries):
            raise Stop('S7', path=path, c12=commit[:12])
        name = PurePosixPath(path).name
        try:
            package = adoptable(path, name, package_entries(bare, entries, path))
        except Refused as exc:
            raise Stop('P', path=path, reason=exc.reason) from None
        staged = stage(tmp, package)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return Fetched(repository, path, requested, commit, package, staged, owned=True)


def staged_mismatch(files, tree_sha256, entries):
    """The first entry whose path, mode or SHA-256 differs from the manifest, or None (C5).

    Operating-system litter is not a member, so a .DS_Store a file manager
    dropped into the staged folder is no difference; anything else is.
    """
    entries = [entry for entry in entries if not is_litter(entry['path'])]
    want = {(item['path'], item['mode'], item['sha256']) for item in files}
    have = {(entry['path'], entry['mode'], sha256(entry['data']) if entry['data'] is not None else '')
            for entry in entries}
    differing = sorted({item[0] for item in want ^ have})
    if differing:
        return differing[0]
    if package_digest([member(entry['path'], entry['mode'], entry['data']) for entry in entries]) != tree_sha256:
        return 'the package digest'
    return None


def read_candidate(file):
    try:
        data = json.loads(Path(file).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        raise Stop('C6', file=file) from None
    try:
        valid = (isinstance(data, dict) and data.get('schema_version') == 1
                 and data.get('kind') == 'trace-skills-candidate'
                 and all(isinstance(data['source'][key], str) for key in ('repository', 'path', 'requested_ref', 'commit'))
                 and isinstance(data['package']['files'], list) and isinstance(data['package']['tree_sha256'], str)
                 and isinstance(data['staged'], str))
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise Stop('C6', file=file)
    return data


def candidate_package(project, file, repository, path, ref):
    """The package a candidate names, re-hashed from its staged bytes, or fetched again when they are gone (5.2)."""
    data = read_candidate(file)
    source = data['source']
    command_repository = check_address(repository)
    check_package_path(path)
    if source['repository'] != command_repository or source['path'] != path or \
            (ref is not None and ref not in (source['requested_ref'], source['commit'])):
        raise Stop('C4', repository=source['repository'], path=source['path'], c12=source['commit'][:12],
                   command_repository=command_repository, command_path=path, ref=ref or 'HEAD')
    files, tree = data['package']['files'], data['package']['tree_sha256']
    staged = Path(data['staged'])
    if inside(project, staged):
        raise Stop('E10', dir=staged.resolve().as_posix())
    owned = False
    if staged.is_dir():
        # AH-234: on Windows the staged files carry no execute bit, so their modes are the manifest's.
        entries = disk_entries(staged, manifest_modes(files))
    else:
        # The operating system may have cleaned the folder show kept; fetch the same commit again.
        tmp = new_temp(project)
        try:
            bare = fetch_commit(tmp, source['repository'], source['commit'])
            tree_list = tree_entries(bare, source['commit'], path)
            if not any(entry['path'].startswith(path + '/') for entry in tree_list):
                raise Stop('S7', path=path, c12=source['commit'][:12])
            entries = package_entries(bare, tree_list, path)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        staged, owned = tmp / 'package', True
    found = staged_mismatch(files, tree, entries)
    if found is not None:
        if owned:
            shutil.rmtree(staged.parent, ignore_errors=True)
        raise Stop('C5', member=found)
    try:
        package = adoptable(path, PurePosixPath(path).name, entries)
    except Refused as exc:
        if owned:
            shutil.rmtree(staged.parent, ignore_errors=True)
        raise Stop('P', path=path, reason=exc.reason) from None
    package.litter = sorted(set(package.litter) | set(data['package'].get('litter') or []))
    if owned:
        stage(staged.parent, package)
    fetched = Fetched(source['repository'], path, source['requested_ref'], source['commit'], package,
                      staged, owned=owned, candidate=data)
    fetched.candidate_file = Path(file).resolve().as_posix() if not owned else None
    return fetched


def manifest_modes(files):
    """Each member's mode as a manifest records it, for reading staged bytes where no execute bit is kept (AH-234).

    disk_entries uses it only on Windows; on POSIX the file system's own
    execute bit is read, so a changed mode there is still a difference.
    """
    return {item.get('path'): item.get('mode') for item in files if isinstance(item, dict)}


def verify_staged(fetched):
    """C5 under the lock: the staged bytes hashed again against the manifest the person was shown."""
    if not fetched.staged.is_dir():
        raise Stop('C5', member='the staged folder')
    files = fetched.package.files
    found = staged_mismatch(files, fetched.package.tree_sha256, disk_entries(fetched.staged, manifest_modes(files)))
    if found is not None:
        raise Stop('C5', member=found)


def write_candidate(fetched, created_by, operation, from_commit=None):
    """The candidate file (5.2) beside the staged bytes; returns its path."""
    package = fetched.package
    data = {'schema_version': 1, 'kind': 'trace-skills-candidate', 'contract': CONTRACT_VERSION,
            'created_at': stamp(utc_now()), 'created_by': created_by,
            'for': {'operation': operation, 'name': package.name, 'from_commit': from_commit},
            'source': fetched.source(),
            'package': {'name': package.name, 'tree_sha256': package.tree_sha256, 'files': package.files,
                        'litter': package.litter, 'definition': package.states,
                        'front_matter_notes': package.notes},
            'staged': fetched.staged.as_posix()}
    path = fetched.staged.parent / 'candidate.json'
    bootstrap.write_atomic(path, bootstrap.encoded(data))
    return path.as_posix()


# Audiences (section 8) -----------------------------------------------------

def observe_audience(address):
    """What GitHub says about who can read the address, with the date, or why it was not checked."""
    if is_local(address):
        return 'not checked: a local repository'
    for form in GITHUB_FORMS:
        match = form.fullmatch(address)
        if match:
            break
    else:
        return 'not checked: not a GitHub repository'
    if shutil.which('gh') is None:
        return 'not checked: gh is not installed'
    try:
        run = subprocess.run(['gh', 'repo', 'view', f'{match.group(1)}/{match.group(2)}', '--json', 'visibility',
                              '--jq', '.visibility'], capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return 'not checked: gh could not see the repository'
    word, day = run.stdout.strip(), utc_now().strftime('%Y-%m-%d')
    if run.returncode == 0 and word == 'PUBLIC':
        return f'public (gh, {day})'
    if run.returncode == 0 and word in ('PRIVATE', 'INTERNAL'):
        return f'restricted (gh, {day})'
    return 'not checked: gh could not see the repository'


def parse_audiences(values):
    found = {}
    for value in values or []:
        match = re.fullmatch(r'(source|project)=(private|collaborators|public)', value)
        if not match:
            raise Stop('A2')
        found[match.group(1)] = match.group(2)
    return found


def project_origin_audience(project):
    run = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=project, capture_output=True, text=True)
    address = run.stdout.strip() if run.returncode == 0 else ''
    return observe_audience(address) if address else 'not checked: the project has no origin remote'


def ranks(side):
    if side['declared'] in RANK:
        return {RANK[side['declared']]}
    if side['observed'].startswith('public'):
        return {2}
    if side['observed'].startswith('restricted'):
        return {0, 1}
    return {0, 1, 2}


def compare_audiences(source, project):
    """The comparison as printed and recorded, and whether --share-with-project is required (A6)."""
    mine, theirs = ranks(source), ranks(project)
    if mine == {2}:
        return 'source public; the copy widens nothing', False
    if max(mine) < min(theirs):
        return 'source narrower than the project', True
    return 'source not known to be public: copying it here lets everyone who can read the project read it', True


def audience_form(side):
    return f"declared {side['declared']}" if side['declared'] in RANK else side['observed']


def audience_cell(entry, shared_by):
    """The inventory's audience cell for an adopted entry (3.2)."""
    side = entry['source']['audience']
    if side['declared'] == 'public':
        cell = 'source public'
    elif side['declared'] == 'collaborators':
        cell = 'source shared with named collaborators'
    elif side['declared'] == 'private':
        cell = f"source private to {side['declared_by']}; updates come through them"
    elif side['observed'].startswith('public'):
        cell = 'source public (observed)'
    elif side['observed'].startswith('restricted'):
        cell = 'source not public (observed; who can read it is not known)'
    else:
        cell = 'source audience not known'
    if shared_by is not None:
        cell += f'; copied here with --share-with-project by {shared_by}'
    return cell


# The lock, the install state and the inventory (sections 3 and 4) ----------

def empty_lock():
    return {'schema_version': 1, 'skills': {}}


def check_entry(name, entry):
    """The field that has the wrong type in a lock entry, or None."""
    def kind(value, *types):
        return isinstance(value, types)

    manager = entry.get('manager')
    if manager == 'external':
        checks = [('source', kind(entry.get('source'), dict)), ('upstream', kind(entry.get('upstream'), dict)),
                  ('adopted', kind(entry.get('adopted'), dict)), ('detached', kind(entry.get('detached'), bool)),
                  ('receipts', kind(entry.get('receipts'), list))]
        if all(ok for _, ok in checks):
            source, upstream, adopted = entry['source'], entry['upstream'], entry['adopted']
            checks += [(f'source.{key}', kind(source.get(key), str)) for key in ('repository', 'path', 'requested_ref', 'commit')]
            checks += [('source.audience', kind(source.get('audience'), dict))]
            checks += [(f'upstream.{key}', kind(upstream.get(key), str)) for key in ('tree_sha256', 'tracked_sha256', 'snapshot')]
            checks += [(f'adopted.{key}', kind(adopted.get(key), str)) for key in ('path', 'tree_sha256', 'receipt')]
            checks += [('adopted.kept_existing', kind(adopted.get('kept_existing', False), bool))]
    elif manager == 'outside':
        checks = [('held_by', kind(entry.get('held_by'), str)), ('digest', kind(entry.get('digest'), dict)),
                  ('limitation', kind(entry.get('limitation'), str)), ('receipt', kind(entry.get('receipt'), str)),
                  ('receipts', kind(entry.get('receipts'), list)),
                  ('path', entry.get('path') is None or kind(entry.get('path'), str)),
                  ('beside_project', kind(entry.get('beside_project', False), bool))]
    elif manager == 'retired':
        checks = [('was', entry.get('was') in ('external', 'outside')),
                  ('source', entry.get('source') is None or kind(entry.get('source'), dict)),
                  ('upstream', entry.get('upstream') is None or kind(entry.get('upstream'), dict)),
                  ('receipt', kind(entry.get('receipt'), str)), ('receipts', kind(entry.get('receipts'), list))]
    else:
        return f'the entry {name} has an unknown manager {manager!r}'
    for field, ok in checks:
        if not ok:
            return f'the entry {name} has a field of the wrong type: {field}'
    if not all(isinstance(item, str) for item in entry['receipts']):
        return f'the entry {name} has a field of the wrong type: receipts'
    return None


def read_lock(project):
    """The lock, or an empty one when there is none (4.7)."""
    path = project / LOCK
    if not path.exists():
        return empty_lock()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stop('E3', reason='not valid JSON' if isinstance(exc, ValueError) else str(exc)) from None
    if not isinstance(data, dict):
        raise Stop('E3', reason='not a JSON object')
    if data.get('schema_version') != 1:
        raise Stop('E4', version=data.get('schema_version'))
    if not isinstance(data.get('skills', {}), dict):
        raise Stop('E3', reason='skills is not an object')
    data.setdefault('skills', {})
    for name, entry in data['skills'].items():
        if not isinstance(entry, dict):
            raise Stop('E3', reason=f'the entry {name} is not an object')
        problem = check_entry(name, entry)
        if problem:
            raise Stop('E3', reason=problem)
    if 'project_audience' in data and not isinstance(data['project_audience'], dict):
        raise Stop('E3', reason='project_audience is not an object')
    return data


def use_state_folder(project):
    """Point the state names at this project's folder: .trace-os/, or an earlier name (STATE_DIRS).

    A project installed by an earlier release keeps its state in .traceware/
    (0.1.0-rc19) or .trace-kit/ (up to 0.1.0-rc18) until the update to this
    release moves it, and a project with none of them gets .trace-os/. One command works in one project, so the names are set once,
    when the command opens it.
    """
    global STATE_FOLDER, STATE, JOURNAL, UPDATE_PENDING, LOCK, BASES, RECEIPTS, OPERATION, BASES_NOTE
    global INVENTORY_HEADER
    STATE_FOLDER = bootstrap.state_dir(project)
    STATE, JOURNAL, UPDATE_PENDING = (bootstrap.in_layout(project, name) for name in (
        bootstrap.STATE, bootstrap.JOURNAL, bootstrap.UPDATE_PENDING))
    LOCK, BASES, RECEIPTS = (f'{STATE_FOLDER}/{name}' for name in ('skills.lock.json', 'skill-bases',
                                                                    'skill-adoptions'))
    OPERATION = f'{STATE_FOLDER}/receipts/skill-operation.json'
    BASES_NOTE = f'{BASES}/README.md'
    INVENTORY_HEADER = ("Written by the kit's skill tool (scripts/trace_skills.py in an installed project) from "
                        f'{LOCK}; change it with that tool, not by hand.')


def read_state(project):
    """The install state, or None in a project with none (7.2)."""
    path = project / STATE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stop('E5', reason='not valid JSON' if isinstance(exc, ValueError) else str(exc)) from None
    if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
        raise Stop('E5', reason='not an installation state with a files table')
    return data


def cell(text):
    return str(text).replace('|', '\\|').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')


def stored_path(project, path):
    """A state path the lock or a receipt recorded, where it is now (AH-281).

    A lock written before an update that moved the state folder names its
    originals and receipts under the earlier folder (.trace-kit/ or
    .traceware/); the update renames the folder and leaves the lock as it was,
    so such a path is read, compared and removed under the folder that holds
    it now, however many moves ago it was written. A path naming no state folder is returned as it is.
    """
    return bootstrap.recorded_path(project, path) if isinstance(path, str) else path


def receipt_data(project, path):
    # A lock written before the state folder moved names its receipts under an earlier folder; read them where they are.
    try:
        return json.loads((project / stored_path(project, path)).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def shared_by(project, entry, current):
    """Who passed --share-with-project for this entry's adoption, read from its latest receipt."""
    receipt = current.get(entry['adopted']['receipt']) or receipt_data(project, entry['adopted']['receipt'])
    try:
        if receipt['audience']['share_with_project']['given']:
            return receipt['decided']['by']
    except (KeyError, TypeError):
        pass
    return None


def inventory_section(project, lock, current=None):
    """The inventory's bytes, generated from the lock as it will stand (3.2)."""
    current = current or {}
    skills = lock['skills']
    adopted = sorted(name for name, entry in skills.items() if entry['manager'] == 'external')
    outside = sorted(name for name, entry in skills.items() if entry['manager'] == 'outside')
    retired = sorted(name for name, entry in skills.items() if entry['manager'] == 'retired')
    lines = [INVENTORY_BEGIN, '## Skills from outside this project', '', INVENTORY_HEADER, '',
             '### Adopted from a source', '']
    if adopted:
        lines += ['| Skill id | Source | Commit | Audience | Detached | Receipt |',
                  '| --- | --- | --- | --- | --- | --- |']
        for name in adopted:
            entry = skills[name]
            source = entry['source']
            lines.append(f"| {cell(name)} | {cell(source['repository'])} `{cell(source['path'])}` | "
                         f"{source['commit'][:12]} | {cell(audience_cell(entry, shared_by(project, entry, current)))} | "
                         f"{'yes' if entry.get('detached') else 'no'} | `{cell(entry['adopted']['receipt'])}` |")
    else:
        lines.append('None.')
    lines += ['', '### Relied on, not in the project', '']
    if outside:
        lines += ['| Skill id | Held by | Digest | Limitation |', '| --- | --- | --- | --- |']
        for name in outside:
            entry = skills[name]
            tree = (entry.get('digest') or {}).get('tree_sha256')
            digest = f'{tree[:12]} (attested)' if tree else 'not stated'
            label = name + (f" (relied on by the project's own .skills/{name})" if entry.get('beside_project') else '')
            lines.append(f"| {cell(label)} | {cell(entry['held_by'])} | {digest} | {cell(entry['limitation'])} |")
    else:
        lines.append('None.')
    lines += ['', '### Retired', '']
    if retired:
        lines += ['| Skill id | Was | Source or holder | Original kept | Receipt |',
                  '| --- | --- | --- | --- | --- |']
        for name in retired:
            entry = skills[name]
            if entry['was'] == 'external':
                source, upstream = entry.get('source') or {}, entry.get('upstream') or {}
                where = f"{cell(source.get('repository'))} `{cell(source.get('path'))}` at {str(source.get('commit'))[:12]}"
                if upstream.get('dropped'):
                    kept = 'no (dropped; Git history keeps it)'
                else:
                    kept = f"`{BASES}/{str(upstream.get('tree_sha256'))[:12]}...`"
                was = 'adopted'
            else:
                where, kept, was = f"held by {cell(entry.get('held_by'))}", 'not applicable', 'declared'
            lines.append(f"| {cell(name)} | {was} | {where} | {kept} | `{cell(entry['receipt'])}` |")
    else:
        lines.append('None.')
    lines.append(INVENTORY_END)
    return ('\n'.join(lines) + '\n').encode('utf-8')


def place_inventory(existing, section):
    """ai/SKILLS-IN-USE.md with the inventory section put in its place, or I1 (3.2)."""
    if not existing:
        return section
    begin, end = INVENTORY_BEGIN.encode(), INVENTORY_END.encode()
    if existing.count(begin) == 0 and existing.count(end) == 0:
        return (existing if existing.endswith(b'\n') else existing + b'\n') + b'\n' + section
    if existing.count(begin) != 1 or existing.count(end) != 1 or existing.index(end) < existing.index(begin):
        raise Stop('I1')
    first = existing.index(begin)
    last = existing.index(end) + len(end)
    if existing[last:last + 1] == b'\n':
        last += 1
    routing_begin, routing_end = ROUTING_BEGIN.encode(), ROUTING_END.encode()
    for earlier in EARLIER_ROUTING:
        if existing.count(routing_begin) or existing.count(routing_end):
            break
        routing_begin, routing_end = (marker.encode() for marker in earlier)
    if existing.count(routing_begin) == 1 and existing.count(routing_end) == 1:
        start = existing.index(routing_begin)
        stop = existing.index(routing_end) + len(routing_end)
        if first < stop and start < last:
            raise Stop('I1')
    return existing[:first] + section + existing[last:]


# Ownership and names (section 7) -----------------------------------------

def kit_files(state, name):
    """The install-state entries a release wrote for this skill: managed, with no canonical key."""
    if state is None:
        return []
    prefix = f'.skills/{name}/'
    return sorted(path for path, item in state['files'].items()
                  if path.startswith(prefix) and item.get('policy') == 'managed' and not item.get('canonical'))


def kit_companions(state, name):
    """The kit's managed files outside .skills/ whose path holds the skill's name; a name match only (AH-255)."""
    if state is None:
        return []
    names = (name,) + KIT_COMPANION_NAMES.get(name, ())
    return sorted(path for path, item in state['files'].items()
                  if not path.startswith('.skills/') and item.get('policy') == 'managed'
                  and not item.get('canonical') and any(part in path for part in names))


def project_files(project, name):
    """The files under .skills/<name>/ that are not litter and that Git does not ignore."""
    folder = project / '.skills' / name
    if not folder.is_dir():
        return []
    names = sorted(path.relative_to(project).as_posix() for path in folder.rglob('*') if path.is_file())
    litter = bootstrap.skill_litter(project, names)
    return [path for path in names if path not in litter]


def claims(project, state, lock, name):
    """Everything that claims a name: kit, external, outside, the project's folder, a single file."""
    found = []
    entry = lock['skills'].get(name)
    if kit_files(state, name):
        found.append('kit')
    if entry and entry['manager'] in ('external', 'outside'):
        found.append(entry['manager'])
    if project_files(project, name) and not any(claim in ('kit', 'external') for claim in found):
        found.append('project')
    if (project / '.skills' / f'{name}.md').is_file():
        found.append('single')
    # AH-260: a declaration made --beside-project-skill relies on a personal skill alongside the project's own.
    if (entry and entry['manager'] == 'outside' and entry.get('beside_project')
            and found in (['outside', 'project'], ['outside', 'single'])):
        return ['beside', found[1]]
    return found


CLAIM_WORDS = {'kit': 'the kit ships it', 'external': 'an adopted entry in the lock',
               'outside': 'a declared entry in the lock', 'project': 'a project folder .skills/{name}/',
               'single': 'a single file .skills/{name}.md'}


# The context of one run ------------------------------------------------------

class Context:
    """One command's arguments, its project and the `<tool>` command its messages name."""

    def __init__(self, args, argv):
        self.args, self.argv = args, list(argv)
        self.project, self.tool = None, None
        self.pending = None  # the operation id whose journal is on disk, for E16

    def open(self):
        """The project, after E2 and E1; sets <tool> for every message."""
        if shutil.which('git') is None:
            raise Stop('E2')
        given = self.args.project
        raw = Path(given) if given is not None else HERE.parent
        shown = given if given is not None else raw.as_posix()
        if raw.is_symlink() or not raw.is_dir():
            raise Stop('E1', project=shown)
        project = raw.resolve()
        top = bootstrap.git(project, 'rev-parse', '--show-toplevel', check=False)
        if not top or Path(top).resolve() != project:
            raise Stop('E1', project=shown)
        script = Path(__file__).resolve()
        try:
            path = script.relative_to(project).as_posix()
        except ValueError:
            path = script.as_posix()
        self.project = project
        self.tool = quoted(['python3', path] + (['--project', given] if given is not None else []))
        use_state_folder(project)
        return project

    @property
    def by(self):
        if getattr(self.args, 'by', None):
            return self.args.by
        return bootstrap.git(self.project, 'config', 'user.name', check=False) or 'not stated'

    def decided(self, confirmed):
        args = self.args
        return {'by': self.by, 'words': args.words if args.words else 'not stated',
                'authority': args.authority if args.authority else 'nothing stated',
                'execution_agent': args.execution_agent, 'confirmed': confirmed}

    def same_command(self, drop_with_value=('--candidate', '--project'), drop_alone=('--yes',)):
        """This command again, for C1: the arguments as given, without the ones C1 adds back."""
        rest, skip = [], False
        for token in self.argv:
            if skip:
                skip = False
                continue
            if token in drop_alone:
                continue
            if token in drop_with_value:
                skip = True
                continue
            if any(token.startswith(flag + '=') for flag in drop_with_value):
                continue
            rest.append(token)
        return self.tool + ' ' + quoted(rest)


def check_pending(ctx):
    """E7, E8 and E9: no journal of this tool's, no setup transaction, no unfinished kit update."""
    project = ctx.project
    if (project / OPERATION).exists():
        journal = receipt_data(project, OPERATION)
        operation_id = journal.get('operation_id') if isinstance(journal, dict) else None
        raise Stop('E7', operation_id=operation_id or OPERATION, tool=ctx.tool)
    if (project / JOURNAL).exists():
        raise Stop('E8', tool=ctx.tool)
    if (project / UPDATE_PENDING).exists():
        raise Stop('E9')


def check_writing_flags(ctx):
    args = ctx.args
    if args.execution_agent is not None:
        try:
            bootstrap.check_executor(args.execution_agent)
        except bootstrap.SetupError:
            raise Stop('E12') from None


def free_operation_id(project, lock, moment, name, operation):
    """`<stamp>-<name>-<operation>`, with -2, -3 and so on until no receipt, lock entry or journal names it."""
    base = f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{name}-{operation}"
    named = {stored_path(project, path) for entry in lock['skills'].values() for path in entry.get('receipts', [])}
    journal = receipt_data(project, OPERATION) if (project / OPERATION).exists() else None
    if isinstance(journal, dict) and journal.get('operation_id'):
        named.add(f"{RECEIPTS}/{journal['operation_id']}.json")
    candidate, count = base, 1
    while (project / RECEIPTS / f'{candidate}.json').exists() or f'{RECEIPTS}/{candidate}.json' in named:
        count += 1
        candidate = f'{base}-{count}'
    return candidate


# Planning the writes (section 9 and 12.3) ------------------------------------

# 'bases' is the originals folder's README.md (AH-242), written before the snapshot it sits beside.
GROUPS = ('copy', 'bases', 'snapshot', 'agents', 'removed', 'state', 'lock', 'inventory')


class Writes:
    """The files one operation writes, in the transaction's order, with the hashes and modes read at the plan."""

    def __init__(self, project):
        self.project = project
        self.groups = {group: {} for group in GROUPS}
        self.observed, self.targets, self.left = {}, {}, {}

    def put(self, group, path, data, mode=None):
        """Plan bytes (or None for a removal) for a path; a path that already holds them is left out (12.1)."""
        current = file_hash(self.project, path)
        self.observed[path] = current
        if data is None:
            if current is not None:
                self.groups[group][path] = None
            self.targets.pop(path, None)
            self.left.pop(path, None)
            return
        self.left[path] = data
        if current != sha256(data):
            self.groups[group][path] = data
        if mode is not None:
            self.targets[path] = mode

    def keep(self, path, mode):
        """A file the operation leaves as it is, whose mode it still records."""
        self.observed[path] = file_hash(self.project, path)
        self.targets[path] = mode

    @property
    def updates(self):
        ordered = {}
        for group in GROUPS:
            for path in sorted(self.groups[group]):
                ordered[path] = self.groups[group][path]
        return ordered

    @property
    def expected(self):
        return {path: self.observed[path] for path in self.updates}

    def modes(self):
        """modes_after, modes_before and the receipt's modes (12.2, AH-230, AH-233).

        A path the transaction writes is a new file written by write_atomic,
        which carries no execute bit whatever the old file had, so every
        written path whose target is 100755 is set again by the forward step.
        """
        after, before = {}, {}
        written = self.updates
        for path, target in sorted(self.targets.items()):
            now = file_mode(self.project, path)
            if (target == '100755' and path in written) or (now is not None and now != target):
                after[path] = target
                if now is not None:
                    before[path] = now
        for path in self.updates:
            if file_mode(self.project, path) == '100755':
                before[path] = '100755'
        left = {path: '100755' for path, target in self.targets.items() if target == '100755'}
        return after, before, dict(sorted(left.items()))


def agent_copies(project, state, name, canonical, removed, writes):
    """Plan the generated copies of one skill as bootstrap.sync_skills makes them (section 9, A1).

    `canonical` maps each .skills/<name>/ path the operation leaves (no litter,
    nothing Git ignores) to its bytes and mode; `removed` lists the canonical
    paths it removes. Returns the receipt's agent_copies and the first edited
    copy (N23), which is reported after the ignore checks. `written` lists the
    copies this operation writes, and `kept` those that already hold their
    bytes, whose mode the forward step may still set (AH-241).
    """
    if state is None:
        return {'agent': None, 'written': [], 'kept': [], 'removed': [], 'note': NOTE_NO_STATE}, None
    agent = state.get('agent')
    if agent not in bootstrap.AGENTS:
        return {'agent': agent, 'written': [], 'kept': [], 'removed': [], 'note': NOTE_NONE}, None
    root, files, edited = bootstrap.AGENTS[agent], state['files'], None
    written, kept, dropped, left = [], [], [], set()
    for path, (data, mode) in sorted(canonical.items()):
        copy = root + '/' + path[len('.skills/'):]
        current, new = file_hash(project, copy), sha256(data)
        if current not in (None, files.get(copy, {}).get('sha256'), new) and edited is None:
            edited = copy
        writes.put('agents', copy, data, mode)
        files[copy] = {'sha256': new, 'policy': 'managed', 'canonical': path}
        left.add(copy)
        (written if copy in writes.groups['agents'] else kept).append(copy)
    for path in sorted(removed):
        for copy, item in sorted(files.items()):
            if item.get('canonical') == path and copy not in left:
                current = file_hash(project, copy)
                if current not in (None, item.get('sha256')) and edited is None:
                    edited = copy
                writes.put('agents', copy, None)
                del files[copy]
                dropped.append(copy)
    return {'agent': agent, 'written': written, 'kept': kept, 'removed': dropped, 'note': NOTE_ADAPTER}, edited


def put_bases_note(project, writes):
    """AH-242: the originals folder's README.md, planned by an operation writing an original, when it is not there."""
    if not bootstrap.confined(project, BASES_NOTE).is_file():
        writes.put('bases', BASES_NOTE, BASES_NOTE_TEXT)


def bases_note_line(writes):
    """The plan's line for the README.md, or none when the operation does not write it."""
    return [f'    {BASES_NOTE} (says the originals there are not skills to follow)'] \
        if BASES_NOTE in writes.updates else []


def conversion_changes(project, path, data):
    """True when Git would store other bytes than these for the path (E19)."""
    clean = subprocess.run(['git', 'hash-object', f'--path={path}', '--stdin'], cwd=project,
                           capture_output=True, input=data)
    raw = subprocess.run(['git', 'hash-object', '--no-filters', '--stdin'], cwd=project,
                         capture_output=True, input=data)
    return clean.returncode == 0 and raw.returncode == 0 and clean.stdout.strip() != raw.stdout.strip()


def converting_attributes(project, paths):
    """The paths with a filter, ident or working-tree-encoding attribute, read with one git check-attr (AH-244).

    None when Git could not say, so that every path is then checked one by one.
    """
    if not paths:
        return set()
    run = subprocess.run(['git', 'check-attr', '-z', '--stdin', *CONVERTING], cwd=project, capture_output=True,
                         input=b''.join(path.encode('utf-8') + b'\0' for path in paths))
    if run.returncode:
        return None
    fields = run.stdout.split(b'\0')
    found = set()
    for index in range(0, len(fields) - 2, 3):
        if fields[index + 2] not in (b'unspecified', b'unset'):
            found.add(fields[index].decode('utf-8', 'surrogateescape'))
    return found


def converted(project, items):
    """The first path, in the order given, whose bytes Git would store otherwise when committed (E19), or None.

    Git's clean step changes a file's bytes only through a filter, ident or
    working-tree-encoding attribute, or by turning CRLF into LF. So a file
    whose bytes hold no CRLF, at a path with none of those attributes, is
    stored as it is, and only the others are hashed both ways, as
    conversion_changes does (AH-244). Before, every file was hashed both ways.
    """
    items = [(path, data) for path, data in items if data is not None]
    attributes = converting_attributes(project, sorted({path for path, _ in items}))
    for path, data in items:
        if attributes is not None and path not in attributes and b'\r\n' not in data:
            continue
        if conversion_changes(project, path, data):
            return path
    return None


def conversion_folder(path, name):
    for prefix in [f'.skills/{name}'] + [f'{root}/{name}' for root in bootstrap.AGENTS.values()]:
        if path.startswith(prefix + '/'):
            return prefix
    if path.startswith(BASES + '/'):
        return BASES
    return PurePosixPath(path).parent.as_posix()


def gate_writes(project, name, writes, must_track, recorded, deferred):
    """E18, E19, then the deferred N23 and I1, in the order of step 8; returns the paths Git ignores."""
    paths = sorted(set(writes.left) | set(must_track) | set(recorded))
    ignored = bootstrap.git_ignored(project, paths)
    for path in must_track:
        if path in ignored:
            raise Stop('E18', path=path)
    path = converted(project, list(recorded.items()) + list(writes.updates.items()))
    if path is not None:
        raise Stop('E19', path=path, folder=conversion_folder(path, name))
    for stop in deferred:
        if stop is not None:
            raise stop
    return sorted(path for path in ignored if path in writes.left or path in must_track)


# Records (section 10) --------------------------------------------------------

def run_trace_record(project, args):
    """Run the project's own trace_record.py with these arguments; its output is captured, never shown."""
    run = subprocess.run([sys.executable, TRACE_RECORD, *args], cwd=project, capture_output=True,
                         text=True, encoding='utf-8', errors='replace')
    return run.returncode, run.stdout, run.stderr


def registry_row(project, registry, field, value):
    """The first row of a registry whose field equals the value, with the bytes of its line."""
    path = project / registry
    if not path.is_file():
        return None, None
    for line in path.read_bytes().split(b'\n'):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and row.get(field) == value:
            return row, line
    return None, None


def intake_args(name, source, tree_sha256, by, checked, used_for, receipt_path, authority):
    """The arguments of the intake row (10.1), in their fixed order."""
    return ['--what', f"skill package {name} ({source['repository']} {source['path']}) at commit "
                      f"{source['commit']}, package digest {tree_sha256}",
            '--origin', f"{source['repository']} {source['path']} at commit {source['commit']}, "
                        f"requested as {source['requested_ref']}",
            '--by', by,
            '--checked', checked,
            '--used-for', used_for or f'a standing instruction for the agents in this project, at .skills/{name}/',
            '--path', receipt_path,
            '--authority', authority]


def checked_text(opening, states, comparison):
    """The intake row's --checked text for adopt and update: the opening clause says what was printed and confirmed.

    The opening is the caller's (AH-231): adopt and update each say what this
    run and its predecessor did, and nothing more. accept-merged has a text
    of its own (10.1), built by merged_checked.
    """
    return (f"{opening}; every file's SHA-256 and the package digest were re-checked just before writing; "
            f"{definition_line(states)}; audience: {comparison}")


def record_intake(project, journal):
    """Step 3 of 12.4: the intake row, once; a row already naming the receipt is taken as it is."""
    receipt_path = journal['receipt_path']
    row, line = registry_row(project, INTAKE, 'object', receipt_path)
    if row is None:
        code, out, err = run_trace_record(project, ['intake', *journal['intake']['args']])
        if code != 0:
            raise RuntimeError(f'trace_record.py exited {code}: {first_line(err or out)}')
        row, line = registry_row(project, INTAKE, 'object', receipt_path)
        if row is None:
            raise RuntimeError(f'no row in {INTAKE} names {receipt_path}')
    return {'id': row.get('id'), 'registry': INTAKE, 'row_sha256': sha256(line)}


# Applying one operation (12.3 to 12.5) ---------------------------------------

class Plan:
    """One operation, computed in full before anything is written."""

    def __init__(self, operation, name, op_id, moment):
        self.operation, self.name, self.op_id, self.moment = operation, name, op_id, moment
        self.writes = None
        self.lines = []
        self.receipt = None
        self.intake = None
        self.staging = None
        self.fetched = None
        self.not_tracked = []
        self.observed = None  # the audiences observed at the plan, reused under the lock

    @property
    def receipt_path(self):
        return f'{RECEIPTS}/{self.op_id}.json'


def prune(project, removed):
    """Step 2 of 12.4: the empty folders removals leave, in .skills/, the agent roots and the snapshots."""
    bootstrap.prune_empty_skill_dirs(project, [path for path in removed if not path.startswith(BASES + '/')])
    for path in removed:
        if not path.startswith(BASES + '/'):
            continue
        parent = PurePosixPath(path).parent
        while str(parent) != BASES and str(parent).startswith(BASES + '/'):
            folder = project / str(parent)
            if not folder.is_dir() or any(folder.iterdir()):
                break
            folder.rmdir()
            parent = parent.parent


def set_modes(project, modes):
    if on_windows():
        return
    for path, mode in sorted(modes.items()):
        target = bootstrap.confined(project, path)
        if target.is_file():
            os.chmod(target, 0o755 if mode == '100755' else 0o644)


def remove_journal(project):
    (project / OPERATION).unlink(missing_ok=True)


def forward(ctx, journal, recovered):
    """The forward steps of 12.4, run after the transaction and again by recover; each one is idempotent."""
    project, op_id, step = ctx.project, journal['operation_id'], 'setting the modes'
    receipt_path = journal['receipt_path']
    try:
        set_modes(project, journal['modes_after'])
        step = 'pruning empty folders'
        prune(project, journal['prune'])
        intake = None
        if journal['intake'] is not None:
            step = 'the intake row'
            intake = record_intake(project, journal)
        step = 'the receipt'
        existing = receipt_data(project, receipt_path)
        if not (isinstance(existing, dict) and existing.get('operation_id') == op_id):
            receipt = dict(journal['receipt'], completed_at=stamp(utc_now()), recovered=recovered)
            receipt['records'] = {'intake': intake, 'transition': TRANSITION_NOTE if journal['operation'] in LINKED
                                  else TRANSITION_NONE}
            bootstrap.write_atomic(bootstrap.confined(project, receipt_path), bootstrap.encoded(receipt))
        step = 'removing the staging folder'
        if journal.get('staging') and Path(journal['staging']).exists():
            shutil.rmtree(journal['staging'])
        step = 'removing the operation journal'
        remove_journal(project)
    except Exception as exc:
        raise Stop('E17', step=step, error=str(exc), tool=ctx.tool, operation_id=op_id) from None
    ctx.pending = None


def stale(plan, final, project):
    """E14: a path whose bytes changed between the plan the person saw and now, under the lock."""
    for path in sorted(set(final.writes.observed) | set(plan.writes.observed)):
        before = plan.writes.observed.get(path)
        if file_hash(project, path) != before:
            raise Stop('E14', path=path)
    if final.writes.updates != plan.writes.updates and final.op_id == plan.op_id:
        changed = sorted(set(final.writes.updates) ^ set(plan.writes.updates)
                         | {path for path in final.writes.updates
                            if final.writes.updates[path] != plan.writes.updates.get(path)})
        raise Stop('E14', path=changed[0] if changed else LOCK)


def journal_of(plan, confirmed_argv):
    after, before, _ = plan.writes.modes()
    updates = plan.writes.updates
    return {'schema_version': 1, 'kind': 'trace-skills-operation', 'operation_id': plan.op_id,
            'operation': plan.operation, 'name': plan.name, 'state': 'applying',
            'started_at': stamp(plan.moment), 'argv': confirmed_argv,
            'writes': {path: {'before': plan.writes.observed[path],
                              'after': None if data is None else sha256(data)}
                       for path, data in sorted(updates.items())},
            'modes_after': after, 'modes_before': before,
            'prune': sorted(path for path, data in updates.items() if data is None),
            'intake': None if plan.intake is None else {'args': plan.intake},
            'receipt_path': plan.receipt_path, 'receipt': plan.receipt,
            'staging': plan.staging}


def undo_sparing(project, spared):
    """Undo a transaction whose own recovery refused only because `spared` changed before it was written.

    bootstrap.transaction compares each path just before writing it, so the
    path that failed that comparison was never written: what it holds is the
    person's edit. bootstrap.recover nevertheless refuses that path and keeps
    the whole half-written transaction (a limit of bootstrap.py, reported with
    this build). Here every other path goes back to its backup, the edit
    stays, and the journal goes, which is what E15 promises.
    """
    journal = bootstrap.load(bootstrap.confined(project, JOURNAL))
    entries = journal['entries']
    for name, entry in entries.items():
        if name == spared:
            continue
        current = file_hash(project, name)
        if current not in (entry['before'], entry['after']):
            raise bootstrap.SetupError(f'recovery would overwrite a later edit: {name}')
        if entry['before'] is not None and current != entry['before'] and \
                file_hash(project, entry['backup']) != entry['before']:
            raise bootstrap.SetupError('recovery backup digest mismatch')
    for name, entry in entries.items():
        if name == spared or file_hash(project, name) == entry['before']:
            continue
        if entry['before'] is None:
            bootstrap.confined(project, name).unlink(missing_ok=True)
        else:
            bootstrap.write_atomic(bootstrap.confined(project, name),
                                   bootstrap.confined(project, entry['backup']).read_bytes())
    (project / JOURNAL).unlink()


def undo_failed(ctx, journal, exc):
    """After an Exception in the transaction, which undid itself: modes back, journal gone, the error named (12.5)."""
    project = ctx.project
    original = exc.__context__
    changed = 'file changed while applying setup: '
    refused = 'recovery would overwrite a later edit: '
    if isinstance(exc, bootstrap.SetupError) and isinstance(original, bootstrap.SetupError) \
            and str(original).startswith(changed) and str(exc) == refused + str(original)[len(changed):] \
            and (project / JOURNAL).exists():
        undo_sparing(project, str(original)[len(changed):])
        exc = original
    elif (project / JOURNAL).exists():
        # The transaction could not undo itself: both journals stay, and recover names what blocks it.
        message = str(exc)
        if message.startswith(refused):
            return Stop('R2', path=message[len(refused):])
        return Stop('E17', step='undoing the transaction', error=message, tool=ctx.tool,
                    operation_id=journal['operation_id'])
    set_modes(project, journal['modes_before'])
    prune(project, [path for path, item in journal['writes'].items() if item['before'] is None])
    remove_journal(project)
    ctx.pending = None
    message = str(exc)
    if isinstance(exc, bootstrap.SetupError):
        for prefix, ident in (('file changed since the setup plan: ', 'E14'),
                              ('file changed while applying setup: ', 'E15')):
            if message.startswith(prefix):
                return Stop(ident, path=message[len(prefix):])
    return Stop('E13', error=message, advice=PERMISSION_ADVICE if isinstance(exc, PermissionError) else None)


@contextlib.contextmanager
def setup_lock(project):
    """bootstrap.locked, with a busy lock reported as E6."""
    held = bootstrap.locked(project)
    try:
        held.__enter__()
    except bootstrap.SetupError as exc:
        if str(exc) == 'another setup process owns this target':
            raise Stop('E6') from None
        raise
    try:
        yield
    finally:
        held.__exit__(None, None, None)


def execute(ctx, plan, replan, confirmed):
    """Steps 2 to 6 of 12.3: lock, re-check, journal, transaction, forward steps; returns the final plan.

    `replan(confirmed)` makes the plan again under the lock, which re-checks
    every precondition and picks the final operation id; D's commands pass
    their own.
    """
    project = ctx.project
    with bootstrap.interruptible(), setup_lock(project):
        check_pending(ctx)
        final = replan(confirmed)
        stale(plan, final, project)
        if final.fetched is not None:
            verify_staged(final.fetched)
        journal = journal_of(final, ctx.argv)
        bootstrap.write_atomic(project / OPERATION, bootstrap.encoded(journal))
        ctx.pending = final.op_id
        try:
            bootstrap.transaction(project, final.writes.updates, final.writes.expected)
        except Exception as exc:
            raise undo_failed(ctx, journal, exc) from None
        journal['state'] = 'applied'
        bootstrap.write_atomic(project / OPERATION, bootstrap.encoded(journal))
        forward(ctx, journal, recovered=False)
        return final


def confirm(ctx, write_candidate_file):
    """The confirmation of 6.0: --yes, a y at the terminal, or C1 with a candidate file."""
    if ctx.args.yes:
        return '--yes'
    if stdin_is_terminal():
        print('Apply? Nothing is committed or pushed. [y/N] ', end='', flush=True)
        answer = sys.stdin.readline().strip().casefold()
        if answer in ('y', 'yes'):
            return 'at the terminal'
        raise Stop('C2')
    file = write_candidate_file()
    raise Stop('C1', command=ctx.same_command(), file=file)


def stdin_is_terminal():
    return sys.stdin is not None and sys.stdin.isatty()


# show and list (6.1, 6.2) ----------------------------------------------------

def header_lines(fetched):
    package = fetched.package
    lines = [f'Source: {fetched.repository} at {fetched.commit} (requested {fetched.requested_ref})',
             f'Package: {fetched.path} ({package.name}), {len(package.members)} files, {package.size} bytes',
             f'Package digest: {package.tree_sha256}',
             definition_line(package.states)]
    for note in package.notes:
        lines.append(note[0].upper() + note[1:])
    if package.litter:
        lines.append('Left out as operating-system files: ' + ', '.join(package.litter))
    return lines


def print_package(fetched):
    """The whole package as show prints it, from Source: to the last member block."""
    for line in header_lines(fetched):
        print(line)
    print()
    for item in fetched.package.members:
        print(f"--- {item['path']} ({item['mode']}, {item['size']} bytes, sha256 {item['sha256']})")
        text = printable(item['data'])
        if text is None:
            print(f"(not UTF-8 text: {item['size']} bytes; not printed)")
        else:
            sys.stdout.write(text if not text or text.endswith('\n') else text + '\n')
        print(f"--- end of {item['path']}")
        print()


def print_manifest(fetched):
    for line in header_lines(fetched):
        print(line)
    for item in fetched.package.members:
        print(f"{item['mode']} {item['size']} {item['sha256']} {item['path']}")


def command_list(ctx):
    project = ctx.open()
    repository = check_address(ctx.args.repository)
    tmp = new_temp(project)
    try:
        requested, commit = resolve(repository, ctx.args.ref)
        bare = fetch_commit(tmp, repository, commit)
        entries = tree_entries(bare, commit)
        folders = sorted({PurePosixPath(entry['path']).parent.as_posix() for entry in entries
                          if PurePosixPath(entry['path']).name == 'SKILL.md' and entry['kind'] == 'blob'
                          and '/' in entry['path']})
        print(f'Source: {repository} at {commit[:12]} (requested {requested})')
        print(f'Packages: {len(folders)}' if folders else
              'Packages: 0 (no folder holding a SKILL.md at this commit)')
        for folder in folders:
            try:
                package = adoptable(folder, PurePosixPath(folder).name, package_entries(bare, entries, folder))
            except Refused as exc:
                print(f'  {folder}: refused: {exc.reason}')
                continue
            name = package.front.get('name') if package.front else None
            description = package.front.get('description') if package.front else None
            print(f"  {folder}: {name or '(no name)'}")
            if isinstance(description, str) and description:
                description = ' '.join(description.split())
                print('    ' + escape(description[:200] + ('...' if len(description) > 200 else '')))
            else:
                print('    (no description)')
            print('    ' + definition_line(package.states))
        print('Nothing was copied into any project.')
        print(LIMIT)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def command_show(ctx):
    project = ctx.open()
    fetched = fetch_package(project, ctx.args.repository, ctx.args.path, ctx.args.ref)
    candidate = write_candidate(fetched, 'show', 'adopt')
    print_package(fetched)
    print(f'Candidate: {candidate}')
    print('To adopt exactly these bytes: ' + ctx.tool + ' ' +
          quoted(['adopt', fetched.repository, fetched.path, '--candidate', candidate]))
    print(f'The staged copy at {fetched.staged.as_posix()} is removed when adopt applies it.')
    print(LIMIT)
    return 0


# adopt (6.3) -------------------------------------------------------------------

def name_check(ctx, state, lock, name, fetched):
    """Step 5 of adopt: N2 to N10 in that order; returns what the name was before (ownership.before)."""
    args, project = ctx.args, ctx.project
    entry = lock['skills'].get(name)
    if entry and entry['manager'] == 'external':
        source = entry['source']
        if source['repository'] == fetched.repository and source['path'] == fetched.path:
            if source['commit'] == fetched.commit:
                raise Done(f"Already adopted: {name} from {fetched.repository} {fetched.path} at "
                           f"{fetched.commit[:12]}; nothing to do.")
            raise Stop('N2', name=name, c12=source['commit'][:12])
        raise Stop('N3', name=name, repository=source['repository'], path=source['path'], tool=ctx.tool)
    if entry and entry['manager'] == 'outside':
        raise Stop('N4', name=name, held_by=entry['held_by'], tool=ctx.tool)
    kit = kit_files(state, name)
    if kit and not args.replace_kit_copy:
        raise Stop('N5', name=name)
    if kit:
        for path in kit:
            current = file_hash(project, path)
            if current not in (None, state['files'][path].get('sha256')):
                raise Stop('N6', name=name, path=path)
    folder = project_files(project, name)
    if folder and not kit and not args.keep_existing:
        raise Stop('N7', name=name)
    if (project / '.skills' / f'{name}.md').is_file():
        raise Stop('N8', name=name)
    if args.replace_kit_copy and not kit:
        raise Stop('N9', name=name)
    if args.keep_existing and not folder:
        raise Stop('N10', name=name)
    if kit:
        return 'kit'
    if folder:
        return 'project'
    return 'retired' if entry else 'free'


def kept_copy(project, name):
    """The existing .skills/<name>/ read as a package for --keep-existing (2.2, 2.4; N20 for its name)."""
    folder = f'.skills/{name}'
    try:
        return adoptable(folder, name, copy_entries(project, folder))
    except Refused as exc:
        if exc.ident == 'P14':
            raise Stop('N20', path=folder, x=exc.fields['name'], name=name) from None
        raise Stop('P', path=folder, reason=exc.reason) from None


def audiences(ctx, lock, fetched, observed=None):
    """Both audiences as compared at this operation, the comparison, and whether the flag is required."""
    declared = parse_audiences(ctx.args.audience)
    by = ctx.by
    source_observed = observed['source'] if observed else observe_audience(fetched.repository)
    project_observed = observed['project'] if observed else project_origin_audience(ctx.project)
    source = {'declared': declared.get('source', 'not declared'),
              'declared_by': by if 'source' in declared else None, 'observed': source_observed}
    standing = lock.get('project_audience') or {}
    if 'project' in declared:
        project = {'declared': declared['project'], 'declared_by': by, 'observed': project_observed}
    else:
        project = {'declared': standing.get('declared', 'not declared'),
                   'declared_by': standing.get('declared_by'), 'observed': project_observed}
    comparison, required = compare_audiences(source, project)
    return {'source': source, 'project': project, 'comparison': comparison,
            'share_with_project': {'required': required, 'given': bool(ctx.args.share_with_project)}}


def plan_adopt(ctx, fetched, op_id, moment, confirmed=None, observed=None):
    """Every precondition of adopt from step 3 on, and every write, with the plan's text (6.3)."""
    project, args = ctx.project, ctx.args
    lock, state = read_lock(project), read_state(project)
    state_before = json.loads(json.dumps(state)) if state is not None else None
    package, name = fetched.package, fetched.package.name
    before = name_check(ctx, state, lock, name, fetched)
    audience = audiences(ctx, lock, fetched, observed)
    if audience['share_with_project']['required'] and not args.share_with_project:
        raise Stop('A1', source=audience_form(audience['source']), project=audience_form(audience['project']))
    if not (project / TRACE_RECORD).is_file():
        raise Stop('E11', row='intake')
    plan = Plan('adopt', name, op_id, moment)
    plan.fetched = fetched
    plan.observed = {'source': audience['source']['observed'], 'project': audience['project']['observed']}
    writes = plan.writes = Writes(project)
    copy_dir, snapshot = f'.skills/{name}', f'{BASES}/{package.tree_sha256}'
    copy_ignored = bootstrap.git_ignored(project, [f'{copy_dir}/{item["path"]}' for item in package.members])
    tracked = [item for item in package.members if f'{copy_dir}/{item["path"]}' not in copy_ignored]
    tracked_sha256 = package_digest(manifest(tracked))
    recorded = {}
    if args.keep_existing:
        copy = kept_copy(project, name)
        copy_members = copy.members
        for item in copy_members:
            writes.keep(f'{copy_dir}/{item["path"]}', item['mode'])
            recorded[f'{copy_dir}/{item["path"]}'] = item['data']
        canonical = {f'{copy_dir}/{item["path"]}': (item['data'], item['mode']) for item in copy_members}
    else:
        copy_members = tracked
        for item in package.members:
            writes.put('copy', f'{copy_dir}/{item["path"]}', item['data'], item['mode'])
        canonical = {f'{copy_dir}/{item["path"]}': (item['data'], item['mode']) for item in tracked}
    # AH-239: how the kept copy differs from the package, which is what status will compare it with;
    # AH-256: with the text of each change, so the person can tell whose change it is.
    plan.kept_difference = differences(tracked, copy_members) if args.keep_existing else None
    plan.kept_diff = changed_diffs(tracked, copy_members, ('package', 'copy')) if args.keep_existing else []
    # AH-251: the template route's scripts folder, which neither the plan's list nor status compares.
    plan.kept_outside = [f'scripts/{name}'] if args.keep_existing and (project / 'scripts' / name).is_dir() else []
    put_bases_note(project, writes)
    for item in package.members:
        writes.put('snapshot', f'{snapshot}/{item["path"]}', item['data'], item['mode'])
    kit = kit_files(state, name) if args.replace_kit_copy else []
    removed_kit = [path for path in kit if path not in canonical and path not in writes.left]
    copies, edited = agent_copies(project, state, name, canonical, kit, writes)
    for path in removed_kit:
        writes.put('removed', path, None)
    retired_added = []
    if state is not None:
        for path in kit:
            state['files'].pop(path, None)
        if args.replace_kit_copy and name not in state.get('retired_skills', []):
            state['retired_skills'] = sorted(set(state.get('retired_skills', [])) | {name})
            retired_added = [name]
        if state != state_before:
            writes.put('state', STATE, bootstrap.encoded(state))
    receipt_path = plan.receipt_path
    old = lock['skills'].get(name)
    # A retired entry's receipts carry forward to the adoption that replaces it (4.4).
    receipts = list(old.get('receipts', [])) if old and old['manager'] == 'retired' else []
    adopted_digest = package_digest(manifest(copy_members))
    entry = {'manager': 'external',
             'source': dict(fetched.source(), audience=audience['source']),
             'project_audience': audience['project'],
             'upstream': {'tree_sha256': package.tree_sha256, 'tracked_sha256': tracked_sha256, 'snapshot': snapshot},
             'adopted': {'path': copy_dir, 'tree_sha256': adopted_digest, 'receipt': receipt_path,
                         'kept_existing': bool(args.keep_existing)},
             'detached': False,
             'replaced_kit_copy': bool(args.replace_kit_copy),
             'receipts': receipts + [receipt_path]}
    lock_after = json.loads(json.dumps(lock))
    lock_after['skills'][name] = entry
    declared = parse_audiences(args.audience)
    if 'project' in declared:
        lock_after['project_audience'] = {'declared': declared['project'], 'declared_by': ctx.by,
                                          'declared_at': stamp(moment)}
    put_lock(writes, lock_after)
    receipt = receipt_base(ctx, plan, state, confirmed)
    decided = receipt['decided']
    receipt.update({
        'source': fetched.source(),
        'upstream': {'tree_sha256': package.tree_sha256, 'tracked_sha256': tracked_sha256,
                     'snapshot': snapshot, 'files': package.files, 'litter': package.litter},
        'previous': None,
        'copy': {'path': copy_dir, 'tree_sha256': adopted_digest, 'kept_existing': bool(args.keep_existing),
                 'files': manifest(copy_members)},
        'definition': package.states,
        'audience': audience,
        'ownership': {'before': before, 'replaced_kit_copy': bool(args.replace_kit_copy),
                      'kit_files_removed': sorted(kit), 'retired_skills_added': retired_added},
        'kept_outside': plan.kept_outside,
        'agent_copies': copies})
    deferred = [Stop('N23', name=name, path=edited) if edited else None]
    put_inventory(project, writes, lock_after, {plan.receipt_path: receipt}, deferred)
    must_track = [f'{copy_dir}/SKILL.md', LOCK, plan.receipt_path, f'{snapshot}/SKILL.md', INVENTORY]
    seal(plan, receipt, gate_writes(project, name, writes, must_track, recorded, deferred))
    if confirmed is not None:
        if fetched.candidate is not None:
            opening = (f'the whole package ({len(package.members)} files, {package.size} bytes) was printed by '
                       f"{fetched.candidate.get('created_by', 'show')} before writing and the person confirmed it "
                       f'({confirmed})')
        else:
            opening = (f'the whole package ({len(package.members)} files, {package.size} bytes) was printed '
                       f'before writing and the person confirmed it ({confirmed})')
        checked = checked_text(opening, package.states, audience['comparison'])
        if args.keep_existing:
            # AH-254: the instruction at .skills/<name>/ stays the project's own text, and the row says so.
            checked = ("the project's existing copy was kept and connected to the package (differences from the "
                       f"package: {', '.join(plan.kept_difference) or 'none'}); {checked}")
        plan.intake = intake_args(name, fetched.source(), package.tree_sha256, decided['by'], checked,
                                  args.used_for, plan.receipt_path, decided['authority'])
    plan.staging = fetched.staging.as_posix()
    plan.lines = adopt_lines(ctx, plan, fetched, before, audience, copies, kit, state)
    return plan


def tool_path(project):
    script = Path(__file__).resolve()
    try:
        return script.relative_to(project).as_posix()
    except ValueError:
        return script.as_posix()


# The writers every writing command shares; each takes the whole lock as data.

def receipt_base(ctx, plan, state, confirmed):
    """The fields of 5.1 every operation's receipt carries; the command adds the rest."""
    project = ctx.project
    return {'schema_version': 1, 'kind': 'trace-skills-receipt', 'contract': CONTRACT_VERSION,
            'operation': plan.operation, 'operation_id': plan.op_id, 'name': plan.name,
            'started_at': stamp(plan.moment), 'recovered': False,
            'tool': {'path': tool_path(project), 'sha256': sha256(Path(__file__).resolve().read_bytes())},
            'project': {'installed': state is not None,
                        'kit_version': state.get('version') if state else None,
                        'agent': state.get('agent') if state else None,
                        'head': bootstrap.git(project, 'rev-parse', '--verify', 'HEAD', check=False) or None},
            'decided': ctx.decided(confirmed),
            'limits': list(RECEIPT_LIMITS)}


def put_lock(writes, lock_after):
    """The lock, written whole (4.1)."""
    writes.put('lock', LOCK, bootstrap.encoded(lock_after))


def put_inventory(project, writes, lock_after, receipts, deferred):
    """The inventory section generated from the lock as it will stand; I1 joins the deferred refusals.

    `receipts` maps a receipt path this operation has not written yet to its
    data, so the section can say who shared an entry with the project.
    """
    try:
        existing = (project / INVENTORY).read_bytes() if (project / INVENTORY).is_file() else None
        writes.put('inventory', INVENTORY, place_inventory(existing, inventory_section(project, lock_after, receipts)))
    except Stop as exc:
        deferred.append(exc)


def seal(plan, receipt, not_tracked):
    """The receipt's writes, modes and not_tracked, from the writes as planned."""
    writes = plan.writes
    _, _, left = writes.modes()
    receipt['writes'] = {path: {'before': writes.observed[path], 'after': None if data is None else sha256(data)}
                         for path, data in sorted(writes.updates.items())}
    receipt['modes'] = left
    receipt['not_tracked'] = not_tracked
    plan.not_tracked, plan.receipt = not_tracked, receipt


def adopt_lines(ctx, plan, fetched, before, audience, copies, kit, state):
    package, name, args = fetched.package, plan.name, ctx.args
    naming = {'free': f'{name} is free', 'retired': 'a retired entry is replaced',
              'kit': "the kit's copy is replaced (--replace-kit-copy)",
              'project': 'the existing copy is kept and connected (--keep-existing)'}[before]
    lines = [f'Plan: adopt {name} into {ctx.project.as_posix()}',
             f'  from: {fetched.repository} {fetched.path} at {fetched.commit} (requested {fetched.requested_ref})',
             f'  package digest: {package.tree_sha256}',
             '  ' + definition_line(package.states),
             f'  name: {naming}']
    if plan.kept_difference is not None:
        title = '  difference from the package to the kept copy'
        if plan.kept_difference:
            lines.append(f'{title} (status will report the copy adapted):')
            lines += [f'    {line}' for line in plan.kept_difference]
        else:
            lines.append(f'{title}: none (status will report the copy unchanged)')
        lines += ['  ' + KEPT_OUTSIDE_NOTE.format(folder=folder, name=name) for folder in plan.kept_outside]
        # The diffs as update's adapted view prints them; each line of unified() ends with its newline.
        lines += [line[:-1] for line in plan.kept_diff]
    if args.replace_kit_copy:
        companions = kit_companions(state, name)
        if companions:
            lines.append(f"  the kit's files that share this skill's name stay where the kit ships them: "
                         f"{', '.join(companions)}")
    if state is not None and name in state.get('retired_skills', []) and not args.replace_kit_copy:
        lines.append(f"  the kit's updater keeps {name} retired; this adoption does not change that")
    lines.append(f"  audience: source {audience_form(audience['source'])}; project "
                 f"{audience_form(audience['project'])}; {audience['comparison']}")
    if args.share_with_project:
        lines.append(f"  Shared with the project: by {ctx.by}; words: {args.words or 'not stated'}")
    lines.append('  writes, in this order:')
    if args.keep_existing:
        lines.append(f'    .skills/{name}/ is kept as it is')
    else:
        lines.append(f'    {len(package.members)} file(s) under .skills/{name}/')
    lines += bases_note_line(plan.writes)
    lines.append(f'    {len(package.members)} file(s) under {BASES}/{package.tree_sha256}/')
    if copies['note'] == NOTE_ADAPTER:
        lines.append(f"    {copies_left(copies)} agent copies under {bootstrap.AGENTS[copies['agent']]}/{name}/")
    elif copies['note'] == NOTE_NONE:
        lines.append('    no agent copies (agent none)')
    else:
        lines.append('    no agent copies (no adapter state here)')
    if args.replace_kit_copy:
        lines.append(f"    {len(kit)} kit file(s) removed: {', '.join(kit)}")
    if STATE in plan.writes.updates:
        lines.append(f'    {STATE}')
    lines.append(f'    {LOCK}')
    lines.append(f'    {INVENTORY} (the inventory section)')
    lines.append(f'  then: an intake row in {INTAKE}, and last the receipt {plan.receipt_path}')
    lines.append('  not tracked by Git here: ' + (', '.join(plan.not_tracked) if plan.not_tracked else 'none'))
    return lines


def command_adopt(ctx):
    project = ctx.open()
    args = ctx.args
    check_writing_flags(ctx)
    if args.replace_kit_copy and args.keep_existing:
        raise Stop('N11')
    parse_audiences(args.audience)
    if args.yes and args.candidate is None and not (args.ref and HEX40.fullmatch(args.ref)):
        raise Stop('C3')
    check_pending(ctx)
    read_lock(project)
    read_state(project)
    if args.candidate is not None:
        fetched = candidate_package(project, args.candidate, args.repository, args.path, args.ref)
    else:
        fetched = fetch_package(project, args.repository, args.path, args.ref)
    moment = utc_now()

    def replan(confirmed):
        final_id = free_operation_id(project, read_lock(project), moment, fetched.package.name, 'adopt')
        return plan_adopt(ctx, fetched, final_id, moment, confirmed, plan.observed)

    def candidate_file():
        if fetched.candidate_file:
            return fetched.candidate_file
        return write_candidate(fetched, 'adopt', 'adopt')

    try:
        op_id = free_operation_id(project, read_lock(project), moment, fetched.package.name, 'adopt')
        plan = plan_adopt(ctx, fetched, op_id, moment)
        if args.candidate is not None:
            # show printed the contents; the header and one manifest line per member stand for them.
            print_manifest(fetched)
            print()
        else:
            print_package(fetched)
        for line in plan.lines:
            print(line)
        # 2.5: every output that prints definition 1 prints the limit line; once, after the plan, as update does.
        print(LIMIT)
        confirmed = confirm(ctx, candidate_file)
        final = execute(ctx, plan, replan, confirmed)
    except (Stop, Done, KeyboardInterrupt) as exc:
        # A refusal or an interruption before the journal leaves nothing to finish: a folder
        # this run fetched goes, except the one C1 just pointed the person to.
        if ctx.pending is None and getattr(exc, 'ident', None) != 'C1':
            fetched.discard()
        raise
    receipt = receipt_data(project, final.receipt_path) or {}
    intake = (receipt.get('records') or {}).get('intake') or {}
    print(f'Adopted {final.name} from {fetched.repository} {fetched.path} at {fetched.commit[:12]}.')
    print(f'  receipt: {final.receipt_path}')
    print(f"  intake: {intake.get('id')} in {INTAKE}")
    print(f"  agent copies: {copies_text(final.receipt['agent_copies'])}")
    print('Next: review and commit these files, then record the adoption against that commit:')
    print(f'  {ctx.tool} link {final.receipt_path} --commit <sha>')
    print('Nothing was committed or pushed.')
    return 0


# link (6.10) -------------------------------------------------------------------

def command_link(ctx):
    project = ctx.open()
    given = ctx.args.receipt
    path = Path(given)
    if path.is_absolute():
        try:
            receipt = path.resolve().relative_to(project).as_posix()
        except ValueError:
            raise Stop('L1', receipt=given) from None
    else:
        receipt = PurePosixPath(given.replace('\\', '/')).as_posix()
    # A receipt named under the folder's name before 0.1.0-rc19 (or after) is read where it is now.
    receipt = bootstrap.recorded_path(project, receipt)
    if (project / OPERATION).exists():
        journal = receipt_data(project, OPERATION)
        raise Stop('E7', operation_id=(journal or {}).get('operation_id') or OPERATION, tool=ctx.tool)
    if not (project / TRACE_RECORD).is_file():
        raise Stop('E11', row='transition')
    with setup_lock(project):
        return link_locked(ctx, project, given, receipt)


def link_locked(ctx, project, given, receipt):
    if PurePosixPath(receipt).parent.as_posix() != RECEIPTS or not (project / receipt).is_file():
        raise Stop('L1', receipt=given)
    data = receipt_data(project, receipt)
    if not isinstance(data, dict) or data.get('kind') != 'trace-skills-receipt' or data.get('schema_version') != 1:
        raise Stop('L1', receipt=given)
    if data.get('operation') not in LINKED:
        raise Stop('L2', receipt=receipt, operation=data.get('operation'))
    resolved = bootstrap.git(project, 'rev-parse', '--verify', '--quiet', ctx.args.commit + '^{commit}', check=False)
    if not re.fullmatch(r'[0-9a-f]{40}', resolved or ''):
        raise Stop('L3', commit=ctx.args.commit)
    # The commit names the receipt where the state folder was then: an adoption committed before an update
    # that moved the folder holds it under an earlier name, and the row names it as that commit holds it.
    committed = None
    for held in [receipt] + [other + receipt[len(folder):] for folder in bootstrap.STATE_DIRS
                             for other in bootstrap.STATE_DIRS if other != folder and receipt.startswith(folder + '/')]:
        run = subprocess.run(['git', 'cat-file', 'blob', f'{resolved}:{held}'], cwd=project, capture_output=True)
        if not run.returncode and run.stdout == (project / receipt).read_bytes():
            committed, receipt = run, held
            break
    if committed is None:
        raise Stop('L4', c12=resolved[:12], receipt=receipt)
    name = data['name']
    skill = subprocess.run(['git', 'cat-file', 'blob', f'{resolved}:.skills/{name}/SKILL.md'], cwd=project,
                           capture_output=True)
    if skill.returncode:
        raise Stop('L5', c12=resolved[:12], name=name)
    digest = sha256(skill.stdout)
    for row in registry_rows(project, TRANSITIONS):
        if row.get('kind') == 'default-adoption' and row.get('checks') == receipt:
            commits = row.get('commits') or []
            if resolved in commits:
                raise Done(f"Already linked: {row.get('id')} ({TRANSITIONS}); nothing written.")
            raise Stop('L6', receipt=receipt, commits=', '.join(str(sha)[:12] for sha in commits) or 'no commit',
                       tr=row.get('id'))
    decided = data.get('decided') or {}
    destination = f'.skills/{name}/ (a standing instruction for the agents in this project)'
    # AH-261: a kept copy is the project's own text connected to a package, not the package becoming the
    # instruction; the row says so, and says when the commit did not change the SKILL.md at all.
    kept = bool((data.get('copy') or {}).get('kept_existing'))
    unchanged = None
    if kept:
        parent = subprocess.run(['git', 'cat-file', 'blob', f'{resolved}^:.skills/{name}/SKILL.md'], cwd=project,
                                capture_output=True)
        unchanged = parent.returncode == 0 and parent.stdout == skill.stdout
        destination = (f".skills/{name}/ (the project's existing copy, kept and connected to the package, which did "
                       'not replace it' + ('; this commit did not change its SKILL.md, so no instruction changed)'
                                           if unchanged else ')'))
    args = ['transition', '--kind', 'default-adoption', '--object', f'.skills/{name}/SKILL.md',
            '--digest', digest, '--checks', receipt, '--by', decided.get('by') or 'not stated',
            '--authority', decided.get('authority') or 'nothing stated', '--decision', 'adopt',
            '--destination', destination, '--commits', resolved]
    code, out, err = run_trace_record(project, args)
    row, _ = registry_row(project, TRANSITIONS, 'checks', receipt)
    if code != 0 or row is None:
        raise Stop('E13', error=f'trace_record.py transition exited {code}: {first_line(err or out)}'
                   if code else f'no row in {TRANSITIONS} names {receipt}')
    print(f"Linked {receipt} to {resolved[:12]}: {row.get('id')} in {TRANSITIONS}.")
    if kept:
        print(f"The receipt kept the project's existing .skills/{name}/; the row says so"
              + (f', and that {resolved[:12]} did not change its SKILL.md.' if unchanged else '.'))
    wrote = next((item.get('sha256') for item in (data.get('copy') or {}).get('files') or []
                  if item.get('path') == 'SKILL.md'), None)
    if wrote != digest:
        print(f'The committed .skills/{name}/SKILL.md differs from the one this receipt wrote; '
              'the row records the committed file.')
    return 0


def registry_rows(project, registry):
    path = project / registry
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# recover (6.11, 12.5) --------------------------------------------------------

def command_recover(ctx):
    project = ctx.open()
    with bootstrap.interruptible(), setup_lock(project):
        return recover_locked(ctx, project)


def recover_locked(ctx, project):
    if (project / UPDATE_PENDING).exists():
        raise Stop('E9')
    transaction = project / JOURNAL
    if not (project / OPERATION).exists():
        if transaction.exists():
            raise Stop('R1', path=first_entry(project, set()))
        print('Nothing to recover.')
        return 0
    try:
        journal = json.loads((project / OPERATION).read_text(encoding='utf-8'))
        if not isinstance(journal, dict) or journal.get('kind') != 'trace-skills-operation' or \
                journal.get('state') not in ('applying', 'applied') or not isinstance(journal.get('writes'), dict):
            raise ValueError('not an operation journal of this tool')
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise Stop('R3', reason=str(exc)) from None
    op_id = journal['operation_id']
    ctx.pending = op_id
    if journal['state'] == 'applying':
        if transaction.exists():
            stranger = first_entry(project, set(journal['writes']))
            if stranger is not None:
                raise Stop('R1', path=stranger)
            try:
                bootstrap.recover(project)
            except bootstrap.SetupError as exc:
                if str(exc).startswith('recovery would overwrite a later edit: '):
                    raise Stop('R2', path=str(exc)[len('recovery would overwrite a later edit: '):]) from None
                raise
            set_modes(project, journal.get('modes_before') or {})
            prune(project, [path for path, item in journal['writes'].items() if item.get('before') is None])
            if journal.get('staging') and Path(journal['staging']).exists():
                shutil.rmtree(journal['staging'], ignore_errors=True)
            remove_journal(project)
            ctx.pending = None
            print(f"Undone: {op_id}; the project is as it was before. Run the command again to "
                  f"{journal['operation']}.")
            return 0
        writes = journal['writes']
        now = {path: file_hash(project, path) for path in sorted(writes)}
        if all(now[path] == item['before'] for path, item in writes.items()):
            remove_journal(project)
            ctx.pending = None
            print(f'Cleared: nothing of {op_id} had been written.')
            return 0
        if not all(now[path] == item['after'] for path, item in writes.items()):
            # A path at neither hash was edited by someone; with none, the writes are half done.
            edited = [path for path in sorted(writes) if now[path] not in (writes[path]['before'],
                                                                           writes[path]['after'])]
            raise Stop('R2', path=(edited or [path for path in sorted(writes)
                                              if now[path] != writes[path]['after']])[0])
        journal['state'] = 'applied'
        bootstrap.write_atomic(project / OPERATION, bootstrap.encoded(journal))
    forward(ctx, journal, recovered=True)
    print(f"Completed: {op_id}; receipt {journal['receipt_path']}.")
    return 0


def first_entry(project, known):
    """The first path of the pending setup transaction that is not one of this tool's writes, or None."""
    try:
        entries = json.loads((project / JOURNAL).read_text(encoding='utf-8')).get('entries') or {}
    except (OSError, ValueError, AttributeError):
        return JOURNAL
    strangers = sorted(path for path in entries if path not in known)
    if not known:
        return strangers[0] if strangers else JOURNAL
    return strangers[0] if strangers else None


# status (section 11) -----------------------------------------------------------

def shadow(name):
    """The shadowed: lines, one per personal folder holding the name, each with that folder's observation.

    AH-237 put what the pilot observed on the line; AH-257 gives each folder
    the observation of the client that reads it, and names every such folder.
    """
    found = [f'  shadowed: by a personal skill at ~/{root}/{name}; {SHADOW_OBSERVED.get(root, SHADOW_UNREHEARSED)}'
             for root in SHADOW_ROOTS if (Path.home() / root / name).exists()]
    return '\n'.join(found or ['  shadowed: no'])


def upstream_state(project, entry, offline):
    """The upstream line: current, moved with the package unchanged, behind, or unknown with the reason."""
    if offline:
        return 'unknown (not fetched (--offline))'
    source = entry['source']
    ref = source['requested_ref']
    if HEX40.fullmatch(ref):
        return 'unknown (pinned to a commit; update --ref <ref> compares with a branch or tag)'
    tmp = None
    try:
        _, commit = resolve(source['repository'], None if ref == 'HEAD' else ref)
        if commit == source['commit']:
            return 'current'
        tmp = new_temp(project)
        bare = fetch_commit(tmp, source['repository'], commit)
        entries = tree_entries(bare, commit, source['path'])
        if not any(item['path'].startswith(source['path'] + '/') for item in entries):
            return f"unknown (the package folder {source['path']} is gone at {commit[:12]})"
        found = package_entries(bare, entries, source['path'])
        members = [member(item['path'], item['mode'], item['data'] or b'') for item in found
                   if not is_litter(item['path'])]
        if package_digest(manifest(members)) == entry['upstream']['tree_sha256']:
            return f'current (the source moved to {commit[:12]}; the package is unchanged)'
        return f'behind {commit[:12]}'
    except Stop as exc:
        if exc.ident == 'S3':
            return f'unknown (the source has no branch or tag named {ref})'
        # All the tool can say about being offline, revoked access or a deleted repository.
        return f"unknown (fetch failed: {exc.fields.get('error', str(exc))})"
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def copy_state(project, name, entry):
    """The copy line and whether the copy is unchanged."""
    folder = entry['adopted']['path']
    if not (project / folder / 'SKILL.md').is_file():
        return f'missing ({folder}/SKILL.md is not here)', False
    receipt = receipt_data(project, entry['adopted']['receipt'])
    recorded = {item['path']: item['mode'] for item in ((receipt or {}).get('copy') or {}).get('files') or []}
    members = disk_members(project, folder, recorded)
    if package_digest(manifest(members)) == entry['upstream']['tracked_sha256']:
        return 'unchanged', True
    files = ((receipt or {}).get('upstream') or {}).get('files') if isinstance(receipt, dict) else None
    if not isinstance(files, list):
        return 'adapted (the files cannot be listed: the receipt is missing)', False
    ignored = bootstrap.git_ignored(project, [f"{folder}/{item['path']}" for item in files])
    original = {(item['path'], item['mode'], item['sha256']) for item in files
                if f"{folder}/{item['path']}" not in ignored}
    here = {(item['path'], item['mode'], item['sha256']) for item in members}
    differing = sorted({item[0] for item in original ^ here})
    shown = ', '.join(differing[:5]) + (f' and {len(differing) - 5} more' if len(differing) > 5 else '')
    return f'adapted ({len(differing)} file(s) differ from the upstream original: {shown})', False


def change_notice(project, folder, unchanged):
    if unchanged:
        return 'not needed (copy unchanged)'
    try:
        front = read_front_matter((project / folder / 'SKILL.md').read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, FrontMatterError):
        return 'absent'
    metadata = front.get('metadata')
    if isinstance(metadata, dict) and metadata.get('changes'):
        return 'present'
    return 'absent'


def folder_definition(project, name):
    states, _, _ = definition(disk_members(project, f'.skills/{name}'), name)
    return definition_line(states)


def status_names(project, state, lock):
    names = set()
    if state is not None:
        names |= {path.split('/')[1] for path in kit_files_all(state)}
    skills = project / '.skills'
    if skills.is_dir():
        for path in skills.iterdir():
            if path.is_dir() and project_files(project, path.name):
                names.add(path.name)
            elif path.is_file() and path.suffix == '.md':
                names.add(path.stem)
    names |= {name for name, entry in lock['skills'].items() if entry['manager'] in ('external', 'outside')}
    return sorted(names)


def kit_files_all(state):
    return sorted(path for path, item in state['files'].items()
                  if path.startswith('.skills/') and path.count('/') >= 2
                  and item.get('policy') == 'managed' and not item.get('canonical'))


def command_status(ctx):
    project = ctx.open()
    lock, state = read_lock(project), read_state(project)
    lock_present = (project / LOCK).exists()
    print(f'Skills in {project.name} (trace_skills.py status, contract {CONTRACT_VERSION})')
    if state is None:
        print('Install state: none here (every skill is the project\'s own unless the lock says otherwise; '
              'no agent copies are managed)')
    else:
        print(f"Install state: {state.get('version')}, agent {state.get('agent')}")
    print(f"Lock: {LOCK}, {len(lock['skills'])} entries" if lock_present else 'Lock: none')
    pending = None
    if (project / OPERATION).exists():
        journal = receipt_data(project, OPERATION)
        operation_id = journal.get('operation_id') if isinstance(journal, dict) else None
        print(f'Pending: operation {operation_id or OPERATION} did not finish; run {ctx.tool} recover')
        # AH-247: the name the journal covers is in flux, so its folder and its lock entry are not read.
        covered = journal.get('name') if isinstance(journal, dict) else None
        if operation_id and valid_name(covered):
            pending = covered
    print()
    for name in sorted(set(status_names(project, state, lock)) | ({pending} if pending else set())):
        print(name)
        if name == pending:
            print(f'  owner: pending (operation {operation_id} did not finish; run {ctx.tool} recover)')
            print()
            continue
        found = claims(project, state, lock, name)
        entry = lock['skills'].get(name)
        if len(found) > 1 and found[0] != 'beside':
            words = ', '.join(CLAIM_WORDS[claim].format(name=name) for claim in found)
            print(f'  owner: conflict ({words})')
        elif found == ['kit']:
            print('  owner: kit')
            print('  ' + folder_definition(project, name))
            print(shadow(name))
        elif found == ['project']:
            print(f'  owner: project (.skills/{name}/)')
            print('  ' + folder_definition(project, name))
            print(shadow(name))
        elif found == ['single']:
            print(f'  owner: project (single file .skills/{name}.md)')
            print('  definition 1: not applicable (a single-file skill, not a package)')
            print(shadow(name))
        elif found == ['external']:
            source = entry['source']
            print(f"  owner: external, from {source['repository']} {source['path']} at {source['commit'][:12]} "
                  f"(requested {source['requested_ref']})")
            copy_line, unchanged = copy_state(project, name, entry)
            print(f'  copy: {copy_line}')
            print(f"  detached: {'yes' if entry.get('detached') else 'no'}")
            print(f'  upstream: {upstream_state(project, entry, ctx.args.offline)}')
            print(f"  change notice: {change_notice(project, entry['adopted']['path'], unchanged)}")
            print('  ' + folder_definition(project, name))
            print(shadow(name))
            receipt = entry['adopted']['receipt']
            print(f"  receipt: {receipt}{'' if (project / bootstrap.recorded_path(project, receipt)).is_file() else ' (missing)'}")
        elif found[:1] == ['beside']:
            if found[1] == 'project':
                print(f'  owner: project (.skills/{name}/)')
                print('  ' + folder_definition(project, name))
            else:
                print(f'  owner: project (single file .skills/{name}.md)')
                print('  definition 1: not applicable (a single-file skill, not a package)')
            print(shadow(name))
            tree = (entry.get('digest') or {}).get('tree_sha256')
            digest = f'digest {tree[:12]} (attested)' if tree else 'digest not stated'
            print(f"  relies on: a personal skill of the same name, held by {entry['held_by']}; {digest}")
            print(f"  limitation: {entry['limitation']}")
            receipt = entry['receipt']
            print(f"  receipt: {receipt}{'' if (project / bootstrap.recorded_path(project, receipt)).is_file() else ' (missing)'}")
        elif found == ['outside']:
            tree = (entry.get('digest') or {}).get('tree_sha256')
            digest = f'digest {tree[:12]} (attested)' if tree else 'digest not stated'
            print(f"  owner: outside, held by {entry['held_by']}; {digest}")
            print(f"  limitation: {entry['limitation']}")
            receipt = entry['receipt']
            print(f"  receipt: {receipt}{'' if (project / bootstrap.recorded_path(project, receipt)).is_file() else ' (missing)'}")
        print()
    retired = sorted(name for name, entry in lock['skills'].items() if entry['manager'] == 'retired'
                     and name != pending)
    if not retired:
        print('Retired: none')
    else:
        print('Retired:')
        for name in retired:
            entry = lock['skills'][name]
            if entry['was'] == 'external':
                source, upstream = entry.get('source') or {}, entry.get('upstream') or {}
                kept = 'dropped' if upstream.get('dropped') else f"kept at {stored_path(project, upstream.get('snapshot'))}"
                print(f"  {name}: was adopted from {source.get('repository')} {source.get('path')} at "
                      f"{str(source.get('commit'))[:12]}; original {kept}; receipt {entry['receipt']}")
            else:
                print(f"  {name}: was declared as held by {entry.get('held_by')}; receipt {entry['receipt']}")
    print(LIMIT)
    print(PARITY_LIMIT)
    return 0


# The lifecycle after adoption: the shared readers (6.5 to 6.9) ----------------

def valid_name(name):
    """A skill id as 2.4 defines it; any other string names no lock entry and no folder."""
    return isinstance(name, str) and len(name) <= 64 and NAME_RE.fullmatch(name) is not None


def copied(data):
    return json.loads(json.dumps(data))


def latest_receipt(project, entry):
    """The latest adopt, update or accept-merged receipt of an adopted entry, or None when it is missing (4.7)."""
    data = receipt_data(project, entry['adopted']['receipt'])
    return data if isinstance(data, dict) else None


def receipt_files(receipt, part):
    """The manifest a receipt keeps under `copy` or `upstream`, or None when it holds no readable one."""
    section = receipt.get(part) if isinstance(receipt, dict) else None
    files = section.get('files') if isinstance(section, dict) else None
    if not isinstance(files, list):
        return None
    for item in files:
        if not (isinstance(item, dict) and isinstance(item.get('path'), str) and item.get('mode') in REGULAR
                and isinstance(item.get('size'), int) and isinstance(item.get('sha256'), str)):
            return None
    return files


def recorded_modes(receipt):
    """The copy's modes as the latest receipt records them, which Windows reads in place of an execute bit (2.1)."""
    return {item['path']: item['mode'] for item in receipt_files(receipt, 'copy') or []}


def original_members(project, entry, receipt):
    """The current upstream as members, each with its bytes when the snapshot still holds them unchanged.

    The manifest is the latest receipt's; without it, the snapshot is read
    from disk (4.7). Returns the members, whether the snapshot is here, and
    whether the manifest came from the receipt.
    """
    snapshot = stored_path(project, entry['upstream']['snapshot'])
    here = (project / snapshot).is_dir()
    files = receipt_files(receipt, 'upstream')
    if files is None:
        if not here:
            return [], False, False
        found = [member(item['path'], item['mode'], item['data']) for item in disk_entries(project / snapshot)
                 if item['mode'] in REGULAR and not is_litter(item['path'])]
        return found, True, False
    found = []
    for item in files:
        data = None
        path = bootstrap.confined(project, f"{snapshot}/{item['path']}")
        if here and path.is_file():
            data = path.read_bytes()
            if sha256(data) != item['sha256']:
                data = None
        found.append({'path': item['path'], 'mode': item['mode'], 'size': item['size'], 'sha256': item['sha256'],
                      'data': data})
    return found, here, True


def differences(before, after):
    """One line per member that differs, in path order: added, removed, changed or mode (6.5)."""
    old = {item['path']: item for item in before}
    new = {item['path']: item for item in after}
    lines = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            lines.append(f'added {path}')
        elif path not in new:
            lines.append(f'removed {path}')
        elif old[path]['sha256'] != new[path]['sha256']:
            lines.append(f'changed {path}')
        elif old[path]['mode'] != new[path]['mode']:
            lines.append(f'mode {path}')
    return lines


def text_lines(text):
    """The text split after each newline only, so no other line separator in package text splits a line."""
    parts = text.split('\n')
    return [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def unified(before, after, label_before, label_after):
    """A unified diff with three lines of context of two members' text, escaped as 6.2 prints it; [] unless both are text."""
    old = printable(before) if before is not None else None
    new = printable(after) if after is not None else None
    if old is None or new is None:
        return []
    return [line if line.endswith('\n') else line + '\n'
            for line in difflib.unified_diff(text_lines(old), text_lines(new), label_before, label_after, n=3)]


def changed_diffs(before, after, labels):
    """The unified diff of every changed member that is UTF-8 text in both, in path order (AH-256)."""
    old = {item['path']: item for item in before}
    new = {item['path']: item for item in after}
    lines = []
    for line in differences(before, after):
        kind, path = line.split(' ', 1)
        if kind == 'changed':
            lines += unified(old[path].get('data'), new[path].get('data'), f'{labels[0]}/{path}',
                             f'{labels[1]}/{path}')
    return lines


def print_difference(title, before, after, labels):
    """One difference list (6.5), with a unified diff under each changed member that is UTF-8 text in both."""
    print(title)
    lines = differences(before, after)
    if not lines:
        print('  none')
        return
    old = {item['path']: item for item in before}
    new = {item['path']: item for item in after}
    for line in lines:
        print('  ' + line)
        kind, path = line.split(' ', 1)
        if kind == 'changed':
            sys.stdout.write(''.join(unified(old[path].get('data'), new[path].get('data'),
                                             f'{labels[0]}/{path}', f'{labels[1]}/{path}')))


def snapshot_named_elsewhere(project, lock, name, snapshot):
    """True when another lock entry still names this original, so removing it would take another skill's record."""
    for other, entry in lock['skills'].items():
        upstream = entry.get('upstream') if other != name else None
        if isinstance(upstream, dict) and stored_path(project, upstream.get('snapshot')) == snapshot \
                and not upstream.get('dropped'):
            return True
    return False


def drop_snapshot(project, lock, name, entry, original, from_manifest, new_snapshot, writes):
    """Plan the removal of the old original once a new one replaces it; N24 for a file of it that was edited."""
    old = stored_path(project, entry['upstream']['snapshot'])
    if old == new_snapshot or snapshot_named_elsewhere(project, lock, name, old):
        return []
    if not from_manifest and original and not on_windows() and \
            package_digest(manifest(original)) != entry['upstream']['tree_sha256']:
        # Without the receipt's manifest the original can be checked only as a whole.
        raise Stop('N24', snapshot=old, path=old)
    removed = []
    for item in sorted(original, key=lambda item: item['path']):
        path = f"{old}/{item['path']}"
        current = file_hash(project, path)
        if current is None:
            continue
        if current != item['sha256']:
            raise Stop('N24', snapshot=old, path=path)
        writes.put('removed', path, None)
        removed.append(path)
    return removed


def gone_canonicals(state, copy_dir, keep, also=()):
    """Canonical paths under the copy whose generated copies go: the removed members, and any the state still names."""
    found = set(also)
    if state is not None:
        found |= {item['canonical'] for item in state['files'].values()
                  if isinstance(item, dict) and isinstance(item.get('canonical'), str)
                  and item['canonical'].startswith(copy_dir + '/')}
    return sorted(path for path in found if path not in keep)


def no_copies(state):
    """The receipt's agent_copies for an operation that writes no copy: the state's agent and its note (section 9)."""
    if state is None:
        return {'agent': None, 'written': [], 'kept': [], 'removed': [], 'note': NOTE_NO_STATE}
    agent = state.get('agent')
    return {'agent': agent, 'written': [], 'kept': [], 'removed': [],
            'note': NOTE_ADAPTER if agent in bootstrap.AGENTS else NOTE_NONE}


def copies_left(copies):
    """How many agent copies the skill has once the operation is done: those it writes and those it keeps."""
    return len(copies['written']) + len(copies.get('kept') or [])


def copies_text(copies):
    """The result line's agent copies: only those this operation wrote are counted as written (AH-241)."""
    if copies['note'] == NOTE_ADAPTER:
        return f"{len(copies['written'])} written for {copies['agent']}"
    if copies['note'] == NOTE_NONE:
        return 'none (agent none)'
    return 'none (no adapter state here)'


def copies_line(copies, name, removing=False):
    """The plan's line for the agent copies, as adopt words it."""
    if copies['note'] == NOTE_ADAPTER:
        root = f"{bootstrap.AGENTS[copies['agent']]}/{name}/"
        if removing:
            return f"    {len(copies['removed'])} agent copies removed under {root}, and their entries in {STATE}"
        removed = f", {len(copies['removed'])} removed" if copies['removed'] else ''
        return f"    {copies_left(copies)} agent copies under {root}{removed}"
    if copies['note'] == NOTE_NONE:
        return '    no agent copies (agent none)'
    return '    no agent copies (no adapter state here)'


def audience_line(audience):
    return (f"  audience: source {audience_form(audience['source'])}; project "
            f"{audience_form(audience['project'])}; {audience['comparison']}")


def update_audiences(ctx, lock, entry, fetched, observed=None):
    """The audiences of section 8 for update and accept-merged: a source declared earlier stands until a new one is given."""
    audience = audiences(ctx, lock, fetched, observed)
    standing = entry['source'].get('audience')
    if 'source' not in parse_audiences(ctx.args.audience) and isinstance(standing, dict) \
            and standing.get('declared') in RANK:
        audience['source']['declared'] = standing['declared']
        audience['source']['declared_by'] = standing.get('declared_by')
        audience['comparison'], audience['share_with_project']['required'] = \
            compare_audiences(audience['source'], audience['project'])
    if audience['share_with_project']['required'] and not ctx.args.share_with_project:
        raise Stop('A1', source=audience_form(audience['source']), project=audience_form(audience['project']))
    return audience


def declare_project_audience(ctx, lock_after, moment):
    declared = parse_audiences(ctx.args.audience)
    if 'project' in declared:
        lock_after['project_audience'] = {'declared': declared['project'], 'declared_by': ctx.by,
                                          'declared_at': stamp(moment)}


def unrecorded_parts(before, state):
    """The receipt fields an operation that brings in no package leaves empty (5.1)."""
    return {'source': None, 'upstream': None, 'previous': None, 'copy': None, 'definition': None, 'audience': None,
            'ownership': {'before': before, 'replaced_kit_copy': False, 'kit_files_removed': [],
                          'retired_skills_added': []},
            'agent_copies': no_copies(state)}


def candidate_for(candidate):
    found = candidate.get('for') if isinstance(candidate, dict) else None
    return found if isinstance(found, dict) else {}


def plain_confirmation(ctx):
    """C1 for a command that fetches nothing: there is no candidate to name, so the same command with --yes (6.0)."""
    def stop():
        raise Stop('C1n', command=ctx.same_command())
    return stop


def print_applied(ctx, first, final):
    """The lines after an update or a recorded merge: as adopt prints them after its first (6.5, 6.6)."""
    receipt = receipt_data(ctx.project, final.receipt_path) or {}
    intake = (receipt.get('records') or {}).get('intake') or {}
    print(first)
    print(f'  receipt: {final.receipt_path}')
    print(f"  intake: {intake.get('id')} in {INTAKE}")
    print(f"  agent copies: {copies_text(final.receipt['agent_copies'])}")
    print('Next: review and commit these files, then record the adoption against that commit:')
    print(f'  {ctx.tool} link {final.receipt_path} --commit <sha>')
    print('Nothing was committed or pushed.')


def lifecycle_entry(ctx, state, lock, name, operation):
    """The adopted entry update and its forms work on: N12 to N16 and N27; for update and accept-merged, N17 and N26."""
    project = ctx.project
    entry = lock['skills'].get(name) if valid_name(name) else None
    if entry is None:
        if valid_name(name) and kit_files(state, name):
            raise Stop('N13', name=name)
        if valid_name(name) and (project_files(project, name) or (project / '.skills' / f'{name}.md').is_file()):
            raise Stop('N14', name=name)
        raise Stop('N12', name=name)
    if entry['manager'] == 'retired':
        raise Stop('N16', name=name)
    if entry['manager'] == 'outside':
        if operation == 'update':
            raise Stop('N15', name=name)
        raise Stop('N27', name=name, manager=entry['manager'], operation=operation)
    if operation in ('update', 'accept-merged'):
        if entry.get('detached'):
            raise Stop('N17', name=name)
        if not bootstrap.confined(project, f'.skills/{name}/SKILL.md').is_file():
            raise Stop('N26', name=name)
    return entry


def renamed(exc, path, name):
    """N18 in place of P14: the package at the entry's path now names another skill."""
    found = exc.__context__
    if exc.ident == 'P' and isinstance(found, Refused) and found.ident == 'P14':
        return Stop('N18', path=path, x=found.fields['name'], name=name)
    return None


def update_package(ctx, entry):
    """The new upstream: the candidate's bytes, or a fetch of --ref or of the ref the lock records (N25, N18)."""
    args, project, name = ctx.args, ctx.project, ctx.args.name
    source = entry['source']
    try:
        if args.candidate is not None:
            return candidate_package(project, args.candidate, source['repository'], source['path'], args.ref)
        ref = args.ref if args.ref is not None else source['requested_ref']
        if args.ref is None and HEX40.fullmatch(ref):
            raise Stop('N25', name=name, c12=ref[:12])
        return fetch_package(project, source['repository'], source['path'], None if ref == 'HEAD' else ref)
    except Stop as exc:
        other = renamed(exc, source['path'], name)
        if other is not None:
            raise other from None
        raise


def previous(entry):
    return {'commit': entry['source']['commit'], 'tree_sha256': entry['upstream']['tree_sha256'],
            'adopted_tree_sha256': entry['adopted']['tree_sha256']}


def moved_entry(entry, fetched, audience, tracked_sha256, adopted_sha256, kept, receipt_path):
    """The adopted entry once update or accept-merged has moved it to a new upstream (4.2)."""
    package = fetched.package
    return {'manager': 'external',
            'source': dict(fetched.source(), audience=audience['source']),
            'project_audience': audience['project'],
            'upstream': {'tree_sha256': package.tree_sha256, 'tracked_sha256': tracked_sha256,
                         'snapshot': f'{BASES}/{package.tree_sha256}'},
            'adopted': {'path': entry['adopted']['path'], 'tree_sha256': adopted_sha256, 'receipt': receipt_path,
                        'kept_existing': kept},
            'detached': False,
            'replaced_kit_copy': bool(entry.get('replaced_kit_copy', False)),
            'receipts': list(entry['receipts']) + [receipt_path]}


# update (6.5) -------------------------------------------------------------------

def update_opening(fetched, entry, confirmed):
    """The opening clause of an update's intake row: what was printed, by which run, and the confirmation (10.1).

    A candidate that show or adopt wrote printed the whole package but not
    its difference from the current upstream, which this run printed; the
    clause says so rather than credit the earlier run with both (AH-231).
    """
    package = fetched.package
    size = f'{len(package.members)} files, {package.size} bytes'
    old = entry['source']['commit'][:12]
    candidate = fetched.candidate
    if candidate is None or candidate_for(candidate).get('operation') == 'accept-merged':
        return (f'the whole new package ({size}) and its difference from {old} were printed before writing and the '
                f'person confirmed it ({confirmed})')
    by = candidate.get('created_by') if candidate.get('created_by') in ('show', 'adopt', 'update') else 'show'
    if by == 'update':
        return (f'the whole new package ({size}) and its difference from {old} were printed by update before writing '
                f'and the person confirmed it ({confirmed})')
    return (f'the whole new package ({size}) was printed by {by}, and its difference from {old} by update, before '
            f'writing, and the person confirmed it ({confirmed})')


def plan_update(ctx, fetched, op_id, moment, confirmed=None, observed=None):
    """Every precondition of update from the name on, and every write, with the plan's text (6.5).

    The plan's `kind` is the path the update takes: `commit` (the package is
    unchanged, so only the new commit is recorded), `update` (a copy nobody
    adapted takes the new package), or `adapted` (nothing is planned; the
    three versions are printed and nothing is written).
    """
    project, args = ctx.project, ctx.args
    lock, state = read_lock(project), read_state(project)
    state_before = copied(state)
    name, package = args.name, fetched.package
    entry = lifecycle_entry(ctx, state, lock, name, 'update')
    receipt_before = latest_receipt(project, entry)
    original, here, from_manifest = original_members(project, entry, receipt_before)
    copy_dir = f'.skills/{name}'
    copy_members = disk_members(project, copy_dir, recorded_modes(receipt_before))
    plan = Plan('update', name, op_id, moment)
    plan.fetched, plan.entry, plan.original, plan.snapshot_here = fetched, entry, original, here
    plan.copy_members = copy_members
    plan.difference = differences(original, package.files)
    if package.tree_sha256 == entry['upstream']['tree_sha256']:
        plan.kind = 'commit'
    elif package_digest(manifest(copy_members)) == entry['upstream']['tracked_sha256']:
        plan.kind = 'update'
    else:
        plan.kind = 'adapted'
        return plan
    audience = update_audiences(ctx, lock, entry, fetched, observed)
    if not (project / TRACE_RECORD).is_file():
        raise Stop('E11', row='intake')
    plan.observed = {'source': audience['source']['observed'], 'project': audience['project']['observed']}
    writes = plan.writes = Writes(project)
    for item in copy_members:
        # The copy as the plan saw it: an edit before the lock is E14, even to a file the update leaves alone.
        path = f"{copy_dir}/{item['path']}"
        writes.observed[path] = file_hash(project, path)
    snapshot = f'{BASES}/{package.tree_sha256}'
    ignored = bootstrap.git_ignored(project, [f"{copy_dir}/{item['path']}" for item in package.members])
    tracked = [item for item in package.members if f"{copy_dir}/{item['path']}" not in ignored]
    removed = []
    if plan.kind == 'update':
        for item in package.members:
            # Every member carries its mode, so a mode-only change reaches the copy (AH-230).
            writes.put('copy', f"{copy_dir}/{item['path']}", item['data'], item['mode'])
        held = {item['path'] for item in package.members}
        for item in original:
            if item['path'] not in held:
                removed.append(f"{copy_dir}/{item['path']}")
                writes.put('copy', removed[-1], None)
        put_bases_note(project, writes)
        for item in package.members:
            writes.put('snapshot', f"{snapshot}/{item['path']}", item['data'], item['mode'])
        left, kept = tracked, False
    else:
        left, kept = copy_members, bool(entry['adopted'].get('kept_existing', False))
    canonical = {f"{copy_dir}/{item['path']}": (item['data'], item['mode']) for item in left}
    copies, edited = agent_copies(project, state, name, canonical, gone_canonicals(state, copy_dir, canonical, removed),
                                  writes)
    dropped = drop_snapshot(project, lock, name, entry, original, from_manifest, snapshot, writes)
    if state is not None and state != state_before:
        writes.put('state', STATE, bootstrap.encoded(state))
    tracked_sha256, adopted_sha256 = package_digest(manifest(tracked)), package_digest(manifest(left))
    lock_after = copied(lock)
    lock_after['skills'][name] = moved_entry(entry, fetched, audience, tracked_sha256, adopted_sha256, kept,
                                             plan.receipt_path)
    declare_project_audience(ctx, lock_after, moment)
    put_lock(writes, lock_after)
    receipt = receipt_base(ctx, plan, state, confirmed)
    receipt.update({
        'source': fetched.source(),
        'upstream': {'tree_sha256': package.tree_sha256, 'tracked_sha256': tracked_sha256, 'snapshot': snapshot,
                     'files': package.files, 'litter': package.litter},
        'previous': previous(entry),
        'copy': {'path': copy_dir, 'tree_sha256': adopted_sha256, 'kept_existing': kept, 'files': manifest(left)},
        'definition': package.states,
        'audience': audience,
        'ownership': {'before': 'external', 'replaced_kit_copy': False, 'kit_files_removed': [],
                      'retired_skills_added': []},
        'agent_copies': copies})
    deferred = [Stop('N23', name=name, path=edited) if edited else None]
    put_inventory(project, writes, lock_after, {plan.receipt_path: receipt}, deferred)
    must_track = [f'{copy_dir}/SKILL.md', LOCK, plan.receipt_path, f'{snapshot}/SKILL.md', INVENTORY]
    seal(plan, receipt, gate_writes(project, name, writes, must_track, {}, deferred))
    if confirmed is not None:
        decided = receipt['decided']
        plan.intake = intake_args(name, fetched.source(), package.tree_sha256, decided['by'],
                                  checked_text(update_opening(fetched, entry, confirmed), package.states,
                                               audience['comparison']),
                                  args.used_for, plan.receipt_path, decided['authority'])
    plan.staging = fetched.staging.as_posix()
    plan.lines = update_lines(ctx, plan, fetched, audience, copies, dropped)
    return plan


def update_lines(ctx, plan, fetched, audience, copies, dropped):
    package, name, entry = fetched.package, plan.name, plan.entry
    copy_dir = f'.skills/{name}'
    lines = [f"Plan: update {name} from {entry['source']['commit'][:12]} to {fetched.commit[:12]}",
             f'  from: {fetched.repository} {fetched.path} at {fetched.commit} (requested {fetched.requested_ref})',
             f'  package digest: {package.tree_sha256}',
             '  ' + definition_line(package.states),
             audience_line(audience)]
    if ctx.args.share_with_project:
        lines.append(f"  Shared with the project: by {ctx.by}; words: {ctx.args.words or 'not stated'}")
    lines.append('  writes, in this order:')
    if plan.kind == 'commit':
        lines.append(f'    nothing under {copy_dir}/ or {BASES}/: the package is unchanged, so only the new commit '
                     'is recorded')
    else:
        group = plan.writes.groups['copy']
        after, _, _ = plan.writes.modes()
        written = sum(1 for data in group.values() if data is not None)
        moded = sum(1 for path in after if path.startswith(copy_dir + '/') and path not in group)
        lines.append(f'    {written} file(s) written, {len(group) - written} removed and {moded} mode change(s) '
                     f'under {copy_dir}/')
        lines += bases_note_line(plan.writes)
        lines.append(f'    {len(package.members)} file(s) under {BASES}/{package.tree_sha256}/')
    lines.append(copies_line(copies, name))
    if dropped:
        lines.append(f"    {len(dropped)} file(s) of the old original removed from "
                     f"{stored_path(ctx.project, entry['upstream']['snapshot'])}/")
    if STATE in plan.writes.updates:
        lines.append(f'    {STATE}')
    lines += [f'    {LOCK}', f'    {INVENTORY} (the inventory section)',
              f'  then: an intake row in {INTAKE}, and last the receipt {plan.receipt_path}',
              '  not tracked by Git here: ' + (', '.join(plan.not_tracked) if plan.not_tracked else 'none')]
    return lines


def print_three_versions(ctx, plan, fetched, candidate):
    """The adapted copy's output (6.5): the three versions and their differences; nothing is written."""
    name, entry = plan.name, plan.entry
    copy_dir = f'.skills/{name}'
    print(f'{name} is adapted here; the tool does not merge. The three versions:')
    print('  original (what was adopted): ' + (stored_path(ctx.project, entry['upstream']['snapshot'])
                                               if plan.snapshot_here
                                               else "the receipt's manifest; the snapshot is not here"))
    print(f"  this project's copy: {copy_dir}/")
    print(f'  new upstream: {fetched.staged.as_posix()} ({fetched.repository} {fetched.path} at {fetched.commit[:12]})')
    ignored = bootstrap.git_ignored(ctx.project, [f"{copy_dir}/{item['path']}" for item in plan.original])
    tracked = [item for item in plan.original if f"{copy_dir}/{item['path']}" not in ignored]
    print_difference("From the original to this project's copy:", tracked, plan.copy_members, ('original', 'copy'))
    print_difference('From the original to the new upstream:', plan.original, fetched.package.members,
                     ('original', 'upstream'))
    print(f'Nothing was written. Merge by hand into {copy_dir}/, then record the merge: {ctx.tool} '
          + quoted(['update', name, '--accept-merged', '--candidate', candidate]))


def moved_path(project, plan, final):
    """The first path whose change under the lock moved the update to another path (E14)."""
    for path in sorted(plan.writes.observed):
        if file_hash(project, path) != plan.writes.observed[path]:
            return path
    before = {(item['path'], item['mode'], item['sha256']) for item in plan.copy_members}
    after = {(item['path'], item['mode'], item['sha256']) for item in final.copy_members}
    changed = sorted({item[0] for item in before ^ after})
    return f'.skills/{plan.name}/{changed[0]}' if changed else LOCK


def apply_update(ctx, fetched, entry, moment):
    project, name = ctx.project, ctx.args.name
    old = entry['source']['commit']
    plan = plan_update(ctx, fetched, free_operation_id(project, read_lock(project), moment, name, 'update'), moment)
    if plan.kind == 'adapted':
        # The person merges from the staged new upstream, so its folder and the candidate stay (3.3).
        print_three_versions(ctx, plan, fetched, write_candidate(fetched, 'update', 'accept-merged', old))
        return 1
    if fetched.candidate is not None and candidate_for(fetched.candidate).get('operation') != 'accept-merged':
        # The run that wrote the candidate printed the contents; the header and the manifest stand for them.
        print_manifest(fetched)
        print()
    else:
        print_package(fetched)
    print(f'Difference from {old[:12]}, the current upstream:')
    for line in plan.difference or ['none: the package is unchanged']:
        print('  ' + line)
    print()
    for line in plan.lines:
        print(line)
    print(LIMIT)

    def replan(confirmed):
        final_id = free_operation_id(project, read_lock(project), moment, name, 'update')
        final = plan_update(ctx, fetched, final_id, moment, confirmed, plan.observed)
        if final.kind != plan.kind:
            raise Stop('E14', path=moved_path(project, plan, final))
        return final

    def candidate_file():
        if fetched.candidate_file:
            return fetched.candidate_file
        return write_candidate(fetched, 'update', 'update', old)

    confirmed = confirm(ctx, candidate_file)
    final = execute(ctx, plan, replan, confirmed)
    print_applied(ctx, f'Updated {name} from {old[:12]} to {fetched.commit[:12]}.', final)
    return 0


# update --accept-merged (6.6) ---------------------------------------------------

def merged_copy(project, name, folder, recorded):
    """The person's merged copy read as a package (2.2, 2.4), its name still the entry's (N18)."""
    try:
        return adoptable(folder, name, copy_entries(project, folder, recorded))
    except Refused as exc:
        if exc.ident == 'P14':
            raise Stop('N18', path=folder, x=exc.fields['name'], name=name) from None
        raise Stop('P', path=folder, reason=exc.reason) from None


def merged_checked(fetched, decided, copy_sha256, states):
    """The intake row's --checked text for accept-merged: what this run and its predecessor did, never more (AH-231).

    The merge is credited to whoever --merged-by names, and to nobody when it
    names no one: the execution agent runs the tool, which says nothing about
    who merged (AH-238, AH-248).
    """
    candidate = fetched.candidate
    if candidate is not None and candidate.get('created_by') == 'update' \
            and candidate_for(candidate).get('operation') == 'accept-merged':
        opening = f"the three versions were printed by update on {candidate.get('created_at')}"
    else:
        opening = ("no update run preceded this; the new upstream's manifest and its difference from the merged copy "
                   'were printed by this command')
    merger, digest = decided.get('merged_by'), copy_sha256[:12]
    whose = (f"the merged copy, merged by {merger} under {decided['by']}'s direction, is recorded as it stands with "
             f'digest {digest}' if merger
             else f'the merged copy is recorded as it stands with digest {digest} (who merged it was not stated)')
    return (f'{opening}; {whose}; '
            f"the upstream original's files were re-checked against commit {fetched.commit[:12]}; "
            f"definition 1 of the merged copy: {states['format']}; {states['licence']}; {states['handoff']}")


def plan_merged(ctx, fetched, op_id, moment, confirmed=None, observed=None):
    """Every precondition of update --accept-merged, and every write; the copy is recorded and never changed (6.6)."""
    project, args = ctx.project, ctx.args
    lock, state = read_lock(project), read_state(project)
    state_before = copied(state)
    name, package = args.name, fetched.package
    entry = lifecycle_entry(ctx, state, lock, name, 'accept-merged')
    receipt_before = latest_receipt(project, entry)
    copy_dir = f'.skills/{name}'
    copy = merged_copy(project, name, copy_dir, recorded_modes(receipt_before))
    copy_sha256 = package_digest(copy.files)
    original, _, from_manifest = original_members(project, entry, receipt_before)
    audience = update_audiences(ctx, lock, entry, fetched, observed)
    if not (project / TRACE_RECORD).is_file():
        raise Stop('E11', row='intake')
    plan = Plan('accept-merged', name, op_id, moment)
    plan.fetched, plan.entry, plan.copy, plan.copy_sha256 = fetched, entry, copy, copy_sha256
    plan.observed = {'source': audience['source']['observed'], 'project': audience['project']['observed']}
    writes = plan.writes = Writes(project)
    recorded = {}
    for item in copy.members:
        path = f"{copy_dir}/{item['path']}"
        writes.keep(path, item['mode'])
        recorded[path] = item['data']
    snapshot = f'{BASES}/{package.tree_sha256}'
    put_bases_note(project, writes)
    for item in package.members:
        writes.put('snapshot', f"{snapshot}/{item['path']}", item['data'], item['mode'])
    ignored = bootstrap.git_ignored(project, [f"{copy_dir}/{item['path']}" for item in package.members])
    tracked = [item for item in package.members if f"{copy_dir}/{item['path']}" not in ignored]
    plan.upstream_tracked = tracked
    canonical = {f"{copy_dir}/{item['path']}": (item['data'], item['mode']) for item in copy.members}
    copies, edited = agent_copies(project, state, name, canonical, gone_canonicals(state, copy_dir, canonical), writes)
    dropped = drop_snapshot(project, lock, name, entry, original, from_manifest, snapshot, writes)
    if state is not None and state != state_before:
        writes.put('state', STATE, bootstrap.encoded(state))
    kept = bool(entry['adopted'].get('kept_existing', False))
    tracked_sha256 = package_digest(manifest(tracked))
    lock_after = copied(lock)
    lock_after['skills'][name] = moved_entry(entry, fetched, audience, tracked_sha256, copy_sha256, kept,
                                             plan.receipt_path)
    declare_project_audience(ctx, lock_after, moment)
    put_lock(writes, lock_after)
    receipt = receipt_base(ctx, plan, state, confirmed)
    # Whoever made the merge, as --merged-by names them; never the execution agent, which only runs the tool.
    merger = receipt['decided']['merged_by'] = args.merged_by or None
    receipt.update({
        'source': fetched.source(),
        'upstream': {'tree_sha256': package.tree_sha256, 'tracked_sha256': tracked_sha256, 'snapshot': snapshot,
                     'files': package.files, 'litter': package.litter},
        'previous': previous(entry),
        'copy': {'path': copy_dir, 'tree_sha256': copy_sha256, 'kept_existing': kept, 'files': copy.files},
        'definition': copy.states,
        'audience': audience,
        'ownership': {'before': 'external', 'replaced_kit_copy': False, 'kit_files_removed': [],
                      'retired_skills_added': []},
        'agent_copies': copies})
    deferred = [Stop('N23', name=name, path=edited) if edited else None]
    put_inventory(project, writes, lock_after, {plan.receipt_path: receipt}, deferred)
    must_track = [f'{copy_dir}/SKILL.md', LOCK, plan.receipt_path, f'{snapshot}/SKILL.md', INVENTORY]
    seal(plan, receipt, gate_writes(project, name, writes, must_track, recorded, deferred))
    if confirmed is not None:
        decided = receipt['decided']
        plan.intake = intake_args(name, fetched.source(), package.tree_sha256, decided['by'],
                                  merged_checked(fetched, decided, copy_sha256, copy.states),
                                  args.used_for, plan.receipt_path, decided['authority'])
    plan.staging = fetched.staging.as_posix()
    lines = [f'Plan: record the merged copy of {name} against {fetched.commit[:12]}',
             f'  from: {fetched.repository} {fetched.path} at {fetched.commit} (requested {fetched.requested_ref})',
             f'  package digest: {package.tree_sha256}',
             f'  merged copy: {copy_dir}/ is recorded as it stands and not changed, digest {copy_sha256}',
             f"  merged by: {merger or 'not stated'}",
             audience_line(audience)]
    if args.share_with_project:
        lines.append(f"  Shared with the project: by {ctx.by}; words: {args.words or 'not stated'}")
    lines += ['  writes, in this order:', *bases_note_line(writes),
              f'    {len(package.members)} file(s) under {snapshot}/',
              copies_line(copies, name) + (', made from the copy as it stands' if copies['note'] == NOTE_ADAPTER else '')]
    if dropped:
        lines.append(f"    {len(dropped)} file(s) of the old original removed from "
                     f"{stored_path(ctx.project, entry['upstream']['snapshot'])}/")
    if STATE in writes.updates:
        lines.append(f'    {STATE}')
    lines += [f'    {LOCK}', f'    {INVENTORY} (the inventory section)',
              f'  then: an intake row in {INTAKE}, and last the receipt {plan.receipt_path}',
              '  not tracked by Git here: ' + (', '.join(plan.not_tracked) if plan.not_tracked else 'none')]
    plan.lines = lines
    return plan


def apply_merged(ctx, fetched, entry, moment):
    project, name = ctx.project, ctx.args.name
    plan = plan_merged(ctx, fetched, free_operation_id(project, read_lock(project), moment, name, 'accept-merged'),
                       moment)
    print_manifest(fetched)
    print()
    print(f'Merged copy: .skills/{name}/, digest {plan.copy_sha256}')
    print_difference('From the new upstream to the merged copy:', plan.upstream_tracked, plan.copy.members,
                     ('upstream', 'copy'))
    states = plan.copy.states
    print(f"definition 1 of the merged copy: {states['format']}; {states['licence']}; {states['handoff']}")
    print()
    for line in plan.lines:
        print(line)
    print(LIMIT)

    def replan(confirmed):
        final_id = free_operation_id(project, read_lock(project), moment, name, 'accept-merged')
        return plan_merged(ctx, fetched, final_id, moment, confirmed, plan.observed)

    def candidate_file():
        if fetched.candidate_file:
            return fetched.candidate_file
        # This run printed no three versions, so the candidate is not one an adapted update wrote (AH-231).
        return write_candidate(fetched, 'update', 'update', entry['source']['commit'])

    confirmed = confirm(ctx, candidate_file)
    final = execute(ctx, plan, replan, confirmed)
    print_applied(ctx, f'Recorded the merged copy of {name} against {fetched.commit[:12]}.', final)
    return 0


def command_update(ctx):
    project = ctx.open()
    args = ctx.args
    check_writing_flags(ctx)
    if args.merged_by is not None and not args.accept_merged:
        raise Stop('E21')
    if args.detach or args.attach:
        return command_detach(ctx)
    parse_audiences(args.audience)
    if args.yes and args.candidate is None and not (args.ref and HEX40.fullmatch(args.ref)):
        raise Stop('C3')
    check_pending(ctx)
    lock, state = read_lock(project), read_state(project)
    entry = lifecycle_entry(ctx, state, lock, args.name, 'accept-merged' if args.accept_merged else 'update')
    fetched = update_package(ctx, entry)
    try:
        if fetched.commit == entry['source']['commit']:
            raise Done(f"Current: {args.name} is at {fetched.commit[:12]}, the commit {fetched.requested_ref} "
                       'resolves to; nothing to do.')
        if args.accept_merged:
            return apply_merged(ctx, fetched, entry, utc_now())
        return apply_update(ctx, fetched, entry, utc_now())
    except (Stop, Done, KeyboardInterrupt) as exc:
        # As adopt: a folder this run fetched goes, except the one C1 just pointed the person to.
        if ctx.pending is None and getattr(exc, 'ident', None) != 'C1':
            fetched.discard()
        raise


# update --detach and --attach (6.7) -------------------------------------------

def plan_detach(ctx, op_id, moment, confirmed=None):
    """The lock and the inventory with the entry detached or attached again; the copy is not touched (6.7)."""
    project, args = ctx.project, ctx.args
    lock, state = read_lock(project), read_state(project)
    name, attach = args.name, bool(args.attach)
    operation = 'attach' if attach else 'detach'
    entry = lifecycle_entry(ctx, state, lock, name, operation)
    if attach and not entry.get('detached'):
        raise Done(f'Already attached: {name}; nothing to do.')
    if not attach and entry.get('detached'):
        raise Done(f'Already detached: {name}; nothing to do.')
    plan = Plan(operation, name, op_id, moment)
    writes = plan.writes = Writes(project)
    after = copied(entry)
    if attach:
        after['detached'] = False
        after.pop('detached_at', None)
        after.pop('detached_by', None)
    else:
        after.update(detached=True, detached_at=stamp(moment), detached_by=ctx.by)
    after['receipts'] = list(entry['receipts']) + [plan.receipt_path]
    lock_after = copied(lock)
    lock_after['skills'][name] = after
    put_lock(writes, lock_after)
    receipt = receipt_base(ctx, plan, state, confirmed)
    receipt.update(unrecorded_parts('external', state))
    deferred = []
    put_inventory(project, writes, lock_after, {plan.receipt_path: receipt}, deferred)
    seal(plan, receipt, gate_writes(project, name, writes, [LOCK, plan.receipt_path, INVENTORY], {}, deferred))
    source = entry['source']
    if attach:
        lines = [f"Plan: attach {name}: updates are offered again from {source['repository']} {source['path']}.",
                 '  writes, in this order:', f'    {LOCK} (detached false; detached_at and detached_by removed)']
    else:
        lines = [f'Plan: detach {name}: later updates are not offered; status keeps reporting whether its source '
                 'moved.', '  writes, in this order:', f'    {LOCK} (detached, detached_at, detached_by)']
    plan.lines = lines + [f'    {INVENTORY} (the inventory section)', f'  then: the receipt {plan.receipt_path}']
    return plan


def command_detach(ctx):
    project, name = ctx.project, ctx.args.name
    operation = 'attach' if ctx.args.attach else 'detach'
    check_pending(ctx)
    lock, state = read_lock(project), read_state(project)
    lifecycle_entry(ctx, state, lock, name, operation)
    moment = utc_now()
    plan = plan_detach(ctx, free_operation_id(project, lock, moment, name, operation), moment)
    for line in plan.lines:
        print(line)
    confirmed = confirm(ctx, plain_confirmation(ctx))
    execute(ctx, plan, lambda confirmed: plan_detach(
        ctx, free_operation_id(project, read_lock(project), moment, name, operation), moment, confirmed), confirmed)
    print(f"{'Attached' if ctx.args.attach else 'Detached'} {name}. Nothing was committed or pushed.")
    return 0


# retire (6.8) -------------------------------------------------------------------

def unrecorded(project, paths):
    """The paths under these folders whose changes Git has not recorded, tracked or not, as git status names them."""
    run = subprocess.run(['git', '--no-optional-locks', 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                          '--', *paths], cwd=project, capture_output=True)
    if run.returncode:
        raise bootstrap.SetupError(first_line(run.stderr))
    found, records, index = set(), run.stdout.split(b'\0'), 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        found.add(record[3:].decode('utf-8', 'surrogateescape'))
        if record[:1] in (b'R', b'C'):
            index += 1  # a rename or copy names its source in the next record
    return sorted(found)


def files_under(project, folder):
    root = bootstrap.confined(project, folder)
    if not root.is_dir():
        return []
    return sorted(path.relative_to(project).as_posix() for path in root.rglob('*')
                  if path.is_file() or path.is_symlink())


def plan_retire(ctx, op_id, moment, confirmed=None):
    """Every precondition of retire and every write: the copy and its agent copies go, the original stays (6.8)."""
    project, args = ctx.project, ctx.args
    lock, state = read_lock(project), read_state(project)
    state_before = copied(state)
    name = args.name
    entry = lock['skills'].get(name) if valid_name(name) else None
    if entry is None:
        if valid_name(name) and kit_files(state, name):
            raise Stop('N13r', name=name)
        if valid_name(name) and (project_files(project, name) or (project / '.skills' / f'{name}.md').is_file()):
            raise Stop('N14', name=name)
        raise Stop('N12', name=name)
    if entry['manager'] == 'retired':
        raise Done(f'Already retired: {name}; nothing to do.')
    plan = Plan('retire', name, op_id, moment)
    writes = plan.writes = Writes(project)
    receipt = receipt_base(ctx, plan, state, confirmed)
    receipt.update(unrecorded_parts(entry['manager'], state))
    copy_dir, deferred, left, removed, dropped = f'.skills/{name}', [], [], [], []
    retired = {'manager': 'retired', 'was': entry['manager'], 'source': None, 'held_by': None, 'upstream': None,
               'retired_at': stamp(moment), 'receipt': plan.receipt_path,
               'receipts': list(entry['receipts']) + [plan.receipt_path]}
    if entry['manager'] == 'external':
        snapshot = stored_path(project, entry['upstream']['snapshot'])
        changed = unrecorded(project, [copy_dir, snapshot])
        if changed:
            raise Stop('N22', name=name, paths=', '.join(changed))
        receipt_before = latest_receipt(project, entry)
        on_disk = files_under(project, copy_dir)
        listed = [f"{copy_dir}/{item['path']}" for item in receipt_files(receipt_before, 'copy') or []]
        everything = sorted(set(on_disk) | set(listed))
        # Litter and the files Git ignores stay where they are; Git history keeps the rest once committed.
        litter = bootstrap.skill_litter(project, everything)
        ignored = bootstrap.git_ignored(project, on_disk)
        left = sorted(path for path in on_disk if path in ignored)
        for path in everything:
            if path not in litter:
                writes.put('copy', path, None)
        removed = sorted(writes.groups['copy'])
        copies, edited = agent_copies(project, state, name, {},
                                      gone_canonicals(state, copy_dir, {}, [path for path in everything
                                                                             if path not in litter]), writes)
        deferred.append(Stop('N23', name=name, path=edited) if edited else None)
        if args.drop_base:
            # AH-235: as for the copy, a file Git ignores under the original was never committed, so Git history
            # could not keep it; it stays. Every other file there is committed, since N22 found nothing unrecorded.
            in_snapshot = files_under(project, snapshot)
            ignored_there = bootstrap.git_ignored(project, in_snapshot)
            left = sorted(set(left) | {path for path in in_snapshot if path in ignored_there})
            for path in in_snapshot:
                if path not in ignored_there:
                    writes.put('removed', path, None)
            dropped = sorted(writes.groups['removed'])
        if state is not None and state != state_before:
            writes.put('state', STATE, bootstrap.encoded(state))
        source = entry['source']
        retired.update(source={key: source[key] for key in ('repository', 'path', 'requested_ref', 'commit')},
                       upstream={'tree_sha256': entry['upstream']['tree_sha256'], 'snapshot': snapshot,
                                 'dropped': bool(args.drop_base)})
        upstream_files = receipt_files(receipt_before, 'upstream')
        if upstream_files is None:
            original, _, _ = original_members(project, entry, None)
            upstream_files = manifest(original)
        litter_before = ((receipt_before or {}).get('upstream') or {}).get('litter')
        receipt.update(source=retired['source'], agent_copies=copies,
                       upstream={'tree_sha256': entry['upstream']['tree_sha256'],
                                 'tracked_sha256': entry['upstream']['tracked_sha256'], 'snapshot': snapshot,
                                 'files': upstream_files,
                                 'litter': litter_before if isinstance(litter_before, list) else []})
    else:
        retired['held_by'] = entry['held_by']
    lock_after = copied(lock)
    lock_after['skills'][name] = retired
    put_lock(writes, lock_after)
    receipt['history'] = RETIRE_HISTORY
    # The files Git ignores that the removal leaves where they are (6.8, AH-235); retire only, beside history.
    receipt['left_in_place'] = left
    receipt['limits'] = list(RECEIPT_LIMITS) + [RETIRE_LIMIT]
    put_inventory(project, writes, lock_after, {plan.receipt_path: receipt}, deferred)
    seal(plan, receipt, gate_writes(project, name, writes, [LOCK, plan.receipt_path, INVENTORY], {}, deferred))
    lines = [f'Plan: retire {name}', '  writes, in this order:']
    if entry['manager'] == 'external':
        lines += [f'    {len(removed)} file(s) removed under {copy_dir}/', copies_line(receipt['agent_copies'], name, True)]
        if args.drop_base:
            lines.append(f"    {len(dropped)} file(s) of the original removed under {snapshot}/ (--drop-base)")
        else:
            lines.append(f"    the original under {snapshot}/ is kept")
    if STATE in writes.updates:
        lines.append(f'    {STATE}')
    lines += [f'    {LOCK} (the entry becomes retired)', f'    {INVENTORY} (the inventory section)',
              f'  then: the receipt {plan.receipt_path}; no intake row and no transition row']
    if entry['manager'] == 'external':
        folders = f".skills/{name}/" + (f" and {snapshot}/" if args.drop_base else '')
        lines.append('Git history keeps every removed file once the removal is committed; files Git ignores under '
                     f"{folders} are left in place: {', '.join(left) if left else 'none'}")
    plan.lines = lines
    return plan


def command_retire(ctx):
    project = ctx.open()
    args = ctx.args
    check_writing_flags(ctx)
    check_pending(ctx)
    lock = read_lock(project)
    read_state(project)
    if not valid_name(args.name):
        raise Stop('N12', name=args.name)
    moment = utc_now()
    plan = plan_retire(ctx, free_operation_id(project, lock, moment, args.name, 'retire'), moment)
    for line in plan.lines:
        print(line)
    confirmed = confirm(ctx, plain_confirmation(ctx))
    final = execute(ctx, plan, lambda confirmed: plan_retire(
        ctx, free_operation_id(project, read_lock(project), moment, args.name, 'retire'), moment, confirmed),
        confirmed)
    print(f'Retired {args.name}.')
    upstream = final.receipt.get('upstream')
    if upstream is not None:
        print('Original dropped; Git history keeps it.' if args.drop_base else f"Original kept at {upstream['snapshot']}.")
    print('Nothing was committed or pushed.')
    return 0


# declare (6.9) ------------------------------------------------------------------

def home_form(folder):
    """A folder as the lock records it: under the home folder written with ~, so no home path is copied (rule 4)."""
    resolved, home = Path(folder).expanduser().resolve(), Path.home().resolve()
    if resolved == home:
        return '~'
    if home in resolved.parents:
        return '~/' + resolved.relative_to(home).as_posix()
    return resolved.as_posix()


def plan_declare(ctx, op_id, moment, confirmed=None):
    """Every precondition of declare and its two writes, the lock and the inventory; nothing is copied (6.9)."""
    project, args = ctx.project, ctx.args
    lock, state = read_lock(project), read_state(project)
    name, holder = args.name, args.held_by
    if not valid_name(name):
        raise Stop('N19', name=name)
    entry = lock['skills'].get(name)
    if entry is not None and entry['manager'] == 'external':
        raise Stop('N28', name=name, owner='external')
    if kit_files(state, name):
        raise Stop('N28', name=name, owner='kit')
    beside = bool(getattr(args, 'beside_project_skill', False))
    owned = bool(project_files(project, name)) or (project / '.skills' / f'{name}.md').is_file()
    if owned and not beside:
        raise Stop('N28', advice=N28_PROJECT, name=name, owner='project')
    if beside and not owned:
        raise Stop('N29', name=name)
    if entry is not None and entry['manager'] == 'outside' and entry['held_by'] != holder:
        raise Stop('N4', name=name, held_by=entry['held_by'], tool=ctx.tool)
    path, digest = None, {'tree_sha256': None, 'basis': 'not stated'}
    if args.path is not None:
        folder = Path(args.path).expanduser()
        if not folder.is_dir() or not (folder / 'SKILL.md').is_file():
            raise Stop('N21', path=args.path)
        try:
            held = adoptable(args.path, name, disk_entries(folder))
        except Refused as exc:
            if exc.ident == 'P14':
                raise Stop('N20', path=args.path, x=exc.fields['name'], name=name) from None
            raise Stop('P', path=args.path, reason=exc.reason) from None
        # The holder's report, made on their machine: nobody in the project observed it (4.3).
        path, digest = home_form(folder), {'tree_sha256': held.tree_sha256, 'basis': 'attested'}
    limitation = args.limitation if args.limitation else DEFAULT_LIMITATION
    if entry is not None and entry['manager'] == 'outside':
        if ((entry.get('path'), entry.get('digest'), entry.get('limitation'), bool(entry.get('beside_project')))
                == (path, digest, limitation, beside)):
            raise Done(f'Already declared: {name}; nothing to do.')
        raise Stop('N4', name=name, held_by=entry['held_by'], tool=ctx.tool)
    plan = Plan('declare', name, op_id, moment)
    writes = plan.writes = Writes(project)
    # A retired entry's receipts carry forward to the declaration that replaces it, as for an adoption (4.4).
    receipts = list(entry['receipts']) if entry is not None and entry['manager'] == 'retired' else []
    lock_after = copied(lock)
    lock_after['skills'][name] = {'manager': 'outside', 'held_by': holder, 'path': path, 'digest': digest,
                                  'limitation': limitation, 'declared_by': ctx.by, 'receipt': plan.receipt_path,
                                  'receipts': receipts + [plan.receipt_path]}
    if beside:
        lock_after['skills'][name]['beside_project'] = True
    put_lock(writes, lock_after)
    receipt = receipt_base(ctx, plan, state, confirmed)
    receipt.update(unrecorded_parts('retired' if entry is not None else 'project' if beside else 'free', state))
    deferred = []
    put_inventory(project, writes, lock_after, {plan.receipt_path: receipt}, deferred)
    seal(plan, receipt, gate_writes(project, name, writes, [LOCK, plan.receipt_path, INVENTORY], {}, deferred))
    plan.lines = [f'Plan: declare {name} as held outside the project by {holder}',
                  f"  path: {path if path is not None else 'not stated'}",
                  f"  digest: {digest['tree_sha256']} (attested)" if digest['tree_sha256'] else '  digest not stated',
                  f'  limitation: {limitation}']
    if beside:
        own = f'.skills/{name}/' if project_files(project, name) else f'.skills/{name}.md'
        plan.lines.append(f"  beside: the project's own {own} stays as it is and relies on this personal skill; "
                          'nothing of it is read or changed')
    plan.lines += ['  writes, in this order:', f'    {LOCK}', f'    {INVENTORY} (the inventory section)',
                  f'  then: the receipt {plan.receipt_path}; no intake row, since nothing enters the project, '
                  'and no agent copies']
    return plan


def command_declare(ctx):
    project = ctx.open()
    args = ctx.args
    check_writing_flags(ctx)
    check_pending(ctx)
    lock = read_lock(project)
    read_state(project)
    if not valid_name(args.name):
        raise Stop('N19', name=args.name)
    moment = utc_now()
    plan = plan_declare(ctx, free_operation_id(project, lock, moment, args.name, 'declare'), moment)
    for line in plan.lines:
        print(line)
    confirmed = confirm(ctx, plain_confirmation(ctx))
    execute(ctx, plan, lambda confirmed: plan_declare(
        ctx, free_operation_id(project, read_lock(project), moment, args.name, 'declare'), moment, confirmed),
        confirmed)
    print(f'Declared {args.name}, held by {args.held_by}. Nothing was committed or pushed.')
    return 0


COMMANDS = {'list': command_list, 'show': command_show, 'adopt': command_adopt, 'status': command_status,
            'link': command_link, 'recover': command_recover,
            'update': command_update, 'retire': command_retire, 'declare': command_declare}


def build_parser():
    parser = argparse.ArgumentParser(prog='trace_skills.py', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n\n'.join(__doc__.split('\n\n')[1:]), allow_abbrev=False)
    parser.add_argument('--project', help="the project's root (default: the folder above this script's scripts/)")
    commands = parser.add_subparsers(dest='command', required=True)

    def command(name, text):
        sub = commands.add_parser(name, help=text, description=text, allow_abbrev=False)
        sub.add_argument('--project', default=argparse.SUPPRESS, help="the project's root")
        return sub

    def fetching(sub):
        sub.add_argument('--ref', help='a branch, a tag, a full ref or a full commit id (default: HEAD)')

    def bringing(sub):
        sub.add_argument('--candidate', help='apply exactly the bytes a candidate file names')
        sub.add_argument('--audience', action='append',
                         help='source=VALUE or project=VALUE, VALUE private, collaborators or public (repeatable)')
        sub.add_argument('--share-with-project', action='store_true',
                         help='the person agreed to copy a source not known to be public here; give their words')
        sub.add_argument('--used-for', help='what the skill will be used for, for the intake row')

    def writing(sub):
        sub.add_argument('--yes', action='store_true', help='the person has seen the plan and agreed; apply')
        sub.add_argument('--by', help='who directed the operation (default: git config user.name)')
        sub.add_argument('--words', help="the person's words directing or agreeing to it, verbatim")
        sub.add_argument('--authority', help='what authorized it: a turn id in a trace record, or a standing instruction')
        sub.add_argument('--execution-agent', help='the model id of an agent running this tool')

    sub = command('list', 'print the packages a source holds (reads only)')
    sub.add_argument('repository')
    fetching(sub)
    sub = command('show', 'print every file of one package and write a candidate file')
    sub.add_argument('repository')
    sub.add_argument('path')
    fetching(sub)
    sub = command('adopt', 'bring one package into this project, after a yes')
    sub.add_argument('repository')
    sub.add_argument('path')
    fetching(sub)
    bringing(sub)
    sub.add_argument('--replace-kit-copy', action='store_true', help="remove the kit's unedited copy of this name")
    sub.add_argument('--keep-existing', action='store_true',
                     help='keep the existing .skills/<name>/ and record this package as its original')
    writing(sub)
    sub = command('status', 'say where every skill stands (reads only)')
    sub.add_argument('--offline', action='store_true', help='do not fetch any source')
    sub = command('update', 'review a new upstream of an adopted skill like a first adoption, and apply it after a '
                            'yes; an adapted copy gets the three versions and nothing is written')
    sub.add_argument('name')
    sub.add_argument('--ref', help='the branch, tag, full ref or full commit id to update to (default: the ref the '
                                   'lock records)')
    bringing(sub)
    forms = sub.add_mutually_exclusive_group()
    forms.add_argument('--accept-merged', action='store_true',
                       help='record a merge made by hand in .skills/<name>/ against the new upstream; the copy is '
                            'not changed')
    forms.add_argument('--detach', action='store_true',
                       help='stop offering updates; status keeps reporting whether the source moved')
    forms.add_argument('--attach', action='store_true', help='offer updates again after --detach')
    sub.add_argument('--merged-by', help='with --accept-merged: who made the merge, a person or a model id (the '
                                         'execution agent is never taken to be the merger)')
    writing(sub)
    sub = command('retire', 'remove an adopted skill\'s copy and agent copies, or end a declaration; the original '
                            'and the receipts stay')
    sub.add_argument('name')
    sub.add_argument('--drop-base', action='store_true',
                     help='remove the untouched original too; Git history keeps it once the removal is committed')
    writing(sub)
    sub = command('declare', 'record a skill a person runs from their own setup on this project; nothing is copied')
    sub.add_argument('name')
    sub.add_argument('--held-by', required=True, help='who holds the skill')
    sub.add_argument('--path', help='the folder holding it, read to record its digest as attested')
    sub.add_argument('--limitation', help='what co-authors cannot check (default: a fixed sentence)')
    sub.add_argument('--beside-project-skill', action='store_true',
                     help="the project's own skill of this name stays, and relies on this personal skill too")
    writing(sub)
    sub = command('link', 'record the default-adoption transition for one receipt, after the commit')
    sub.add_argument('receipt')
    sub.add_argument('--commit', required=True)
    command('recover', 'finish or undo an interrupted operation')
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or later is required')
    ctx = Context(args, argv)
    try:
        return COMMANDS[args.command](ctx)
    except Stop as exc:
        if exc.stdout:
            print(str(exc))
        else:
            print('trace-skills: ' + str(exc), file=sys.stderr)
        return exc.code
    except Done as exc:
        print(str(exc))
        return 0
    except KeyboardInterrupt as exc:
        signum = getattr(exc, 'signum', signal.SIGINT)
        if ctx.pending is not None:
            print('trace-skills: ' + CATALOGUE['E16'][0].format(tool=ctx.tool, operation_id=ctx.pending),
                  file=sys.stderr)
        else:
            print('trace-skills: interrupted; nothing was written', file=sys.stderr)
        return 128 + int(signum)
    except PermissionError as exc:
        print('trace-skills: ' + CATALOGUE['E13'][0].format(error=exc) + '; ' + PERMISSION_ADVICE, file=sys.stderr)
        return 2
    except bootstrap.SetupError as exc:
        print('trace-skills: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
