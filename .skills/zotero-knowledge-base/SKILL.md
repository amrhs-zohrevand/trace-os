---
name: zotero-knowledge-base
description: Manage a Zotero library as a knowledge base, in reviewable passes: audit and clean it, fill and merge metadata, add and attach the project's papers, restructure a collection into a taxonomy, give each paper a research card, build a semantic index and ask it, suggest citations for manuscript placeholders, and verify the full text behind each item.
compatibility: Needs a Zotero library with a Web API key and user id in the project `.env` (`.env.example` lists every key); the running Zotero client's local API makes the read-only passes fast and keyless. The semantic passes need numpy and one embedding provider; the synthesis passes need one chat provider; the clean-text pass needs the docling package. `paper/bib/` and `workspace/shared/papers/` exist only in the academic profile, and each pass using them reports their absence rather than assuming them.
---

# Manage a Zotero library as a knowledge base

## Purpose

Turn a shared Zotero library into something a project can rely on: clean metadata, the project's own PDFs and notes attached to the right items, a collection structure that reflects the research, a one-paragraph card on each paper, a semantic index that answers questions from the library rather than from memory, and a check that the full text behind an item is the article and not a landing page.

Every pass here is a script under `scripts/`, and every script that writes to the library plans first and writes only with `--write`.
The plan is the artifact a person reviews; the write is the same plan applied.
Nothing here accepts, returns or refuses anything.

## When to use

| User request | Run |
|---|---|
| audit or clean the library | audit, then fill, then dedupe |
| put our papers into Zotero | add, then attach |
| restructure a messy collection | reorganize |
| summarize every paper in a collection | enrich |
| what does the library say about X | index once, then ask |
| find citations for the placeholders in the draft | index once, then suggest |
| is the full text behind these items real | clean text |

Default to one pass at a time and report what it planned before the next.
Do not refresh a plan somebody is reviewing.

## Setup: confirm before use

Read the project `.env` for `ZOTERO_USER_ID` and `ZOTERO_API_KEY`.
A script that lacks a key it needs names the key and stops; copy `.env.example` to `.env` and fill it, never print a value, never commit `.env`.

Confirm what is available on this machine rather than assuming it:

```bash
python3 scripts/zotero_clean.py --help          # the scripts are present
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:23119/api/users/0/items?limit=1   # 200: the client's local API answers
python3 -c "import numpy"                       # the semantic passes
python3 -c "import docling"                     # the clean-text pass
```

The local API is read-only and needs no key; enable it in the client under Settings, Advanced.
Writes go through the Web API to the cloud library, so the client must be synced before a write and after it.
An institutional proxy or VPN may block `POST` to `api.zotero.org` while allowing `GET`; a write that fails only on the proxy is that, not a bug in the plan.

An API key pasted from a chat or a document sometimes carries Cyrillic look-alike letters; the scripts repair them on load.
If a key still fails, retype it.

## Audit, fill and dedupe

```bash
python3 scripts/zotero_clean.py                  # read-only: duplicates, missing abstracts and DOIs, scraped titles
python3 scripts/zotero_fill.py --limit 10        # plan: abstracts by DOI, DOIs by title, from Crossref and OpenAlex
python3 scripts/zotero_fill.py --write           # apply the saved plan
python3 scripts/zotero_dedupe.py                 # plan: exact-DOI groups, one master, the rest to trash
python3 scripts/zotero_dedupe.py --write         # re-parent children onto the master, then trash the duplicates
```

The audit writes `workspace/sandbox/zotero/cleanup-audit.md`.
The fill is additive only and never overwrites a filled field.
A DOI is planned only when exactly one Crossref work has the item's whole title, in any script, and, where the item names a first author and the work lists authors, has that surname among its authors, in any position; a short title needs that surname among the work's authors, and the plan shows the Crossref title and DOI each item would be tied to.
The dedupe merges exact-DOI groups only; title-only near-duplicates stay in the audit for a person.
A duplicate goes to the trash, recoverable until the trash is emptied, never deleted outright.
Each write carries the item's version from the plan, so an item that changed since the plan fails with HTTP 412 rather than being overwritten; rebuild the plan.
Set `CROSSREF_MAILTO` to join the polite pools; without it the lookups still work, more slowly.

## Add and attach

```bash
python3 scripts/zotero_attach.py                 # dry-run: every cited key with a PDF or note
python3 scripts/zotero_add.py KEY ... --write    # create the items the library lacks, then attach
python3 scripts/zotero_attach.py --write
```

Matching goes from the cite key to `paper/bib/`, and from the entry's DOI or title to the item.
The match index reads the whole library once; the local API does that in seconds where the Web API pages a large library in minutes.
A PDF is attached as a linked file: nothing is uploaded, no storage quota is used, and a full quota cannot answer with HTTP 413.
A linked file resolves on the machine that holds it, so keep the PDFs in the shared literature folder that every machine syncs.
Do not attach through WebDAV placement expecting the Web API to see it; an attachment the Web API did not create is not the same object.

## Reorganize a collection

```bash
cp scripts/zotero_taxonomy.example.json workspace/shared/zotero/reorg/taxonomy.json   # then edit the root, tree and rules
python3 scripts/zotero_reorg.py plan-map --coded workspace/shared/zotero/reorg/coded.json
python3 scripts/zotero_reorg.py create-tree      # then zotero_reorg.py --write create-tree
python3 scripts/zotero_reorg.py apply            # then zotero_reorg.py --write apply
python3 scripts/zotero_reorg.py delete-old       # then zotero_reorg.py --write delete-old, once the counts match the plan
```

`--write` goes before the subcommand, as above; placed after it, the parser refuses it.

The taxonomy is data: the root collection key, the tree, and the routing rules in priority order, with the specific buckets first so a broad word does not poach them.
`coded.json` is a list of `{key, title, theme, ...}` rows; a row's text fields are what the rules match.
Derive the taxonomy from the items, not from a guess: open-code a sample bottom-up, cluster, and check the result against a top-down draft.
Then iterate `plan-map` until the unsorted list is empty and a per-leaf sample reads right.

Membership is additive and multi-membership: `apply` never removes an item from a collection.
Cross-cutting facets that span themes go in tags or a `crosscut` collection rather than forcing a strict partition.
`create-tree` reuses a collection that already exists by that name anywhere in the library.
`delete-old` deletes the legacy subcollections of the root, each with every subcollection under it, and leaves every item in the library; a collection deletion is outright, and nothing goes to the trash.
It keeps the tree, every cross-cut collection that a `--write crosscut` run created or reused, each `--keep-name` collection, and any collection one of those sits under.
It knows a cross-cut collection by the key saved in `workspace/shared/zotero/reorg/crosscut-keys.json`, and a `--write crosscut` run from before that file was kept saved none, so name such a collection with `--keep-name` before `zotero_reorg.py --write delete-old`.
Check that each leaf's count in the client equals the plan's before running it.
`meta-apply` writes proposed metadata fixes; an item type change drops the fields the new type lacks, because the API refuses a `PUT` that carries them.

## Enrich with research cards

```bash
python3 scripts/zotero_enrich.py plan --root KEY --n 20
python3 scripts/zotero_enrich.py generate        # cards from the chat model, or write cards.json yourself
python3 scripts/zotero_enrich.py apply           # then --write
```

A card is a child note (TL;DR, key finding, method and data, relevance) plus facet tags; notes sync as data, so no storage is involved.
The tag `enriched` marks an item done and makes the write idempotent.
It is added only after Zotero accepted the card note, and a card note already on the item is reused, so a retry after a refused write never posts a second card; each refused write is listed with its item key and status.
A card is written from the abstract; it says so on its face, and it is not a reading of the paper.
Where the project holds a source note for the paper (`workspace/shared/papers/notes/`), write the card from that instead.

## Index and ask

```bash
python3 scripts/zotero_index.py --limit 20       # a pilot, then without --limit
python3 scripts/zotero_ask.py "what is the evidence on X?" --list
python3 scripts/zotero_ask.py "what is the evidence on X?"
```

The index embeds title and abstract, plus the opening of the clean full text when the project holds one that the clean-corpus manifest classifies GOOD or PARTIAL.
While the client runs with its local API on, the window is read from it and no Zotero key is needed; a collection read, or a client that is not running, goes through the Web API and needs the key and user id.
Items without an abstract are skipped rather than embedded on their title.
The provider follows the keys in `.env` (DeepInfra, else OpenRouter, the same embedding space, else a custom endpoint); the index records which one built it, and a query under a different model stops with the mismatch named.
`--list` retrieves without a chat call and is the mode to use when an agent will read the retrieved set itself.
A synthesis is grounded only in the retrieved abstracts and says where the library has no evidence; it never supplies a reference the retrieval did not.

## Suggest citations

Mark the draft with `(CITE)`, `(CITE topic)` or `(CITE [search online])`, then:

```bash
python3 scripts/cite_suggest.py                                                          # the paper, against paper/bib/
python3 scripts/cite_suggest.py workspace/sandbox/draft.md --bib references/project.bib   # named files, another bibliography
```

An online placeholder is queried by its sentence, or by a topic written after the brackets, never by the words "search online".
Each placeholder is retrieved from the index, the hits are mapped to the project's cite keys as paste-ready `\citep{}` (`[@key]` in a Markdown file), an item the bibliography lacks is flagged with its DOI, and the recommendation is grounded only in those candidates.
The report is `workspace/sandbox/zotero/citation-suggestions.md`; nothing is written into `paper/`.
Pasting a suggestion into the manuscript is a manuscript edit and follows the project's rule for `paper/`.

## Clean full text and verify it

Retrieval does not guarantee full text.
A fetcher can save a landing page, an abstract page or a paywall notice under the paper's name, and a later reading of that file attributes to the paper what it does not say.

```bash
python3 scripts/docling_extract_corpus.py --limit 5   # Docling markdown and clean text per PDF, OCR off
python3 scripts/clean_corpus.py                       # every document classified GOOD, PARTIAL, JUNK or MISSING
python3 scripts/zotero_attach_cleantext.py            # then --write: the GOOD or PARTIAL clean text beside the PDF, as a linked file
python3 scripts/text_clean.py FILE                    # word counts raw, cleaned, embedding-ready
```

`clean_corpus.py` writes `workspace/shared/papers/clean-corpus-manifest.csv`; the JUNK and MISSING rows are what still needs fetching.
Only a GOOD or PARTIAL key keeps `clean/<key>.txt`: a JUNK or MISSING key's clean text is not written, and one left by an earlier run is removed; `docling_extract_corpus.py` holds back a landing page by the same rule.
Attaching and indexing read the manifest and use a key's clean text only when its status there is GOOD or PARTIAL, so without a manifest nothing is attached and no full text is embedded.
GOOD is a word-count and heading test, not a reading: a sectioned body is present, not necessarily the right paper.
For coding, feed the whole clean document with its section markers, not an excerpt.
For embedding, `strip_for_embedding` drops the reference list and the in-text citations first, so the vector reflects the content rather than the names of the authors cited.

## Guardrails

- Plan before writing; `--write` is always explicit and never the default.
- A synthesis or a suggestion is grounded only in retrieved items and says when the library lacks evidence. Never invent a citation.
- Keys stay in `.env`; never print, log or commit them.
- Plans and the index are regenerable and live under `workspace/shared/zotero/`; reports for a person live under `workspace/sandbox/zotero/`. The synced library is the source of truth. A reorg's plan file (`mapping.json`) records the memberships it intends, not the old hierarchy: it is not a backup of the collections `delete-old` deletes.
- Do not write into `paper/` from any of these passes.

## Quality gate

Before reporting a pass complete, confirm that:

- the plan was printed and, for a write, the plan applied is the plan reviewed;
- every count in the report is a count the script printed, not an estimate;
- a write that failed is listed with its item key and the API's status;
- a synthesis names the items it drew on and states what the library did not cover;
- nothing under `paper/` changed.

Report the pass, the scope (window, collection or keys), the artifact paths, the counts, and any capability or evidence limit.

## Hand the run to the record

Read `ai/SKILL-RUN-CONTRACT.md` and record each completed pass in the session trace record rather than in a parallel log.

Write one check row per pass, with `procedure` set to `zotero-knowledge-base` and `procedure_version` set to the value `python3 scripts/trace_record.py procedure-version zotero-knowledge-base` reads off this file, the library scope and the plan file as the `candidate`, the quality-gate items actually confirmed as the `criterion`, the report, plan or index as the `artifact`, and the provider, window and coverage limits as the `limits`.

Mark the artifact `"reference": "declared"` when it lies under `workspace/shared/`, which this checkout cannot resolve and which is where plans and the index live.

A synthesis or a citation suggestion is advisory model output, recorded with `python3 scripts/trace_record.py accept --agent` over the commits it informed, and never an acceptance.

Library items, cards and clean text that enter the project's own evidence base are outside material: record them with `python3 scripts/trace_record.py intake`, naming the library, what was checked before use and the intended use.

If a later reading contradicts a card, a suggestion or a GOOD classification, open the finding with `python3 scripts/defect_ledger.py open` and carry the returned ledger id in `defect_ids`.
