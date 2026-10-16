---
name: meeting-and-idea-intake
description: Bring a meeting's decisions or a noted idea into the trace record as human direction, keeping the transcript private, capturing the directing words verbatim with their speakers and consent, and making those speakers the owners of the work they directed.
compatibility: Requires no credential, network or external service. The optional helper `scripts/meeting_quotes.py` is standard-library Python and reads `.vtt` and plain-text files; another format needs a plain-text copy.
---

# Take in a meeting or an idea as direction

## Purpose

A decision taken in a meeting, or an idea somebody wrote down, often directs work an agent later executes.
That direction arrives outside the prompt, so a record holding only the prompt names the wrong principal: the person who asked for the notes to be processed, not the people whose words decided the work.
This method brings in what will direct work, verbatim and attributed, through the record commands the project already has, and nothing more.

## 1. Keep the raw material private and admit it

Put the transcript, chat log or notes file in `workspace/record-staging/`, which the project ignores; never in `workspace/shared/`, `sources/` or anywhere Git tracks.
Once it is read (step 2), record its entry, which keeps the file's digest and not its words:

```bash
python3 scripts/trace_record.py intake --what "Transcript of the <topic> meeting, <date>" \
    --origin "<where it came from>" --by "<who admits it>" --person --checked "read end to end" \
    --used-for "the decisions that direct <work>" --authority "<turn id of the request>" \
    --path workspace/record-staging/<file>
```

`--person` declares that the one admitting it is a person; use `--agent` or `--authorized-by` when that is who decided, since a name alone declares no kind.

## 2. Read the transcript first

- The transcript is the primary source. An AI-generated summary is outside material: check its points against the transcript, and never take a decision, an owner or a quotation from it alone. A person's own notes rank between the two.
- A garbled or missing speaker label stays unattributed. A name reconstructed from context is an inference and never becomes an owner.
- Do not reconstruct a meeting from memory. When only a summary or notes exist, say so.
- An action item needs an owner, a verb and an output; otherwise it is an open question.

## 3. Consent before words enter the record

A participant's words enter the record verbatim only when that person agreed; ask the user who agreed rather than assuming it.
Without that agreement, replace the words in the packet with a declared redaction and a paraphrase naming who reported it, for example `[REDACTED: participant words, no consent to quote] Paraphrase reported by <recorder>: split the sample by year.`
Keep the speaker's name in `who` only if they agreed to be named; otherwise use `unattributed`.
Words captured before consent is known stay in private staging, redacted before the record is cleared unless the speaker agrees.
Clearing `consent.third_party_review` is a disclosure review, not the participants' consent.

## 4. Capture the words that direct work

Select only the lines that decide, instruct or object; discussion that directs nothing stays in the transcript.
Each becomes one item of a `comments` packet, which the helper builds from quotes:

```bash
python3 scripts/meeting_quotes.py workspace/record-staging/<file>.vtt --source "<topic> meeting, <date>" \
    --quote "we run the pilot on the first cohort" --quote "I will draft the survey" \
    > workspace/record-staging/<meeting>-packet.json
python3 scripts/trace_record.py comments workspace/record-staging/<session> \
    --input workspace/record-staging/<meeting>-packet.json
```

Each item carries the words, the speaker label, the meeting, the line and the file's digest; runs of whitespace count as one space.
In a `.vtt` cue, each `Name:` line with the lines under it and each `<v Name>` voice is its own turn, so a quote never runs across two speakers, and the line given is the cue's first text line.
A quote not found verbatim, or found more than once, is reported and left out: quote more of the line or correct the wording rather than writing the item by hand.
Read the packet before capturing it, check each `who`, and apply step 3.

`comments` writes only into ignored staging and sets `consent.third_party_review` to `PENDING`, so the record cannot be promoted before its content is reviewed.
Capture into the session's own staging record, so the request and the meeting's direction sit together.
A line from an AI summary is not human direction: give it `"speaker_kind": "agent"` or leave it out, and `comments` keeps it out of the human turns.
Use `comments --speaker NAME` for unattributed lines only when the context, not a guess, identifies the speaker.

## 5. Make the speakers the owners

For each consequential choice the meeting directed, run `python3 scripts/trace_record.py decision workspace/record-staging/<session> --input <decision.json>`, shaped like `scripts/decision.example.json`, with `human_inputs` listing the captured turns and the request.
`decision` then names their human speakers as the owners by default; an unattributed turn names nobody.
How the agent executes the work stays an agent selection, recorded as one.

## 6. Meeting talk is not acceptance

Agreement in passing, "looks good" or silence accepts nothing.
A person's stated decision to accept, return or refuse a named piece of work is recorded in their name, with their words, only if they agreed to those words entering the tracked registry:

```bash
python3 scripts/trace_record.py accept --record <record-id> --person --by "<their name>" \
    --recorded-by <model-id> --words "<their words, verbatim>" --through <sha>
```

`<sha>` is the commit the `version:` line of `trace_record.py brief <record-id>` showed when the decision was put to them, so the decision covers the version they saw and a commit that lands later stays waiting.

Add `--decision return` or `--decision refuse` when that is what they said.
If they did not name the work, ask; never infer the scope.

## 7. Ideas

A noted idea follows `sources/memos/NOTES-CONVENTION.md`: the Inbox first, then a new `[PREFIX-NNN]` id and a `Tags:` line when the inbox is processed, keeping the writer's words.
When an idea directs work, cite its id in the decision.
When someone other than the person prompting wrote it, capture it like a meeting line, with the notes file and line as its source and the writer as `who`.

## 8. A memo, when asked

Write the decisions, the actions with owners and the open questions to `sources/memos/`, paraphrased and citing turn ids; the record carries the words.
Never send the transcript or the staged record to an external model reviewer, and write nothing into `paper/`.

## Hand the run to the record

Read `ai/SKILL-RUN-CONTRACT.md` and record the run in the session trace record rather than in a parallel log.

Write one check row with `procedure` set to `meeting-and-idea-intake` and `procedure_version` set to the value `python3 scripts/trace_record.py procedure-version meeting-and-idea-intake` reads off this file, the transcript's digest and the meeting date as the `candidate`, what was actually confirmed as the `criterion` (read end to end, summary checked against the transcript, each quote verbatim at its line, consent per speaker), the captured turn ids and any memo as the `artifact`, and pending consent, unresolved names and a summary-only or notes-only source as the `limits`.
Mark the artifact `"reference": "declared"` under `workspace/record-staging/`, and put any summary point the transcript contradicts in the row's `result`.

Before promotion, follow `ai/RECORD-CONVENTION.md`: read every captured turn, apply step 3, and clear `consent.third_party_review` only after that review.
