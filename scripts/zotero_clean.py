#!/usr/bin/env python3
"""Audit the Zotero library: duplicates, missing abstracts and DOIs, scraped titles.

Read-only. Nothing is written to the library; the audit is printed and saved
to workspace/sandbox/zotero/cleanup-audit.md for a person to read. The fixes
are separate, reviewable scripts: zotero_fill.py adds missing abstracts and
DOIs, zotero_dedupe.py merges exact-DOI duplicates.

Reads the running Zotero client's local API by default, which is complete and
fast and needs no key. `--source cloud` reads the Web API instead, for a
machine where the client is not running.

Usage:
  python3 scripts/zotero_clean.py                  # items added in the last 5 years
  python3 scripts/zotero_clean.py --years 8        # widen the window
  python3 scripts/zotero_clean.py --all            # the whole library
  python3 scripts/zotero_clean.py --source cloud   # read api.zotero.org instead of the client
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402


def audit(items: list[dict]) -> dict:
    """Group the item data dicts by DOI and by normalized title; count the gaps."""
    by_doi, by_title = defaultdict(list), defaultdict(list)
    no_abstract = no_doi = scraped = 0
    for d in items:
        doi = Z.norm_doi(d.get("DOI"))
        if doi:
            by_doi[doi].append(d)
        by_title[Z.norm_title(d.get("title"))].append(d)
        if not (d.get("abstractNote") or "").strip():
            no_abstract += 1
        if d.get("itemType") == "journalArticle" and not doi:
            no_doi += 1
        if "|" in (d.get("title") or "") or d.get("itemType") in ("webpage", "document"):
            scraped += 1
    dup_doi = {k: v for k, v in by_doi.items() if len(v) > 1}
    covered = {Z.norm_title(x.get("title")) for grp in dup_doi.values() for x in grp}
    dup_title = {k: v for k, v in by_title.items() if k and len(v) > 1 and k not in covered}
    return {"items": len(items), "dup_doi": dup_doi, "dup_title": dup_title,
            "no_abstract": no_abstract, "no_doi": no_doi, "scraped": scraped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=int, default=5, help="window on dateAdded (default 5)")
    ap.add_argument("--all", action="store_true", help="the whole library")
    ap.add_argument("--source", choices=("local", "cloud"), default="local",
                    help="local: the running client's API (default); cloud: api.zotero.org")
    args = ap.parse_args(argv)
    years = None if args.all else args.years
    cutoff = Z.cutoff_for(years)

    if args.source == "local":
        try:
            raw = Z.local_items()
        except OSError as e:
            print(f"the Zotero local API is not reachable ({e}); start the client with the local API "
                  f"enabled under Settings, Advanced, or run with --source cloud")
            return 2
        items = [it["data"] for it in raw if it["data"].get("itemType") not in ("attachment", "note")]
        if cutoff:
            items = [d for d in items if (d.get("dateAdded") or "") >= cutoff]
    else:
        items = [it["data"] for it in Z.cloud_items(years)]

    scope = "whole library" if years is None else f"last {years} years, added on or after {cutoff}"
    a = audit(items)
    print(f"Auditing {a['items']} items ({scope}, {args.source})\n")
    print(f"DUPLICATES by DOI:   {len(a['dup_doi'])} groups")
    for v in list(a["dup_doi"].values())[:12]:
        print(f"   - {(v[0].get('title') or '')[:55]}  x{len(v)}  [{', '.join(x['key'] for x in v)}]")
    print(f"\nDUPLICATES by title (no shared DOI): {len(a['dup_title'])} groups")
    for v in list(a["dup_title"].values())[:8]:
        print(f"   - {(v[0].get('title') or '')[:55]}  x{len(v)}  [{', '.join(x['key'] for x in v)}]")
    print("\nMETADATA GAPS:")
    print(f"   missing abstract:            {a['no_abstract']}")
    print(f"   journalArticles missing DOI: {a['no_doi']}")
    print(f"   scraped or webpage titles:   {a['scraped']}")

    report = Z.ensure_dir(Z.SANDBOX) / "cleanup-audit.md"
    with report.open("w", encoding="utf-8") as f:
        f.write(f"# Zotero cleanup audit ({scope})\n\nItems: {a['items']}\n\n")
        f.write(f"## Duplicate DOI groups ({len(a['dup_doi'])})\n")
        for v in a["dup_doi"].values():
            f.write(f"- **{(v[0].get('title') or '')[:80]}** x{len(v)}: "
                    + ", ".join(f"{x['key']}({(x.get('date') or '')[:4]})" for x in v) + "\n")
        f.write(f"\n## Duplicate title groups ({len(a['dup_title'])})\n")
        for v in a["dup_title"].values():
            f.write(f"- **{(v[0].get('title') or '')[:80]}** x{len(v)}: " + ", ".join(x["key"] for x in v) + "\n")
        f.write(f"\n## Gaps\n\n- missing abstract: {a['no_abstract']}\n- journalArticles missing DOI: {a['no_doi']}\n"
                f"- scraped or webpage titles: {a['scraped']}\n")
    print(f"\nFull report -> {Z.rel(report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
