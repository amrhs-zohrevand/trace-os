#!/usr/bin/env python3
"""Clean extracted article text for reading, coding and embedding.

De-hyphenation, ligature and spaced-letter repair, running-header, footer and
page-number removal, wrapped-line merging, and an optional drop of
parentheticals, which is how in-text citations leave the embedding text.

- clean_pdf_text(text, remove_parentheses=False): clean reading text.
- deep_clean(text): the same with parentheses kept, for coding and display.
- strip_for_embedding(text): drop the reference list, the parentheticals and
  the numeric citations, so a vector reflects content rather than the names
  of the authors cited.

Pairs best with a markdown-first extractor (docling_extract_corpus.py); it
also improves a pdftotext extraction. Standard library only.

Usage:
  python3 scripts/text_clean.py FILE     # word counts raw, cleaned, embedding-ready
"""
import re
from collections import Counter

_SECT_KW = {"abstract", "introduction", "background", "literature review", "related work", "methods",
            "method", "methodology", "results", "findings", "discussion", "conclusion", "references",
            "limitations", "implications", "theory", "hypotheses", "data", "appendix"}

def _is_heading(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) > 120:
        return False
    if re.match(r"^\d+(\.\d+)*\.?\s+[A-Z]", s):                 # "3.1 Methods"
        return True
    low = s.lower().rstrip(":.").strip()
    if low in _SECT_KW:
        return True
    if s.isupper() and 1 <= len(s.split()) <= 12:               # ALL CAPS short
        return True
    words = [w for w in s.split() if w]
    if (words and s[0].isupper() and not s.endswith(".") and len(words) <= 12
            and sum(1 for w in words if w[0].isupper()) >= max(1, len(words) // 2)):
        return True
    return False

def _is_numeric_line(s: str) -> bool:
    return bool(re.match(r"^\d{1,3}$", (s or "").strip()))

def _is_list_line(s: str) -> bool:
    return bool(re.match(r"^([-*]\s+|\(?\d+[.)]\s+|\(?[a-zA-Z][.)]\s+)", (s or "").strip()))

def _filter_artifacts(lines):
    counts = Counter(re.sub(r"\s+", " ", l.strip().lower()) for l in lines if l.strip())
    out = []
    for i, line in enumerate(lines):
        t = line.strip()
        if not t:
            out.append(line); continue
        if re.match(r"^page\s+\d+(\s+of\s+\d+)?$", t, re.I):     # "Page 3 of 21"
            continue
        if re.match(r"^\d+\s*/\s*\d+$", t):                      # "3 / 21"
            continue
        if _is_numeric_line(t):                                  # bare page numbers
            prev = lines[i - 1].strip() if i > 0 else ""
            nxt = lines[i + 1].strip() if i < len(lines) - 1 else ""
            if (not prev and not nxt) or (_is_heading(nxt) and len(prev) > 40 and not _is_heading(prev)):
                continue
        key = re.sub(r"\s+", " ", t.lower())
        if counts[key] >= 3 and len(t) <= 80 and not re.search(r"[.!?]", t) and not _is_heading(t):
            continue                                            # repeated running header/footer
        out.append(line)
    return out

def _merge_wrapped(lines):
    blocks, cur = [], ""
    def push():
        nonlocal cur
        if cur.strip():
            blocks.append(cur.strip()); cur = ""
    for raw in lines:
        line = re.sub(r"\s+", " ", raw.strip())
        if not line:
            push(); continue
        if _is_numeric_line(line):
            push(); cur = line; continue
        if _is_heading(line) or _is_list_line(line):
            push(); blocks.append(line); continue
        if not cur:
            cur = line; continue
        if _is_heading(cur) or _is_list_line(cur):
            push(); cur = line; continue
        cur += " " + line
    push()
    return "\n\n".join(blocks)

def clean_pdf_text(text: str, remove_parentheses: bool = False) -> str:
    c = str(text or "")
    c = c.replace("­", "").replace("\f", "\n").replace("\r\n", "\n").replace("\r", "\n")
    c = re.sub(r"^\[\[\s*INSERT.+?HERE\s*\]\]\s*$", "", c, flags=re.I | re.M)
    c = re.sub(r"([A-Za-z])-\n([a-z])", r"\1\2", c)                                  # de-hyphenate
    c = re.sub(r"\b(?:[A-Za-z]\s){3,}[A-Za-z]\b", lambda m: re.sub(r"\s+", "", m.group(0)), c)  # "A R T" -> "ART"
    c = re.sub(r"[^\S\n]+", " ", c)
    c = re.sub(r"([A-Za-z0-9])([,;:])([A-Za-z])", r"\1\2 \3", c)
    c = re.sub(r"[ \t]+\n", "\n", c)
    c = re.sub(r"\n{3,}", "\n\n", c).strip()
    c = _merge_wrapped(_filter_artifacts(c.split("\n")))
    c = re.sub(r"[ \t]+\n", "\n", c)
    c = re.sub(r"\n{3,}", "\n\n", c).strip()
    if remove_parentheses:
        c = re.sub(r"\([^()\n]{0,200}\)", "", c)                                     # parentheticals (incl. cites)
        c = re.sub(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]", "", c)                       # numeric citations [12]
        c = re.sub(r"\s+([.,;:])", r"\1", c)
        c = re.sub(r"  +", " ", c)
        c = re.sub(r"\n{3,}", "\n\n", c).strip()
    return c

_REFS = re.compile(r"\n\s*(=*\s*section:\s*references.*|references|bibliography|works cited|literature cited)\s*\n", re.I)

def deep_clean(text: str) -> str:
    return clean_pdf_text(text, remove_parentheses=False)

def strip_for_embedding(text: str) -> str:
    t = _REFS.split(deep_clean(text), maxsplit=1)[0]   # drop the reference list
    return clean_pdf_text(t, remove_parentheses=True)  # drop in-text citations / parentheticals

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print(__doc__.strip())
        sys.exit(2)
    raw = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
    print("raw words:", len(raw.split()))
    print("deep_clean words:", len(deep_clean(raw).split()))
    print("strip_for_embedding words:", len(strip_for_embedding(raw).split()))
