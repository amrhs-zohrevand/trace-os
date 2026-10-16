#!/usr/bin/env python3
"""Give each item in a collection a research card (a child note) and faceted tags.

A research card is a short structured summary a person can read inside Zotero:
TL;DR, key finding, method and data, relevance. It is a child note, and notes
sync as data, so no file storage, quota or WebDAV is involved. Faceted tags
(`method:`, `evidence:`, and any others the cards carry) make the collection
filterable; the tag `enriched` marks an item as done, which makes the write
idempotent. The tag is added only after Zotero accepted the card note, and a
card note already on the item is reused, so a retry after a refused tag write
never posts a second card. Every refused write is listed with its item key
and Zotero's status.

Three passes, so the cards are reviewable before they land:
  plan --root KEY [--n N]   select the items in the collection subtree that have
                            an abstract and are not yet enriched -> input.json
  generate [--n N]          write a card per input item with the chat model in
                            .env (OPENROUTER_API_KEY) -> cards.json. Optional:
                            an agent can write cards.json from input.json itself,
                            in the same schema, and skip this pass.
  apply [--write]           post each card as a child note and add the facet tags

Card schema (cards.json, a list):
  {"key": "ITEMKEY", "tldr": "...", "finding": "...", "method": "...",
   "relevance": "...", "facets": ["method:field-experiment", "evidence:causal"]}

State lives in workspace/shared/zotero/enrich/. Writes go to api.zotero.org,
so the client must be synced.

Usage:
  python3 scripts/zotero_enrich.py plan --root ABCD1234 --n 20
  python3 scripts/zotero_enrich.py generate
  python3 scripts/zotero_enrich.py apply            # then --write
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402

STATE = Z.SHARED / "enrich"
INPUT = STATE / "input.json"
CARDS = STATE / "cards.json"

CARD = ("<h2>Research card</h2>"
        "<p><b>TL;DR:</b> {tldr}</p>"
        "<p><b>Key finding:</b> {finding}</p>"
        "<p><b>Method and data:</b> {method}</p>"
        "<p><b>Relevance:</b> {relevance}</p>"
        "<p><i>Research card written from the abstract by {by}; check it against the paper before relying on it.</i></p>")

CARD_FIELDS = ("tldr", "finding", "method", "relevance")

PROMPT = ("You write one research card from a paper's title and abstract. Return JSON only, with keys "
          "tldr (one sentence), finding (one or two sentences, the main result), method (design, data, "
          "sample, in one sentence), relevance (one sentence: what kind of question this paper answers), "
          "and facets (a list of two to four short tags of the form prefix:value, using the prefixes "
          "method: and evidence:, for example method:field-experiment, evidence:causal, evidence:descriptive). "
          "Say only what the abstract supports. No preamble.")


def cmd_plan(args) -> int:
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    cols = Z.subtree(Z.all_collections(), args.root) + [args.root]
    seen = {}
    for ck in cols:
        for it in Z.cloud_items(None, collection=ck):
            d = it["data"]
            if d["key"] in seen or not d.get("abstractNote"):
                continue
            if "enriched" in {t["tag"] for t in d.get("tags", [])}:
                continue
            seen[d["key"]] = {"key": d["key"], "title": d.get("title", ""), "abstract": d["abstractNote"][:1500]}
    items = list(seen.values())[:args.n] if args.n else list(seen.values())
    Z.ensure_dir(STATE)
    INPUT.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} enrichable items -> {Z.rel(INPUT)}")
    print(f"Next: generate (or write {Z.rel(CARDS)} yourself in the card schema), then apply --write.")
    return 0


def parse_card(text: str) -> dict | None:
    """The JSON object in a model answer, with any fence or preamble around it ignored."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        card = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(card, dict):
        return None
    if not all(isinstance(card.get(f), str) for f in CARD_FIELDS):
        return None
    card["facets"] = [f for f in card.get("facets", []) if isinstance(f, str) and ":" in f]
    return card


def cmd_generate(args) -> int:
    if not INPUT.is_file():
        print("run plan first")
        return 2
    items = json.loads(INPUT.read_text(encoding="utf-8"))
    if args.n:
        items = items[:args.n]
    cards = json.loads(CARDS.read_text(encoding="utf-8")) if CARDS.is_file() else []
    have = {c["key"] for c in cards}
    model = Z.chat_model()
    written = failed = 0
    for it in items:
        if it["key"] in have:
            continue
        answer = Z.llm([{"role": "system", "content": PROMPT},
                        {"role": "user", "content": f"Title: {it['title']}\n\nAbstract: {it['abstract']}"}], max_tokens=500)
        card = parse_card(answer)
        if not card:
            failed += 1
            print(f"  no valid card for {it['key']}: {answer[:80]!r}")
            continue
        card["key"] = it["key"]
        card["by"] = model
        cards.append(card)
        written += 1
        Z.ensure_dir(STATE)
        CARDS.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(0.2)
    print(f"{written} cards written by {model}, {failed} failed -> {Z.rel(CARDS)} ({len(cards)} total)")
    return 0 if not failed else 1


def write_result(res) -> str | None:
    """None when Zotero accepted the one object posted, else what it said.

    A multi-object write answers HTTP 200 even when it refuses an object; the
    refusal is in the answer's `failed` map, with the object's code and message.
    """
    if not isinstance(res, dict):
        return f"unexpected answer {str(res)[:80]}"
    refused = (res.get("failed") or {}).get("0")
    if refused:
        return f"HTTP {refused.get('code', '?')}: {str(refused.get('message', ''))[:80]}"
    if any("0" in (res.get(field) or {}) for field in ("successful", "success", "unchanged")):
        return None
    return f"no result for the object: {json.dumps(res)[:80]}"


def post_one(obj: dict) -> str | None:
    """POST one object to /items, never retried. None on success, else the reason it failed."""
    try:
        _, res = Z.zreq("POST", "/items", data=json.dumps([obj]).encode(),
                        headers={"Content-Type": "application/json"}, tries=1)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:80]}"
    return write_result(res)


CARD_MARK = re.compile(r"<h2[^>]*>\s*Research card\s*</h2>", re.I)


def has_card(item_key: str) -> bool:
    """True when the item already has a research-card child note, from an earlier apply."""
    return any(k["data"].get("itemType") == "note" and CARD_MARK.search(k["data"].get("note") or "")
               for k in Z.children(item_key))


def cmd_apply(args) -> int:
    """Post each card, then its tags; `enriched` is added only once the card note is in the library.

    A card note already on the item (a run whose tag write failed) is reused,
    not posted again. Each refused write is listed with the item key and
    Zotero's status, and the run exits 1.
    """
    Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    if not CARDS.is_file():
        print(f"no cards at {Z.rel(CARDS)}; run generate, or write it in the card schema")
        return 2
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    wrote = reused = tagged = skipped = failed = 0
    for c in cards:
        k = c["key"]
        try:
            _, item = Z.zreq("GET", f"/items/{k}")
            d = item["data"]
            card_there = has_card(k)
        except Exception as e:
            failed += 1
            print(f"  FAIL read {k}: {type(e).__name__}: {str(e)[:80]}")
            continue
        tags = d.get("tags", [])
        if "enriched" in {t["tag"] for t in tags}:
            skipped += 1
            continue
        note_html = CARD.format(**{f: html.escape(str(c.get(f, "not stated"))) for f in CARD_FIELDS},
                                by=html.escape(str(c.get("by", "an agent"))))
        facets = list(c.get("facets", [])) + ["enriched"]
        if not args.write:
            if card_there:
                print(f"  WOULD tag {k}: it already has its card note; tags {facets}")
                reused += 1
            else:
                print(f"  WOULD enrich {k}: note + tags {facets}")
                wrote += 1
            continue
        if card_there:
            print(f"  {k} already has its card note: adding the tags only")
            reused += 1
        else:
            reason = post_one({"itemType": "note", "parentItem": k, "note": note_html})
            if reason:
                failed += 1
                print(f"  FAIL note {k}: {reason}; no tags added, so the next apply tries the card again")
                continue
            wrote += 1
        have = {t["tag"] for t in tags}
        new_tags = tags + [{"tag": t} for t in facets if t not in have]
        if new_tags != tags:
            reason = post_one({"key": k, "version": d["version"], "tags": new_tags})
            if reason:
                failed += 1
                print(f"  FAIL tags {k}: {reason}; the card note is there, and the next apply adds the tags "
                      f"without posting a second card")
                continue
            tagged += 1
        time.sleep(0.2)
    print(f"{'APPLIED' if args.write else 'DRY-RUN'}: {wrote} cards, {reused} existing cards reused, "
          f"{tagged} tag updates, {skipped} already enriched, {failed} failed.")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan", help="select enrichable items")
    s.add_argument("--root", required=True, help="the collection key whose subtree to enrich")
    s.add_argument("--n", type=int, default=0, help="at most N items")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("generate", help="write cards with the chat model in .env")
    s.add_argument("--n", type=int, default=0)
    s.set_defaults(fn=cmd_generate)
    s = sub.add_parser("apply", help="post cards and tags")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_apply)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
