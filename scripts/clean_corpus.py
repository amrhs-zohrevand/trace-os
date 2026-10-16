#!/usr/bin/env python3
"""Gate every document in the literature folder: is its text a real, sectioned article body?

Retrieval does not guarantee full text. A fetcher can save a landing page, an
abstract page or a paywall notice under the paper's name, and a later reading
of that file misattributes what the paper says. This script checks every
document rather than a sample.

For each key (the basename of a PDF, or of a Docling markdown when one
exists) it picks the best source (Docling markdown, then the PDF through
pdftotext, then an HTML file with its tags stripped), cleans it, scans for the
canonical sections, inserts "===== SECTION: X =====" markers, and records
one row per key in workspace/shared/papers/clean-corpus-manifest.csv:

  GOOD      at least 3000 body words and at least 3 core sections
  PARTIAL   a body, but few sections found
  JUNK      a landing page, an abstract only, or under 1500 words
  MISSING   no usable source

Only a GOOD or PARTIAL key gets workspace/shared/papers/clean/<key>.txt. A
JUNK or MISSING key gets none, and a clean file an earlier run or
docling_extract_corpus.py left for it is removed, so a landing page never
stands as the paper's clean text. The attach and index steps read this
manifest and use a key's clean text only when its status is GOOD or PARTIAL.

The JUNK and MISSING rows are what still needs fetching or a manual download.
A GOOD row is a word-count and heading test, not a reading; it says a
sectioned body is present, not that it is the right paper.

Usage:
  python3 scripts/clean_corpus.py
  python3 scripts/clean_corpus.py --pdfs DIR --quarantine DIR2
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402
from text_clean import deep_clean  # noqa: E402

PDFS = Z.PAPERS / "pdfs"
QUARANTINE = Z.PAPERS / "pdfs-quarantine"
DOCLING_MD = Z.PAPERS / "docling-md"
OUT = Z.PAPERS / "clean"
MANIFEST = Z.CLEAN_MANIFEST

SECTIONS = [
    ("abstract", re.compile(r"^\s*abstract\b", re.I)),
    ("introduction", re.compile(r"^\s*(1\.?\s+)?introduction\b", re.I)),
    ("background", re.compile(r"^\s*(2\.?\s+)?(theoretical background|background|literature review|related work|conceptual development|theory and hypotheses)\b", re.I)),
    ("methods", re.compile(r"^\s*(\d\.?\s+)?(method|methods|methodology|materials and methods|research design|data and method|empirical setting|empirical strategy|research methodology|study design)\b", re.I)),
    ("results", re.compile(r"^\s*(\d\.?\s+)?(results|findings|empirical results|analysis and results)\b", re.I)),
    ("discussion", re.compile(r"^\s*(\d\.?\s+)?(discussion|general discussion)\b", re.I)),
    ("conclusion", re.compile(r"^\s*(\d\.?\s+)?(conclusion|concluding remarks|concluding|implications and conclusion)\b", re.I)),
    ("references", re.compile(r"^\s*(references|bibliography|works cited)\b", re.I)),
]
CORE = {"introduction", "methods", "results", "discussion", "conclusion"}
LANDING = ["purchase pdf", "get access", "does not have access", "please sign in", "sign in to access",
           "verify you are human", "subscribe to read", "to access this article", "article is available to"]


def pdftext(p: Path) -> str:
    try:
        return subprocess.run(["pdftotext", "-q", str(p), "-"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def html_text(p: Path) -> str:
    """The visible text of an HTML file, tags and scripts stripped. A landing page still reads as one."""
    raw = p.read_text(encoding="utf-8", errors="ignore")
    raw = re.sub(r"<(script|style|nav|header|footer)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def best_source(key: str, pdfs: Path, quarantine: Path, docling_md: Path) -> tuple[str, str]:
    md = docling_md / f"{key}.md"
    if md.is_file():
        t = md.read_text(encoding="utf-8", errors="ignore")
        if len(t.split()) > 200:
            return t, "docling"
    for base, tag in ((pdfs, "pdf"), (quarantine, "quarantine-pdf")):
        p = base / f"{key}.pdf"
        if p.is_file():
            t = pdftext(p)
            if len(t.split()) > 200:
                return t, tag
    for base, tag in ((pdfs, "html"), (quarantine, "quarantine-html")):
        p = base / f"{key}.html"
        if p.is_file():
            t = html_text(p)
            if len(t.split()) > 200:
                return t, tag
    return "", "none"


def mark_sections(text: str) -> tuple[str, set]:
    found, out = set(), []
    for line in text.splitlines():
        s = re.sub(r"^#{1,6}\s*", "", line.strip()).strip()
        hit = None
        if 0 < len(s) < 60:
            for name, rx in SECTIONS:
                if rx.match(s):
                    hit = name
                    break
        if hit:
            found.add(hit)
            out.append(f"\n===== SECTION: {hit.upper()} =====")
        out.append(line)
    return "\n".join(out), found


def classify(text: str, found: set) -> str:
    words = len(text.split())
    landing = any(m in text.lower()[:5000] for m in LANDING)
    if words < 200:
        return "MISSING"
    if landing or words < 1500:
        return "JUNK"
    if words >= 3000 and len(found & CORE) >= 3:
        return "GOOD"
    return "PARTIAL"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pdfs", type=Path, default=PDFS)
    ap.add_argument("--quarantine", type=Path, default=QUARANTINE)
    ap.add_argument("--docling-md", type=Path, default=DOCLING_MD)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    args = ap.parse_args(argv)
    keys = set()
    for base in (args.pdfs, args.quarantine):
        if base.is_dir():
            keys.update(p.stem for p in base.iterdir() if p.suffix.lower() in (".pdf", ".html"))
    if args.docling_md.is_dir():
        keys.update(p.stem for p in args.docling_md.glob("*.md"))
    if not keys:
        print(f"no documents under {Z.rel(args.pdfs)}, {Z.rel(args.quarantine)} or {Z.rel(args.docling_md)}")
        return 2
    Z.ensure_dir(args.out)
    rows, counts, removed = [], {"GOOD": 0, "PARTIAL": 0, "JUNK": 0, "MISSING": 0}, 0
    for i, key in enumerate(sorted(keys), 1):
        text, src = best_source(key, args.pdfs, args.quarantine, args.docling_md)
        text = deep_clean(text)
        marked, found = mark_sections(text)
        status = classify(text, found)
        counts[status] += 1
        words = len(text.split())
        out = args.out / f"{key}.txt"
        if status in Z.CLEAN_ELIGIBLE:
            out.write_text(
                f"KEY: {key}\nSOURCE: {src}\nWORDS: {words}\nSECTIONS_FOUND: {','.join(sorted(found))}\n\n{marked}",
                encoding="utf-8")
        elif out.is_file():
            out.unlink()
            removed += 1
        rows.append((key, src, words, len(found & CORE), ";".join(sorted(found)), status))
        if i % 200 == 0:
            print(f"  ...{i}/{len(keys)}", flush=True)
    Z.ensure_dir(args.manifest.parent)
    with args.manifest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "source", "words", "core_sections", "sections_found", "status"])
        w.writerows(rows)
    n = len(rows)
    print(f"\nEVERY DOCUMENT CHECKED: {n}")
    for status, label in (("GOOD", "at least 3000 words and 3 core sections"), ("PARTIAL", "a body, few sections"),
                          ("JUNK", "landing, abstract or short"), ("MISSING", "no usable source")):
        print(f"  {status:8} {counts[status]:5} ({counts[status] * 100 // n}%)  {label}")
    print(f"  clean text for GOOD and PARTIAL -> {Z.rel(args.out)}/ ; manifest -> {Z.rel(args.manifest)}")
    if removed:
        print(f"  removed {removed} clean text file(s) whose key is now JUNK or MISSING")
    return 0


if __name__ == "__main__":
    sys.exit(main())
