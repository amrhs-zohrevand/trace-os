#!/usr/bin/env python3
"""Create Zotero items for cited papers the library lacks, then attach the PDF and note.

Metadata comes from Crossref when the bib entry has a DOI, otherwise from the
bib entry itself. After the item exists, the local PDF is attached as a linked
file and the source note as a child note, through scripts/zotero_attach.py.

Preview by default. --write creates. Reads ZOTERO_USER_ID and ZOTERO_API_KEY
from the project .env; CROSSREF_MAILTO, when set, joins the polite pool.

Usage:
  python3 scripts/zotero_add.py KEY [KEY ...]            # preview
  python3 scripts/zotero_add.py KEY [KEY ...] --write    # create and attach
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_attach as A  # noqa: E402
import zotero_common as Z  # noqa: E402


def creators(author: str) -> list[dict]:
    out = []
    for a in re.split(r"\s+and\s+", author or ""):
        a = a.strip()
        if "," in a:
            last, first = a.split(",", 1)
            out.append({"creatorType": "author", "firstName": first.strip(), "lastName": last.strip()})
        elif a:
            parts = a.split()
            out.append({"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
    return out


def crossref_item(doi: str) -> dict | None:
    w = Z.crossref_work(doi)
    if not w:
        return None
    year = (w.get("issued", {}).get("date-parts", [[None]])[0] + [None])[0]
    return {"itemType": "journalArticle", "title": " ".join(w.get("title") or []),
            "creators": [{"creatorType": "author", "firstName": a.get("given", ""), "lastName": a.get("family", "")}
                         for a in w.get("author", [])],
            "publicationTitle": " ".join(w.get("container-title") or []), "volume": w.get("volume", ""),
            "issue": w.get("issue", ""), "pages": w.get("page", ""), "date": str(year or ""), "DOI": doi,
            "abstractNote": Z.strip_jats(w.get("abstract"))}


def bib_item(e: dict) -> dict:
    return {"itemType": "journalArticle", "title": e["title"], "creators": creators(e["author"]),
            "publicationTitle": e["journal"] or e["booktitle"] or e["institution"],
            "volume": e["volume"], "issue": e["number"], "pages": e["pages"],
            "date": e["year"], "DOI": e["doi"] or ""}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("keys", nargs="+", help="cite keys from paper/bib")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    entries = Z.bib_entries()
    if not entries:
        print(f"no BibTeX under {Z.rel(Z.BIB_DIR)}: nothing to create from.")
        return 2
    for k in args.keys:
        e = entries.get(k)
        if not e:
            print(f"  no bib entry: {k}")
            continue
        item = (crossref_item(e["doi"]) if e["doi"] else None) or bib_item(e)
        print(f"  {'CREATE' if args.write else 'preview'} {k}: \"{item['title'][:48]}\" | "
              f"{len(item.get('creators', []))} authors | {item.get('publicationTitle', '')[:28]} {item.get('date', '')}")
        if not args.write:
            continue
        _, res = Z.zreq("POST", "/items", data=json.dumps([item]).encode(),
                        headers={"Content-Type": "application/json"}, tries=1)
        ik = res["successful"]["0"]["key"]
        done = [f"item {ik}"]
        pdf, note = A.find_pdf(k), A.NOTES / f"{k}.md"
        if pdf:
            A.attach_pdf(ik, pdf)
            done.append("PDF")
        if note.is_file():
            A.add_note(ik, note.read_text(encoding="utf-8", errors="ignore"))
            done.append("note")
        print(f"    -> {', '.join(done)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
