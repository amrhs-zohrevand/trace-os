#!/usr/bin/env python3
"""Check that panel reviewers quoted the text instead of inventing it.

Raising temperature is what buys a panel diversity, and it is also what invites
fabricated defects. This closes that loop: every issue must carry a Quote span
copied verbatim from the artifact, and this verifies each one is really there.
An issue whose quote does not appear is not a finding, it is an invention, and
it is reported as such rather than triaged.

Matching collapses whitespace on both sides, because the LaTeX source is one
sentence per line and a quoted span may cross a newline.

An issue can also quote a source the artifact is checked against (a dataset, a
cited paper, a table): a `Source quote:` line, with an optional `Source:` line
naming the file. Each `--source` file is read and hashed, and each source quote
is labelled the same three ways against it, so both sides of a mismatch are
checked. Without `--source`, a source quote is reported as not compared.

Usage:
  python3 scripts/review_grounding.py --input artifact.tex --dir .agent-reviews/x
  python3 scripts/review_grounding.py --input source.py --dir .agent-reviews/x --representation literal-source
  python3 scripts/review_grounding.py --input paper/tables/t2.tex --dir .agent-reviews/x \
      --source workspace/shared/data/survey.csv
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

WS = re.compile(r"\s+")
# A reviewer quoting LaTeX writes what the sentence says, not what the source
# escapes: "95%" for "95\%", "57,162" for "57{,}162". Treating that as
# fabrication would be a false accusation, so both sides are reduced to the
# rendered text before matching.
MACRO = re.compile(r"\\(?:emph|textit|textbf|texttt|textsc)\{([^{}]*)\}")
# Fragment separators: an elision, or a reviewer stuffing several spans
# into one Quote field. Both are quoting style, not invention.
ELISION = re.compile(r'\s*(?:\.\.\.|\u2026|"\s*&\s*"|"\s+and\s+")\s*')
DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
TRIM = ' \t"\'.,;:'
LATEX_COMMENT = re.compile(r"(?<!\\)%.*$", re.M)


# A reviewer formats the heading the prompt asks for, `Issue N (severity): claim`,
# in whatever Markdown it favours: a list, quote or heading marker, a number,
# bold or italic around the heading, a capitalized severity. Those wrappers are
# read through rather than read as a different line (AH-164).
_MARKS = r'^[ \t]*(?:(?:[-*+>]|#{1,6}|\d+[.)])[ \t]+)*'
_EMPH = r'(?:\*\*|__|\*|_)'
ISSUE_HEAD = re.compile(
    _MARKS + r'(?P<open>' + _EMPH + r')?[ \t]*'
    r'Issue[ \t]+#?(?P<n>\d+)[ \t]*\([ \t]*(?P<severity>[A-Za-z]+)[ \t]*\)[ \t]*' + _EMPH
    + r'?[ \t]*:[ \t]*' + _EMPH + r'?[ \t]*(?P<claim>.+?)[ \t]*$', re.M | re.I)
# Any line that opens as an issue heading. One that ISSUE_HEAD cannot read still
# ends the issue before it, so it cannot lend its quote to that issue, and it is
# counted, so a finding in an unexpected shape is visible rather than dropped.
ISSUE_CANDIDATE = re.compile(_MARKS + _EMPH + r'?[ \t]*Issue[ \t]+#?\d+\b.*$', re.M | re.I)
# The Quote line takes the same wrappers, in any case: `**Quote:** "..."`,
# `- Quote: "..."`, `> Quote: ...`, `**Quote: "..."**` (AH-200).
QUOTE = re.compile(
    _MARKS + r'(?P<open>' + _EMPH + r')?[ \t]*Quote[ \t]*(?:(?P=open)[ \t]*)?:[ \t]*'
    r'(?:(?P=open)[ \t]*)?(?P<mark>")?(?P<quote>.+?)"?[ \t]*$', re.M | re.I)
# The source side of a mismatch, in the same wrappers: `Source quote: "..."` for the
# text, and `Source: path` for the file it comes from. A Quote line never matches
# these, because each must open its line.
SOURCE_QUOTE = re.compile(
    _MARKS + r'(?P<open>' + _EMPH + r')?[ \t]*Source[ \t]+quote[ \t]*(?:(?P=open)[ \t]*)?:'
    r'[ \t]*(?:(?P=open)[ \t]*)?(?P<mark>")?(?P<quote>.+?)"?[ \t]*$', re.M | re.I)
SOURCE_NAME = re.compile(
    _MARKS + r'(?P<open>' + _EMPH + r')?[ \t]*Source[ \t]*(?:(?P=open)[ \t]*)?:[ \t]*'
    r'(?:(?P=open)[ \t]*)?`?(?P<name>[^`]+?)`?[ \t]*$', re.M | re.I)


def quote_of(body: str, pattern: re.Pattern = QUOTE) -> str | None:
    """The first Quote line's span in an issue's body, or None. `pattern` reads a
    Source quote line instead."""
    q = pattern.search(body)
    if not q:
        return None
    span, opened = q.group("quote"), q.group("open")
    if opened and span.endswith(opened) and span[:-len(opened)].strip():
        span = span[:-len(opened)].rstrip()
        if q.group("mark") and span.endswith('"'):
            span = span[:-1]
    return span


def split_issues(text: str) -> tuple[list[dict], list[str]]:
    """The parsed issues, and the candidate issue headings that did not parse."""
    out, unparsed = [], []
    heads = list(ISSUE_CANDIDATE.finditer(text))
    for i, h in enumerate(heads):
        line = h.group(0)
        parsed = ISSUE_HEAD.match(line)
        if not parsed:
            unparsed.append(line.strip())
            continue
        body = text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        claim, opened = parsed.group("claim"), parsed.group("open")
        if opened and claim.endswith(opened) and claim[:-len(opened)].strip():
            claim = claim[:-len(opened)].rstrip()
        named = SOURCE_NAME.search(body)
        out.append({"severity": parsed.group("severity").lower(), "claim": claim,
                    "quote": quote_of(body),
                    "source_quote": quote_of(body, SOURCE_QUOTE),
                    "source": named.group("name") if named else None})
    return out, unparsed


def parse_issues(text: str) -> list[dict]:
    """Split a review into issue blocks, pairing each claim with its OWN quote.

    Zipping independent findall lists silently drops issues when a reviewer
    omits a Quote line, and worse, shifts every later quote onto the wrong
    claim. Blocks keep the pairing honest and make a missing quote visible as
    a missing quote. `split_issues` also returns the headings that did not parse.
    """
    return split_issues(text)[0]


def norm(t: str) -> str:
    t = MACRO.sub(r"\1", t).translate(DASHES)
    for a, b in (("{,}", ","), ("\\%", "%"), ("\\&", "&"), ("\\_", "_"),
                 ("\\$", "$"), ("\\#", "#"), ("~", " "), ("\u2019", "'"),
                 ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'),
                 ("``", '"'), ("''", '"'), ("--", "-")):
        t = t.replace(a, b)
    return WS.sub(" ", t).strip().lower()


def source_text(path: Path, representation: str = "reader-visible") -> str:
    """Prepare the representation whose wording a review quote must match.

    Reader-visible is the default for manuscript review and removes unescaped
    LaTeX comments from .tex files. Literal-source retains every source byte as
    text for code or instruction review. Grounding in either representation
    establishes only that the words occur there.
    """
    text = path.read_text(encoding="utf-8")
    if representation == "reader-visible" and path.suffix.lower() == ".tex":
        text = LATEX_COMMENT.sub("", text)
    return norm(text)


SENT = re.compile(r"(?<=[.;:])\s+")


def verdict(quote: str, source: str) -> str:
    """Classify a Quote field as grounded, stitched, or invented.

    Three outcomes, because conflating them would libel a reviewer. A quote
    present verbatim is grounded. A quote whose sentences are each real but
    which joins passages that are not adjacent is stitched: a citation-hygiene
    fault, since it presents distant text as continuous, but not a fabricated
    claim. Only a span with no source at all is invented.

    Every nonempty fragment is checked, whatever its length: a floor of
    thirteen characters read a short verbatim quote such as "n = 1,204" as
    invented and let a short made-up clause ride along unchecked (AH-152).
    """
    frags = [f.strip(TRIM) for f in ELISION.split(norm(quote))]
    frags = [f for f in frags if f]
    if not frags:
        return "invented"
    if all(f in source for f in frags):
        return "grounded"
    parts = [x.strip(TRIM) for f in frags for x in SENT.split(f)]
    parts = [x for x in parts if x]
    if parts and all(x in source for x in parts):
        return "stitched"
    return "invented"


def grounded(quote: str, source: str) -> bool:
    return verdict(quote, source) in ("grounded", "stitched")


# Best first: a source quote found in any source given is labelled by the best.
RANK = ("grounded", "stitched", "invented")


def source_verdict(quote: str, named: str | None, sources: dict[str, str]) -> tuple[str, str]:
    """(verdict, the source that holds it) for a quote taken from a source.

    A quote whose issue names one of the sources given is checked against that one;
    an issue that names none is checked against each, and the best verdict stands
    with the file that gave it. Without a source to compare with, the verdict is
    `not compared`, which says the check did not happen, not that the quote is wrong.
    An issue whose `Source:` names a file that was not given (or was given and is not
    text) is that case too: until rc22's review (f1ca7a5 F002) its quote was looked
    for in every other source, so text found in a file the reviewer did not name
    read grounded.
    """
    if not sources:
        return "not compared", ""
    chosen = {path: text for path, text in sources.items()
              if named and (path == named or Path(path).name == Path(named).name)}
    if named and not chosen:
        return "not compared", ""
    chosen = chosen or sources
    found = sorted(((verdict(quote, text), path) for path, text in chosen.items()),
                   key=lambda pair: (RANK.index(pair[0]), pair[1]))
    best, path = found[0]
    return best, (path if best != "invented" else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--representation", choices=("reader-visible", "literal-source"),
                    default="reader-visible",
                    help="text being checked; manuscript review defaults to rendered prose")
    ap.add_argument("--source", action="append", default=[], metavar="PATH",
                    help="a file the artifact is checked against; an issue's Source quote is "
                         "labelled against it. Repeat for each; each is named with its sha256")
    args = ap.parse_args()

    source = source_text(Path(args.input), args.representation)
    # The sources, read once, each named with the digest of the bytes read.
    # A source that is not UTF-8 (a PDF, a spreadsheet) is named and hashed, and
    # no quote is compared with it.
    sources, digests = {}, []
    for name in args.source:
        data = Path(name).read_bytes()
        digests.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
        try:
            sources[name] = source_text(Path(name), args.representation)
        except UnicodeDecodeError:
            digests[-1]["compared"] = False
            print(f"source {name} is not UTF-8, so no quote was compared with it")
    rows, per_file = [], {}
    for f in sorted(Path(args.dir).glob("*.md")):
        if f.name == "combined.md":
            continue
        parsed, unparsed = split_issues(f.read_text(encoding="utf-8"))
        issues = parsed
        quotes = [i["quote"] for i in parsed if i["quote"]]
        noquote = sum(1 for i in parsed if not i["quote"])
        ok = [q for q in quotes if grounded(q, source)]
        bad = [q for q in quotes if not grounded(q, source)]
        vs = [verdict(q, source) for q in quotes]
        per_file[f.stem] = {"issues": len(issues), "quotes": len(quotes),
                            "grounded": vs.count("grounded"),
                            "stitched": vs.count("stitched"),
                            "noquote": noquote,
                            "invented": [q for q, v in zip(quotes, vs)
                                         if v == "invented"],
                            "unparsed": unparsed}
        sourced = [(it, source_verdict(it["source_quote"], it["source"], sources))
                   for it in parsed if it["source_quote"]]
        if sourced:
            # Only where an issue quotes a source, so a report without one reads
            # as it did before source quotes existed.
            per_file[f.stem]["source_quotes"] = {
                v: sum(1 for _, (w, _) in sourced if w == v)
                for v in RANK + ("not compared",)}
        for it in parsed:
            if not it["quote"] and not it["source_quote"]:
                continue
            row = {"reviewer": f.stem, "severity": it["severity"],
                   "claim": it["claim"], "quote": it["quote"],
                   "verdict": verdict(it["quote"], source) if it["quote"] else "no quote",
                   "grounded": grounded(it["quote"], source) if it["quote"] else False}
            if it["source_quote"]:
                found, held = source_verdict(it["source_quote"], it["source"], sources)
                row.update(source_quote=it["source_quote"], source_named=it["source"],
                           source_verdict=found, source_held_by=held)
            rows.append(row)

    print(f"{'reviewer':<26} {'issues':>6} {'quoted':>7} {'grounded':>9} "
          f"{'stitched':>9} {'invented':>9}")
    for k, v in per_file.items():
        print(f"{k:<26} {v['issues']:>6} {v['quotes']:>7} {v['grounded']:>9} "
              f"{v['stitched']:>9} {len(v['invented']):>9}")
    tot_q = sum(v["quotes"] for v in per_file.values())
    tot_g = sum(v["grounded"] + v["stitched"] for v in per_file.values())
    rate = (tot_g / tot_q * 100) if tot_q else 0.0
    print(f"\ngrounding rate: {tot_g}/{tot_q} = {rate:.0f}%")
    for k, v in per_file.items():
        for b in v["invented"]:
            print(f"  INVENTED [{k}]: {b[:110]}")
    counted = [v["source_quotes"] for v in per_file.values() if "source_quotes" in v]
    if counted:
        totals = {k: sum(c[k] for c in counted) for k in counted[0]}
        print(f"\nsource quotes: {sum(totals.values())}, "
              + ", ".join(f"{n} {k}" for k, n in totals.items())
              + ("" if sources else " (no --source given, so none was compared)"
                 if not args.source else " (no source given is UTF-8, so none was compared)"))
        for row in rows:
            if row.get("source_verdict") == "invented":
                print(f"  INVENTED SOURCE QUOTE [{row['reviewer']}]: {row['source_quote'][:110]}")
            elif row.get("source_verdict") == "not compared" and sources and row.get("source_named"):
                print(f"  NOT COMPARED [{row['reviewer']}]: the issue names {row['source_named']}, "
                      "which no --source gave as text")
    unread = sum(len(v["unparsed"]) for v in per_file.values())
    if unread:
        print(f"\n{unread} issue heading(s) did not parse and are counted in no column above:")
        for k, v in per_file.items():
            for line in v["unparsed"]:
                print(f"  UNPARSED [{k}]: {line[:110]}")
    Path(args.dir, "grounding.json").write_text(
        json.dumps(dict({"representation": args.representation, "rate": rate,
                         "per_reviewer": per_file, "issues": rows},
                        **({"sources": digests} if digests else {})), indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
