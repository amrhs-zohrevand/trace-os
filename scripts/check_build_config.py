#!/usr/bin/env python3
"""Check document build configuration; do not build a PDF or require TeX."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def read_utf8(path, root, issues):
    """The file's text, or None with the file named: no byte is replaced or guessed."""
    data = path.read_bytes()
    try:
        return data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeDecodeError as exc:
        issues.append(f'{path.relative_to(root)}: not UTF-8 (byte 0x{data[exc.start]:02x} '
                      f'at offset {exc.start}), so it was not checked')
        return None


def problems(root=ROOT):
    issues, documents = [], []
    for directory in ('paper', 'presentations'):
        for path in sorted((root / directory).rglob('*.tex')):
            if 'build' in path.relative_to(root).parts:
                continue
            text = read_utf8(path, root, issues)
            if text is None:
                continue
            if re.search(r'^%\s*!\s*TEX\s+(?:TS-)?program\s*=', text, re.M | re.I):
                issues.append(f'{path.relative_to(root)}: engine belongs in .latexmkrc')
            if not re.search(r'^\s*\\documentclass', text, re.M):
                continue
            documents.append(path)
            rc = path.parent / '.latexmkrc'
            if not rc.is_file():
                issues.append(f'{path.relative_to(root)}: missing .latexmkrc')
                continue
            config = read_utf8(rc, root, issues)
            if config is None:
                continue
            if not re.search(r"^\$out_dir\s*=\s*'build'\s*;", config, re.M):
                issues.append(f'{rc.relative_to(root)}: output must be build/')
            if not re.search(r'^\$pdf_mode\s*=\s*[15]\s*;', config, re.M):
                issues.append(f'{rc.relative_to(root)}: select pdflatex (1) or xelatex (5)')
            if path.with_suffix('.pdf').exists():
                issues.append(f'{path.relative_to(root)}: PDF exists beside source')
    return documents, issues


def main():
    documents, issues = problems()
    for issue in issues:
        print(issue)
    print(f'{len(documents)} document build configurations checked; no PDF was built')
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())
