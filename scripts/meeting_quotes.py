#!/usr/bin/env python3
"""Find quoted words in a transcript and print a packet for trace_record.py comments.

Each --quote must match exactly once, verbatim, inside one line of a plain-text
file or one speaker turn of a .vtt cue: a line opening with a `Name:` label
starts a turn and the lines under it continue it, and each `<v Name>` voice is
a turn (a cue with neither counts as one). Lines end at CRLF, LF or CR only; a
form feed or a Unicode line separator inside a line counts as whitespace.
Overlapping occurrences count separately. Runs of whitespace count as one space;
nothing else is loosened, so case, punctuation and spelling must be the
transcript's.

The speaker is the label on the matched line or turn, as the transcript writes
it: `Name: words`, optionally after a time stamp, or the `<v Name>` voice the
words sit in, so a quote that runs across two speakers matches neither; without
a label it is `unattributed`. A quote in a cue reports the cue's first text
line. The label is read, not judged:
check each `who` before capturing the packet.

The packet goes to stdout in the form `trace_record.py comments --input` reads:
per quote, the words, the speaker, --source, the line, the file's path and its
sha256. A quote not found, or found more than once, is reported on stderr and
left out; it is never matched loosely and its speaker is never guessed.
Nothing is written to disk. Exit 0 when every quote was found once, 1 when any
was left out (the packet still holds the others), 2 when the file is unreadable.

Example:
  python3 scripts/meeting_quotes.py workspace/record-staging/meeting.vtt \\
      --source "Project meeting, 2026-01-15" \\
      --quote "run the pilot on the first cohort" \\
      > workspace/record-staging/meeting-packet.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

# A label opens the line: an optional time stamp, then a name that starts with
# a letter and runs to at most forty characters, then a colon and a space.
LABEL = re.compile(r"^(?:\[?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]?\s+)?"
                   r"([^\W\d_][\w .'-]{0,39}?)\s*:\s+(.*)$")
# A WebVTT voice tag, `<v Name>` or `<v.class Name>`; one cue may hold several.
VOICE = re.compile(r"<v(?:\.[\w.-]+)?\s+([^>]+)>")
TAG = re.compile(r"</?[^>]*>")


def squash(text: str) -> str:
    """The text with each run of whitespace as one space, ends trimmed."""
    return " ".join(text.split())


def labelled(text: str) -> tuple[str, str]:
    """(speaker, words) from a line or a cue's text."""
    match = LABEL.match(text)
    if match:
        return squash(match.group(1)), squash(match.group(2))
    return "unattributed", squash(text)


def physical_lines(text: str) -> list[str]:
    """The file's lines as a text editor numbers them: split at CRLF, LF or CR only.

    str.splitlines also breaks at a form feed, a vertical tab, U+0085, U+2028
    and U+2029, which would shift every later line number; inside a line
    those count as whitespace, like any other.
    """
    return re.split(r"\r\n|\r|\n", text)


def cues(text: str) -> list[tuple[int, list[str]]]:
    """Each cue as (line of its first text line, its text lines).

    Only a timing line carries `-->`, so a header, NOTE or STYLE block is skipped.
    """
    lines, found, n = physical_lines(text), [], 0
    while n < len(lines):
        if "-->" in lines[n]:
            first, payload = n + 2, []
            n += 1
            while n < len(lines) and lines[n].strip():
                payload.append(lines[n].strip())
                n += 1
            if payload:
                found.append((first, payload))
        n += 1
    return found


def turns(lines: list[str]) -> tuple[list[str], str]:
    """(the label-style turns before the cue's first voice tag, the rest from that tag on).

    A line that opens with a `Name:` label starts a turn; a line without one
    continues the turn above it, so a speaker's second line stays theirs.
    Once a voice tag appears, the voices own the rest of the cue.
    """
    found: list[str] = []
    for i, line in enumerate(lines):
        voice = VOICE.search(line)
        head = line[:voice.start()] if voice else line
        if head.strip():
            if not found or LABEL.match(TAG.sub("", head).strip()):
                found.append(head)
            else:
                found[-1] += " " + head
        if voice:
            return found, " ".join([line[voice.start():]] + lines[i + 1:])
    return found, ""


def units(path: pathlib.Path, text: str) -> list[tuple[int, str, str]]:
    """(line, speaker, words) for every line of a plain-text file, or every speaker turn of a .vtt cue.

    A cue's turns all carry the line of the cue's first text line.
    """
    if path.suffix.lower() != ".vtt":
        return [(n, *labelled(line)) for n, line in enumerate(physical_lines(text), 1)]
    read = []
    for n, lines in cues(text):
        labelled_turns, voiced = turns(lines)
        # Each label line opens its own turn, so two speakers in one cue never share words.
        for turn in labelled_turns:
            read.append((n, *labelled(TAG.sub("", turn))))
        # Each voice speaks until the next voice tag, so its words are its own.
        voices = list(VOICE.finditer(voiced))
        for voice, after in zip(voices, voices[1:] + [None]):
            end = after.start() if after else len(voiced)
            read.append((n, squash(voice.group(1)), squash(TAG.sub("", voiced[voice.end():end]))))
    return read


def locate(found: list[tuple[int, str, str]], quote: str) -> list[tuple[int, str]]:
    """(line, speaker) for every verbatim occurrence of the quote."""
    wanted = squash(quote)
    if not wanted:
        return []
    hits = []
    for n, who, words in found:
        # str.count skips overlaps, which would let "no no" in "no no no" pass as unique.
        at = words.find(wanted)
        while at != -1:
            hits.append((n, who))
            at = words.find(wanted, at + 1)
    return hits


def shown(path: pathlib.Path) -> str:
    """The path relative to the working directory, else its name: never an absolute path."""
    try:
        return path.resolve().relative_to(pathlib.Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


def packet(path: pathlib.Path, data: bytes, source: str, quotes: list[str]) -> tuple[dict, list[str]]:
    """The comments packet for the quotes found once, and a report line for each other quote."""
    digest = hashlib.sha256(data).hexdigest()
    found = units(path, data.decode("utf-8-sig"))
    kind = "cue" if path.suffix.lower() == ".vtt" else "line"
    items, left_out = [], []
    for quote in quotes:
        hits = locate(found, quote)
        if len(hits) != 1:
            where = ", ".join(str(n) for n, _ in hits)
            reason = (f"found {len(hits)} times, at lines {where}; quote more of the line"
                      if hits else "not found verbatim within one line or speaker turn")
            left_out.append(f"left out {quote!r}: {reason}")
            continue
        line, who = hits[0]
        items.append({"text": squash(quote), "who": who, "speaker_kind": "human",
                      "source": source, "source_id": f"{path.name}@{digest[:12]}:{line}",
                      "kind": kind, "file": shown(path), "line": line, "file_sha256": digest})
    return {"items": items}, left_out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     epilog=__doc__.split("\n", 2)[2],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("transcript", help="a .vtt file, read cue by cue, or a .txt or other "
                                           "plain-text file, read line by line")
    parser.add_argument("--source", required=True,
                        help="the meeting or notes the file records, with its date; "
                             "kept as each item's source")
    parser.add_argument("--quote", action="append", required=True,
                        help="words to find verbatim, without the speaker label; "
                             "repeat for each line that directs work")
    args = parser.parse_args(argv)
    path = pathlib.Path(args.transcript)
    try:
        data = path.read_bytes()
        result, left_out = packet(path, data, args.source, args.quote)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read {path.name}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    for line in left_out:
        print(line, file=sys.stderr)
    return 1 if left_out else 0


if __name__ == "__main__":
    sys.exit(main())
