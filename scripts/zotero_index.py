#!/usr/bin/env python3
"""Build a semantic index over the Zotero library for zotero_ask.py and cite_suggest.py.

Each item's title and abstract, plus the opening of its clean full text when
the project holds one that clean_corpus.py's manifest classifies GOOD or
PARTIAL, is embedded and stored with its metadata under
workspace/shared/zotero/index/. Items without an abstract are skipped: an
index built on titles alone retrieves by wording, not by content.

The embedding provider comes from .env: DEEPINFRA_API_KEY, else
OPENROUTER_API_KEY (the same Qwen3-Embedding-8B space), else
EMBED_PROVIDER=custom with EMBED_URL, EMBED_API_KEY and EMBED_MODEL. The
index records which provider and model built it; a query with a different
model is reported by zotero_ask.py rather than silently compared.

Needs numpy. Read-only against the library; the embedding call is paid by
the provider's rate, a fraction of a cent per hundred abstracts. While the
Zotero client runs with its local API on, the window is read from it and no
Zotero key is needed; a collection read, or a client that is not running,
goes through the Web API and needs ZOTERO_USER_ID and ZOTERO_API_KEY.

Usage:
  python3 scripts/zotero_index.py                  # last 5 years by dateAdded
  python3 scripts/zotero_index.py --years 8
  python3 scripts/zotero_index.py --all
  python3 scripts/zotero_index.py --collection KEY # one collection subtree
  python3 scripts/zotero_index.py --limit 20       # a pilot: the 20 newest eligible items
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402

INDEX_DIR = Z.SHARED / "index"
VECTORS = INDEX_DIR / "vectors.npy"
META = INDEX_DIR / "meta.json"
INFO = INDEX_DIR / "index.json"
# Only clean/ is read: clean_corpus.py's manifest classifies what is there.
# An extracted/ folder, which no kit script writes and no gate classifies, is not.
CLEAN_DIRS = [Z.PAPERS / "clean"]
MANIFEST = Z.CLEAN_MANIFEST


def fulltext_opening(d: dict, doi2k: dict, t2k: dict, chars: int = 2000, eligible: set | None = None) -> str:
    """The first characters of the project's clean text for this item, when it has one the manifest allows.

    `eligible` is the set of keys clean_corpus.py's manifest classifies GOOD or
    PARTIAL; read from MANIFEST when not given. A key outside it, or any key
    when there is no manifest, adds no text: a landing page saved under
    the paper's name must not be embedded as its full text.
    """
    key = doi2k.get(Z.norm_doi(d.get("DOI"))) or t2k.get(Z.norm_title(d.get("title"), 50))
    if not key:
        return ""
    if eligible is None:
        eligible = Z.clean_eligible(Z.clean_statuses(MANIFEST))
    if key not in eligible:
        return ""
    for base in CLEAN_DIRS:
        for f in (base / f"{key}.txt", base / f"{key}.md"):
            if f.is_file():
                body = re.sub(r"^---\n.*?\n---\n", "", f.read_text(encoding="utf-8", errors="ignore"), flags=re.S)
                body = re.sub(r"^(KEY|DOI|TITLE|SOURCE|WORDS|SECTIONS_FOUND):.*$", "", body, flags=re.M)
                return " " + re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", body)).strip()[:chars]
    return ""


def item_text(d: dict, opening: str) -> str:
    return f"{d.get('title', '')}. {d['abstractNote']}{opening}"[:7000]


def item_meta(d: dict) -> dict:
    year = re.search(r"\b(\d{4})\b", d.get("date") or "")
    return {"key": d["key"], "title": d.get("title", ""),
            "creators": ", ".join(c.get("lastName", "") for c in d.get("creators", [])[:3]),
            "year": year.group(1) if year else "", "doi": d.get("DOI", ""),
            "abstract": (d.get("abstractNote") or "")[:400]}


def needs_cloud_keys(collection: str | None) -> bool:
    """A collection is read from the Web API; a window is read from the running client when it answers."""
    return bool(collection) or not Z.local_api_answers()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--collection", help="index one collection subtree instead of the window")
    ap.add_argument("--limit", type=int, default=0, help="at most N items, newest first (a pilot)")
    args = ap.parse_args(argv)
    try:
        import numpy as np
    except ImportError:
        print("numpy is not installed; the index needs it (pip install numpy)")
        return 2
    if needs_cloud_keys(args.collection):
        Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    provider = Z.embed_provider()
    if provider is None:
        print("no embedding provider in .env: set DEEPINFRA_API_KEY or OPENROUTER_API_KEY, or EMBED_PROVIDER=custom")
        return 2
    name, _, model, _ = provider

    years = None if args.all else args.years
    raw, source = Z.items_for_reading(None if args.collection else years, args.collection)
    items = [it["data"] for it in raw]
    scope = (f"collection {args.collection} and its subtree" if args.collection
             else "whole library" if years is None else f"last {years} years")
    scope += f", read from the {source} API"
    rows = [d for d in items if (d.get("abstractNote") or "").strip()]
    if args.limit:
        rows = rows[:args.limit]
    doi2k, t2k = Z.bib_index()
    statuses = Z.clean_statuses(MANIFEST)
    eligible = Z.clean_eligible(statuses)
    openings = {d["key"]: fulltext_opening(d, doi2k, t2k, eligible=eligible) for d in rows}
    n_open = sum(1 for v in openings.values() if v)
    print(f"{len(items)} items in scope ({scope}), {len(rows)} with abstracts -> embedding with {name}:{model}; "
          f"{n_open} carry the opening of a clean full text classified GOOD or PARTIAL")
    if statuses is None:
        print(f"  (no manifest at {Z.rel(MANIFEST)}, so no clean full text is used; run clean_corpus.py for it)")

    meta, vecs = [], []
    batch = 50
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vecs += Z.embed([item_text(d, openings[d["key"]]) for d in chunk])
        meta += [item_meta(d) for d in chunk]
        print(f"  embedded {min(i + batch, len(rows))}/{len(rows)}")
        time.sleep(0.2)
    if not vecs:
        print("nothing to index")
        return 1
    arr = np.array(vecs, dtype=np.float32)
    arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)
    Z.ensure_dir(INDEX_DIR)
    np.save(VECTORS, arr)
    META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    INFO.write_text(json.dumps({"provider": name, "model": model, "dimensions": int(arr.shape[1]),
                                "items": int(arr.shape[0]), "scope": scope, "with_fulltext_opening": n_open,
                                "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")},
                               indent=1), encoding="utf-8")
    print(f"\nindex: {arr.shape} -> {Z.rel(INDEX_DIR)}/ (vectors.npy, meta.json, index.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
