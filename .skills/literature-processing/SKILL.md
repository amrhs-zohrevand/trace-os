---
name: literature-processing
description: Process one source through extraction, a structured source note and a durable coding record, reporting unavailable extractors rather than assuming them.
compatibility: Text extraction needs a tool you confirm on this machine, Poppler's pdftotext or the shipped `scripts/docling_extract_corpus.py` with the optional docling package. `paper/bib/` and `workspace/shared/papers/` exist only in the academic profile, and each mode creates the directory it writes into.
---

# Process one source into a note and a coding record

## Purpose

Process one source at a time through three separable modes:

1. source file to readable extraction under `workspace/shared/papers/text/`;
2. extraction to a structured source note under `workspace/shared/papers/notes/`;
3. source note to a durable coding record under `sources/coding/`.

The extraction and source note are shared working material and remain untracked.
The coding record is the canonical tracked analytical output.
Do not write to `paper/` unless the user explicitly requests a manuscript or bibliography change.

In the core profile there is no `paper/` and no `workspace/shared/papers/`; create the directories this skill writes into, and report a citation check that cannot be performed rather than skipping it silently.

## Choose the stop point

| User request | Complete |
|---|---|
| extract a PDF | extraction mode only |
| make or revise a source note | extraction as needed, then source-note mode |
| code the paper | prerequisites as needed, then coding mode |
| process the paper end to end | all three modes |

Default to one source at a time.
Do not overwrite an existing extraction, note, or coding record unless the user asked to refresh or revise it.

## Establish the source and citation key

Reuse the project's BibTeX key when the source already appears in a bibliography, a note, or a coding record.
For a new source, use lowercase `authorYear`, adding a short disambiguating word only when needed.
Report a missing BibTeX entry, but do not add it to `paper/` without an explicit request.

Look for source files in this order:

- an explicit path supplied by the user;
- `workspace/shared/papers/pdfs/<cite-key>.pdf`;
- a matching file anywhere under `workspace/shared/papers/`, including its subdirectories, since a library is often organized by topic;
- an existing extraction under `workspace/shared/papers/text/`.

If more than one candidate matches, or the library is large enough that a search is ambiguous, ask for an explicit path rather than guessing.

PDFs, EPUBs, web archives, extracted text, and shared source notes stay under `workspace/shared/`.
Do not commit them.

## Extraction mode

Use only an extractor you have confirmed on this machine.
Do not call a script path you have not verified, and do not describe an extraction you did not perform.
The one extractor this project ships is `scripts/docling_extract_corpus.py`, and it runs only where the optional docling package is installed.

For a text-based PDF, confirm that Poppler is available:

```bash
command -v pdftotext
```

When it succeeds, run:

```bash
mkdir -p workspace/shared/papers/text
pdftotext -layout <source.pdf> workspace/shared/papers/text/<cite-key>.txt
```

`pdftotext` overwrites its target, so record the source path, its digest and the extraction method by prepending a short header to the extracted file after the command runs, not by expecting the command to write one.

If that check fails, report that Poppler is unavailable on this machine, and confirm the shipped Docling wrapper instead:

```bash
python3 -c "import docling" && python3 scripts/docling_extract_corpus.py --help
```

When that succeeds, give the source a folder of its own, named for its cite key, because the wrapper extracts every PDF in the folder it is given that has no markdown yet, and a shared folder would bring back a PDF left from an earlier source; then run it:

```bash
mkdir -p workspace/shared/papers/docling-in/<cite-key>
cp <source.pdf> workspace/shared/papers/docling-in/<cite-key>/<cite-key>.pdf
python3 scripts/docling_extract_corpus.py --pdfs workspace/shared/papers/docling-in/<cite-key>
```

Read what it printed: a PDF it reports as failed has no extraction, and one whose markdown already existed was skipped, so say that the existing file was used rather than that an extraction ran.
The extraction is `workspace/shared/papers/docling-md/<cite-key>.md`; copy it to `workspace/shared/papers/text/<cite-key>.txt` and prepend the same header, naming Docling as the method.
If neither check succeeds, report that no text extractor is available on this machine and stop; do not substitute another script or claim an extraction.

Then verify that the title, authors, abstract or opening, method, findings, limitations, references, and relevant appendices are readable.
If the result is empty, badly ordered, or dominated by OCR noise, stop and report that the extraction is not adequate for a note or coding record.

Apply these tests rather than an impression:

- Empty: the file has no words, or its word count is implausible for the page count.
- OCR noise: a large share of characters are non-alphabetic or non-ASCII where the source is ordinary prose.
- Badly ordered: consecutive lines alternate between two unrelated sentences. This is what `-layout` does to a two-column source, and it is recoverable rather than fatal.

To recover a two-column source, find the gutter and read each column as its own stream.
`-layout` keeps the gutter blank on nearly every body line, so the gutter is the column with the highest share of blank characters across the lines that carry text on both sides of it, not the column where a wide gap most often begins.
A ragged right-hand edge produces many wide gaps at low column numbers, and taking the most frequent one splits the left column mid-sentence.
Check the choice by printing a few consecutive lines split at that column: if both halves read as continuous prose, it is the gutter.
Record in the extraction header and in the note that the source is two-column, which column was used, and that the columns were read separately.
A linear read of an interleaved two-column extraction splices sentences across the gutter and misattributes them, so never take one without saying so.

No EPUB or OCR extractor is bundled.
For an EPUB or scanned PDF, use a user-supplied readable extraction or an explicitly available extraction tool, and record that tool and its limits.

## Source-note mode

Save the note at:

```text
workspace/shared/papers/notes/<cite-key>.md
```

Create `workspace/shared/papers/notes/` if it does not exist.
Use this schema, which is the whole schema; this skill depends on no separate template file:

```markdown
---
title: "Full title"
authors: "Last, First; Last, First"
year: 2026
venue: "Journal or venue"
cite_key: authorYear
status: draft | reviewed
tags: []
added: "YYYY-MM-DD"
---

## Why it's in scope

## Key claims and findings

## Evidence and method

## How it bears on our argument

## Coding notes
```

A project that already keeps notes in a different shape should follow its own; say which shape was used.

Ground the note in the readable source or extraction.
Use page numbers, section names, table numbers, or stable anchors as locators when available.
Do not invent quotations, page numbers, metadata, or claims.

The note is a shared working artifact and is not a verified reading.
Carry any judgment that must survive review or support later synthesis into `sources/coding/` or `sources/memos/`.

## Coding mode

Create or revise:

```text
sources/coding/<cite-key>.yml
```

Create `sources/coding/` if it does not exist.
One YAML file per source, named by its citation key, is this skill's format; a project that keeps its coding records in another format should say so and follow its own.

Ground the coding record in the source note, with spot checks against the extraction or source for consequential claims.
Preserve the original coder and date when revising an existing record, and add revision provenance rather than replacing it.

Use fields that fit the source rather than forcing an unrelated substantive codebook.
At minimum record:

```yaml
cite_key: authorYear
coded_by: "<model id the runtime reports, or the coder's name>"
date_coded: "YYYY-MM-DD"
title: "Full title"
scope_decision: include | exclude | background
scope_reason: "Grounded reason"
claims: []
evidence: []
limitations: []
trace_relevance: []
project_use: "Intended role in this project"
```

`coded_by` holds the bare model identifier the runtime reports, in the same form as the `Execution-agent:` trailer, with no prefix and no abbreviation.
When the runtime exposes no exact identifier, write `unknown`.
When a person did the coding, write their name.
Never guess, shorten, or generalize an identifier.

For each consequential claim, distinguish what the authors claim, what evidence they report, and this project's assessment.
Preserve sample sizes, settings, model identifiers, code or data locations, and other details that determine reproducibility.
Record released-code coverage separately from methods described only in the paper.

A coding record is a tracked judgment and not an accepted one; nothing here accepts, returns or refuses anything.

## Quality gate

Before marking a requested mode complete, confirm that:

- the source opens and the extraction is readable enough for the work performed;
- the extraction, note, and coding record use the same citation key;
- every consequential statement is grounded in the source;
- empirical, causal, and generality claims are not overstated;
- unresolved extraction or evidence gaps are visible;
- durable judgments are stored in tracked coding records or memos;
- no file under `paper/` changed without an explicit request.

Report the completed mode, source and artifact paths, extraction method, checks performed, and any capability or evidence limit.

## Hand the run to the record

Read `ai/SKILL-RUN-CONTRACT.md` and record each completed mode in the session trace record rather than in a parallel log.

Write one check row per mode, with `procedure` set to `literature-processing` and `procedure_version` set to the value `python3 scripts/trace_record.py procedure-version literature-processing` reads off this file, the source file and its digest as the `candidate`, the quality-gate items actually confirmed as the `criterion`, the extraction, note or coding record as the `artifact`, and the extractor, coverage and reading limits as the `limits`.

Mark the artifact `"reference": "declared"` when it lies under an untracked or shared location this checkout cannot resolve, which is where the extraction and the note live.

A source arriving from outside the project is outside material: record it with `python3 scripts/trace_record.py intake`, naming its origin, what was checked before use and the intended use.

If a later reading contradicts a coding record, open the finding with `python3 scripts/defect_ledger.py open` and carry the returned ledger id in `defect_ids`.
