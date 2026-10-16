#!/usr/bin/env python3
"""Check the selected agent's installed skill copies without reading global content."""
import json
from pathlib import Path
import sys
from bootstrap import confined, SetupError, AGENTS, STATE, in_layout, skill_files, skill_litter
ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    # .trace-os/install.json, or the same file in an earlier folder (.traceware/, .trace-kit/) not yet moved.
    state = json.loads(confined(root, in_layout(root, STATE)).read_text(encoding='utf-8'))
    problems, checked = [], 0
    shipped = {name for name, item in state['files'].items()
               if name.startswith('.skills/') and not item.get('canonical')}
    # AH-172: an entry an earlier sync made for an operating-system file or a file
    # Git ignores is not a skill; the next --sync-skills drops it.
    litter = skill_litter(root, sorted({item['canonical'] for item in state['files'].values()
                                        if item.get('canonical')}), shipped)
    for name, item in state['files'].items():
        if item.get('canonical') and item['canonical'] not in litter:
            checked += 1
            source, target = confined(root, item['canonical']), confined(root, name)
            if not source.is_file():
                problems.append('canonical skill removed; run scripts/bootstrap.py --sync-skills '
                                'to retire its copy: ' + item['canonical'])
            elif not target.is_file() or source.read_bytes() != target.read_bytes():
                problems.append(name)
    if state['agent'] in AGENTS:
        for source in skill_files(root, shipped):
            name = AGENTS[state['agent']] + '/' + source[len('.skills/'):]
            if name not in state['files']:
                problems.append('uninstalled canonical skill: ' + source)
    # AH-208: with every shipped skill retired, and none of the project's own, no
    # installed skill is the intended set rather than a missing installation.
    if state['agent'] != 'none' and checked == 0 and not state.get('retired_skills'):
        problems.append('no installed skills recorded for the selected agent')
    return checked, problems


def main():
    try:
        count, problems = check()
        print(json.dumps({'checked': count, 'mismatched': problems,
                          'limit': 'File parity only; native discovery and global overrides are not tested.'}))
        return 1 if problems else 0
    except (OSError, ValueError, KeyError, SetupError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
