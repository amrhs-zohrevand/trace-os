#!/usr/bin/env python3
"""Attach each paper's clean full text to its Zotero item, beside the PDF.

The clean text (workspace/shared/papers/clean/<cite-key>.txt, from
docling_extract_corpus.py or clean_corpus.py) is attached as a linked file of
type text/plain, so the verified reading text travels with the library and is
there for coding, retrieval or embedding later. A linked file uploads nothing
and takes no storage quota; it resolves on the machine that holds the file.
Idempotent: an item that already has a <cite-key>.txt attachment is skipped.

Only a key that clean_corpus.py's manifest
(workspace/shared/papers/clean-corpus-manifest.csv) classifies GOOD or
PARTIAL is attached. A JUNK or MISSING key, or a clean file with no manifest
row, is held back and listed, even when named; without a manifest nothing is
attached.

Matching goes through the project's BibTeX, as zotero_attach.py does: the
cite key finds the bib entry, and its DOI or title finds the item.

Dry-run by default. --write attaches. Reads ZOTERO_USER_ID and
ZOTERO_API_KEY from the project .env.

Usage:
  python3 scripts/zotero_attach_cleantext.py             # every GOOD or PARTIAL clean text with a bib entry
  python3 scripts/zotero_attach_cleantext.py KEY ...     # specific cite keys
  python3 scripts/zotero_attach_cleantext.py --write
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_attach as A  # noqa: E402
import zotero_common as Z  # noqa: E402

CLEAN = Z.PAPERS / "clean"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("keys", nargs="*", help="cite keys; default: every GOOD or PARTIAL clean text with a bib entry")
    ap.add_argument("--clean", type=Path, default=CLEAN)
    ap.add_argument("--manifest", type=Path, default=Z.CLEAN_MANIFEST,
                    help=f"clean_corpus.py's manifest, default {Z.rel(Z.CLEAN_MANIFEST)}")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    if not args.clean.is_dir():
        print(f"no clean text under {Z.rel(args.clean)}; run docling_extract_corpus.py or clean_corpus.py first")
        return 2
    statuses = Z.clean_statuses(args.manifest)
    if statuses is None:
        print(f"no manifest at {Z.rel(args.manifest)}: nothing says which clean text is the article; "
              f"run clean_corpus.py first. Nothing was attached.")
        return 2
    eligible = Z.clean_eligible(statuses)
    meta = Z.bib_entries()
    if not meta:
        print(f"no BibTeX under {Z.rel(Z.BIB_DIR)}: nothing to match against")
        return 2
    present = sorted(p.stem for p in args.clean.glob("*.txt") if p.stem in meta)
    keys = args.keys or [k for k in present if k in eligible]
    held_back = [k for k in (args.keys or present) if k not in eligible]
    print(f"{'WRITE' if args.write else 'DRY-RUN'} | {sum(k in eligible for k in keys)} clean text(s)\n")
    attached = skipped = missing = failed = 0
    for k in held_back:
        print(f"  HELD   {k:<34} manifest status {statuses.get(k) or 'none (no row)'}; only GOOD or PARTIAL is attached")
    for k in keys:
        if k not in eligible:
            continue
        f = args.clean / f"{k}.txt"
        if not f.is_file():
            print(f"  NOFILE {k:<34} no clean text")
            missing += 1
            continue
        if k not in meta:
            print(f"  NOBIB  {k:<34} not in paper/bib")
            missing += 1
            continue
        item_key, title = A.web_match(meta[k])
        if not item_key:
            print(f"  NOITEM {k:<34} not found in Zotero")
            missing += 1
            continue
        _, _, names = A.has_children(item_key)
        if f.name in names:
            print(f"  OK     {k:<34} {item_key} already has {f.name}")
            skipped += 1
            continue
        if not args.write:
            print(f"  PLAN   {k:<34} {item_key} -> attach {f.name}  ({(title or '')[:30]})")
            attached += 1
            continue
        try:
            A.attach_linked_file(item_key, f, "text/plain")
            print(f"  DONE   {k:<34} {item_key} -> {f.name} (linked)")
            attached += 1
        except Exception as e:
            print(f"  ERROR  {k:<34} {item_key} -> {type(e).__name__}: {str(e)[:80]}")
            failed += 1
        time.sleep(0.3)
    print(f"\n{'attached' if args.write else 'would attach'} {attached}, already there {skipped}, "
          f"no file, entry or item {missing}, held back by the manifest {len(held_back)}, failed {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
