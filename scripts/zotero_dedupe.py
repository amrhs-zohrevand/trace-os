#!/usr/bin/env python3
"""Merge Zotero items that share an identical DOI.

Two phases, so a deletion is reviewable before it happens:
  default   build the merge plan, no writes. For each exact-DOI group one
            item is the master (most children, then has an abstract, then
            more filled fields, then the older dateAdded) and the rest are
            duplicates. Saved to workspace/shared/zotero/dedupe-plan.json and
            printed to workspace/sandbox/zotero/dedupe-plan.md.
  --write   re-parent each duplicate's child attachments and notes onto the
            master, so nothing is lost, then move the duplicate to the trash.

Only exact-DOI groups are merged. Title-only near-duplicates are listed by
zotero_clean.py and left for a person. A trashed item stays recoverable in
the client until the trash is emptied; this script never deletes permanently.

Usage:
  python3 scripts/zotero_dedupe.py            # plan over the last 5 years
  python3 scripts/zotero_dedupe.py --all      # the whole library
  python3 scripts/zotero_dedupe.py --write    # execute the saved plan
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402

PLAN = Z.SHARED / "dedupe-plan.json"
REPORT = Z.SANDBOX / "dedupe-plan.md"


def score(item: dict) -> tuple:
    """Higher sorts first: children, abstract, filled fields, then the older item wins ties."""
    d = item["data"]
    added = int(re.sub(r"\D", "", d.get("dateAdded") or "0") or "0")
    return (item.get("meta", {}).get("numChildren", 0),
            1 if (d.get("abstractNote") or "").strip() else 0,
            sum(1 for v in d.values() if v),
            -added)


def merge_plan(items: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for it in items:
        doi = Z.norm_doi(it["data"].get("DOI"))
        if doi:
            groups[doi].append(it)
    plan = []
    for doi, its in groups.items():
        if len(its) < 2:
            continue
        its.sort(key=score, reverse=True)
        master, dupes = its[0], its[1:]
        plan.append({"doi": doi, "title": (master["data"].get("title") or "")[:70],
                     "master": master["data"]["key"],
                     "master_children": master.get("meta", {}).get("numChildren", 0),
                     "dupes": [{"key": d["data"]["key"], "version": d["version"],
                                "children": d.get("meta", {}).get("numChildren", 0)} for d in dupes]})
    return plan


def build_plan(years: int | None) -> int:
    plan = merge_plan(Z.cloud_items(years))
    Z.ensure_dir(PLAN.parent)
    PLAN.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    n_del = sum(len(p["dupes"]) for p in plan)
    n_kids = sum(d["children"] for p in plan for d in p["dupes"])
    Z.ensure_dir(REPORT.parent)
    with REPORT.open("w", encoding="utf-8") as f:
        f.write(f"# Zotero dedupe plan ({len(plan)} DOI groups, {n_del} items to trash)\n\n")
        for p in plan:
            f.write(f"- **{p['title']}**  keep `{p['master']}` (children {p['master_children']}); "
                    f"trash {', '.join(d['key'] + '(' + str(d['children']) + 'ch)' for d in p['dupes'])}\n")
    print(f"DEDUPE PLAN: {len(plan)} DOI groups -> trash {n_del} duplicates, re-parent {n_kids} child items")
    print(f"plan -> {Z.rel(PLAN)} | report -> {Z.rel(REPORT)}")
    for p in plan[:10]:
        print(f"   keep {p['master']} ({p['master_children']}ch) | trash {[d['key'] for d in p['dupes']]} | {p['title'][:45]}")
    return 0


def apply_plan() -> int:
    if not PLAN.is_file():
        print(f"no plan at {Z.rel(PLAN)}; run without --write first")
        return 2
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    print(f"executing {len(plan)} merges...")
    moved = trashed = failed = 0
    for p in plan:
        for d in p["dupes"]:
            try:
                for kid in Z.children(d["key"]):
                    Z.zreq("PATCH", f"/items/{kid['data']['key']}",
                           data=json.dumps({"parentItem": p["master"]}).encode(),
                           headers={"Content-Type": "application/json",
                                    "If-Unmodified-Since-Version": str(kid["version"])}, tries=1)
                    moved += 1
                # Trash, not delete: `deleted: true` is what the client's Move to Trash sets.
                Z.zreq("PATCH", f"/items/{d['key']}", data=json.dumps({"deleted": True}).encode(),
                       headers={"Content-Type": "application/json",
                                "If-Unmodified-Since-Version": str(d["version"])}, tries=1)
                trashed += 1
            except Exception as e:
                failed += 1
                print(f"   FAIL group {p['title'][:30]} duplicate {d['key']}: {str(e)[:80]}")
            time.sleep(0.3)
    print(f"\nDone: trashed {trashed} duplicates, re-parented {moved} child items, {failed} failed.")
    return 0 if not failed else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true", help="execute the saved plan")
    args = ap.parse_args(argv)
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    if args.write:
        return apply_plan()
    return build_plan(None if args.all else args.years)


if __name__ == "__main__":
    sys.exit(main())
