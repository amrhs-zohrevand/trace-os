#!/usr/bin/env python3
"""Report whether each project skill states its handoff to the trace record.

A skill is a markdown procedure an agent follows. TRACE governs how a method's
work is recorded, not whether the method is valid, so the one thing a skill owes
the record is a handoff step: a check row whose `procedure` names the skill.
A skill without one runs fine, produces its artifact, and leaves a session in
which nothing was checked. That failure was found once, by a person reading the
two shipped example methods before 0.1.0-rc5, and nothing mechanical reported
it. This reports it.

Three literal signals are read off each skill's text:

    contract named               the text names SKILL-RUN-CONTRACT
    record mechanism named       the text names trace_record or checks.jsonl
    procedure set to its own id  one line carries the backticked word
                                 `procedure` and the skill's own backticked id

All three present is `handoff stated`. Anything else is `handoff not stated`,
parallel to the kit's `not reviewed`, with the absent signals named. The signals
are matched on those distinctive stems rather than on a full path, so a relative
or bare reference to the contract still counts.

Both layouts are read: `.skills/<id>/SKILL.md`, and `.skills/<id>.md` where a
project keeps its skills as single files.

The limit is printed with the result and is not a detail. This check reads text.
It cannot tell whether a stated handoff was followed. That is established per
run by the check row's `procedure_version`, the digest of the skill file in
force, which `trace_record.py verify` compares against the digest the same
record kept.

Informational: exit 0 whether or not a skill states its handoff, because a
skill that states none is a state to report and not a reason to refuse
anything. Exit 2 only when the tree cannot be read.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

CONTRACT = 'SKILL-RUN-CONTRACT'
RECORD_MECHANISMS = ('trace_record', 'checks.jsonl')
CONTRACT_NAMED = 'contract named'
RECORD_NAMED = 'record mechanism named'
PROCEDURE_NAMED = 'procedure set to its own id'
SIGNALS = (CONTRACT_NAMED, RECORD_NAMED, PROCEDURE_NAMED)
STATED = 'handoff stated'
NOT_STATED = 'handoff not stated'
LIMIT = ('This check reads the skill text. It does not establish that a stated '
         'handoff was followed, which is established per run by the check row\'s '
         'procedure_version digest of the skill file in force.')


def skill_files(skills: Path) -> list:
    """Every skill file under both layouts, with the id each one claims.

    A directory's name is the id in the `<id>/SKILL.md` layout; the file's stem
    is the id in the single-file layout. A project holding both shapes for one
    id gets one entry per file, because each file is a text an agent may follow.
    """
    found = [(path.parent.name, path) for path in skills.glob('*/SKILL.md')]
    found += [(path.stem, path) for path in skills.glob('*.md')]
    return sorted(found, key=lambda item: (item[0], item[1].name))


def absent_signals(text: str, skill_id: str) -> list:
    """Which of the three signals the text does not carry, in signal order."""
    absent = []
    if CONTRACT not in text:
        absent.append(CONTRACT_NAMED)
    if not any(name in text for name in RECORD_MECHANISMS):
        absent.append(RECORD_NAMED)
    own_id = '`' + skill_id + '`'
    if not any('`procedure`' in line and own_id in line for line in text.splitlines()):
        absent.append(PROCEDURE_NAMED)
    return absent


def report(skills=None) -> dict:
    """The state of every skill found, and the states' own limit."""
    skills = Path(skills) if skills is not None else ROOT / '.skills'
    base = skills.parent
    result = {'skills_dir': name_under(skills, base), 'applicable': True,
              'checked': 0, 'handoff_stated': 0, 'handoff_not_stated': 0,
              'states': [], 'signals': list(SIGNALS), 'limit': LIMIT}
    if not skills.is_dir():
        result.update(applicable=False,
                      skip_reasons=[f'no {skills.name} directory; there are no '
                                    'project skills to read'])
        return result
    for skill_id, path in skill_files(skills):
        absent = absent_signals(path.read_text(encoding='utf-8'), skill_id)
        result['states'].append({'skill': skill_id, 'path': name_under(path, base),
                                 'state': NOT_STATED if absent else STATED,
                                 'absent': absent})
    result['checked'] = len(result['states'])
    result['handoff_stated'] = sum(1 for s in result['states'] if s['state'] == STATED)
    result['handoff_not_stated'] = result['checked'] - result['handoff_stated']
    return result


def name_under(path: Path, base: Path) -> str:
    """A path as written under the tree that was read, never an absolute path."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--skills', default=None,
                        help='the skills directory to read (default: .skills/ '
                             'beside this script\'s project root)')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(report(args.skills)))
    except (OSError, UnicodeDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
