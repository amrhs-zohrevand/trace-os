#!/usr/bin/env python3
"""Restructure a Zotero collection into a taxonomy, in reviewable passes.

The taxonomy, its routing rules and the root collection key live in a JSON
file, not in this script: scripts/zotero_taxonomy.example.json is the filled-in
example. Copy it to workspace/shared/zotero/reorg/taxonomy.json (or pass
--taxonomy) and edit the tree and rules for your library.

Subcommands, each a plan by default and a write only with --write, which goes
before the subcommand (zotero_reorg.py --write apply):
  plan-map --coded FILE   route every coded item to a target leaf with the rules;
                          write mapping.json; print the distribution and the
                          unsorted items. Reads only titles from the library.
  create-tree             create the collection tree under the root; idempotent,
                          a collection that already exists by name is reused.
  apply                   add each mapped item to its leaf. Additive: existing
                          memberships stay, so an item can sit in several leaves.
  crosscut --name NAME --graded FILE --tag TIER=tag ...
                          build a cross-cutting collection under the root from a
                          graded list and tag its members by tier.
  meta-apply --fixes FILE apply proposed metadata fixes (date, DOI, venue, itemType)
                          at or above --min-confidence, with a version guard.
  rename --name NAME [--top-level]
                          rename the root collection, optionally promoting it.
  delete-old              delete the legacy subcollections of the root that are not
                          part of the new tree or a cross-cut, nor hold one. A
                          collection DELETE removes the collection and every
                          subcollection under it outright; nothing goes to the
                          trash. The items stay in the library.

State (mapping.json, tree-keys.json, crosscut-keys.json) lives in
workspace/shared/zotero/reorg/.
Writes go to api.zotero.org, so the client must be synced; a proxy that blocks
POST requests to api.zotero.org blocks every write here.

Usage:
  python3 scripts/zotero_reorg.py plan-map --coded workspace/shared/zotero/reorg/coded.json
  python3 scripts/zotero_reorg.py create-tree            # then --write create-tree
  python3 scripts/zotero_reorg.py apply                  # then --write apply
  python3 scripts/zotero_reorg.py delete-old             # then --write delete-old, after checking counts
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

STATE = Z.SHARED / "reorg"
TAXONOMY = STATE / "taxonomy.json"
MAPPING = STATE / "mapping.json"
TREE_KEYS = STATE / "tree-keys.json"
CROSSCUT_KEYS = STATE / "crosscut-keys.json"


# ---- taxonomy and routing --------------------------------------------------

def load_taxonomy(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"no taxonomy at {Z.rel(path)}; copy scripts/zotero_taxonomy.example.json there and edit it")
    tax = json.loads(path.read_text(encoding="utf-8"))
    for field in ("root", "tree", "rules"):
        if field not in tax:
            raise SystemExit(f"taxonomy lacks '{field}'")
    ids = {n["id"] for n in tax["tree"]}
    for n in tax["tree"]:
        if n.get("parent") is not None and n["parent"] not in ids:
            raise SystemExit(f"tree node {n['id']} names an unknown parent {n['parent']}")
    for r in tax["rules"]:
        if r["leaf"] not in ids:
            raise SystemExit(f"rule for {r['leaf']} names a leaf the tree lacks")
    tax.setdefault("unsorted", "UNSORTED")
    return tax


def route(row: dict, title: str, rules: list[dict], unsorted: str = "UNSORTED") -> str:
    """The first rule whose keywords occur in the row's text, or the unsorted label.

    The text is the title plus every string field of the coded row (theme,
    codes, notes). `theme_any` matches against the theme field only; `any`
    against the whole text; `none` excludes. Rule order is the priority: put
    the specific buckets first so a broad word does not poach them.
    """
    theme = (row.get("theme") or "").lower()
    text = " ".join([theme, (title or "").lower()] + [str(v).lower() for k, v in row.items()
                                                       if k not in ("key", "title", "theme") and isinstance(v, str)])
    for r in rules:
        if r.get("theme_any") and not any(k in theme for k in r["theme_any"]):
            continue
        if r.get("any") and not any(k in text for k in r["any"]):
            continue
        if not r.get("theme_any") and not r.get("any"):
            continue
        if r.get("none") and any(k in text for k in r["none"]):
            continue
        return r["leaf"]
    return unsorted


def titles_for(keys: list[str]) -> dict:
    out = {}
    for i in range(0, len(keys), 25):
        chunk = keys[i:i + 25]
        _, items = Z.zreq("GET", f"/items?itemKey={','.join(chunk)}&limit=25")
        for it in items or []:
            out[it["data"]["key"]] = it["data"].get("title", "")
    return out


def cmd_plan_map(args) -> int:
    tax = load_taxonomy(args.taxonomy)
    coded = json.loads(Path(args.coded).read_text(encoding="utf-8"))
    missing = [r["key"] for r in coded if not r.get("title")]
    titles = titles_for(missing) if missing else {}
    names = {n["id"]: n["name"] for n in tax["tree"]}
    mapping, dist = {}, {}
    for r in coded:
        title = r.get("title") or titles.get(r["key"], "")
        leaf = route(r, title, tax["rules"], tax["unsorted"])
        mapping[r["key"]] = {"leaf": leaf, "theme": r.get("theme"), "title": title}
        dist[leaf] = dist.get(leaf, 0) + 1
    Z.ensure_dir(STATE)
    MAPPING.write_text(json.dumps(mapping, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Routed {len(mapping)} items -> {Z.rel(MAPPING)}\n")
    for n in tax["tree"]:
        if dist.get(n["id"]):
            print(f"  {dist[n['id']]:4}  {names[n['id']]}")
    if dist.get(tax["unsorted"]):
        print(f"\n  {dist[tax['unsorted']]:4}  {tax['unsorted']} (a rule is missing):")
        for k, v in mapping.items():
            if v["leaf"] == tax["unsorted"]:
                print(f"        - {v['title'][:80]}  | theme={v['theme']}")
    return 0


# ---- the collection tree ---------------------------------------------------

def cmd_create_tree(args) -> int:
    tax = load_taxonomy(args.taxonomy)
    existing = {d["name"]: k for k, d in Z.all_collections().items()}
    tkeys = json.loads(TREE_KEYS.read_text(encoding="utf-8")) if TREE_KEYS.is_file() else {}
    for n in tax["tree"]:  # ordered parent before child
        if n["id"] in tkeys:
            continue
        if n["name"] in existing:
            tkeys[n["id"]] = existing[n["name"]]
            print(f"  exists  {n['name']}  [{existing[n['name']]}]")
            continue
        parent_key = tax["root"] if n.get("parent") is None else tkeys.get(n["parent"])
        if not parent_key:
            print(f"  SKIP    {n['name']}: parent {n['parent']} not created yet")
            continue
        if not args.write:
            print(f"  WOULD CREATE  {n['name']}  under {parent_key}")
            continue
        _, res = Z.zreq("POST", "/collections", data=json.dumps([{"name": n["name"], "parentCollection": parent_key}]).encode(),
                        headers={"Content-Type": "application/json"}, tries=1)
        created = (res.get("successful") or res.get("success") or {}).get("0")
        new_key = created.get("key") if isinstance(created, dict) else created
        if not new_key:
            print(f"  FAIL  {n['name']}: {json.dumps(res)[:200]}")
            break
        tkeys[n["id"]] = new_key
        Z.ensure_dir(STATE)
        TREE_KEYS.write_text(json.dumps(tkeys, indent=1), encoding="utf-8")
        print(f"  created {n['name']}  [{new_key}]")
        time.sleep(0.2)
    Z.ensure_dir(STATE)
    TREE_KEYS.write_text(json.dumps(tkeys, indent=1), encoding="utf-8")
    print(f"\ntree keys -> {Z.rel(TREE_KEYS)}  ({len(tkeys)}/{len(tax['tree'])})")
    return 0


def batched_update(keys: list[str], make_update, write: bool) -> tuple[int, int, int, int]:
    """Fetch items 25 at a time, build updates with make_update(data) -> dict | None, POST them.

    Returns (updated, unchanged, failed items, failed batches). A batch whose
    request fails (a 412 version conflict, say) is reported with its item keys
    and the run goes on with the next batch; only confirmed writes count as
    updated (AH-201).
    """
    done = skipped = failed = failed_batches = 0
    for i in range(0, len(keys), 25):
        chunk = keys[i:i + 25]
        try:
            _, items = Z.zreq("GET", f"/items?itemKey={','.join(chunk)}&limit=25")
        except Exception as e:
            failed += len(chunk)
            failed_batches += 1
            print(f"\n  batch {i // 25}: FAILED reading {type(e).__name__}: {str(e)[:160]}; "
                  f"items {', '.join(chunk)}")
            continue
        found = {it["data"]["key"]: it["data"] for it in items or []}
        updates = []
        for k in chunk:
            d = found.get(k)
            if not d:
                failed += 1
                print(f"  MISSING {k}")
                continue
            u = make_update(d)
            if u is None:
                skipped += 1
                continue
            updates.append(u)
        if updates and write:
            try:
                _, res = Z.zreq("POST", "/items", data=json.dumps(updates).encode(),
                                headers={"Content-Type": "application/json"}, tries=1)
            except Exception as e:
                failed += len(updates)
                failed_batches += 1
                print(f"\n  batch {i // 25}: FAILED {type(e).__name__}: {str(e)[:160]}; "
                      f"items {', '.join(u['key'] for u in updates)}")
                res = None
            if res is not None:
                res = res or {}
                ok = len(res.get("successful", res.get("success", {})))
                fl = res.get("failed", {})
                done += ok
                failed += len(fl)
                if fl:
                    print(f"  batch {i // 25}: {len(fl)} failed {json.dumps(fl)[:200]}")
            time.sleep(0.3)
        elif updates:
            done += len(updates)
        print(f"  ...{min(i + 25, len(keys))}/{len(keys)}  (+{done} updated, {skipped} unchanged, {failed} failed)", end="\r")
    print()
    if failed_batches:
        print(f"{failed_batches} batch(es) failed; their items are listed above and were not updated.")
    return done, skipped, failed, failed_batches


def cmd_apply(args) -> int:
    if not MAPPING.is_file() or not TREE_KEYS.is_file():
        print("run plan-map and create-tree first")
        return 2
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    tkeys = json.loads(TREE_KEYS.read_text(encoding="utf-8"))

    def update(d):
        leaf = tkeys.get(mapping[d["key"]]["leaf"])
        cols = d.get("collections", [])
        if not leaf or leaf in cols:
            return None
        return {"key": d["key"], "version": d["version"], "collections": cols + [leaf]}

    done, skipped, failed, failed_batches = batched_update(list(mapping), update, args.write)
    print(f"{'APPLIED' if args.write else 'DRY-RUN'}: {done} memberships, {skipped} already present or unmapped, {failed} failed.")
    return 1 if failed_batches else 0


def save_crosscut_key(name: str, key: str) -> None:
    """Record a cross-cut collection's key, created or reused, so delete-old keeps it."""
    keys = json.loads(CROSSCUT_KEYS.read_text(encoding="utf-8")) if CROSSCUT_KEYS.is_file() else {}
    keys[name] = key
    Z.ensure_dir(CROSSCUT_KEYS.parent)
    CROSSCUT_KEYS.write_text(json.dumps(keys, indent=1, ensure_ascii=False), encoding="utf-8")


def cmd_crosscut(args) -> int:
    tax = load_taxonomy(args.taxonomy)
    tags = dict(t.split("=", 1) for t in args.tag)
    graded = json.loads(Path(args.graded).read_text(encoding="utf-8"))
    selected = {x["key"]: tags[x["tier"]] for x in graded if x.get("tier") in tags}
    existing = {d["name"]: k for k, d in Z.all_collections().items()}
    ck = existing.get(args.name)
    if not ck:
        if not args.write:
            print(f"WOULD CREATE collection {args.name!r} under {tax['root']}")
            ck = "(to be created)"  # the plan counts the items it would file; never sent
        else:
            try:
                _, res = Z.zreq("POST", "/collections",
                                data=json.dumps([{"name": args.name, "parentCollection": tax["root"]}]).encode(),
                                headers={"Content-Type": "application/json"}, tries=1)
            except Exception as e:
                res = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
            created = ((res or {}).get("successful") or (res or {}).get("success") or {}).get("0")
            ck = created.get("key") if isinstance(created, dict) else created
            if not ck:
                print(f"FAIL creating collection {args.name!r}: {json.dumps(res)[:200]}")
                print("Nothing was filed or tagged; fix the cause and run --write crosscut again.")
                return 1
            print(f"created {args.name!r} [{ck}]")
    if args.write:
        save_crosscut_key(args.name, ck)

    def update(d):
        tag = selected[d["key"]]
        cols, item_tags = d.get("collections", []), d.get("tags", [])
        have_tag = any(t.get("tag") == tag for t in item_tags)
        new_cols = cols + ([ck] if (ck and ck not in cols) else [])
        new_tags = item_tags + ([{"tag": tag}] if not have_tag else [])
        if new_cols == cols and have_tag:
            return None
        return {"key": d["key"], "version": d["version"], "collections": new_cols, "tags": new_tags}

    done, skipped, failed, failed_batches = batched_update(list(selected), update, args.write)
    print(f"{'APPLIED' if args.write else 'DRY-RUN'}: {done} items filed and tagged into {args.name!r}, "
          f"{skipped} already there, {failed} failed. Tiers: {tags}")
    return 1 if failed_batches else 0


def cmd_delete_old(args) -> int:
    """Delete the root's legacy subcollections: DELETE, not the trash, with their subcollections.

    Kept: every tree key, every cross-cut key a --write crosscut run recorded, every
    collection named by --keep-name, and every ancestor of one of those, since
    deleting a collection deletes the collections under it. Only the topmost
    legacy collections are sent a DELETE; those under them go with them.
    """
    tax = load_taxonomy(args.taxonomy)
    keep = set(json.loads(TREE_KEYS.read_text(encoding="utf-8")).values()) if TREE_KEYS.is_file() else set()
    if CROSSCUT_KEYS.is_file():
        keep |= set(json.loads(CROSSCUT_KEYS.read_text(encoding="utf-8")).values())
    keep_names = set(args.keep_name or [])
    allc = Z.all_collections()
    keep |= {k for k, d in allc.items() if d.get("name") in keep_names}
    holders = set()
    for k in keep:
        parent = (allc.get(k) or {}).get("parentCollection")
        while parent and parent in allc and parent not in holders:
            holders.add(parent)
            parent = allc[parent].get("parentCollection")
    legacy = [(k, allc[k]["name"]) for k in Z.subtree(allc, tax["root"]) if k not in keep and k not in holders]
    legacy_keys = {k for k, _ in legacy}
    tops = [(k, name) for k, name in legacy if allc[k].get("parentCollection") not in legacy_keys]
    print(f"Legacy collections to delete ({len(legacy)}), each with its subcollections; "
          f"the items stay in the library, and nothing goes to the trash:")
    for k, name in legacy:
        print(f"  - {name}  [{k}]")
    kept_holders = sorted(allc[k]["name"] for k in holders if k in Z.subtree(allc, tax["root"]))
    if kept_holders:
        print(f"Kept because a kept collection sits under them: {', '.join(kept_holders)}")
    if not args.write:
        print("\n(dry-run) re-run as --write delete-old to delete these collections.")
        return 0
    done = 0
    for k, name in tops:
        try:
            request = urllib.request.Request(f"{Z.zotero_base()}/collections/{k}", method="DELETE",
                                             headers=Z.zotero_headers({"If-Unmodified-Since-Version": str(allc[k]["version"])}))
            urllib.request.urlopen(request, timeout=30)
            done += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
        time.sleep(0.2)
    print(f"Deleted {done}/{len(tops)} top legacy collections, each with its subcollections "
          f"({len(legacy)} collections listed).")
    return 0


# ---- metadata fixes and the root ------------------------------------------

VENUE_FIELD = {"journalArticle": "publicationTitle", "magazineArticle": "publicationTitle",
               "newspaperArticle": "publicationTitle", "conferencePaper": "proceedingsTitle",
               "bookSection": "bookTitle", "report": "institution", "preprint": "repository"}
DOI_TYPES = {"journalArticle", "conferencePaper", "preprint"}
CONFIDENCE = {"low": 0, "med": 1, "medium": 1, "high": 2}


def cmd_meta_apply(args) -> int:
    floor = CONFIDENCE[args.min_confidence]
    fixes = [x for x in json.loads(Path(args.fixes).read_text(encoding="utf-8"))
             if CONFIDENCE.get((x.get("confidence") or "").lower(), -1) >= floor and x.get("proposed")]
    templates: dict = {}

    def allowed(item_type):
        # An itemType change must drop the fields the new type lacks, or the PUT answers 400.
        if item_type not in templates:
            _, t = Z.zreq("GET", f"https://api.zotero.org/items/new?itemType={item_type}")
            templates[item_type] = set(t) | {"key", "version", "collections", "relations", "dateAdded", "dateModified", "tags"}
        return templates[item_type]

    changed = skipped = failed = 0
    for x in fixes:
        k, p = x["key"], x["proposed"]
        try:
            _, item = Z.zreq("GET", f"/items/{k}")
            d = item["data"]
        except Exception as e:
            failed += 1
            print(f"  GET fail {k}: {e}")
            continue
        before = json.dumps(d, sort_keys=True)
        new_type = p.get("itemType") or d.get("itemType")
        if new_type != d.get("itemType"):
            d["itemType"] = new_type
            d = {f: v for f, v in d.items() if f in allowed(new_type)}
        if p.get("date"):
            d["date"] = p["date"]
        elif p.get("year") and not d.get("date"):
            d["date"] = str(p["year"])
        if p.get("DOI") and new_type in DOI_TYPES:
            d["DOI"] = p["DOI"]
        if p.get("venue"):
            d[VENUE_FIELD.get(new_type, "publicationTitle")] = p["venue"]
        if json.dumps(d, sort_keys=True) == before:
            skipped += 1
            continue
        if not args.write:
            changed += 1
            if changed <= 5:
                print(f"  WOULD UPDATE {k}: type={d.get('itemType')} date={d.get('date')} "
                      f"venue={str(d.get(VENUE_FIELD.get(new_type, 'publicationTitle'), ''))[:40]}")
            continue
        try:
            Z.zreq("PUT", f"/items/{k}", data=json.dumps(d).encode(),
                   headers={"Content-Type": "application/json", "If-Unmodified-Since-Version": str(d["version"])}, tries=1)
            changed += 1
        except Exception as e:
            failed += 1
            print(f"  PUT fail {k}: {e}")
        time.sleep(0.15)
    print(f"{'APPLIED' if args.write else 'DRY-RUN'}: {changed} items updated, {skipped} unchanged, {failed} failed "
          f"(of {len(fixes)} fixes at or above {args.min_confidence}).")
    return 0


def cmd_rename(args) -> int:
    tax = load_taxonomy(args.taxonomy)
    _, c = Z.zreq("GET", f"/collections/{tax['root']}")
    d = c["data"]
    print(f"current: name={d['name']!r} parent={d.get('parentCollection')}")
    d["name"] = args.name
    if args.top_level:
        d["parentCollection"] = False
    if not args.write:
        print(f"WOULD set name={args.name!r}{', parent=top-level' if args.top_level else ''}")
        return 0
    Z.zreq("PUT", f"/collections/{tax['root']}", data=json.dumps(d).encode(),
           headers={"Content-Type": "application/json", "If-Unmodified-Since-Version": str(d["version"])}, tries=1)
    print(f"renamed to {args.name!r}{' and promoted to top level' if args.top_level else ''}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--taxonomy", type=Path, default=TAXONOMY, help=f"default {Z.rel(TAXONOMY)}")
    ap.add_argument("--write", action="store_true", help="apply; every subcommand plans without it")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan-map", help="route coded items to leaves")
    s.add_argument("--coded", required=True, help="JSON list of {key, title?, theme?, ...}")
    s.set_defaults(fn=cmd_plan_map)
    sub.add_parser("create-tree", help="create the collection tree").set_defaults(fn=cmd_create_tree)
    sub.add_parser("apply", help="file mapped items into their leaves").set_defaults(fn=cmd_apply)
    s = sub.add_parser("crosscut", help="a cross-cutting collection from a graded list")
    s.add_argument("--name", required=True)
    s.add_argument("--graded", required=True, help="JSON list of {key, tier}")
    s.add_argument("--tag", action="append", required=True, metavar="TIER=tag")
    s.set_defaults(fn=cmd_crosscut)
    s = sub.add_parser("meta-apply", help="apply proposed metadata fixes")
    s.add_argument("--fixes", required=True, help="JSON list of {key, confidence, proposed: {...}}")
    s.add_argument("--min-confidence", choices=("low", "med", "high"), default="med")
    s.set_defaults(fn=cmd_meta_apply)
    s = sub.add_parser("rename", help="rename the root collection")
    s.add_argument("--name", required=True)
    s.add_argument("--top-level", action="store_true")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("delete-old", help="delete legacy subcollections of the root, with their subcollections")
    s.add_argument("--keep-name", action="append",
                   help="a collection name to keep besides the new tree and the cross-cuts saved in "
                        "crosscut-keys.json; name a cross-cut from a --write crosscut run made before "
                        "that file was kept, since it saved no key; the collections above it are kept too")
    s.set_defaults(fn=cmd_delete_old)
    args = ap.parse_args(argv)
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
