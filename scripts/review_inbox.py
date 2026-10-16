#!/usr/bin/env python3
"""Collect human review comments into one place agents read.

You review a diff, leave notes, and walk away. The notes then have to survive
until an agent picks them up, which means they need a location an agent is told
to check, not a place you happened to type them.

Two sources, because they suit different moments:

  INLINE      a marker written straight into the file you are reading:

                  % REVIEW(miros): this claim needs the 2011 cite
                  # REVIEW: is this still true after the epoch change?
                  <!-- REVIEW(miros): tighten this paragraph -->

              Works in any file type, in any editor, with no network. The
              comment sits on the line it is about, which is the whole point.

  PR          line comments left in a GitHub pull request, in the browser or in
              the VSCode GitHub Pull Requests extension. Better review UI, and
              the comment is anchored to a commit rather than to a line that may
              move. Needs the branch pushed.

Both land in `ai/review-inbox.md`, which `AGENTS.md` points agents at. Resolve an
item by deleting the marker (or resolving the PR thread) and re-running collect.
A comment that diagnoses a defect, rather than asking for a change, also enters
the defect ledger with `defect_ledger.py open` before it is fixed; the inbox
says so.

Every file is read as UTF-8. A tracked file that is not UTF-8 and carries the
word REVIEW is named as unreadable rather than read with replacement characters,
because its words would then be captured as verbatim when they are not.

Usage:
  python3 scripts/review_inbox.py collect
  python3 scripts/review_inbox.py collect --pr 7
  python3 scripts/review_inbox.py check      # non-zero if anything is open or unread
  python3 scripts/review_inbox.py collect --json   # the source-linked packet; exits 0
  python3 scripts/review_inbox.py check --json     # the same packet; exits as check does

stdlib only; `gh` needed only for --pr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

# `REVIEW:` or `REVIEW(who):`, but only after a comment leader. Requiring the
# leader is not pedantry: the first version matched `"REVIEW: " + ...` inside a
# Python string in the clarity scorer and reported it as a human review comment.
MARKER_RE = re.compile(
    r"(?:^|\s)(?:\#+|%+|//+|<!--|;+|--|\*)\s*"
    r"REVIEW(?:\(([^)]*)\))?\s*:\s*(.+?)\s*(?:-->|\*/)?\s*$"
)

# Matched as whole path components, never as string prefixes: `.git` must not
# swallow `.github/` or `.gitignore`, nor `build` a file named `buildnotes.md`.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".agent-reviews",
             "build", "workspace/record-staging"}
SKIP_SUFFIXES = {".pdf", ".png", ".jpg", ".zip", ".parquet", ".ttf", ".otf",
                 ".synctex.gz", ".aux", ".log", ".fdb_latexmk", ".fls"}


def git(*args: str) -> str:
    # Git writes paths and messages as UTF-8 bytes, whatever the locale says.
    # surrogateescape keeps a path that is not UTF-8 openable.
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="surrogateescape").stdout


def tracked_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Only tracked files. An untracked scratch file is not a review comment.

    `-z` is what leaves a path alone: without it git quotes a non-ASCII name,
    and the quoted path names no file.
    """
    out = git("-C", str(root), "ls-files", "-z")
    return [root / name for name in out.split("\0") if name]


def skipped_dir(rel: str) -> bool:
    """Whether a path lies under a skipped directory, matched component by component."""
    return any(f"/{d}/" in f"/{rel}" for d in SKIP_DIRS)


def scan_inline(root: pathlib.Path) -> list[dict]:
    return scan_tracked(root)[0]


def scan_tracked(root: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """Markers in tracked files, and the files that could not be read as UTF-8."""
    found = []
    unreadable = []
    occurrences = {}
    for path in tracked_files(root):
        rel = path.relative_to(root).as_posix()
        if skipped_dir(rel):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or not path.exists():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            # A binary file cannot hold a marker; a text file in another encoding
            # can, and replacement characters are not the reviewer's words.
            if b"REVIEW" in data:
                unreadable.append({"file": rel, "reason": (
                    f"not UTF-8 (byte 0x{data[exc.start]:02x} at offset {exc.start}); "
                    "its REVIEW markers were not collected")})
            continue
        for n, line in enumerate(lines, 1):
            match = MARKER_RE.search(line)
            if not match:
                continue
            # Skip this file's own documentation of the marker format.
            if rel == "scripts/review_inbox.py":
                continue
            who = (match.group(1) or "").strip() or "unattributed"
            text = match.group(2).strip()
            key = (rel, who, text)
            occurrences[key] = occurrences.get(key, 0) + 1
            source_id = hashlib.sha256(json.dumps([*key, occurrences[key]]).encode()).hexdigest()
            found.append({"source": "inline", "source_id": "inline:" + source_id,
                          "file": rel, "line": n, "who": who, "text": text,
                          "raw_line": line, "speaker_kind": "human",
                          "file_sha256": hashlib.sha256(data).hexdigest()})
    return found, unreadable


def scan_pr(number: int) -> list[dict]:
    """All line comments on a pull request, via gh; no resolution inferred."""
    repo = git("remote", "get-url", "origin").strip()
    slug = re.sub(r"^.*github\.com[:/](.+?)(?:\.git)?$", r"\1", repo)
    try:
        raw = subprocess.run(
            ["gh", "api", f"repos/{slug}/pulls/{number}/comments", "--paginate"],
            capture_output=True, text=True, encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"could not read PR #{number}: gh returned text that is not UTF-8") from exc
    if raw.returncode != 0:
        raise RuntimeError(f"could not read PR #{number}: {raw.stderr.strip()}")
    found = []
    for c in json.loads(raw.stdout or "[]"):
        found.append({"source": f"PR #{number}", "file": c.get("path", "?"),
                      "line": c.get("line") or c.get("original_line") or 0,
                      "who": c.get("user", {}).get("login", "?"),
                      "text": c.get("body") or "",
                      "source_id": f"{slug}:comment:{c['id']}",
                      "url": c.get("html_url"), "commit_id": c.get("commit_id"),
                      "created_at": c.get("created_at"),
                      "speaker_kind": "agent" if c.get("user", {}).get("type") == "Bot" else "human"})
    return found


def write_inbox(root: pathlib.Path, items: list[dict],
                unreadable: list[dict] = ()) -> pathlib.Path:
    out = root / "ai/review-inbox.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Review inbox",
        "",
        "Human review comments collected for inspection. Generated by",
        "`scripts/review_inbox.py collect`; do not hand-edit, the next run",
        "overwrites it.",
        "",
        "Leave a comment by writing `REVIEW(you): what you want changed` on the",
        "line it concerns, in any file and any comment syntax, or as a line",
        "comment on a pull request. Capture the comment in a trace",
        "record before resolving it and deleting the marker. PR collection",
        "includes all line comments; resolution status is not inferred.",
        "",
        "A comment that diagnoses a defect (an error in the work, not a request",
        "for a change) also gets a defect-ledger row before it is fixed:",
        "`python3 scripts/defect_ledger.py open --label \"...\" --severity ...",
        "--by <who> --source <file> --quote \"<the marked text>\"`; name the",
        "row's id after `Fixes` in the commit that resolves the comment.",
        "",
    ]
    if not items:
        lines += ["Nothing open.", ""]
    else:
        lines += [f"**{len(items)} collected.**", ""]
        by_file: dict[str, list[dict]] = {}
        for item in items:
            by_file.setdefault(item["file"], []).append(item)
        for path in sorted(by_file):
            lines.append(f"## {path}")
            lines.append("")
            for item in sorted(by_file[path], key=lambda i: i["line"]):
                where = f"{path}:{item['line']}"
                lines.append(f"- **{where}** ({item['who']}, {item['source']}) "
                             f"{item['text']}")
            lines.append("")
    if unreadable:
        lines += ["## Could not be read", "",
                  "These tracked files mention REVIEW but are not UTF-8, so any marker",
                  "in them was not collected. Save them as UTF-8 and collect again.", ""]
        lines += [f"- **{u['file']}** {u['reason']}" for u in unreadable]
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["collect", "check"])
    parser.add_argument("--pr", type=int, help="also read comments from this PR")
    parser.add_argument("--json", action="store_true", help="emit source-linked comments for trace-record capture")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        # The --json packet is captured as verbatim words, so it is UTF-8
        # whatever the locale, which is how trace_record.py comments reads it.
        sys.stdout.reconfigure(encoding="utf-8")
    root = pathlib.Path(git("rev-parse", "--show-toplevel").strip())
    items, unreadable = scan_tracked(root)
    if args.pr:
        try:
            items += scan_pr(args.pr)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    # `check` exits 1 when anything is open or unread, in either output form.
    status = 1 if args.command == "check" and (items or unreadable) else 0

    if args.json:
        print(json.dumps({"items": items, "unreadable": unreadable}, indent=2, ensure_ascii=False))
        return status

    if args.command == "check":
        print(f"{len(items)} open review comment(s)")
        for item in items:
            print(f"  {item['file']}:{item['line']}  {item['text'][:70]}")
        report_unreadable(unreadable)
        return status

    out = write_inbox(root, items, unreadable)
    print(f"{len(items)} open review comment(s) -> {out.relative_to(root)}")
    for item in items[:5]:
        print(f"  {item['file']}:{item['line']}  ({item['who']}) {item['text'][:60]}")
    if len(items) > 5:
        print(f"  ... and {len(items) - 5} more")
    report_unreadable(unreadable)
    return status


def report_unreadable(unreadable: list[dict]) -> None:
    if unreadable:
        print(f"{len(unreadable)} file(s) could not be read as UTF-8; markers in them were not collected:")
        for u in unreadable:
            print(f"  {u['file']}  {u['reason']}")


if __name__ == "__main__":
    raise SystemExit(main())
