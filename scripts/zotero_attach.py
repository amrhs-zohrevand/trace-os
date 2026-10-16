#!/usr/bin/env python3
"""Attach the project's PDFs and source notes to the matching Zotero items.

For each cited paper, or each key given, find its Zotero item (exact DOI, then
normalized title, against an index of the whole library) and add the PDF as a
linked-file attachment and the source note as a child note. Idempotent: an
item that already has a PDF attachment or a note is skipped.

A linked file points Zotero at the local path and uploads nothing, so it does
not consume the Zotero storage quota (a full quota answers an upload with
HTTP 413). It resolves on the machine that holds the file; for a library that
several machines read, keep the PDFs in the shared, synced literature folder.

Dry-run by default. --write executes. Reads ZOTERO_USER_ID and
ZOTERO_API_KEY from the project .env.

Where it looks:
  workspace/shared/papers/pdfs/<cite-key>.pdf     (and pdfs/processed/)
  workspace/shared/papers/notes/<cite-key>.md
  paper/bib/*.bib and the \\cite keys in paper/main.tex and paper/sections/

Usage:
  python3 scripts/zotero_attach.py              # dry-run over cited papers with a PDF or note
  python3 scripts/zotero_attach.py KEY ...      # specific cite keys
  python3 scripts/zotero_attach.py --write      # attach
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402

PDF_DIRS = [Z.PAPERS / "pdfs", Z.PAPERS / "pdfs" / "processed"]
NOTES = Z.PAPERS / "notes"

_INDEX: tuple[dict, dict] | None = None


def find_pdf(key: str) -> Path | None:
    for d in PDF_DIRS:
        p = d / f"{key}.pdf"
        if p.is_file():
            return p
    return None


def library_index() -> tuple[dict, dict]:
    """(doi -> item data, normalized title -> item data) over the whole library, built once.

    The Web API's q= search does not reliably match a DOI or an author with an
    apostrophe, so the library is pulled once and matched exactly here. The
    running client's local API is read when it answers, because paging a
    large library through the Web API takes minutes; the keys are the same.
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    doi_idx, title_idx = {}, {}
    items, source = Z.items_for_reading(None)
    print(f"  (matching against {len(items)} library items read from the {source} API)")
    for it in items:
        d = it["data"]
        doi = Z.norm_doi(d.get("DOI"))
        if doi:
            doi_idx.setdefault(doi, d)
        nt = Z.norm_title(d.get("title"))
        if nt:
            title_idx.setdefault(nt, d)
    _INDEX = (doi_idx, title_idx)
    return _INDEX


def web_match(meta: dict) -> tuple[str | None, str | None]:
    """(item key, title) for a bib entry, by exact DOI then normalized title."""
    doi_idx, title_idx = library_index()
    doi = Z.norm_doi(meta.get("doi"))
    if doi and doi in doi_idx:
        d = doi_idx[doi]
        return d["key"], d.get("title", "")
    nt = Z.norm_title(meta.get("title"))
    if nt and nt in title_idx:
        d = title_idx[nt]
        return d["key"], d.get("title", "")
    return None, None


def has_children(item_key: str) -> tuple[bool, bool, set]:
    """(has a PDF attachment, has a note, the attachment filenames) for an item."""
    kids = Z.children(item_key)
    has_pdf = any(k["data"].get("contentType") == "application/pdf" for k in kids)
    has_note = any(k["data"].get("itemType") == "note" for k in kids)
    names = {k["data"].get("filename") or k["data"].get("title") for k in kids
             if k["data"].get("itemType") == "attachment"}
    return has_pdf, has_note, names


def md_to_html(md: str) -> str:
    body = re.sub(r"^---\n.*?\n---\n", "", md, flags=re.S)
    out = []
    for para in re.split(r"\n\s*\n", body.strip()):
        p = html.escape(para.strip())
        p = re.sub(r"^#{1,6}\s*(.+)$", r"<strong>\1</strong>", p, flags=re.M)
        out.append("<p>" + p.replace("\n", "<br/>") + "</p>")
    return "".join(out)[:50000]


def attach_linked_file(parent: str, path: Path, content_type: str) -> str:
    item = [{"itemType": "attachment", "parentItem": parent, "linkMode": "linked_file",
             "title": path.name, "path": str(path.resolve()), "contentType": content_type}]
    _, res = Z.zreq("POST", "/items", data=json.dumps(item).encode(),
                    headers={"Content-Type": "application/json"}, tries=1)
    return res["successful"]["0"]["key"]


def attach_pdf(parent: str, pdf: Path) -> tuple[str, str]:
    return attach_linked_file(parent, pdf, "application/pdf"), "linked"


def add_note(parent: str, md: str) -> str:
    item = [{"itemType": "note", "parentItem": parent, "note": md_to_html(md)}]
    _, res = Z.zreq("POST", "/items", data=json.dumps(item).encode(),
                    headers={"Content-Type": "application/json"}, tries=1)
    return res["successful"]["0"]["key"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("keys", nargs="*", help="cite keys; default: every cited key with a PDF or note")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    meta = Z.bib_entries()
    if not meta:
        print(f"no BibTeX under {Z.rel(Z.BIB_DIR)}: nothing to match against. This project keeps no bibliography there.")
        return 2
    keys = args.keys or [k for k in Z.cited_keys() if k in meta and (find_pdf(k) or (NOTES / f"{k}.md").is_file())]
    print(f"{'WRITE' if args.write else 'DRY-RUN'} | {len(keys)} paper(s)\n")
    for k in keys:
        if k not in meta:
            print(f"  NOBIB  {k:<34} not in paper/bib (skip)")
            continue
        pdf, note = find_pdf(k), NOTES / f"{k}.md"
        item_key, title = web_match(meta[k])
        if not item_key:
            print(f"  NOITEM {k:<34} not found in Zotero (skip; zotero_add.py creates it)")
            continue
        has_pdf, has_note, _ = has_children(item_key)
        plan = []
        if pdf and not has_pdf:
            plan.append("PDF")
        if note.is_file() and not has_note:
            plan.append("note")
        if not plan:
            print(f"  OK     {k:<34} {item_key} already has {'PDF ' if has_pdf else ''}{'note' if has_note else ''}".rstrip())
            continue
        if not args.write:
            print(f"  PLAN   {k:<34} {item_key} -> attach {', '.join(plan)}  ({(title or '')[:30]})")
            continue
        try:
            done = []
            if "PDF" in plan:
                _, mode = attach_pdf(item_key, pdf)
                done.append(f"PDF ({mode})")
            if "note" in plan:
                add_note(item_key, note.read_text(encoding="utf-8", errors="ignore"))
                done.append("note")
            print(f"  DONE   {k:<34} {item_key} -> {', '.join(done)}")
        except Exception as e:
            print(f"  ERROR  {k:<34} {item_key} -> {type(e).__name__}: {str(e)[:80]}")
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
