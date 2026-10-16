#!/usr/bin/env python3
"""Suggest citations for the manuscript's placeholders from the Zotero library.

Scans paper/main.tex and paper/sections/*.tex, or the files named on the
command line (TeX or Markdown), for:
  (CITE)                         suggest from the surrounding sentence
  (CITE <topic>)                 retrieve the library for that topic
  (CITE [search online])         also look for top-tier journal articles on Crossref,
                                 querying the surrounding sentence (or any topic
                                 written after the brackets), never the phrase itself
  \\ours{... cite ... }            a TODO macro whose text mentions cite or literature

For each placeholder the library index (zotero_index.py) is searched by
meaning, the hits are mapped to the project's cite keys (paste-ready
\\citep{key}, or [@key] for a Markdown file; an item the bibliography lacks is
flagged with its DOI), and the chat model recommends the best two or three
with a one-line reason, grounded only in those candidates. Read-only; the
report goes to workspace/sandbox/zotero/citation-suggestions.md.

The bibliography is every .bib under paper/bib/, or the file or directory
--bib names. The journal list for the online search is a management-research
default; --journals FILE (one journal name per line) replaces it.

Usage:
  python3 scripts/cite_suggest.py [-k 8] [--journals FILE]
  python3 scripts/cite_suggest.py workspace/sandbox/draft.md --bib references/project.bib
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_ask as A  # noqa: E402
import zotero_common as Z  # noqa: E402

REPORT = Z.SANDBOX / "citation-suggestions.md"
TOP_JOURNALS = [
    "Management Science", "Strategic Management Journal", "Administrative Science Quarterly",
    "Academy of Management Journal", "Academy of Management Review", "Organization Science",
    "Research Policy", "Journal of Political Economy", "American Economic Review",
    "The Quarterly Journal of Economics", "Science", "Nature", "Nature Human Behaviour",
    "Strategy Science", "Journal of Finance", "Journal of Marketing", "Marketing Science"]


def clean_tex(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\\[a-zA-Z]+\*?|[{}]", " ", s or "")).strip()


def find_placeholders(files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        if not f.is_file():
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        md = f.suffix.lower() in (".md", ".markdown")
        for m in re.finditer(r"\(CITE\b([^)]*)\)", txt, re.I):
            topic = m.group(1).strip(" :[]")
            ctx = clean_tex(txt[max(0, m.start() - 320):m.start()].rsplit(".", 1)[-1] + " "
                            + txt[m.end():m.end() + 140].split(".")[0])
            out.append({"file": f.name, "topic": topic, "online": "search online" in topic.lower(),
                        "context": ctx[:400], "md": md})
        for m in re.finditer(r"\\ours\{([^}]*)\}", txt):
            body = m.group(1)
            if re.search(r"cite|literature", body, re.I):
                ctx = clean_tex(body + " " + txt[max(0, m.start() - 250):m.start()].rsplit(".", 1)[-1])
                out.append({"file": f.name, "topic": clean_tex(body), "online": False, "context": ctx[:400], "md": md})
    return out


ONLINE = re.compile(r"\[?\s*search online\s*\]?", re.I)


def query_topic(p: dict) -> str:
    """The topic words a placeholder asks about, without the `[search online]` control phrase.

    The parser keeps the phrase in `topic`, where `online` is read from it; the
    phrase is an instruction, not a subject, so it is never sent as a query.
    """
    return ONLINE.sub(" ", p["topic"]).strip(" :[]") if p["online"] else p["topic"]


def query_for(p: dict) -> str:
    """The retrieval query: the topic the placeholder names, else the sentence around it."""
    return query_topic(p) or p["context"]


def cite_form(key: str, md: bool) -> str:
    """The paste-ready citation for a key: pandoc's [@key] in Markdown, \\citep{key} in TeX."""
    return f"[@{key}]" if md else f"\\citep{{{key}}}"


def resolve_input(path: Path) -> Path:
    """A file named on the command line: as given, else relative to the repository root."""
    if path.is_absolute() or path.is_file():
        return path.resolve()
    return (Z.ROOT / path).resolve()


def crossref_top(query: str, journals: set, n: int = 4) -> list[str]:
    d = Z.get_json(f"https://api.crossref.org/works?{urllib.parse.urlencode({'query': query, 'rows': 15})}{Z.mailto_param()}", timeout=20)
    out = []
    for it in (d or {}).get("message", {}).get("items", []):
        journal = " ".join(it.get("container-title") or [])
        if journal.lower() in journals:
            author = (it.get("author") or [{}])[0].get("family", "")
            year = (it.get("issued", {}).get("date-parts", [[None]])[0] + [None])[0]
            out.append(f"{author} ({year}): {' '.join(it.get('title') or [])[:60]} [{journal}] doi:{it.get('DOI')}")
        if len(out) >= n:
            break
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*", type=Path, help="files to scan instead of the paper (TeX or Markdown)")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--bib", type=Path, help="a .bib file or a directory of them, instead of paper/bib/")
    ap.add_argument("--journals", type=Path, help="one journal name per line for the online search")
    args = ap.parse_args(argv)
    journals = {j.strip().lower() for j in (args.journals.read_text(encoding="utf-8").splitlines()
                                             if args.journals else TOP_JOURNALS) if j.strip()}
    if args.files:
        files = [resolve_input(f) for f in args.files]
        missing = [Z.rel(f) for f in files if not f.is_file()]
        if missing:
            print(f"not a file: {', '.join(missing)}")
            return 2
    else:
        paper = Z.ROOT / "paper"
        files = [paper / "main.tex"] + sorted((paper / "sections").glob("*.tex"))
        if not (paper / "main.tex").is_file():
            print(f"no manuscript at {Z.rel(paper / 'main.tex')}: nothing to scan")
            return 2
    bib = resolve_input(args.bib) if args.bib else None
    doi2k, t2k = Z.bib_index(bib)
    if bib is not None and not doi2k and not t2k:
        print(f"no entries read from {Z.rel(bib)}: every suggestion will read as not in bib")
    placeholders = find_placeholders(files)
    if not placeholders:
        print("No placeholders. Use (CITE), (CITE <topic>), (CITE [search online]) or \\ours{...cite...}.")
        return 0
    print(f"{len(placeholders)} citation placeholder(s) found\n")
    report = [f"# Citation suggestions ({len(placeholders)} placeholders)\n"]
    for p in placeholders:
        query, topic = query_for(p), query_topic(p)
        candidates = []
        for s, m in A.retrieve(query, args.k):
            key = doi2k.get(Z.norm_doi(m.get("doi"))) or t2k.get(Z.norm_title(m.get("title"), 50))
            tag = cite_form(key, p["md"]) if key else f"(not in bib; add doi:{m.get('doi') or '?'})"
            candidates.append(f"{tag} | {m['creators']} ({m['year']}) {m['title'][:62]} [sim {s:.2f}]\n     {m['abstract'][:150]}")
        online = crossref_top(query, journals) if p["online"] else []
        rec = Z.llm([
            {"role": "system", "content":
             "You recommend citations only from the provided candidates. Output the best 2-3 as "
             "paste-ready citations exactly as each candidate is tagged (\\citep{key}, [@key], or the "
             "'not in bib' note), each with a one-line reason. "
             "If none truly fit, say so. Never invent a reference."},
            {"role": "user", "content":
             f"Manuscript context needing a citation:\n{p['context']}\n\nTopic: {topic or '(infer from context)'}\n\n"
             f"Library candidates (semantic):\n" + "\n".join(candidates)
             + ("\n\nTop-tier online candidates:\n" + "\n".join(online) if online else "")}], 600)
        block = f"## [{p['file']}] {(p['topic'] or '(generic)')[:70]}\n\n**Context:** {p['context'][:220]}\n\n{rec}\n"
        report.append(block)
        print(block)
    Z.ensure_dir(REPORT.parent)
    REPORT.write_text("\n".join(report), encoding="utf-8")
    print(f"Full report -> {Z.rel(REPORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
