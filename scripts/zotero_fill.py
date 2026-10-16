#!/usr/bin/env python3
"""Fill missing abstracts and DOIs on recent Zotero items from Crossref and OpenAlex.

Additive only: it adds an abstract where there is none and a DOI where a
journal article has none. It never overwrites a field that has a value.

Two passes, so the write is reviewable:
  default   look up abstracts (by DOI) and DOIs (by title and first author),
            write the plan to workspace/shared/zotero/fill-plan.json, print it.
            A DOI is planned only when one Crossref work has the item's whole
            title (and its first author, where both name one), and the plan
            shows the Crossref title and DOI it matched
  --write   apply the saved plan through the Web API, one PATCH per item with
            the item's version as the guard, rate-limited

Reads and writes the cloud library, so the client must be synced. Keys from
the project .env; CROSSREF_MAILTO, when set, joins the polite pools.

Usage:
  python3 scripts/zotero_fill.py                 # plan over the last 5 years
  python3 scripts/zotero_fill.py --years 8       # widen the window
  python3 scripts/zotero_fill.py --all           # the whole library
  python3 scripts/zotero_fill.py --limit 10      # a pilot: plan the first ten items needing a fill
  python3 scripts/zotero_fill.py --write         # apply the saved plan
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402

PLAN = Z.SHARED / "fill-plan.json"


def plan_for(item: dict) -> tuple[dict, dict]:
    """(the fields to add for one item, what each looked-up DOI matched). Both empty when nothing is missing or found.

    A DOI found by title carries the Crossref title and DOI it matched, so the
    person reviewing the plan sees what the item would be tied to.
    """
    d = item["data"]
    fields, matched = {}, {}
    doi = (d.get("DOI") or "").strip()
    if not (d.get("abstractNote") or "").strip() and doi:
        abstract = Z.crossref_abstract(doi) or Z.openalex_abstract(doi)
        if abstract and len(abstract) > 60:
            fields["abstractNote"] = abstract
    if d.get("itemType") == "journalArticle" and not doi:
        creators = d.get("creators") or []
        first = creators[0].get("lastName", "") if creators else ""
        found = Z.crossref_match(d.get("title"), first)
        if found:
            fields["DOI"] = found["DOI"]
            matched["DOI"] = {"doi": found["DOI"], "crossref_title": found["title"]}
    return fields, matched


def build_plan(years: int | None, limit: int = 0) -> int:
    items = Z.cloud_items(years)
    scope = "whole library" if years is None else f"last {years} years"
    print(f"scanning {len(items)} cloud items ({scope}){f', planning at most {limit}' if limit else ''}...")
    plan, n_abstract, n_doi = [], 0, 0
    for it in items:
        if limit and len(plan) >= limit:
            break
        fields, matched = plan_for(it)
        if not fields:
            continue
        n_abstract += "abstractNote" in fields
        n_doi += "DOI" in fields
        entry = {"key": it["data"]["key"], "version": it["version"],
                 "title": (it["data"].get("title") or "")[:60], "fields": fields}
        if matched:
            entry["matched"] = matched
        plan.append(entry)
        time.sleep(0.2)
    Z.ensure_dir(PLAN.parent)
    PLAN.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nPLAN: {len(plan)} items to update ({n_abstract} abstracts, {n_doi} DOIs)")
    print(f"saved -> {Z.rel(PLAN)}  (review it, then run with --write)")
    for p in plan[:8]:
        print(f"   {p['key']} {list(p['fields'])} | {p['title']}")
        doi = p.get("matched", {}).get("DOI")
        if doi:
            print(f"      DOI {doi['doi']} <- Crossref title {doi['crossref_title'][:70]!r}")
    if len(plan) > 8 and n_doi:
        print(f"   (every DOI's matched Crossref title is in {Z.rel(PLAN)} under \"matched\")")
    return 0


def apply_plan() -> int:
    if not PLAN.is_file():
        print(f"no plan at {Z.rel(PLAN)}; run without --write first")
        return 2
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    print(f"applying {len(plan)} updates...")
    ok = fail = 0
    for p in plan:
        try:
            Z.zreq("PATCH", f"/items/{p['key']}", data=json.dumps(p["fields"]).encode(),
                   headers={"Content-Type": "application/json",
                            "If-Unmodified-Since-Version": str(p["version"])}, tries=1)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"   FAIL {p['key']}: {str(e)[:80]}")
        time.sleep(0.3)
    print(f"\nDone: {ok} updated, {fail} failed. A failure with HTTP 412 means the item changed since the plan; rebuild it.")
    return 0 if not fail else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="plan at most N items (a pilot)")
    ap.add_argument("--write", action="store_true", help="apply the saved plan")
    args = ap.parse_args(argv)
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    if args.write:
        return apply_plan()
    return build_plan(None if args.all else args.years, args.limit)


if __name__ == "__main__":
    sys.exit(main())
