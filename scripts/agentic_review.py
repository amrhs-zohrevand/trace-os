#!/usr/bin/env python3
"""Agentic review: one reviewer inside a disposable packet, checked host-side.

One agent opens files and runs commands for itself inside an export of one
commit, and the host then checks what it did against the object store and the
transcript. The packet has no `.git`, so the excluded trace records cannot
be recovered from the object store; reads outside it are not blocked, so a
command naming the main checkout is flagged afterwards rather than prevented.
Nothing blocks: every label is printed, the default run records advisory model
review, and only a mechanical failure gives a non-zero exit.

The reviewer is whichever headless CLI the project has: `codex` or `claude`. The
claude invocation, its timeout handling and its reply handling are carried from
the `agentic-openclaw-review` skill's `claude_review_call.py`, which this script
cannot import because the kit ships to other machines and its tests run offline.
`--timeout` bounds every backend, codex included, which had none before.

A run is a run of the kit's `trace-review` procedure. Besides the model-review
row it records, the host writes the check row the skill run contract asks for
(candidate, criterion, method, result, artifact, limits, `procedure` set to
`trace-review` and the version read off the skill file the project holds) to
`check-row.json`, and appends it to a trace record when `--checks-jsonl` names
that record's file.

A run writes two directories of the same name. The review directory,
`ai/reviews/<name>/`, is tracked and holds what a reader needs to judge the
check: the report with the host's labels, the criteria requested and applied and
what the reviewer says it examined, the reply as returned, the prompt, the check
row and the record call. The rows point at that report, so it resolves on any
clone once it is committed. The run directory, `.agent-reviews/agentic-review/<name>/`,
is ignored and keeps the reviewer's own stream, which holds whatever it read;
the report names each file there with its sha256. When the packet carried
people's own words or files Git ignores, the reviewer's text stays in the run
directory too, and the tracked report says so.

The reviewer can be given the sources the candidate is checked against: `--source`
names a file or a folder in the repository, in the project's shared folder (which
is usually a link to a sync app's folder elsewhere on the machine), or a verified
snapshot under workspace/snapshots/. Each file goes into the packet under
`.review/sources/`, and its sha256 is written into the prompt, the report and the
check row's `sources`. A finding may quote a source as well as the candidate, and
the host labels both quotes. A `--path` candidate may lie in the shared folder too.

Prose rules are the project's: the text of `ai/review-prose-rules.md`, when the
project has one, is added to the prompt; a project without it sets none.

Usage:
  python3 scripts/agentic_review.py <sha> --checks
  python3 scripts/agentic_review.py <sha> --checks --backend claude
  python3 scripts/agentic_review.py <sha> --checks --criteria "..." \
      --checks-jsonl workspace/record-staging/<id>/checks.jsonl
  python3 scripts/agentic_review.py <sha> --replay --backend claude \
      --transcript t.jsonl --findings f.json
  python3 scripts/agentic_review.py --path paper/sections/03-x.tex workspace/sandbox/y.md
  python3 scripts/agentic_review.py --path paper/tables/t2.tex \
      --source workspace/shared/data/survey.csv --criteria "every cell matches the data"
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

# The one model-id comparison, shared with `accept`, so the involvement this
# report states and the self-review flag the row records cannot disagree.
from trace_record import actor_id, declared_agents

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONTRACT = HERE / "agentic-review-contract.md"
SCHEMA = HERE / "agentic_review.schema.json"
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_TIMEOUT = 1800
# Seconds the host waits past the stated timeout before it kills the reviewer.
GRACE = 30
# Whether the host can signal a process group. Windows cannot, and kills the
# reviewer's process tree instead.
POSIX_GROUPS = hasattr(os, "killpg")
# The kit's review procedure. A run of this script is a run of that skill, so the
# check row it writes names it, and the version is read off the skill file.
PROCEDURE = "trace-review"
# The three paths that carry people's own words, kept out of the packet.
EXCLUDE = (":!ai/records", ":!sources/intake.jsonl", ":!sources/transitions.jsonl")
# Where a run writes. The run directory keeps the reviewer's own stream and is
# ignored; the review directory, of the same name, is tracked, so the report the
# rows point at resolves on any clone (until 2026-10-14 an advisory run wrote
# everything to the run directory, and another clone read its report as unavailable).
RUN_BASE = Path(".agent-reviews/agentic-review")
REVIEW_BASE = Path("ai/reviews")
# Where the packet carries the sources the candidate is compared with, each under
# the path the project names it by, and the listing of their digests.
SOURCES_DIR = ".review/sources"
# The two finding fields that quote a source. A reply written before they existed
# carries neither, and is read as citing no source.
SOURCE_KEYS = ("source_file", "source_quote")
# The project's own prose rules for a reviewer, when it sets any. Until rc22 the
# contract imposed this repository's rules (no em dash, no AI-tell words) on every
# project the kit was installed in.
PROSE_RULES = Path("ai/review-prose-rules.md")
WS = re.compile(r"\s+")
RECORD_LINE = re.compile(r"^Record:\s*([^\s,;]+)", re.M)
DECISIONS = ("accept", "return", "refuse")
# `codex exec` reports a command as the shell that ran it. Both are needed to ask
# what the reviewer actually executed rather than what its text happens to contain.
# The claude backend reports the bare command, which this matches nothing in, so
# the comparison means the same thing for both.
SHELL_WRAP = re.compile(r"^\s*(?:\S*/)?(?:ba|z|da)?sh\s+(?:-[a-z]+\s+)*-[a-z]*c\s+(?P<body>.*)$",
                        re.S)
SEPARATOR = re.compile(r"&&|\|\||[|;]")
# AH-262: trailing stream redirections, which route output and leave the exit
# status the command's own. Only these: a redirect to a file can fail to open.
STREAM_REDIRECT = re.compile(r"(?:\s+[12]?>&[12]|\s+[12]?>\s*/dev/null)+\s*$")
# How the claude transcript reports a non-zero exit.
CLAUDE_EXIT = re.compile(r"^Exit code (\d+)\s*$")
# The two headless reviewers, each with the model it defaults to, where its prompt
# goes, and whether the operating system confines it to the packet.
BACKENDS = {
    "codex": {"cli": "codex", "model": DEFAULT_MODEL, "prompt": "stdin", "os_sandbox": True},
    "claude": {"cli": "claude", "model": "opus", "prompt": "stdin", "os_sandbox": False},
}
# What each backend's reader counts when a line cannot be read as the run's own
# shape. The second name differs because each stream loses a command differently.
MALFORMED = {
    "codex": ("invalid_json", "completion_without_a_typed_item"),
    "claude": ("invalid_json", "tool_result_without_a_tool_use"),
}
# AH-099: what a clean transcript means, per counter, so the sentence the report
# prints for a backend and the counter that backend's reader keeps stay together.
CLEAN_TRANSCRIPT = {"completion_without_a_typed_item": "every completion row carried a typed item",
                    "tool_result_without_a_tool_use": "every tool result matched a tool use"}
# AH-101: the tools a claude reviewer can reach a path with, and the input keys it
# puts that path or pattern in. codex has no such tools; it runs commands.
FILE_TOOLS = ("Read", "Grep", "Glob")
FILE_TOOL_KEYS = ("file_path", "path", "pattern", "glob")
# Packet-safe, except the suite's git-dependent tests, which the prompt names as a
# packet artifact. check_build_config.py is absent on purpose: it finds documents
# through Git, so in an export it reports zero projects and cannot fail.
DEFAULT_CHECKS = [
    {"name": "portable test suite",
     "command": "python3 -X utf8 -I -S -m unittest discover -s tests",
     "note": "tests needing a Git repository fail in the packet for that reason"},
    {"name": "defect ledger", "command": "python3 scripts/defect_ledger.py check"},
]


def git(*args: str) -> str:
    """Git's output as text. Git writes UTF-8 whatever the host's locale says, so
    the decoding is pinned (AH-155): a Latin-1 or cp1252 locale turned an en dash
    into three characters. Metadata only; a source blob read as quote evidence
    goes through `git_bytes`, where a byte that is not UTF-8 is reported."""
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          check=True, encoding="utf-8", errors="replace").stdout


def git_bytes(*args: str) -> bytes:
    """Git's output as the bytes it wrote, nothing decoded or translated."""
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          check=True).stdout


def norm(text: str) -> str:
    """Collapse whitespace, keeping case: in source, a different case is different text."""
    return WS.sub(" ", text or "").strip()


def manifest(root: Path) -> dict[str, str]:
    """sha256 of every packet file, so a write shows. `__pycache__` is skipped:
    running any Python command writes byte-code there and buries the rest.
    Keys are posix paths: a Windows host must not write `scripts\\b.py`."""
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts}


def rows_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()] if path.exists() else []


def records_in(shas: list[str]) -> dict[str, list[str]]:
    """The trace records the covered commits name in their `Record:` line."""
    found: dict[str, list[str]] = {}
    for sha in shas:
        for rec in RECORD_LINE.findall(git("log", "-1", "--format=%B", sha)):
            rec = rec.rstrip("/")
            if (ROOT / rec / "evidence.json").exists():
                found.setdefault(rec, []).append(sha)
    return found


def commits_naming(record: str, root: Path = ROOT) -> list[str]:
    """Every commit on the checked-out history whose `Record:` line names this
    record, oldest first; empty where the history cannot be read.

    A record's work often spans several commits, of which a review covers one.
    The view named only the reviewed commit, under `commits`, and a reviewer read
    that as the record's whole commit list and reported the others as missing
    (AH-341), so the view carries both lists under names that say which is which.
    """
    def key(text: str) -> str:
        text = text.rstrip("/.,;:)")
        return text if "/" in text else f"ai/records/{text}"

    wanted = key(record)
    result = subprocess.run(["git", "-C", str(root), "log", "--reverse", "-F",
                             f"--grep={Path(wanted).name}", "--format=%x1e%H%x00%B"],
                            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        return []
    found = []
    for entry in result.stdout.split("\x1e"):
        sha, _, body = entry.strip().partition("\x00")
        if sha and any(key(rec) == wanted for rec in RECORD_LINE.findall(body)):
            found.append(sha)
    return found


def minimal_view(record: str, shas: list[str], root: Path = ROOT,
                 named_by: list[str] | None = None) -> dict:
    """One record, reduced to what a reviewer needs to ask whether it fits the diff.
    The human input text never enters it: a decision's question, choice and reason
    are the agent's own summary, and the prompts behind them stay here.

    The reason is sent since rc22 (Miros, 14 October 2026: send the agent's stated
    reasons to outside reviewers), so a reviewer can weigh why a choice was taken
    against the diff; a row that states none reads "not stated".

    `shas` are the reviewed commits that name the record; `named_by`, every
    commit on the history that names it, read from git when not given."""
    from trace_story import decision_reason
    src = root / record
    evidence = json.loads((src / "evidence.json").read_text(encoding="utf-8"))
    return {"record": record, "commits_under_review": shas,
            "commits_naming_the_record": commits_naming(record, root) if named_by is None else named_by,
            "commits_note": "commits_under_review are the commits this review covers that name the "
                            "record; commits_naming_the_record are every commit on the history checked "
                            "out when the review ran whose Record: line names it, the reviewed ones "
                            "included. A commit outside this review is not in the packet.",
            "turn_count": len(evidence.get("turns") or []),
            "decisions": [dict({k: row.get(k, "") for k in ("question", "choice", "status")},
                               reason=decision_reason(row))
                          for row in rows_of(src / "decisions.jsonl")],
            "checks": [{"name": row.get("kind", ""), "outcome": row.get("result", "")}
                       for row in rows_of(src / "checks.jsonl")],
            "withheld": "the human's turns (evidence.json), each decision's human-input "
                        "fields, the summary and the coverage manifest are not in this view",
            "limit": "the question, choice, reason and outcome fields are the agent's own "
                     "summaries and may paraphrase the human's direction"}


def records_evidence(shas: list[str], review: Path, mode: str, inspected_by: str) -> list[str]:
    """Write the records view the mode allows, and say exactly what it holds."""
    if mode == "excluded":
        return ["records: nothing sent; the review is a code review of the commit"]
    found = records_in(shas)
    if not found:
        return ["records: no covered commit names a trace record, so none was sent"]
    (review / "records").mkdir(parents=True, exist_ok=True)
    sent = []
    for record, covered in found.items():
        name = Path(record).name
        if mode == "full":
            shutil.copytree(ROOT / record, review / "records" / name, dirs_exist_ok=True)
            sent.append(f".review/records/{name}/: the whole directory, evidence.json included, "
                        f"so the human's own words went to the model. Inspected first by "
                        f"{inspected_by}")
        else:
            (review / "records" / f"{name}.json").write_text(
                json.dumps(minimal_view(record, covered, root=ROOT), indent=1) + "\n", encoding="utf-8")
            sent.append(f".review/records/{name}.json: record id, the reviewed commits that name "
                        "it (commits_under_review) and every commit on the history whose Record: line "
                        "names it (commits_naming_the_record), turn count, each "
                        "decision's question, choice, reason and status, each check's name and "
                        "outcome. The human's turns, the decisions' human-input fields, the "
                        "summary and the coverage manifest are withheld; a question, choice or "
                        "reason is the agent's own summary and may paraphrase the human's "
                        "direction")
    return sent


# --- Sources: what the candidate is checked against ---------------------------
#
# R asks that a check name what it checks against. Until rc22 the packet held the
# candidate alone, so a reviewer could not reach the data or the cited papers a
# project keeps in its shared folder, outside Git, and a finding could quote only
# the candidate's side of a mismatch.

# The loader for the starter's scripts and the snapshot logic (which path is in a
# snapshot, and whether the snapshot verifies against its manifest) are shared
# with `trace_record.py check --source`, so both say the same of one snapshot.
from trace_record import SNAPSHOTS, snapshot_parts, starter_script, verify_snapshot  # noqa: E402,F401


def shared_name(root: Path | None = None) -> str:
    """The project's shared path, relative to its root, as `shared_folder.py` reads
    it: `shared_root` in ai/project.json, else workspace/shared."""
    module = starter_script("shared_folder")
    return module.shared_path(Path(root or ROOT)) if module else "workspace/shared"


def shared_is_link(root: Path) -> bool:
    """Whether the shared path is a link (a symlink or a Windows junction), so that
    Git holds nothing under it."""
    module = starter_script("shared_folder")
    link = Path(root) / shared_name(root)
    return module.is_link(link) if module else link.is_symlink()


def project_path(name: str, root: Path | None = None) -> str:
    """The path the project names a file or folder by, for one in the repository or
    in its shared folder; ValueError for anything else.

    The shared folder is usually a link to a sync app's folder elsewhere on the
    machine, so what lies in it resolves outside the repository. It is named by the
    path the project knows it under (workspace/shared/..., or `shared_root` in
    ai/project.json) and read through the link, and an absolute path into the
    folder the link points at is named the same way. Until rc22 the resolved path
    alone decided, so every file in a linked shared folder was refused as outside.
    A link inside the shared folder that leads out of it is still refused.
    """
    base = Path(root or ROOT).resolve()
    given = Path(name)
    full = (given if given.is_absolute() else base / given).resolve()
    if full.is_relative_to(base):
        return full.relative_to(base).as_posix()
    shared = shared_name(base)
    if (base / shared).is_dir():
        target = (base / shared).resolve()
        if full.is_relative_to(target):
            rest = full.relative_to(target).as_posix()
            return shared if rest == "." else f"{shared}/{rest}"
    raise ValueError(f"{name} is outside {base} and its shared folder ({shared})")


def file_sha256(path: Path) -> str:
    """The sha256 of a file, read in blocks: a source can be a large dataset."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def walk_files(folder: Path) -> list[Path]:
    """Every regular file under a folder, sorted, links below it not followed and the
    sync clients' housekeeping files (`shared_folder.is_noise`) left out."""
    module = starter_script("shared_folder")
    out = []
    for directory, dirs, names in os.walk(folder):
        dirs.sort()
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink() and not (module and module.is_noise(name)):
                out.append(path)
    return out


def collect_sources(given: list[str], root: Path | None = None) -> list[dict]:
    """Every source file named, once, sorted by the path the project names it by,
    each with its sha256, its size and where it came from.

    A folder stands for every file under it. A snapshot store stands for the files
    of its current snapshot, a snapshot's own folder for the files of its data, and
    a folder or file inside a snapshot is taken from it, each once
    `shared_snapshot.py` has verified that snapshot against its manifest; one that
    does not verify is refused, because a review against bytes the manifest does
    not describe would say nothing about the snapshot it names. `file` is where the
    bytes are on this machine, and goes into no prompt, report or row, because it
    can name a person's home folder.
    """
    base = Path(root or ROOT).resolve()
    shared = shared_name(base)
    found: dict[str, dict] = {}
    for name in given:
        try:
            rel = project_path(name, base)
        except ValueError:
            raise ValueError(f"--source {name} is outside {base}: a source is a file or a folder "
                             f"in this repository, in its shared folder ({shared}) or in a "
                             f"snapshot under {SNAPSHOTS}/") from None
        full, snapshot = base / rel, ""
        parts = snapshot_parts(rel)
        if parts and (parts[1] or (full / "current.json").is_file()):
            snapshot, data = verify_snapshot(base / parts[0], parts[1])
            # What the path names inside the snapshot's folder (f1ca7a5 F001): the
            # folder itself stands for its data, as the store stands for its
            # current snapshot's, and a folder or file in it is taken as named,
            # once the snapshot has verified.
            inner = rel.split("/")[len(parts[0].split("/")) + 2:] if parts[1] else []
            if not parts[1] or not inner:
                files = walk_files(data)
            elif full.is_dir():
                files = walk_files(full)
            elif full.is_file():
                files = [full]
            else:
                raise ValueError(f"--source {name} is not a file or a folder")
            where = f"verified snapshot {snapshot[:12]} of {Path(parts[0]).name}"
        elif full.is_dir():
            files, where = walk_files(full), ""
        elif full.is_file():
            files, where = [full], ""
        else:
            raise ValueError(f"--source {name} is not a file or a folder")
        for path in files:
            rel_file = project_path(str(path), base)
            entry = {"path": rel_file, "file": str(path), "sha256": file_sha256(path),
                     "size": path.stat().st_size,
                     "where": where or ("the shared folder"
                                        if rel_file.startswith(shared + "/") else "the repository")}
            if snapshot:
                entry["snapshot"] = snapshot
            found.setdefault(rel_file, entry)
    return [found[key] for key in sorted(found)]


def source_listing(sources: list[dict]) -> str:
    """`<sha256>  <path>` per source, sorted, as the packet's `.review/sources.txt`."""
    return "".join(f"{entry['sha256']}  {entry['path']}\n" for entry in sources)


def source_sent(sources: list[dict], replay: bool = False, inspected_by: str = "") -> list[str]:
    """What the prompt and the report say of each source: its path, where it came
    from and its sha256, and for a trace-record or registry file that its words went
    to the model and who read them first."""
    if not sources:
        return []
    if replay:
        return [f"{entry['path']}: a source, from {entry['where']}, sha256 {entry['sha256']}, "
                "read again for this replay to label the quotes taken from it"
                for entry in sources]
    held = protected_paths([entry["path"] for entry in sources])
    return ([f"{SOURCES_DIR}/{entry['path']}: a source to check the candidate against, from "
             f"{entry['where']}, sha256 {entry['sha256']}"
             + (f"; a trace-record or registry file, so the words in it went to the model. "
                f"Inspected first by {inspected_by}" if entry["path"] in held else "")
             for entry in sources]
            + [".review/sources.txt: each source above with its sha256"])


def pack_sources(sources: list[dict], packet: Path, inspected_by: str = "") -> list[str]:
    """Copy each source into the packet under `.review/sources/`, check that the copy
    is the bytes the run hashed, and say what was sent."""
    for entry in sources:
        target = packet / SOURCES_DIR / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry["file"], target)
        if file_sha256(target) != entry["sha256"]:
            raise ValueError(f"{entry['path']} changed while it was copied into the packet; "
                             "nothing was run")
    if sources:
        (packet / ".review" / "sources.txt").write_bytes(source_listing(sources).encode("utf-8"))
    return source_sent(sources, inspected_by=inspected_by)


def source_rows(sources: list[dict]) -> list[dict]:
    """The check row's `sources`: each path with its sha256, and the snapshot it was
    taken from when it was."""
    return [dict({"path": entry["path"], "sha256": entry["sha256"]},
                 **({"snapshot": entry["snapshot"]} if entry.get("snapshot") else {}))
            for entry in sources]


def source_prompt(sources: list[dict]) -> list[str]:
    """What the reviewer is told about the sources, when the run sends any."""
    if not sources:
        return []
    return ["", f"Sources to check the candidate against are under `{SOURCES_DIR}/`, each at the "
            "path listed above. Where a finding rests on a source, or says the candidate "
            "disagrees with one, put that source's path as listed (without the "
            f"`{SOURCES_DIR}/` prefix) in `source_file` and copy the source's own text, "
            "verbatim, into `source_quote`. The host labels that quote against the bytes it "
            "hashed, as it labels `quote` against the candidate. A source that is not text, "
            "such as a PDF or a spreadsheet, cannot be labelled; say what you compared in `why`."]


def finding_source(finding: dict) -> str:
    """The source a finding names, as the run listed it."""
    name = str(finding.get("source_file") or "").strip().replace("\\", "/").removeprefix("./")
    return name.removeprefix(SOURCES_DIR + "/")


def source_label(sources: list[dict], finding: dict, fallback=None) -> tuple[str, str]:
    """`verified`, `stitched`, `invented` or `not compared` for the quote a finding
    takes from a source, or `none` when it quotes none.

    The quote is looked for anywhere in the source, because a finding names no lines
    in it: `verified` says the text is in that source, not where, and not that it
    supports the finding. The source is read again and compared with the digest the
    run sent, so one changed since says so rather than being compared. A source the
    run did not send is read through `fallback`, which hands back the bytes and what
    holds them (the commit's tree, or the reviewed copy), and is invented when that
    has none either.
    """
    name, quote = finding_source(finding), norm(str(finding.get("source_quote") or ""))
    if not quote:
        return "none", (f"names {name} and quotes nothing from it" if name else "")
    entry = next((row for row in sources if row["path"] == name), None)
    if entry is None:
        data, held = fallback(name) if fallback and name else (None, "")
        if data is None:
            return "invented", f"{name or 'no source'} is not among the sources this run sent"
    else:
        held = "the source the run sent"
        try:
            data = Path(entry["file"]).read_bytes()
        except OSError as exc:
            return NOT_COMPARED, f"{name} could not be read again ({exc.strerror or exc})"
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            return NOT_COMPARED, (f"{name} changed after the run sent it, so the quote was not "
                                  "compared with it")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return NOT_COMPARED, (f"{name} is not UTF-8 as {held} has it (byte {exc.start}), so the "
                              "quote was not compared with it")
    if quote in norm(text):
        return "verified", ""
    if stitched_quote(quote, norm(text)):
        return "stitched", f"every part of the quote is in {name}, the span as quoted is not"
    return "invented", f"not in {name} as {held} has it"


def commit_source(sha: str):
    """The fallback for a commit run: a source the run did not send, read from the
    commit's tree."""
    def read(name: str):
        try:
            return git_bytes("show", f"{sha}:{name}"), "the commit"
        except subprocess.CalledProcessError:
            return None, ""
    return read


def prose_rules(root: Path | None = None) -> tuple[str, str]:
    """The prose rules this project sets for a reviewer, with their sha256: the text
    of ai/review-prose-rules.md when the project has one, else two empty strings.
    A project without the file sets none, and none is imposed on it."""
    try:
        data = (Path(root or ROOT) / PROSE_RULES).read_bytes()
    except OSError:
        return "", ""
    text = data.decode("utf-8", errors="replace").strip()
    return (text, hashlib.sha256(data).hexdigest()) if text else ("", "")


def prose_section() -> list[str]:
    """The prompt's section for the project's prose rules, or nothing."""
    text, digest = prose_rules()
    if not text:
        return []
    return ["", "## Prose rules this project sets", "",
            f"From `{PROSE_RULES.as_posix()}` (sha256 {digest}):", "", text]


def prose_named() -> str:
    """The prose rules as the report and the row name them: the file and its sha256."""
    digest = prose_rules()[1]
    return f"{PROSE_RULES.as_posix()} (sha256 {digest})" if digest else ""


def build_packet(sha: str, shas: list[str], packet: Path, mode: str,
                 inspected_by: str, sources: list[dict] = ()) -> tuple[dict[str, str], list[str]]:
    """Export the commit, pre-render the diff and metadata, add the sources, and say
    what was sent."""
    packet.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", sha, *EXCLUDE],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(packet)], input=archive, check=True)
    review = packet / ".review"
    review.mkdir(parents=True, exist_ok=True)
    # As bytes (AH-155): the diff is the commit's, and a decoding or a newline
    # translation on the way would hand the reviewer different text.
    (review / "diff.patch").write_bytes(git_bytes("show", sha, "--", ".", *EXCLUDE))
    (review / "commit.txt").write_bytes(
        git_bytes("show", "-s", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B", sha))
    sent = [f"the tree of {sha[:7]}, with ai/records, sources/intake.jsonl and "
            "sources/transitions.jsonl excluded by pathspec, and no .git",
            ".review/commit.txt: the commit's metadata and message",
            ".review/diff.patch: the commit's diff, with the same three paths excluded"]
    sent += records_evidence(shas, review, mode, inspected_by)
    sent += pack_sources(list(sources), packet, inspected_by)  # hash after, not before
    return manifest(packet), sent


def render_prompt(sha: str, subject: str, checks: list[dict], mode: str,
                  sent: list[str], authority: str, criteria: str = "",
                  sources: list[dict] = ()) -> str:
    lines = [CONTRACT.read_text(encoding="utf-8").rstrip()] + prose_section() + [
             "", "## Run", "",
             f"Target commit: `{sha}`", f"Subject: {subject}", f"Records mode: {mode}.", "",
             "Evidence supplied to you:"] + [f"- {item}" for item in sent] + source_prompt(
                 list(sources)) + [""]
    if checks:
        lines += ["Checks you may run, each with its exact command:"]
        lines += [f"- {c['name']}: `{c['command']}`" + (f" ({c['note']})" if c.get("note") else "")
                  for c in checks]
        lines += ["", "Run every check you report, and report its outcome as printed.", ""]
    else:
        lines += ["The sandbox is read-only and no checks are supplied. "
                  "Return `checks` as an empty list.", ""]
    lines += (["Delegation. A named researcher has authorized this review to record a decision "
               "on the covered work. The authorization, verbatim:", "", authority, "",
               "Set `disposition` to accept, return or refuse, and put everything you still "
               "object to in `unresolved_objections`. The researcher remains accountable."]
              if authority else ["No delegation. Set `disposition` to `advisory`."])
    if criteria.strip():
        lines += ["", "Criteria the researcher set:", "", criteria.strip(), "",
                  "Report in `criteria` the criteria you actually applied, whether or not they "
                  "are these. The host records both, so a difference stays visible."]
    return "\n".join(lines) + "\n"


def codex_command(packet: Path, out: Path, model: str, writable: bool) -> list[str]:
    return ["codex", "exec", "-C", str(packet),
            "-s", "workspace-write" if writable else "read-only",
            "--skip-git-repo-check", "--ephemeral", "-m", model,
            "--output-schema", str(SCHEMA), "--json", "-o", str(out / "findings.json"), "-"]


def claude_command(model: str, writable: bool) -> list[str]:
    """`--restricted` drops the shell tools unless `--tools` names them and ignores
    the user's own settings, so the allowlist here is the whole of what the
    reviewer may call; `--permission-prompts none` denies the rest rather than
    asking. The schema goes in as text, and the model returns it through the
    `StructuredOutput` tool."""
    tools = "Bash,Read,Grep,Glob" if writable else "Read,Grep,Glob"
    return ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
            "--restricted", "--tools", tools, "--allowedTools", tools,
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--disable-slash-commands",
            "--json-schema", claude_schema()]


def claude_schema() -> str:
    """The schema as the claude CLI takes it. Its validator knows no 2020-12
    meta-schema, so the `$schema` key is dropped; every rule stays as written
    (a run with the key failed on 2026-09-22 before the reviewer started)."""
    rules = json.loads(SCHEMA.read_text(encoding="utf-8"))
    rules.pop("$schema", None)
    return json.dumps(rules)


def command_for(packet: Path, out: Path, args) -> list[str]:
    """The reviewer's own command line, from the reference implementation for each."""
    if args.backend == "claude":
        return claude_command(args.model, args.checks)
    return codex_command(packet, out, args.model, args.checks)


def printable(command: list[str]) -> list[str]:
    """The command with its one bulky argument named rather than printed: the schema
    is the file beside this script, and a dry run prints the prompt underneath."""
    named = {SCHEMA.read_text(encoding="utf-8"): f"<{SCHEMA.name}>",
             claude_schema(): f"<{SCHEMA.name} without its $schema key>"}
    return [named.get(argument, argument) for argument in command]


def workdir(backend: str, packet: Path) -> Path:
    """Where the child starts. codex is told the packet with `-C` and keeps its own
    launch directory; claude is started inside the packet."""
    return ROOT if backend == "codex" else packet


def child_env(backend: str) -> dict[str, str] | None:
    """A Claude Code session exports these two markers, and the reviewer is a
    separate run rather than a continuation of the session that started it."""
    if backend != "claude":
        return None
    env = dict(os.environ)
    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(name, None)
    return env


def terminate(proc: subprocess.Popen) -> None:
    """Kill the whole group: the reviewer starts its own tools, and killing the
    CLI alone would leave them running.

    AH-153: the CLI dying to SIGTERM says nothing about a tool that ignores it, so
    after the grace period the group gets SIGKILL whether or not the CLI is gone;
    a group already empty is the outcome wanted, not an error. Windows has no
    process groups to signal, so there `taskkill /T /F` ends the CLI's process
    tree while the CLI is still its root; that branch is untested on Windows.
    """
    if not POSIX_GROUPS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def launch(command: list[str], out: Path, cwd: Path, env: dict | None, feed_stdin: bool,
           timeout: int) -> int:
    """Run the reviewer with its stream on disk, and hand back its exit code.

    The wait is the stated timeout plus a grace period, as in the reference
    script: a reviewer told the same number would be killed at the same second,
    before it could write its own last line.
    """
    with (out / "prompt.md").open(encoding="utf-8") as prompt, \
            (out / "transcript.jsonl").open("w", encoding="utf-8") as stdout, \
            (out / "stderr.txt").open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(command, stdin=prompt if feed_stdin else subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, cwd=str(cwd), env=env,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout + GRACE)
        except subprocess.TimeoutExpired:
            terminate(proc)
            print(f"the reviewer was killed after {timeout + GRACE}s; the artifacts are kept "
                  f"in {out}", file=sys.stderr)
            return 124


def parsed_lines(transcript: str, malformed: dict):
    """Every line that reads as a JSON object, counting the ones that do not, so a
    run cannot look auditable because the host skipped what it could not parse."""
    for line in transcript.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            malformed["invalid_json"] += 1
            continue
        if isinstance(row, dict):
            yield row


def codex_facts(transcript: str, malformed: dict) -> tuple[list[dict], int]:
    """The `item.completed` envelope is what says a command finished, not the item's
    own `status`, which reads `failed` on a non-zero exit. A completion counts only
    when it carries a typed `item`, so a bare marker cannot pass as an auditable run.
    """
    ran, conforming = [], 0
    for row in parsed_lines(transcript, malformed):
        if row.get("type") != "item.completed":
            continue
        item = row.get("item")
        if not isinstance(item, dict) or not str(item.get("type") or "").strip():
            malformed["completion_without_a_typed_item"] += 1
            continue
        conforming += 1
        if item["type"] == "command_execution":
            ran.append({"command": item.get("command", ""), "exit_code": item.get("exit_code"),
                        "output": item.get("aggregated_output", ""),
                        "status": item.get("status", "")})
    return ran, conforming


def result_text(content) -> str:
    """A tool result's text, whether it arrived as a string or as text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def claude_row(command: str, result: dict) -> dict:
    """One finished Bash call. A zero exit returns the output alone, and a non-zero
    exit arrives as a leading `Exit code N` line with the output below it."""
    text = result_text(result.get("content"))
    if not result.get("is_error"):
        return {"command": command, "exit_code": 0, "output": text, "status": "completed"}
    head, _, rest = text.partition("\n")
    found = CLAUDE_EXIT.match(head)
    return {"command": command, "exit_code": int(found.group(1)) if found else None,
            "output": rest if found else text, "status": "failed"}


def claude_asked(transcript: str) -> tuple[dict[str, str], dict[str, str]]:
    """Every tool call the reviewer made, by id: what it was called and, for Bash,
    what it asked to run."""
    named, commands = {}, {}
    for row in parsed_lines(transcript, {"invalid_json": 0}):
        message = row.get("message")
        if row.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            continue
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                ident = str(part.get("id") or "")
                named[ident] = str(part.get("name") or "")
                if part.get("name") == "Bash":
                    commands[ident] = str((part.get("input") or {}).get("command") or "")
    return named, commands


def claude_denials(transcript: str) -> dict[str, dict]:
    """The calls the CLI refused, as the CLI itself declares them: the
    `permission_denied` event and the result event's own list, matched to the
    request by `tool_use_id`. The English text of a tool result is not the
    evidence, because a command is free to print those words itself."""
    named, commands = claude_asked(transcript)
    denied: dict[str, dict] = {}
    for row in parsed_lines(transcript, {"invalid_json": 0}):
        if row.get("type") == "system" and row.get("subtype") == "permission_denied":
            ident = str(row.get("tool_use_id") or "")
            denied[ident] = {
                "tool": str(row.get("tool_name") or named.get(ident, "")),
                "command": commands.get(ident, ""),
                "reason": norm(str(row.get("message") or "")).split(". ")[0]
                          or f"decision reason {row.get('decision_reason_type') or 'not stated'}"}
        elif row.get("type") == "result":
            for entry in row.get("permission_denials") or []:
                if not isinstance(entry, dict):
                    continue
                ident = str(entry.get("tool_use_id") or "")
                denied.setdefault(ident, {
                    "tool": str(entry.get("tool_name") or named.get(ident, "")),
                    "command": str((entry.get("tool_input") or {}).get("command")
                                   or commands.get(ident, "")),
                    "reason": "listed in the result event's permission_denials"})
    return denied


def denied_calls(transcript: str, backend: str) -> list[dict]:
    """Calls the reviewer asked for and did not get. Only the claude CLI declares
    them; for codex the host has nothing to read and claims nothing."""
    return list(claude_denials(transcript).values()) if backend == "claude" else []


def claude_facts(transcript: str, malformed: dict) -> tuple[list[dict], int]:
    """Bash calls paired with their results by `tool_use_id`. A call of any other
    tool conforms without a row, so a read-only run is auditable with no commands.
    A denied call is left out: it never ran, so it has no exit status to report and
    nothing it printed, and it is reported apart from the commands that did run.

    AH-102: the result event conforms on its own, so a run that answered without
    calling any tool is auditable with no commands, as the codex agent message is.
    """
    named, commands, ran, conforming = {}, {}, [], 0
    denied = claude_denials(transcript)
    for row in parsed_lines(transcript, malformed):
        if row.get("type") == "result":
            conforming += 1
            continue
        message = row.get("message")
        if row.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            continue
        for part in message.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                conforming += 1
                named[str(part.get("id") or "")] = str(part.get("name") or "")
                if part.get("name") == "Bash":
                    commands[str(part.get("id") or "")] = str(
                        (part.get("input") or {}).get("command") or "")
            elif part.get("type") == "tool_result":
                ident = str(part.get("tool_use_id") or "")
                if ident not in named:
                    malformed["tool_result_without_a_tool_use"] += 1
                elif named[ident] == "Bash" and ident not in denied:
                    ran.append(claude_row(commands.get(ident, ""), part))
    return ran, conforming


def file_tool_output(transcript: str, backend: str) -> list[str]:
    """What each Read, Grep or Glob call returned, when it returned without error.

    AH-163: a claude reviewer reads a file with a tool rather than a shell, so a
    quote it saw was never `seen in output` on a read-only run. These are kept
    apart from `ran`, which holds commands only and is what checks are matched
    against. A denied or failed call returned no file text and is left out; codex
    has no such tools, so nothing is read there.
    """
    if backend != "claude":
        return []
    named, _ = claude_asked(transcript)
    denied = claude_denials(transcript)
    found = []
    for row in parsed_lines(transcript, {"invalid_json": 0}):
        message = row.get("message")
        if row.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            continue
        for part in message.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            ident = str(part.get("tool_use_id") or "")
            if named.get(ident) in FILE_TOOLS and ident not in denied and not part.get("is_error"):
                found.append(result_text(part.get("content")))
    return found


READERS = {"codex": codex_facts, "claude": claude_facts}


def transcript_facts(transcript: str, backend: str = "codex") -> tuple[list[dict], str, dict]:
    """Finished commands with their exit codes and output, plus an audit label.

    Every backend returns the same rows and the same label, so what the host can
    check does not depend on which reviewer ran. An unreadable line is counted
    rather than skipped in silence.
    """
    malformed = dict.fromkeys(MALFORMED[backend], 0)
    ran, conforming = READERS[backend](transcript, malformed)
    label = "auditable" if conforming else "unauditable"
    skipped = sum(malformed.values())
    if skipped:
        label += f", {skipped} malformed line(s) skipped"
    return ran, label, malformed


def reviewer_id(transcript: str, backend: str, stated: str) -> tuple[str, str]:
    """The model the run resolved to, and whether that was observed or only asked
    for. The codex stream names no model anywhere, so there the flag is all there
    is, and the report says which of the two it is printing."""
    for row in parsed_lines(transcript, {"invalid_json": 0}):
        if backend == "claude" and row.get("type") == "system" and row.get("subtype") == "init":
            found = str(row.get("model") or "").strip()
        else:
            continue
        if found:
            return found, "observed"
    return stated, "requested"


def last_event(transcript: str, kind: str) -> dict | None:
    """The last line of that type, which is where the claude CLI puts its reply."""
    found = None
    for row in parsed_lines(transcript, {"invalid_json": 0}):
        if row.get("type") == kind:
            found = row
    return found


def review_schema() -> dict:
    """The schema beside this script, which every reply is held against."""
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def schema_violation(value, rule: dict, where: str = "the reply") -> str:
    """Where the value departs from the schema first, or an empty string when it
    does not. Enough of JSON Schema for the file beside this script, because a key
    that is present but holds the wrong shape would reach the labelling code and
    fail there, where the reply is no longer in hand to explain it."""
    kinds = {"object": dict, "array": list, "string": str, "number": (int, float),
             "integer": int, "boolean": bool}
    want = rule.get("type")
    if want in kinds and not isinstance(value, kinds[want]):
        return f"{where} is not {want}"
    if want in ("number", "integer") and isinstance(value, bool):
        return f"{where} is not {want}"
    if rule.get("enum") is not None and value not in rule["enum"]:
        return f"{where} is not one of {', '.join(map(str, rule['enum']))}"
    if isinstance(value, dict):
        properties = rule.get("properties") or {}
        for name in rule.get("required") or []:
            if name not in value:
                return f"{where} is missing {name}"
        if rule.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    return f"{where} carries {name}, which the schema does not allow"
        for name, inner in properties.items():
            if name in value and (found := schema_violation(value[name], inner,
                                                            f"{where}.{name}")):
                return found
    if isinstance(value, list) and isinstance(rule.get("items"), dict):
        for index, entry in enumerate(value):
            if found := schema_violation(entry, rule["items"], f"{where}[{index}]"):
                return found
    return ""


def claude_findings(transcript: str) -> tuple[dict | None, str]:
    """The object the `StructuredOutput` tool produced, which the CLI validated
    against the schema before putting it in the result event."""
    result = last_event(transcript, "result")
    if result is None:
        return None, "the transcript holds no result event"
    if "structured_output" not in result:
        return None, (f"the result event (subtype {result.get('subtype') or 'none'}) carries no "
                      "structured_output, so nothing the reviewer returned matched the schema")
    found = result["structured_output"]
    if not isinstance(found, dict):
        return None, "structured_output is not a JSON object"
    return found, ""


def collect_findings(args, out: Path, transcript: str) -> dict | None:
    """The reviewer's reply, from wherever its own backend leaves it.

    codex is told to write `findings.json` itself; claude writes no file, so the
    host takes the reply off the transcript and writes that file for it.
    None means there is nothing to check, and why is already printed.
    """
    if args.backend == "codex":
        if not (out / "findings.json").exists():
            print("no findings.json: the reviewer returned nothing to check", file=sys.stderr)
            return None
        try:
            return json.loads((out / "findings.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"findings.json is not JSON: {exc}", file=sys.stderr)
            return None
    found, why = claude_findings(transcript)
    if found is None:
        print(f"no findings from {args.backend}: {why}. Nothing is verified and nothing "
              f"is recorded; the artifacts are kept in {out}.", file=sys.stderr)
        return None
    (out / "findings.json").write_text(json.dumps(found, indent=1) + "\n", encoding="utf-8")
    return found


def with_source_keys(findings):
    """The reply as the schema reads it, a reply written before rc22 included.

    The schema requires `source_file` and `source_quote` on every finding, because
    codex's structured output takes only a schema whose every key is required, so
    a finding that quotes no source returns them empty. A reply from before the
    two existed has neither; it is read as quoting no source rather than refused.
    The reply itself is not changed."""
    if not isinstance(findings, dict) or not isinstance(findings.get("findings"), list):
        return findings
    rows = [dict({key: "" for key in SOURCE_KEYS}, **row)
            if isinstance(row, dict) and not any(key in row for key in SOURCE_KEYS) else row
            for row in findings["findings"]]
    return dict(findings, findings=rows)


def schema_refusal(findings) -> str:
    """Where a loaded reply departs from the schema, as the refusal to print.

    AH-096: this runs on the findings every path loads, fresh or replay and
    whichever backend answered, because a key that is present but holds the wrong
    shape reaches the labelling code and fails there, where the reply is no longer
    in hand to explain it. Empty means the reply matches.
    """
    broken = schema_violation(with_source_keys(findings), review_schema())
    return (f"refusing to verify: {broken}. The reply is held against "
            f"scripts/{SCHEMA.name} before anything is labelled, in a fresh run as in a "
            "replay; nothing is verified and nothing is recorded.") if broken else ""


def sandbox_of(backend: str, writable: bool) -> str:
    """What actually held the reviewer in, in that backend's own terms."""
    if backend == "claude":
        return ("restricted tools Bash,Read,Grep,Glob, no operating-system sandbox" if writable
                else "restricted tools Read,Grep,Glob, no shell")
    return "workspace-write" if writable else "read-only"


def classify_quote(text: str | None, finding: dict, held_by: str = "the commit") -> tuple[str, str]:
    """`verified`, `misplaced` or `invented`, over the file's text as the commit has it.

    `held_by` names what holds that text, so a path run's note says the copy it
    read rather than a commit it never read.
    """
    path, quote = str(finding.get("file") or ""), norm(str(finding.get("quote") or ""))
    if not quote:
        return "invented", "the finding carries no quote"
    if text is None:
        return "invented", f"{path} is not in {held_by}"
    lines = text.splitlines()
    start = max(1, int(finding.get("line_start") or 1))
    end = min(len(lines), int(finding.get("line_end") or start))
    if quote in (norm("\n".join(lines[start - 1:end])) if start <= end else ""):
        return "verified", ""
    if quote in norm(text):
        return "misplaced", f"the text is in {path}, not at lines {start}-{end}"
    return "invented", f"not in {path} as {held_by} has it"


def quote_label(sha: str, finding: dict) -> tuple[str, str]:
    """Read the cited file from the object store, never from the packet, then classify."""
    try:
        data = git_bytes("show", f"{sha}:{finding.get('file') or ''}")
    except subprocess.CalledProcessError:
        return refine_invented(classify_quote(None, finding), None, finding)
    return label_bytes(data, finding, "the commit")


# AH-155: the label for a file whose bytes are not UTF-8. Replacing the bad bytes
# would let a quote carrying the replacement character match text that is not
# there, and calling the quote invented would say the reviewer made it up.
NOT_COMPARED = "not compared"


def label_bytes(data: bytes, finding: dict, held_by: str) -> tuple[str, str]:
    """Classify a quote over the file's bytes, decoded strictly, and say so when
    they cannot be decoded rather than compare the quote with altered text."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return NOT_COMPARED, (f"{finding.get('file') or 'the file'} is not UTF-8 as {held_by} "
                              f"has it (byte {exc.start}), so the quote was not compared with it")
    return refine_invented(classify_quote(text, finding, held_by), text, finding)


# A sentence ends at one of these and the break stays with the part before it, so a
# part keeps the punctuation it was quoted with.
SENTENCE_END = re.compile(r"(?<=[.;:])\s+")


def stitched_quote(quote: str, text: str) -> bool:
    """Whether the quote joins parts that are each in the text but are not adjacent.

    Read over the same text `classify_quote` reads, whitespace collapsed and case
    kept, so a retyped quote is not rescued here. Every part must occur verbatim
    and there must be more than one, which is what makes this a joint rather than
    a fabrication: one invented clause, of any length, leaves the quote invented.
    """
    parts = [part for part in SENTENCE_END.split(norm(quote)) if part.strip()]
    return len(parts) > 1 and all(part in text for part in parts)


def refine_invented(labelled: tuple[str, str], text: str | None,
                    finding: dict) -> tuple[str, str]:
    """Separate a stitched quote from an invented one, in either mode.

    `classify_quote` asks whether the span occurs. Where it does not, this asks
    whether each part of it does, because calling a quote invented says the
    reviewer made the text up, and a joint of real passages is a citation fault
    instead. Only `invented` is ever revisited.
    """
    label, note = labelled
    if label != "invented" or text is None:
        return label, note
    if not stitched_quote(str(finding.get("quote") or ""), norm(text)):
        return label, note
    return "stitched", (f"every part of the quote is in {finding.get('file') or 'the file'}, "
                        "the span as quoted is not")


def seen_label(quote: str, ran: list[dict], read: list[str] = ()) -> str:
    """Whether the normalized quote appears in some completed command's captured
    output, or in what a file-tool call returned (AH-163)."""
    quote = norm(quote)
    outputs = [r["output"] for r in ran] + list(read)
    return "seen in output" if quote and any(quote in norm(text) for text in outputs) \
        else "not seen in output"


def unwrapped(command: str) -> str:
    """The command the shell was asked to run, with up to three wrappers peeled."""
    text = command
    for _ in range(3):
        match = SHELL_WRAP.match(text)
        if not match:
            break
        text = match.group("body").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
            text = text[1:-1]
    return text


def executed(command: str) -> str:
    """The leading executable segment: what the command started by running."""
    return norm(SEPARATOR.split(unwrapped(command), 1)[0])


def bare(command: str) -> str:
    """The command with its trailing stream redirections dropped (AH-262): a check
    run as `... 2>&1` is the check run alone, and its exit is still its own."""
    return STREAM_REDIRECT.sub("", command)


def check_label(entry: dict, ran: list[dict]) -> dict:
    """Match a reported check to a command the transcript shows finishing.

    An exit code is the check's own only when the shell ran the check and nothing
    else. With extra arguments the command is not the check that was asked for,
    and in a pipeline or a command list the status belongs to the line, so both
    are labelled as matched with the shell's status rather than the check's.

    AH-094: a row whose exit the transcript did not record is never `matched
    (exit N)`. It says so instead, so a failure carrying no execution evidence
    cannot read as a check that ran and passed.

    AH-162: the label is the latest matching run's, because a check run again
    after the packet changed is the one the reviewer's outcome describes. Every
    matching run stays in `repeats`, in order, so a pass followed by a failure, or
    the reverse, is visible rather than decided by which came first.
    """
    want = norm(bare(str(entry.get("command") or "")))
    matches = [found for row in ran if want and (found := match_run(want, row))]
    if not matches:
        return {"label": "no matching command", "exit_code": None, "shell_exit": None,
                "tail": "", "repeats": ""}
    last = dict(matches[-1])
    if len(matches) > 1:
        differ = len({(m["exit_code"], m["shell_exit"]) for m in matches}) > 1
        last["repeats"] = (f"{len(matches)} matching runs"
                           + (f" {DIFFERENT_EXITS}" if differ else "")
                           + ", in order: " + "; ".join(m["label"] for m in matches)
                           + "; the label is the last")
    return last


# The words a check's `repeats` carries when its matching runs did not all exit alike.
DIFFERENT_EXITS = "with different exits"


def match_run(want: str, row: dict) -> dict | None:
    """One finished command read against one reported check: its label, or None.
    An isolated run carries the check's own exit; a longer line carries the shell's."""
    whole, head = norm(bare(unwrapped(row["command"]))), executed(row["command"])
    tail = (row["output"].strip().splitlines() or [""])[-1]
    if whole == want and not SEPARATOR.search(want):
        if row["exit_code"] is None:
            return {"label": "matched, exit not recorded", "exit_code": None,
                    "shell_exit": None, "tail": tail, "repeats": ""}
        return {"label": f"matched (exit {row['exit_code']})", "exit_code": row["exit_code"],
                "shell_exit": row["exit_code"], "tail": tail, "repeats": ""}
    if whole == want or head == want or head.startswith(want + " "):
        return {"label": f"matched, exit not isolated (shell exit {row['exit_code']})",
                "exit_code": None, "shell_exit": row["exit_code"], "tail": tail, "repeats": ""}
    return None


def names_outside(text: str, root: Path) -> bool:
    """Whether the text names the main checkout or a records path. Textual, and said
    to be: a search for the string is named too.

    AH-165: separators are made forward on both sides first, because a Windows
    reviewer writes `C:/Users/...` or `ai\\records\\...` as readily as the
    backslashed root, and comparing raw strings missed both.
    """
    flat = (text or "").replace("\\", "/")
    return bool(flat) and (str(root).replace("\\", "/") in flat or "ai/records" in flat)


def file_tool_calls(transcript: str, backend: str, root: Path = ROOT) -> list[dict]:
    """Read, Grep and Glob calls whose path or pattern names the main checkout or a
    records path, as rows carrying the tool name in place of the command.

    AH-101: a claude reviewer reaches a file with a tool rather than a shell, so
    reading only the commands left those calls invisible to every label. codex has
    no such tools, and no other transcript declares them, so nothing is claimed
    there.
    """
    if backend != "claude":
        return []
    found = []
    for row in parsed_lines(transcript, {"invalid_json": 0}):
        message = row.get("message")
        if row.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            continue
        for part in message.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool_use":
                continue
            if part.get("name") not in FILE_TOOLS:
                continue
            inputs = part.get("input")
            for key in FILE_TOOL_KEYS if isinstance(inputs, dict) else ():
                value = str(inputs.get(key) or "")
                if names_outside(value, root):
                    found.append({"command": f"{part['name']} {value}", "exit_code": None,
                                  "output": "", "status": "file-tool call"})
                    break
    return found


def outside_packet(ran: list[dict], root: Path = ROOT, transcript: str = "",
                   backend: str = "codex") -> list[dict]:
    """Commands and file-tool calls naming the main checkout or a records path.
    Textual, and said to be: a search for the string is listed too. This flags; it
    never gates."""
    return ([r for r in ran if names_outside(r["command"], root)]
            + file_tool_calls(transcript, backend, root))


def modified(before: dict, after: dict) -> list[str] | None:
    """Paths the reviewer added, removed or changed, from the two manifests."""
    if not (before or after):
        return None
    return ([f"added {n}" for n in sorted(after.keys() - before.keys())]
            + [f"removed {n}" for n in sorted(before.keys() - after.keys())]
            + [f"changed {n}" for n in sorted(before.keys() & after.keys()) if before[n] != after[n]])


def involved(producers: list[str], selectors: list[str], model: str) -> list[str]:
    """How the reviewer took part in the work it is reviewing. Empty means no visible part."""
    low, hits = actor_id(model), []
    if low in {actor_id(n) for n in producers}:
        hits.append(f"{model} executed a covered commit (Execution-agent trailer)")
    if low in {actor_id(n) for n in selectors}:
        hits.append(f"{model} selected a recorded decision in the cited trace record")
    return hits


def involvement(shas: list[str], model: str) -> tuple[list[str], list[str], list[str]]:
    """Producers from the range's trailers, selectors from the cited records' decisions."""
    producers, selectors = set(), set()
    for sha in shas:
        producers.update(declared_agents(git("log", "-1", "--format=%B", sha)))
    for record in records_in(shas):
        for row in rows_of(ROOT / record / "decisions.jsonl"):
            selectors.add(str((row.get("selected_by") or {}).get("name") or "").strip())
    selectors.discard("")
    return sorted(producers), sorted(selectors), involved(sorted(producers), sorted(selectors), model)


def verify(sha: str, findings: dict, transcript: str, before: dict, after: dict,
           backend: str = "codex", label_quote=None, label_source=None) -> dict:
    """Label every finding and every reported check against evidence the host holds.

    `label_quote` is where the cited text comes from: the object store by default,
    the candidate's own bytes for a path run. `label_source` labels the quote a
    finding takes from a source; by default only the commit's tree can supply one.
    """
    ran, label, malformed = transcript_facts(transcript, backend)
    read = file_tool_output(transcript, backend)
    label_quote = label_quote or (lambda row: quote_label(sha, row))
    label_source = label_source or (lambda row: source_label([], row, commit_source(sha)))
    rows = []
    for finding in findings.get("findings") or []:
        verdict, note = label_quote(finding)
        source, source_note = label_source(finding)
        rows.append(dict(finding, quote_label=verdict, quote_note=note,
                         seen_label=seen_label(str(finding.get("quote") or ""), ran, read),
                         source_label=source, source_note=source_note))
    return {"findings": rows, "ran": ran, "transcript": label, "malformed": malformed,
            "checks": [dict(e, **check_label(e, ran)) for e in findings.get("checks") or []],
            "outside": outside_packet(ran, ROOT, transcript, backend),
            "modified": modified(before, after),
            "denied": denied_calls(transcript, backend)}


def block(pairs: list[tuple[str, object]]) -> list[str]:
    """One labelled field per line, complete: nothing here is truncated."""
    return [f"- {name}: {value}" for name, value in pairs if str(value or "").strip()]


def no_os_sandbox(meta: dict) -> bool:
    """Whether the run had nothing but the reviewer's own rules holding it in."""
    return "no operating-system sandbox" in meta["sandbox"]


def transcript_clean(malformed: dict) -> str:
    """The line the report prints when nothing was malformed.

    AH-099: it is built from the counters this run's own reader kept, so it states
    the check that reader performed rather than one backend's shape.
    """
    told = [CLEAN_TRANSCRIPT[name] for name in malformed if name in CLEAN_TRANSCRIPT]
    return "Every line parsed, and " + ", ".join(told or ["nothing else was counted"]) + "."


def modified_section(meta: dict, result: dict) -> list[str]:
    """The manifest difference, and what a manifest of the packet cannot see."""
    body = (["Not captured: this was a replay, so the packet was not hashed."]
            if result["modified"] is None
            else ([f"- {item}" for item in result["modified"]] or ["None."])
            + ["", "Byte-code caches under `__pycache__` are not counted."])
    body += ["", "Within the packet: both manifests are of the packet alone, so a write "
             "elsewhere is not seen."]
    if no_os_sandbox(meta):
        body += ["No operating-system sandbox: a write outside the packet is not prevented, "
                 "only a command naming the main checkout is flagged."]
    return body


def shown(path: Path) -> str:
    """A path as a row or a report names it: relative to the root where it lies
    under it, with forward slashes whatever the host's separator."""
    path = Path(path)
    return (path.relative_to(ROOT) if path.is_relative_to(ROOT) else path).as_posix()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def criteria_section(meta: dict) -> list[str]:
    """The criteria the researcher set and the ones the reviewer says it applied,
    side by side, so a difference stays visible."""
    requested = meta["criteria"].strip()
    applied = [str(item) for item in meta["findings_json"].get("criteria") or []]
    return ([f"- requested: {requested}" if requested else
             "- requested: none set, so the contract's standing criterion applied "
             "(scripts/agentic-review-contract.md)"]
            + ([f"- reported applied: {item}" for item in applied]
               or ["- reported applied: not stated"])
            + [f"- prose rules: {meta.get('prose') or 'none set by this project'}"])


def examined_section(listed: dict) -> list[str]:
    """`evidence_examined` as the reviewer returned it: what it says it opened,
    compared or ran. The host checks quotes against the source and checks against
    the transcript; this account it prints, and compares with neither."""
    stated = listed.get("evidence_examined")
    if not isinstance(stated, dict):
        return ["Not stated: the reply carries no `evidence_examined`."]
    body = []
    for key, title in (("paths", "Paths it says it opened or compared"),
                       ("commands", "Commands it says it ran")):
        items = [str(item) for item in stated.get(key) or []]
        body += [f"{title}:", ""] + ([f"- `{item}`" for item in items] or ["- none stated"]) + [""]
    return body + ["This is the reviewer's own account. The labels above and below compare quotes "
                   "with the source and checks with the transcript; nothing compares this list "
                   "with either."]


def kept_section(kept: list[tuple[str, Path]]) -> list[str]:
    """The files this report describes and does not carry, each with its sha256,
    so whoever holds them can show they are the ones the report was written over."""
    return ([f"- {name}: `{shown(path)}`, sha256 {sha256_of(path)}" for name, path in kept]
            + ["", "None of these is copied into the review directory, which is tracked: the "
               "reviewer's stream holds whatever the reviewer read, the packet's content included "
               "and anything it reached outside the packet. By default they stay in the run "
               "directory under `.agent-reviews/`, which Git ignores."])


LABELS_ESTABLISH = (
    "A quote label says where the text occurs in the commit, not whether it supports the "
    "finding. `stitched` says every part of the quote is in the file and the span as "
    "quoted is not, which is a citation fault and not a fabrication. `not compared` says "
    "the file is not UTF-8, so the quote was not compared with it. `seen in output` "
    "says the normalized quote appeared in a command's captured "
    "output or in what a Read, Grep or Glob call returned; a numbered listing splits a "
    "multi-line quote, so its absence is weaker "
    "evidence than its presence. A matched check names the command the transcript shows "
    "finishing, the last such run where there were several, and the captured line above "
    "it is the transcript's, not the reviewer's "
    "account of it. Whether a finding is right is judgment; no label here supplies that.")
# Said only where a run sent sources or a finding quoted one, so a report without
# them reads as it did before rc22.
SOURCE_LABELS_ESTABLISH = (
    "A source quote label says whether the text the finding takes from a source is in that "
    "source, read again and compared with the sha256 the run sent; it says nothing about "
    "where in the source, and nothing about whether the source supports the finding. A "
    "source that is not text, or changed after the run sent it, is `not compared`.")


def establishes(meta: dict, result: dict) -> list[str]:
    """The closing section: what the labels establish, and the run's own limits."""
    sourced = meta.get("sources") or any(row.get("source_label", "none") != "none"
                                         for row in result["findings"])
    return ([LABELS_ESTABLISH] + ([SOURCE_LABELS_ESTABLISH] if sourced else [])
            + list(meta.get("limits_extra") or []))


def report_head(meta: dict, result: dict) -> list[str]:
    """The title, the reviewer and the run, and what kind of review this is."""
    top = [f"# {report_title(meta)}", "",
           f"Reviewer: {meta['model']} via {meta['backend']} (identity {meta['identity']}). "
           f"Run {meta['at']}. Sandbox {meta['sandbox']}. Records {meta['records']}. "
           f"Transcript {result['transcript']}, "
           f"{len(result['ran'])} finished commands.", ""]
    top += ([f"Delegated decision: **{meta['decision']}**, under {meta['authorized_by']}'s "
             "authority, who remains accountable for the delegation and for relying on the work. "
             "Authorization, verbatim:", "", "> " + meta["authority"].replace("\n", "\n> ")]
            if meta["authorized_by"] else ["A model raised these. That is review, not acceptance."])
    if meta.get("override"):
        top += ["", meta["override"]]
    if meta["involvement"]:
        top += ["", "**Not independent of production.** " + "; ".join(meta["involvement"])
                + ". Recorded as advisory model review, with no decision."]
    return top


def write_report(out: Path, meta: dict, result: dict, sent: list[str],
                 kept: list[tuple[str, Path]] = ()) -> Path:
    """What was sent, what each label found, and what the labels do not establish.
    `kept` names the files the report describes that stay out of Git."""
    listed, findings, checks = meta["findings_json"], [], []
    top = report_head(meta, result)
    for row in result["findings"]:
        sourced = row.get("source_label", "none") != "none" or finding_source(row)
        findings += [f"### {row.get('id', 'finding')} ({row.get('severity', 'unrated')})", ""] + block(
            [("file", row.get("file")),
             ("lines", f"{row.get('line_start', '')}-{row.get('line_end', '')}"),
             ("quote", row["quote_label"]), ("quote note", row["quote_note"]),
             ("seen", row["seen_label"]),
             ("source", finding_source(row)),
             ("source quote", row.get("source_label") if sourced else ""),
             ("source quote note", row.get("source_note") if sourced else ""),
             ("claim", row.get("claim")),
             ("why", row.get("why")), ("ask", row.get("ask"))]) + [
            "", "Quoted:", "", "> " + str(row.get("quote") or "").replace("\n", "\n> "), ""]
        if str(row.get("source_quote") or "").strip():
            findings += ["Quoted from the source:", "",
                         "> " + str(row.get("source_quote")).replace("\n", "\n> "), ""]
    for row in result["checks"]:
        checks += [f"### {row.get('name', 'check')}", ""] + block(
            [("command", f"`{row.get('command', '')}`"), ("label", row["label"]),
             ("outcome the reviewer reported", row.get("outcome")),
             ("last line captured from the transcript",
              row["tail"] or "nothing captured: no command in the transcript matched"),
             ("runs", row.get("repeats")),
             ("note", "the shell line carried extra arguments or another command, so this exit "
              "status is the line's and not the check's"
              if "not isolated" in row["label"] else "")]) + [""]
    sections = [
        ("Evidence sent to the reviewer", [f"- {item}" for item in sent]),
        ("Criteria", criteria_section(meta)),
        ("Evidence the reviewer examined", examined_section(listed)),
        ("Findings", findings), ("Checks", checks),
        ("Evidence the reviewer could not examine",
         [f"- {item}" for item in listed.get("evidence_unavailable") or []]),
        ("Unresolved objections",
         [f"- {item}" for item in listed.get("unresolved_objections") or []]),
        ("Commands and file-tool calls naming the main checkout or a records path",
         ([f"- `{row['command']}`" for row in result["outside"]] or ["None."])
         + ["", "A read outside the packet is not blocked, so this flags after the fact, and it "
            "is textual: a command or a file-tool call that only names the string is listed too. "
            "A file-tool call is listed by its tool name, and only the claude transcript "
            "declares one."]),
        ("Calls the reviewer was denied",
         [f"- {row['tool']}: `{row['command'] or 'no command recorded'}` ({row['reason']})"
          for row in result["denied"]]
         + (["", "The CLI declared each of these; none of them ran, so none carries an exit "
             "status or any output."] if result["denied"] else [])),
        ("Paths the reviewer modified", modified_section(meta, result)),
        ("Transcript", [f"- {name.replace('_', ' ')}: {count}"
                        for name, count in result["malformed"].items() if count]
         or [transcript_clean(result["malformed"])]),
    ]
    if kept:
        sections.append(("Kept out of the review directory", kept_section(kept)))
    sections.append(("What the labels establish", establishes(meta, result)))
    for title, body in sections:
        top += ["", f"## {title}", ""] + (body or ["None."])
    (out / "report.md").write_text("\n".join(top) + "\n", encoding="utf-8")
    return out / "report.md"


def private_why(name: str, root: Path | None = None) -> str:
    """Why text from this file is not copied into a tracked file, or an empty string.

    A trace-record or registry file, or the packet's view of one, carries people's
    own words, and a copy outside the record escapes the disclosure review that
    binds the record's own files. A file Git ignores is kept out of Git on purpose,
    and a path outside the repository is no part of what was reviewed. The shared
    folder is outside Git by design, linked or not, so it is named here without
    asking Git (rc22 release checks: in an archive unpacked without Git, a shared
    source read as tracked). Elsewhere, where Git cannot answer (no repository, no
    Git), a file is not called ignored, because nothing said it was.
    """
    flat = str(name or "").replace("\\", "/").strip()
    if not flat:
        return ""
    rel = flat.removeprefix("./")
    if rel.casefold().startswith(".review/records") or protected_paths([rel]):
        return "a trace-record or registry file"
    # The root is read when the call is made (ee9a334 F002): a default bound at
    # import time asked Git about the checkout the module was loaded from.
    base = Path(root or ROOT).resolve()
    # Git holds nothing through a linked shared folder, and cannot be asked about a
    # path that crosses the link, so the answer is given here rather than read as
    # "not ignored" off Git's refusal.
    shared = shared_name(base)
    if rel == shared or rel.startswith(shared + "/"):
        return "in the shared folder, which Git does not hold"
    full = (Path(flat) if Path(flat).is_absolute() else base / flat).resolve()
    if not full.is_relative_to(base):
        return "outside the repository"
    try:
        done = subprocess.run(["git", "-C", str(base), "check-ignore", "-q", "--",
                               full.relative_to(base).as_posix() or "."], capture_output=True)
    except OSError:
        return ""
    return "ignored by Git" if done.returncode == 0 else ""


def withheld_reason(records: str, candidates: list[str], cited: list[str],
                    sources: list[str] = ()) -> str:
    """Why the reviewer's own text stays out of the tracked copy of the report, or
    an empty string when it need not.

    The reviewer's text can repeat whatever its packet held. When that included
    trace records sent whole, or a candidate or a source whose text stays out of
    Git, or when a finding cites such a file, the text stays in the run directory
    with the stream, and the tracked copy carries the host's labels and counts.
    A source in the shared folder is such a file: it is kept out of Git on purpose.
    """
    reasons = []
    if records == "full":
        reasons.append("the packet carried trace records whole (--records full), so the "
                       "reviewer's text can repeat people's own words")
    # A cited file that is also a candidate or a source is named once, as that.
    sources = [name for name in sources if name not in candidates]
    for names, what in ((candidates, "the candidates include"),
                        (sources, "the sources include"),
                        ([name for name in dict.fromkeys(cited)
                          if name not in candidates and name not in sources],
                         "a finding cites")):
        held = [f"{name} ({why})" for name in names if (why := private_why(name))]
        if held:
            reasons.append(f"{what} files whose text stays out of Git: {', '.join(held)}")
    return "; ".join(reasons)


def kept_apart(files: list[tuple[str, Path]], review: Path) -> list[tuple[str, Path]]:
    """The files that exist and lie outside the review directory: the ones the
    report names rather than carries."""
    return [(name, Path(path)) for name, path in files
            if Path(path).is_file() and Path(path).resolve().parent != review.resolve()]


def write_withheld_report(review: Path, meta: dict, result: dict, sent: list[str],
                          kept: list[tuple[str, Path]]) -> Path:
    """The tracked copy of a report whose reviewer text stays out of Git: the run,
    what was sent, the criteria requested, and the host's labels and counts, with
    none of the reviewer's own words."""
    listed = meta["findings_json"]
    stated = listed.get("evidence_examined")

    def withheld_count(items, what: str) -> list[str]:
        return [f"{len(items)} {what}, withheld."] if items else []

    reason = meta["withheld"]
    sections = [
        ("Withheld from this copy",
         [reason[:1].upper() + reason[1:] + ".", "",
          "So this copy carries the host's labels and counts and none of the reviewer's own "
          "text: no quote, claim, reason or ask, no criterion it says it applied, no check it "
          "reported or line a check's output ended on, no objection, and no account of what it "
          "examined. The complete report and the reply as returned stay in the run directory, "
          "named below with their sha256."]),
        ("Evidence sent to the reviewer", [f"- {item}" for item in sent]),
        ("Criteria", criteria_section(meta)[:1]
         + [f"- reported applied: {len(listed['criteria'])} stated, withheld"
            if listed.get("criteria") else "- reported applied: not stated"]
         + criteria_section(meta)[-1:]),
        ("Evidence the reviewer examined",
         [f"{len(stated.get('paths') or [])} path(s) and {len(stated.get('commands') or [])} "
          "command(s) stated, withheld."]
         if isinstance(stated, dict) and (stated.get("paths") or stated.get("commands"))
         else ["None stated."] if isinstance(stated, dict)
         else ["Not stated: the reply carries no `evidence_examined`."]),
        ("Findings", [f"- {row.get('id', 'finding')} ({row.get('severity', 'unrated')}), "
                      f"{row.get('file')} lines {row.get('line_start', '')}-"
                      f"{row.get('line_end', '')}: quote {row['quote_label']}, {row['seen_label']}"
                      + (f"; source {finding_source(row) or 'not named'}, source quote "
                         f"{row.get('source_label', 'none')}"
                         if row.get("source_label", "none") != "none" or finding_source(row)
                         else "")
                      for row in result["findings"]]),
        ("Checks", [f"- check {n}: {row['label']}"
                    + (f"; {row['repeats']}" if row.get("repeats") else "")
                    for n, row in enumerate(result["checks"], 1)]),
        ("Evidence the reviewer could not examine",
         withheld_count(listed.get("evidence_unavailable"), "item(s)")),
        ("Unresolved objections",
         withheld_count(listed.get("unresolved_objections"), "objection(s)")),
        ("Commands and file-tool calls naming the main checkout or a records path",
         withheld_count(result["outside"], "command(s) or file-tool call(s)")),
        ("Calls the reviewer was denied", withheld_count(result["denied"], "denied call(s)")),
        ("Paths the reviewer modified", modified_section(meta, result)),
        ("Transcript", [f"- {name.replace('_', ' ')}: {count}"
                        for name, count in result["malformed"].items() if count]
         or [transcript_clean(result["malformed"])]),
        ("Kept out of the review directory", kept_section(kept)),
        ("What the labels establish", establishes(meta, result)),
    ]
    top = report_head(meta, result)
    for title, body in sections:
        top += ["", f"## {title}", ""] + (body or ["None."])
    (review / "report.md").write_text("\n".join(top) + "\n", encoding="utf-8")
    return review / "report.md"


def copy_into(source: Path, target: Path) -> None:
    """Copy a file's bytes as they are, when there is one and it is not the target."""
    if Path(source).is_file() and Path(source).resolve() != Path(target).resolve():
        Path(target).write_bytes(Path(source).read_bytes())


def write_review(out: Path, review: Path, meta: dict, result: dict, sent: list[str],
                 stream: list[tuple[str, Path]], reply: Path) -> tuple[Path, Path]:
    """Write what a reader needs to judge the check into the review directory, and
    hand back the report there, which the rows point at, and the complete report,
    the same file unless the reviewer's text is withheld.

    The stream (the transcript and the CLI's stderr) stays where it is and the
    report names each file with its sha256. The prompt is copied in every case:
    it is the host's own text, naming what was sent without carrying it. The
    reply is copied unless `meta["withheld"]` says why the reviewer's text stays
    out of Git; then the complete report is written beside the stream, and the
    tracked copy carries the labels and counts alone.
    """
    review.mkdir(parents=True, exist_ok=True)
    copy_into(out / "prompt.md", review / "prompt.md")
    if not meta.get("withheld"):
        copy_into(reply, review / "findings.json")
        report = write_report(review, meta, result, sent, kept_apart(stream, review))
        return report, report
    out.mkdir(parents=True, exist_ok=True)
    full = write_report(out, meta, result, sent, kept_apart(stream, out))
    kept = kept_apart([("complete report", full), ("reply as returned", reply)] + stream, review)
    return write_withheld_report(review, meta, result, sent, kept), full


def tally(result: dict) -> tuple[str, str, str]:
    """The count lines the model-review row and the check row share. Denied calls
    are counted apart from the commands that ran, because they did not."""
    rows, checks = result["findings"], result["checks"]
    # The stitched count appears only when there is one: it is the uncommon label,
    # and a zero would read as though the run had looked for something else.
    stitched = sum(1 for r in rows if r["quote_label"] == "stitched")
    # Likewise the source quotes: counted only where a finding quotes a source.
    sourced = [r for r in rows if r.get("source_label", "none") != "none"]
    findings = (f"{len(rows)} findings "
                f"({sum(1 for r in rows if r['quote_label'] == 'verified')} quote-verified, "
                + (f"{stitched} stitched, " if stitched else "")
                + f"{sum(1 for r in rows if r['seen_label'] == 'seen in output')} seen in output"
                + (f"; {len(sourced)} quoting a source, "
                   f"{sum(1 for r in sourced if r['source_label'] == 'verified')} of them "
                   "verified in it" if sourced else "") + ")")
    matched = (f"checks: {sum(1 for c in checks if c['label'].startswith('matched'))} of "
               f"{len(checks)} matched a command in the transcript")
    if (loose := sum(1 for c in checks if "not isolated" in c["label"])):
        matched += f", {loose} of them with the shell line's exit status rather than the check's"
    # AH-094: counted apart, because a match whose exit the transcript never
    # recorded says nothing about whether the check passed.
    if (unrecorded := sum(1 for c in checks if c["label"] == "matched, exit not recorded")):
        matched += f", {unrecorded} of them without a recorded exit"
    # AH-162: a check run more than once with different exits is counted, because
    # its label is the last run's and an earlier one said otherwise.
    if (repeated := sum(1 for c in checks if DIFFERENT_EXITS in (c.get("repeats") or ""))):
        matched += f", {repeated} of them run more than once {DIFFERENT_EXITS}"
    denied = f"{len(result['denied'])} calls the reviewer was denied"
    return findings, matched, denied


def procedure_version(root: Path = ROOT) -> str | None:
    """The version read off `.skills/trace-review/SKILL.md` where the project holds
    it, in the form `trace_record.py procedure-version` prints; None where it holds
    none, because a version nobody can derive is a version somebody typed."""
    from trace_record import procedure_text_version
    return procedure_text_version(PROCEDURE, root)


def check_row(meta: dict, result: dict, report: Path, digest: str, version: str | None,
              replay: bool = False) -> dict:
    """This run as one row of a trace record's checks.jsonl, under the skill run
    contract: what was checked, against what, how, with what result, where the
    report is, and what the labels leave open. The version is the one read off the
    skill file; when the project holds no such file the row says so and carries
    none, rather than a typed one."""
    findings, matched, denied = tally(result)
    listed, requested = meta["findings_json"], meta["criteria"].strip()
    reported = "; ".join(map(str, listed.get("criteria") or []))
    if reported and meta.get("withheld"):
        # The row lands in tracked files, and the reviewer's own words stay with
        # the rest of its text in the run directory.
        reported = (f"reviewer reported applying: {len(listed['criteria'])} criteria, withheld "
                    "with its text" if not requested
                    else f"{len(listed['criteria'])} criteria, withheld with its text")
    criterion = (f"requested: {requested}; reviewer reported applying: "
                 f"{reported or 'nothing stated'}" if requested else reported
                 or "the reviewer's contract: a defect the reviewer can show with a verbatim "
                    "quote from the commit's tree or a command it ran "
                    "(scripts/agentic-review-contract.md)")
    # AH-101: a file-tool call is named here too, because the claude reviewer
    # reaches a path with one and the row is what a reader takes the coverage from.
    limits = ["the reviewer was supplied only the packet listed in the report; a read elsewhere "
              "is detected only when a command or a file-tool call names the main checkout or a "
              f"records path; records {meta['records']}",
              "a quote label says where the text occurs in the commit, not whether it supports "
              "the finding",
              "a matched check names the command the transcript shows finishing, not that the "
              "check was adequate",
              "whether a finding is right is judgment, which no label supplies",
              "delegated decision recorded" if meta["authorized_by"]
              else "model review, not acceptance"]
    if no_os_sandbox(meta):
        limits.append("no operating-system sandbox: a write outside the packet is not prevented, "
                      "only a command naming the main checkout is flagged")
    if meta["involvement"]:
        limits.append("NOT independent: " + "; ".join(meta["involvement"]))
    if meta.get("override"):
        limits.append("the caller's --decision differed from the reviewer's own disposition and "
                      "is recorded as nobody's decision; the report says both")
    if replay:
        limits.append("a replay: the packet was not rebuilt, so modified paths were not captured")
    if version is None:
        limits.append(f"no .skills/{PROCEDURE}/SKILL.md in this checkout, so no procedure_version "
                      "is derived")
    # Where the parts the artifact does not carry are, so a reader on another
    # clone knows what the tracked report describes and cannot show.
    if meta.get("run_dir"):
        limits.append("the reviewer's transcript and stderr stay out of the review directory, "
                      f"in {meta['run_dir']}; the report names each with its sha256")
    if meta.get("withheld"):
        limits.append("the reviewer's own text is withheld from the tracked report, because "
                      f"{meta['withheld']}; the complete report stays in the run directory")
    if meta.get("sources"):
        limits.append(f"{len(meta['sources'])} source(s) sent, each named in sources with the "
                      "sha256 the run hashed; a source quote label says the text is in that "
                      "source, not where, and not that it supports the finding")
    limits += list(meta.get("limits_extra") or [])
    # No `defect_ids`: the validator reads an empty list as invalid, and the ids
    # exist only once a finding enters correction work, which is the agent's step.
    # AH-095: the runner carries the identity basis the report header and the
    # independence declaration carry, so the row does not lose the qualification.
    row = {"kind": "agent-review",
           "runner": f"scripts/agentic_review.py, reviewer {meta['model']} via "
                     f"{meta['backend']} (identity {meta['identity']}) with tools in a packet",
           "method": f"{'replay of an earlier run' if replay else 'fresh run'}: sandbox "
                     f"{meta['sandbox']}, records {meta['records']}, {len(result['ran'])} "
                     f"finished commands, transcript {result['transcript']}"
                     + (f"; criteria set: {requested}" if requested else "")
                     + (f"; prose rules: {meta['prose']}" if meta.get("prose") else ""),
           "result": f"{findings}; {matched}; {denied}; outside-packet commands: "
                     f"{len(result['outside'])}; "
                     + (f"delegated decision {meta['decision']}" if meta["authorized_by"]
                        else "advisory") + f"; report sha256 {digest}",
           "artifact": report.as_posix(), "reference": "path",
           "candidate": meta.get("candidate") or f"commit {meta['sha']} ({meta['subject']})",
           "criterion": criterion, "limits": "; ".join(limits), "procedure": PROCEDURE}
    if version:
        row["procedure_version"] = version
    if meta.get("sources"):
        row["sources"] = source_rows(meta["sources"])
    return row


def record_argv(meta: dict, result: dict, report: Path, digest: str) -> list[str]:
    """The `trace_record.py accept` call this run makes, built here so a test can read
    it. The evidence link rides in `--scope`: an advisory row stores no evidence
    field, and the default run is advisory."""
    findings, matched, denied = tally(result)
    # The path lands in a tracked registry, so it is written with forward slashes
    # whatever the host's separator; a row from a Windows run must read on a Mac.
    report = report.as_posix()
    scope = [f"agentic review of {meta['sha'][:7]}: {findings}", matched, denied,
             f"records: {meta['records']}"]
    if meta.get("sources"):
        scope.append(f"sources: {len(meta['sources'])}, each named with its sha256 in the "
                     "report and the check row")
    scope += [f"outside-packet commands: {len(result['outside'])}",
              f"transcript {result['transcript']}", f"report {report} sha256 {digest}"]
    stated = [f"reviewer {meta['model']} via {meta['backend']}, identity {meta['identity']}",
              "the reviewer was supplied only the packet listed in the report; a read elsewhere "
              "is detected only when a command or a file-tool call names the main checkout or a "
              "records path",
              f"execution trailers over the range: {', '.join(meta['producers']) or 'none'}",
              f"selecting actors in the cited records: {', '.join(meta['selectors']) or 'none'}"]
    if meta["authority"]:
        stated.append("authority, verbatim: " + norm(meta["authority"]))
    if meta["involvement"]:
        scope.append("reviewer participated in producing the covered work: "
                     + "; ".join(meta["involvement"]))
        stated.append("NOT independent: " + "; ".join(meta["involvement"]))
    stated.append("participation in proposing or selecting the covered work is not otherwise "
                  "visible to this tool")
    scope.append("delegated decision recorded" if meta["authorized_by"]
                 else "objections only, not acceptance")
    argv = [sys.executable, "scripts/trace_record.py", "accept", "--by", meta["model"],
            "--range", f"{meta['sha']}^!", "--scope", "; ".join(scope),
            "--independence", "; ".join(stated)]
    if not meta["authorized_by"]:
        # The report is the evidence here too: an advisory row keeps it as advisory,
        # and it names the tracked report rather than only the scope's prose.
        return argv + ["--evidence", report, "--agent"]
    listed = meta["findings_json"]
    reported = "; ".join(map(str, listed.get("criteria") or []))
    objections = "; ".join(map(str, listed.get("unresolved_objections") or []))
    if meta.get("withheld"):
        # ee9a334 F001: the call is written to the tracked review directory and
        # the row to the tracked registry, so a reviewer whose text is withheld
        # is counted here, as check_row counts it, and its words stay with the
        # rest of its text in the run directory.
        where = ("withheld with its text (see the complete report in the run directory, "
                 f"which the tracked report, sha256 {digest}, names)")
        if reported:
            reported = f"{len(listed['criteria'])} criteria, {where}"
        if objections:
            objections = f"{len(listed['unresolved_objections'])} objection(s), {where}"
    requested = meta["criteria"].strip()
    # The two can differ, and which is which is the point: one is what the
    # researcher asked for, the other is what the reviewer says it applied.
    criteria = (f"requested: {requested}; reviewer reported applying: "
                f"{reported or 'nothing stated'}") if requested else reported or "nothing stated"
    # The authorization's words travel as --authority too: trace_record.py
    # records a delegated decision without them as advisory.
    return argv + ["--authorized-by", meta["authorized_by"], "--authority", meta["authority"],
                   "--decision", meta["decision"],
                   "--criteria", criteria, "--evidence", report,
                   "--objections", meta["objections"] or objections or "none stated"]


def append_check_row(target: Path, row: dict, review: Path, code: int) -> None:
    """Append the row to the record named, once the model-review row is recorded. A
    run whose recording failed has not finished, so its row stays in the review
    directory with the report, for the agent to carry over when the cause is fixed."""
    if code:
        print(f"check row not appended to {target}: the recorder failed (exit {code}); the row "
              f"is in {review}/check-row.json", file=sys.stderr)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" (AH-166): text mode on Windows writes CRLF, and a record's rows
    # are LF wherever the run happened.
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row) + "\n")
    print(f"check row appended to {target}: procedure {PROCEDURE}, version "
          f"{row.get('procedure_version', 'none derived')}", file=sys.stderr)


def run_recorder(call: list[str], review: Path) -> int:
    """Run the recorder and hand back its exit code, so a failed write is not a pass."""
    code = subprocess.run(call, cwd=str(ROOT), check=False).returncode
    if code:
        print(f"recording failed (exit {code}); the call and the report are kept in {review}",
              file=sys.stderr)
    return code


def report_title(meta: dict) -> str:
    """The heading: the candidate this run examined, a commit or a set of paths."""
    return meta.get("title") or f"Agentic review of {meta['sha'][:7]}: {meta['subject']}"


def candidate_paths(given: list[str]) -> list[str]:
    """The candidate files as repository-relative posix paths, sorted and unique.

    An absolute path under the root is stored relative, because the listing this
    review is bound to has to read the same on another machine. A file in the
    project's shared folder is a candidate too, named by the path the project knows
    it under (`project_path`); until rc22 it was refused as outside the repository
    whenever the folder was a link to a sync app's folder.
    """
    root, rels = ROOT.resolve(), []
    for name in given:
        try:
            rel = project_path(name, root)
        except ValueError:
            raise ValueError(f"{name} is outside {root}: --path takes files in this repository "
                             f"or in its shared folder ({shared_name(root)})") from None
        if not (root / rel).is_file():
            raise ValueError(f"{name} is not a file")
        rels.append(rel)
    return sorted(dict.fromkeys(rels))


# The paths a commit's packet leaves out, as repository-relative posix paths.
PROTECTED = tuple(spec.removeprefix(":!") for spec in EXCLUDE)


def protected_paths(rels: list[str]) -> list[str]:
    """The candidates that are trace-record or registry files, which carry people's
    own words and which a commit's packet excludes.

    AH-157: `--path` sent them whole with no inspection declared, while `--records
    full` asks for one, and the prompt said no trace record was sent. Compared
    case-folded, because a case-insensitive file system reads `AI/Records` as the
    same directory.
    """
    folded = [p.casefold() for p in PROTECTED]
    return [rel for rel in rels
            if any(rel.casefold() == p or rel.casefold().startswith(p + "/") for p in folded)]


def path_records(held: list[str], inspected_by: str) -> str:
    """What a path run sent of the records, in the words the prompt, the report
    and the check row carry."""
    if not held:
        return "none; path candidate"
    return (f"trace-record or registry files sent whole as candidates, so people's own words "
            f"went to the model, inspected first by {inspected_by or 'nobody named in this run'}: "
            f"{', '.join(held)}")


def discard(holder: Path, keep: bool) -> None:
    """Remove the packet's directory once nothing needs it, however the run ended.

    AH-167: every run left its packet in the temporary directory, 132M for one
    commit, and under `--records full` the human's own words in it. The run
    directory and the review directory stay; `--keep-packet` keeps the packet as
    well, and says where.
    """
    if keep:
        print(f"packet kept in {holder} (--keep-packet)", file=sys.stderr)
        return
    shutil.rmtree(holder, ignore_errors=True)
    if holder.exists():
        # A reviewer or an export can leave a directory unwritable, or a file
        # read-only where that blocks removal. Symbolic links are not followed.
        for base, dirs, names in os.walk(holder):
            for name in dirs + names:
                path = os.path.join(base, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        os.chmod(holder, 0o700)
        shutil.rmtree(holder, ignore_errors=True)
    print(f"packet {'not fully removed' if holder.exists() else 'removed'}: {holder}",
          file=sys.stderr)


def delegation_gap(args) -> str:
    """Why a run naming part of a delegation cannot record a delegated decision;
    empty when it names none of it or all of it.

    AH-154: `--authorized-by` alone recorded a delegated decision in the model's
    name with no authorization anywhere, after the prompt had told the reviewer
    there was none. A delegation is the researcher's name together with the words
    that authorized the run, and without either the run is advisory.
    """
    named, words = args.authorized_by.strip(), args.authority.strip()
    if named and not words:
        return ("--authorized-by names a researcher, and no authorization was given; its words go "
                "in --authority, verbatim")
    if words and not named:
        return "--authority names no researcher; --authorized-by names the one who authorized it"
    return ""


def override_note(asked: str, disposition: str, sha: str) -> str:
    """What the run says when `--decision` differs from the reviewer's own disposition.

    AH-154: the flag used to replace the disposition, so a reviewer's refusal
    could be recorded as its acceptance. A delegated row records the reviewer's
    own disposition. The caller's differing decision is reported here and
    recorded as nobody's, because a person's decision is recorded in that
    person's name with their words, and authorizing the run does not make the
    researcher that person.
    """
    if not asked or asked == disposition:
        return ""
    return (f"--decision {asked} differs from the reviewer's own disposition, "
            f"{disposition or 'none'}: the reviewer's is what this run records, and {asked} is "
            "recorded as nobody's decision. A person's own decision is recorded in that person's "
            "name with their words, and this run does not take the authorizing researcher to be "
            f"that person: python3 scripts/trace_record.py accept --range \"{sha}^!\" --person "
            f"--by \"Full Name\" --decision {asked} --words \"what they said\", with "
            "--recorded-by naming the agent when an agent records it.")


def candidate_files(rels: list[str]) -> dict[str, bytes]:
    """Every candidate read once. These bytes are what the listing hashes, what the
    packet carries and what the quote labels are computed over, so a file edited
    while the reviewer runs cannot change the review under it."""
    return {rel: (ROOT.resolve() / rel).read_bytes() for rel in rels}


def candidate_listing(files: dict[str, bytes]) -> str:
    """`<sha256>  <path>` per file, sorted: the object the findings are bound to."""
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {rel}\n"
                   for rel, data in sorted(files.items()))


def candidate_revision(listing: str) -> str:
    """What the findings must name. The digest is of the whole listing, not of one
    file, so a review is bound to the set it was given rather than to a member."""
    return "paths:" + hashlib.sha256(listing.encode("utf-8")).hexdigest()


def candidate_field(listing: str) -> str:
    """The check row's candidate: each path with the digest the listing gave it."""
    rows = [line.split(None, 1) for line in listing.splitlines() if line.strip()]
    return "paths: " + ", ".join(f"{rel} (sha256 {digest})" for digest, rel in rows)


def build_path_packet(files: dict[str, bytes], listing: str, packet: Path,
                      inspected_by: str = "",
                      sources: list[dict] = ()) -> tuple[dict[str, str], list[str]]:
    """Copy each candidate to the same relative path, with the listing beside it,
    and the sources under `.review/sources/`.
    No `.git`, no diff and no commit text: a path candidate has none of them."""
    packet.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        target = packet / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    review = packet / ".review"
    review.mkdir(parents=True, exist_ok=True)
    # Written as bytes: a Windows host would otherwise store CRLF and the file the
    # reviewer can hash would not be the file the digest was taken over.
    (review / "candidate.txt").write_bytes(listing.encode("utf-8"))
    held = protected_paths(sorted(files))
    sent = [f"{rel}: the file as this run read it"
            + (f"; a trace-record or registry file, so the words in it went to the model. "
               f"Inspected first by {inspected_by}" if rel in held else "")
            for rel in sorted(files)]
    sent.append(f".review/candidate.txt: each path above with its sha256, digest "
                f"{candidate_revision(listing)}")
    sent += pack_sources(list(sources), packet, inspected_by)
    return manifest(packet), sent


def render_path_prompt(rels: list[str], revision: str, sent: list[str],
                       criteria: str = "", records: str = "",
                       sources: list[dict] = ()) -> str:
    """The contract, then a run section for files rather than a commit. `records`
    is what `path_records` says was sent of the records, when a candidate is one."""
    lines = [CONTRACT.read_text(encoding="utf-8").rstrip()] + prose_section() + ["", "## Run", "",
             f"Target: {len(rels)} path(s), listed under Evidence below. They are the whole of "
             "what you are reviewing.",
             "There is no commit here: the packet holds the files themselves, and there is no "
             "`.review/commit.txt` and no `.review/diff.patch`.",
             (f"Records mode: {records}." if records else
              "Records mode: none; a path candidate names no commit, so no trace record was sent."),
             "",
             f"Set `revision` to `{revision}`, the digest of `.review/candidate.txt`. "
             "The host refuses findings naming anything else.", "",
             "Evidence supplied to you:"] + [f"- {item}" for item in sent] + source_prompt(
                 list(sources)) + [""]
    lines += ["The sandbox is read-only and no checks are supplied. "
              "Return `checks` as an empty list.", "",
              "No delegation. Set `disposition` to `advisory`."]
    if criteria.strip():
        lines += ["", "Criteria the researcher set:", "", criteria.strip(), "",
                  "Report in `criteria` the criteria you actually applied, whether or not they "
                  "are these. The host records both, so a difference stays visible."]
    return "\n".join(lines) + "\n"


def path_quote_label(files: dict[str, bytes], finding: dict) -> tuple[str, str]:
    """Label a quote against the candidate's own bytes.

    A finding citing a file this run did not send is invented in the only sense the
    host can check: the reviewer was never given that file.
    """
    name = str(finding.get("file") or "")
    if name not in files:
        return "invented", f"{name or 'no file'} is not among the paths reviewed"
    return label_bytes(files[name], finding, "the reviewed copy")


def head_bytes(rel: str) -> bytes | None:
    """HEAD's copy of the path, or None where Git does not have one."""
    done = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True)
    return done.stdout if done.returncode == 0 else None


def differs_from_head(files: dict[str, bytes]) -> list[str]:
    """Candidates whose reviewed bytes are not HEAD's, untracked ones included.

    The trailer of the commit that last touched a path names who produced that
    commit's bytes. Where the reviewed bytes are different bytes, that trailer is
    not their producer, so the difference is declared rather than assumed away.
    """
    return sorted(rel for rel, data in files.items() if head_bytes(rel) != data)


def path_involvement(rels: list[str], model: str) -> tuple[list[str], list[str], list[str]]:
    """Producers from the last commit touching each path; no selectors.

    The last commit is the bound: the whole history of a file names everyone who
    ever touched it, which says nothing about the copy under review. A decision is
    recorded against a commit's trace record, and a path names no commit, so the
    selector list stays empty rather than being guessed at.
    """
    producers = set()
    for rel in rels:
        try:
            body = git("log", "-1", "--format=%B", "--", rel)
        except subprocess.CalledProcessError:
            body = ""
        producers.update(declared_agents(body))
    producers.discard("")
    return sorted(producers), [], involved(sorted(producers), [], model)


def run_dirs(args, name: str) -> tuple[Path, Path]:
    """The run directory and the review directory.

    The run directory is `--out-dir` or `.agent-reviews/agentic-review/<name>`.
    The review directory is `--review-dir` or `ai/reviews/` under the run
    directory's own name, so a run named with `--out-dir` keeps its name there.
    """
    out = ROOT / (args.out_dir or RUN_BASE / name)
    return out, ROOT / (args.review_dir or REVIEW_BASE / out.name)


def stale_results(out: Path, review: Path) -> str:
    """Why a fresh run will not start, or an empty string: an earlier run's results
    where this one writes its own could stand in for them."""
    found = [out / name for name in ("findings.json", "transcript.jsonl") if (out / name).exists()]
    if review.resolve() != out.resolve():
        found += [review / name for name in ("report.md", "findings.json")
                  if (review / name).exists()]
    return (f"refusing to start: an earlier run left {', '.join(map(str, found))}. A fresh run "
            "writes its own, and reusing a directory would let an earlier result stand in for "
            "this one.") if found else ""


def cited_files(findings: dict) -> list[str]:
    """The files the reviewer's findings cite, as it named them, the sources they
    quote included."""
    rows = [row for row in findings.get("findings") or [] if isinstance(row, dict)]
    return ([str(row.get("file") or "") for row in rows]
            + [name for row in rows if (name := finding_source(row))])


def apart_from(out: Path, review: Path, withheld: str) -> dict:
    """What the run records about its two directories: where the stream stays, and
    why the reviewer's text stays there too. With one directory for both, nothing
    is kept apart, and the run says so rather than claim otherwise."""
    if review.resolve() == out.resolve():
        if withheld:
            print(f"the review directory is the run directory, so nothing is kept apart, "
                  f"although {withheld}", file=sys.stderr)
        return {"run_dir": "", "withheld": ""}
    return {"run_dir": shown(out), "withheld": withheld}


def finish_review(args, out: Path, review: Path, meta: dict, result: dict,
                  sent: list[str]) -> tuple[Path, str]:
    """Write the review directory, print the complete report, and hand back the
    tracked report as the rows name it, with its sha256."""
    transcript = Path(args.transcript) if args.replay else out / "transcript.jsonl"
    reply = Path(args.findings) if args.replay else out / "findings.json"
    report, full = write_review(out, review, meta, result, sent,
                                [("transcript", transcript), ("stderr", out / "stderr.txt")],
                                reply)
    print(full.read_text(encoding="utf-8"))
    print(f"report: {shown(report)}"
          + ("; commit its directory with the record, so the rows that point at it resolve on "
             "any clone" if report.is_relative_to(ROOT)
             else "; it lies outside this repository, so no clone holds it")
          + (f". The reviewer's text is withheld from it, because {meta['withheld']}; the "
             f"complete report is {shown(full)}" if meta.get("withheld") else ""),
          file=sys.stderr)
    return Path(shown(report)), sha256_of(report)


def review_paths(args) -> int:
    """Review files as they stand: no commit and no acceptance row."""
    if args.replay and not (args.transcript and args.findings):
        print("--replay needs --transcript and --findings", file=sys.stderr)
        return 2
    try:
        rels = candidate_paths(args.path)
        files = candidate_files(rels)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    listing = candidate_listing(files)
    revision = candidate_revision(listing)
    sources = getattr(args, "sources", [])
    held = protected_paths(rels)
    records = path_records(held, args.inspected_by.strip())
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out, review = run_dirs(args, f"paths-{stamp}")

    if args.replay:
        before, after = {}, {}
        sent = ["replay: the packet was not rebuilt, so this run verified existing files only"]
        sent += source_sent(sources, replay=True)
        transcript = Path(args.transcript).read_text(encoding="utf-8")
        findings = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    else:
        if refused := stale_results(out, review):
            print(refused, file=sys.stderr)
            return 2
        out.mkdir(parents=True, exist_ok=True)
        holder = Path(tempfile.mkdtemp(prefix="agentic-review-paths-"))
        packet = holder / "packet"
        try:
            before, sent = build_path_packet(files, listing, packet, args.inspected_by.strip(),
                                             sources)
            prompt = render_path_prompt(rels, revision, sent, args.criteria,
                                        records if held else "", sources)
            (out / "prompt.md").write_text(prompt, encoding="utf-8")
            # A path run is read-only for every backend, and `--checks` is refused
            # before it reaches here, so the writable flag `command_for` reads is False.
            command = command_for(packet, out, args)
            if args.dry_run:
                print(" ".join(printable(command)))
                print(prompt)
                return 0
            print(f"reviewing {len(rels)} path(s) in {packet}", file=sys.stderr)
            code = launch(command, out, workdir(args.backend, packet), child_env(args.backend),
                          BACKENDS[args.backend]["prompt"] == "stdin", args.timeout)
            after = manifest(packet)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        finally:
            discard(holder, args.keep_packet)
        if code != 0:
            print(f"{args.backend} exited {code}; see {out}/stderr.txt. Nothing is verified and "
                  f"nothing is recorded; the artifacts are kept in {out}.", file=sys.stderr)
            return code
        transcript = (out / "transcript.jsonl").read_text(encoding="utf-8")
        findings = collect_findings(args, out, transcript)
        if findings is None:
            return 2

    if refusal := schema_refusal(findings):
        print(refusal, file=sys.stderr)
        return 2
    stated = str(findings.get("revision") or "").strip()
    if stated != revision:
        print(f"refusing to record: the findings name {stated or 'no revision'}, not {revision}. "
              "A review is recorded against the candidate it examined, in a fresh run as in a "
              "replay, and the digest changes when any of the files does.", file=sys.stderr)
        return 2
    if args.authorized_by or args.authority:
        print("no delegated decision is recorded for a path candidate: such a decision covers "
              "commits, and these paths name none.", file=sys.stderr)
    # The alias asked for is not the model that answered, and the involvement note
    # is about the reviewer, so the identity comes off the transcript first.
    model, identity = reviewer_id(transcript, args.backend, args.model)
    producers, selectors, hits = path_involvement(rels, model)
    limits = ["labels computed over the bytes hashed in the candidate listing",
              "execution trailers on the last commit touching each path: "
              + (", ".join(producers) or "none")]
    if dirty := differs_from_head(files):
        limits.append("candidate differs from HEAD for: " + ", ".join(dirty))
    meta = {"sha": revision, "subject": ", ".join(rels), "model": model, "backend": args.backend,
            "identity": identity, "at": stamp, "sandbox": sandbox_of(args.backend, False),
            "records": records, "authority": "",
            "authorized_by": "", "decision": "", "criteria": args.criteria,
            "objections": args.objections, "involvement": hits, "producers": producers,
            "selectors": selectors, "findings_json": findings, "limits_extra": limits,
            "title": f"Agentic review of {len(rels)} path(s): {rels[0]}",
            "candidate": candidate_field(listing), "sources": sources, "prose": prose_named()}
    meta.update(apart_from(out, review, withheld_reason("", rels, cited_files(findings),
                                                        [e["path"] for e in sources])))
    result = verify(revision, findings, transcript, before, after, args.backend,
                    label_quote=lambda row: path_quote_label(files, row),
                    label_source=lambda row: source_label(
                        sources, row, lambda name: (files.get(name), "the reviewed copy")))
    report, digest = finish_review(args, out, review, meta, result, sent)
    row = check_row(meta, result, report, digest, procedure_version(), args.replay)
    (review / "check-row.json").write_text(json.dumps(row, indent=1) + "\n", encoding="utf-8")
    if hits:
        print("advisory only: " + "; ".join(hits)
              + ". An actor that produced the work is not independent of it.", file=sys.stderr)
    print("no acceptance-registry row: a path covers no commit, so there is no range to record "
          f"against; the check row is in {review}/check-row.json", file=sys.stderr)
    if args.checks_jsonl:
        append_check_row(Path(args.checks_jsonl), row, review, 0)
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sha", nargs="?", help="the commit to review")
    p.add_argument("--path", nargs="+", default=[], metavar="FILE",
                   help="review these files as they stand instead of a commit")
    p.add_argument("--backend", choices=tuple(BACKENDS), default="codex",
                   help="which headless reviewer opens the packet")
    p.add_argument("--model", default="",
                   help="the reviewer's model; the default is the backend's "
                        + ", ".join(f"{name}: {row['model']}" for name, row in BACKENDS.items()))
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="seconds before the reviewer and the tools it started are killed; it "
                        "bounds every backend, codex included, which had none before")
    p.add_argument("--checks", action="store_true",
                   help="supply the check list and run the sandbox writable")
    p.add_argument("--records", choices=("excluded", "minimal", "full"), default="minimal")
    p.add_argument("--inspected-by", dest="inspected_by", default="",
                   help="--records full, or a --path naming a trace-record or registry file: "
                        "the person who read what is sent first")
    p.add_argument("--authority", default="",
                   help="the prompt or standing instruction authorizing a decision, verbatim")
    p.add_argument("--authorized-by", dest="authorized_by", default="",
                   help="the researcher who authorized this reviewer to decide, and who remains "
                        "accountable")
    p.add_argument("--decision", default="", choices=("",) + DECISIONS,
                   help="delegated run: the decision the caller expects. It never replaces the "
                        "reviewer's own disposition, which is what the run records; one that "
                        "differs is reported and recorded as nobody's decision. A person's own "
                        "decision is recorded with trace_record.py accept --person --by NAME "
                        "--words WORDS")
    p.add_argument("--criteria", default="")
    p.add_argument("--source", action="append", default=[], metavar="PATH",
                   help="a file or a folder to check the candidate against, in this repository, "
                        "in its shared folder or a snapshot under workspace/snapshots/; repeat "
                        "for each. It goes into the packet under .review/sources/, and its "
                        "sha256 into the prompt, the report and the check row")
    p.add_argument("--objections", default="")
    p.add_argument("--out-dir", dest="out_dir", default="",
                   help="the run directory, which keeps the reviewer's prompt, transcript, stderr "
                        "and reply as it ran; .agent-reviews/agentic-review/<name> by default, "
                        "which Git ignores")
    p.add_argument("--review-dir", dest="review_dir", default="",
                   help="the review directory, which holds what a reader needs to judge the "
                        "check: the report the rows point at, the reply, the prompt, the check "
                        "row and the record call; ai/reviews/ under the run directory's name by "
                        "default, which is tracked")
    p.add_argument("--replay", action="store_true",
                   help="verify, report and record existing files without calling a reviewer; "
                        "--backend chooses the parser for the transcript given")
    p.add_argument("--transcript", default="")
    p.add_argument("--findings", default="")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    p.add_argument("--keep-packet", dest="keep_packet", action="store_true",
                   help="keep the packet directory when the run ends; by default it is removed "
                        "however the run ends, and the run and review directories stay either way")
    p.add_argument("--no-record", dest="no_record", action="store_true")
    p.add_argument("--checks-jsonl", dest="checks_jsonl", default="",
                   help="append the run's trace-review check row to this checks.jsonl, a trace "
                        "record's or its staging copy; check-row.json is written either way")
    args = p.parse_args(argv)
    args.model = args.model or BACKENDS[args.backend]["model"]
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.records == "full" and not args.inspected_by.strip():
        print("--records full sends the human's own words to an external model; name the person "
              "who inspected the directory first with --inspected-by", file=sys.stderr)
        return 2
    if args.replay and not (args.transcript and args.findings):
        print("--replay needs --transcript and --findings", file=sys.stderr)
        return 2
    if bool(args.sha) == bool(args.path):
        print("a run reviews one candidate: name a commit, or name files with --path FILE "
              "[FILE ...]. This run named "
              + ("both" if args.sha else "neither") + ".", file=sys.stderr)
        return 2
    if args.path and args.checks:
        print("--checks with --path: the packet holds no repository to check, so there is "
              "no check to supply and nothing to run one against.", file=sys.stderr)
        return 2
    # The candidate is checked before the machine: a file that is not there is the
    # answer on every laptop, whether or not a reviewer CLI is installed.
    if args.path:
        try:
            held = protected_paths(candidate_paths(args.path))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        # AH-157: the same declaration `--records full` asks for, because a path
        # naming a record file sends the same words. A replay sends nothing.
        if held and not args.replay and not args.inspected_by.strip():
            print(f"--path names {', '.join(held)}: trace-record or registry files, which carry "
                  "people's own words and which a commit's packet leaves out. Sent as a "
                  "candidate, each goes whole to an external model; name the person who "
                  "inspected them first with --inspected-by", file=sys.stderr)
            return 2
    # The sources too, once: each is hashed here, and a large dataset is read once.
    try:
        args.sources = collect_sources(args.source)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    # A source carries people's own words as a candidate does when it is a trace-record
    # or registry file, so it asks for the same declaration (AH-157).
    held_sources = protected_paths([entry["path"] for entry in args.sources])
    if held_sources and not args.replay and not args.inspected_by.strip():
        print(f"--source names {', '.join(held_sources)}: trace-record or registry files, which "
              "carry people's own words. Sent as a source, each goes whole to an external model; "
              "name the person who inspected them first with --inspected-by", file=sys.stderr)
        return 2
    cli = BACKENDS[args.backend]["cli"]
    if not (args.replay or args.dry_run) and not shutil.which(cli):
        print(f"the {args.backend} backend needs `{cli}` on PATH, and there is none here. "
              "Nothing was run and nothing was recorded.", file=sys.stderr)
        return 2
    if args.path:
        return review_paths(args)
    sha = git("rev-parse", args.sha).strip()
    subject = git("log", "-1", "--format=%s", sha).strip()
    shas = [line for line in git("rev-list", f"{sha}^!").splitlines() if line]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # A delegation is the researcher's name and the words that authorized the run.
    delegation = bool(args.authorized_by.strip() and args.authority.strip())
    # Advisory or delegated, a run's report goes to the tracked review directory
    # and its stream stays in the ignored run directory (until 2026-10-14 only a
    # delegated run wrote under ai/reviews/, and it wrote its transcript there too).
    out, review = run_dirs(args, f"{sha[:7]}-{stamp}")
    checks = DEFAULT_CHECKS if args.checks else []
    sources = getattr(args, "sources", [])

    if args.replay:
        before, after = {}, {}
        sent = ["replay: the packet was not rebuilt, so this run verified existing files only"]
        sent += source_sent(sources, replay=True)
        transcript = Path(args.transcript).read_text(encoding="utf-8")
        findings = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    else:
        if refused := stale_results(out, review):
            print(refused, file=sys.stderr)
            return 2
        out.mkdir(parents=True, exist_ok=True)
        # Outside the repository: Git walks up, so a nested packet would find
        # the very .git the export exists to keep out of reach.
        holder = Path(tempfile.mkdtemp(prefix=f"agentic-review-{sha[:7]}-"))
        packet = holder / "packet"
        try:
            before, sent = build_packet(sha, shas, packet, args.records, args.inspected_by,
                                        sources)
            # The reviewer is told of a delegation only when the run can record one
            # (AH-154), so the prompt and the row cannot disagree about it.
            prompt = render_prompt(sha, subject, checks, args.records, sent,
                                   args.authority if delegation else "", args.criteria, sources)
            (out / "prompt.md").write_text(prompt, encoding="utf-8")
            command = command_for(packet, out, args)
            if args.dry_run:
                print(" ".join(printable(command)))
                print(prompt)
                return 0
            print(f"reviewing {sha[:7]} ({subject}) in {packet}", file=sys.stderr)
            code = launch(command, out, workdir(args.backend, packet), child_env(args.backend),
                          BACKENDS[args.backend]["prompt"] == "stdin", args.timeout)
            after = manifest(packet)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        finally:
            discard(holder, args.keep_packet)
        if code != 0:
            print(f"{args.backend} exited {code}; see {out}/stderr.txt. Nothing is verified and "
                  f"nothing is recorded; the artifacts are kept in {out}.", file=sys.stderr)
            return code
        transcript = (out / "transcript.jsonl").read_text(encoding="utf-8")
        findings = collect_findings(args, out, transcript)
        if findings is None:
            return 2

    if refusal := schema_refusal(findings):
        print(refusal, file=sys.stderr)
        return 2
    stated = str(findings.get("revision") or "").strip()
    if stated != sha:
        print(f"refusing to record: the findings name {stated or 'no revision'}, not {sha}. "
              "A review is recorded against the commit it examined, in a fresh run as in a "
              "replay.", file=sys.stderr)
        return 2

    # The alias asked for is not the model that answered, and everything downstream
    # names the reviewer, so the identity comes off the transcript first.
    model, identity = reviewer_id(transcript, args.backend, args.model)
    producers, selectors, hits = involvement(shas, model)
    # AH-154: the decision a delegated row records is the reviewer's own
    # disposition, never the caller's `--decision`, which is reported apart.
    disposition = str(findings.get("disposition") or "")
    decision = disposition if disposition in DECISIONS else ""
    if args.authorized_by.strip() or args.authority.strip():
        why = (delegation_gap(args)
               or ("a replay re-reads an earlier examination, not a fresh one under the authority"
                   if args.replay else "")
               or ("" if decision else "the reviewer returned no accept, return or refuse"))
        if why:
            print("recording advisory model review rather than a delegated decision: " + why,
                  file=sys.stderr)
    if override := override_note(args.decision, disposition, sha):
        print(override, file=sys.stderr)
    meta = {"sha": sha, "subject": subject, "model": model, "backend": args.backend,
            "identity": identity, "at": stamp, "sandbox": sandbox_of(args.backend, args.checks),
            "records": args.records, "authority": args.authority,
            "authorized_by": (args.authorized_by
                              if delegation and not (args.replay or hits or not decision) else ""),
            "decision": decision, "override": override, "criteria": args.criteria,
            "objections": args.objections, "involvement": hits, "producers": producers,
            "selectors": selectors, "findings_json": findings, "sources": sources,
            "prose": prose_named()}
    meta.update(apart_from(out, review,
                           withheld_reason(args.records, [], cited_files(findings),
                                           [e["path"] for e in sources])))
    result = verify(sha, findings, transcript, before, after, args.backend,
                    label_source=lambda row: source_label(sources, row, commit_source(sha)))
    report, digest = finish_review(args, out, review, meta, result, sent)
    call = record_argv(meta, result, report, digest)
    (review / "record-call.json").write_text(json.dumps(call, indent=1) + "\n", encoding="utf-8")
    row = check_row(meta, result, report, digest, procedure_version(), args.replay)
    (review / "check-row.json").write_text(json.dumps(row, indent=1) + "\n", encoding="utf-8")
    if hits:
        print("advisory only: " + "; ".join(hits)
              + ". An actor that produced the work is not independent of it.", file=sys.stderr)
    if args.no_record:
        print(f"not recorded (--no-record); the call is in {review}/record-call.json",
              file=sys.stderr)
        code = 0
    else:
        code = run_recorder(call, review)
    if args.checks_jsonl:
        append_check_row(Path(args.checks_jsonl), row, review, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
