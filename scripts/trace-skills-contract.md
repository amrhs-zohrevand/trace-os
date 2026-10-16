# trace_skills.py: interface and recovery contract

Contract version 1, 2026-10-01 (Stage 1 of `ai/skills-library-plan.md`). The amendments since, and their reasons, are in the commit history of this file and in `ai/defects.jsonl`.
Builders B (the core lifecycle) and D (the rest of the lifecycle) implement `scripts/trace_skills.py` against this text, and C writes the scenario tests from it alone.
Where the plan and the code disagreed, the code decided; section 14 lists what this contract could not settle.

The tool brings skill packages from Git repositories into one project.
It keeps the project's copy, the untouched original and a lock, and records each operation in the project's registries.
It builds on `bootstrap.py` (the setup lock, the transaction journal, atomic writes and recovery) and runs `trace_record.py` for the intake and transition rows.
It needs Python 3.11 or later, the standard library and Git; `gh` is optional.

## 0. Conventions

- Paths in files, messages and records are POSIX paths relative to the project root. Two kinds of path are absolute: a local source repository, and a temporary folder.
- `<tool>` in a message means the command that runs the tool again: `python3`, then the script's path relative to the project root when the script is inside the project (otherwise its absolute path), then `--project <project>` when `--project` was given.
- Timestamps are UTC, written `YYYY-MM-DDTHH:MM:SSZ`. Operation-id stamps are `YYYYMMDDTHHMMSSZ`.
- Every JSON file the tool writes (the lock, a receipt, a candidate, the operation journal) is `bootstrap.encoded(data)`: sorted keys, a two-space indent, ASCII escapes and one trailing newline. The same data always gives the same bytes.
- `c12` is the first twelve characters of a commit id, and `d12` the first twelve of a digest.
- Refusals and errors go to stderr, prefixed `trace-skills: `. Plans, results and the confirmation exchange go to stdout.
- Exit codes are the same for every command, as in `kit_projects.py`. 0 means done, nothing to do, or a read-only report. 1 means a person is needed: a refusal caused by the state of the project or the package, a stale candidate, an adapted copy, or an apply nobody confirmed. 2 means a usage or environment error: bad arguments, no Git, a failed fetch, an unreadable file, or a failed write that was undone. 128 plus the signal number means interrupted (130 for Ctrl-C, 143 for SIGTERM). argparse's own usage errors exit 2.
- The tool imports `bootstrap` from its own folder and calls every bootstrap function through the module (`bootstrap.transaction`, `bootstrap.write_atomic`, `bootstrap.locked`, `bootstrap.recover`), never through a name imported into its own namespace. A test can then patch one attribute and interrupt any write.
- Test seams, with fixed names: `main(argv) -> int`; `utc_now() -> datetime`; `observe_audience(address) -> str` (section 8); `run_trace_record(project, args) -> (returncode, stdout, stderr)` (section 10); `package_digest(files) -> str` (section 4.6); `read_front_matter(text) -> dict` (section 2.3), which raises `FrontMatterError(line, reason)`.
- Module constants, with fixed names: `CONTRACT_VERSION = 1`, `LOCK = '.trace-os/skills.lock.json'`, `BASES = '.trace-os/skill-bases'`, `RECEIPTS = '.trace-os/skill-adoptions'`, `OPERATION = '.trace-os/receipts/skill-operation.json'`, `INVENTORY_BEGIN = '<!-- trace-skills:begin inventory -->'`, `INVENTORY_END = '<!-- trace-skills:end inventory -->'`.
- The state folder. A project installed by an earlier release keeps its state in `.traceware/` (0.1.0-rc19) or `.trace-kit/` (up to 0.1.0-rc18) until the update to 0.1.0-rc20 moves it to `.trace-os/` (`SETUP-AGENT.md`, Update). When a command opens its project, `use_state_folder(project)` points `LOCK`, `BASES`, `RECEIPTS`, `OPERATION` and the install-state names at the folder `bootstrap.state_dir(project)` names: the one holding `install.json`, `.trace-os/` first, then `.traceware/`, then `.trace-kit/`, and `.trace-os/` in a project with none. Every `.trace-os/` path below is then under that folder, the catalogue's messages name that folder, and a path a lock or a receipt recorded under another of the folder's names, a receipt's or an original's (`upstream.snapshot`), is read, compared, removed and reported where it is now, after one move or two (`stored_path`, over `bootstrap.recorded_path`; AH-281); a retirement records the original where it was.

## 1. Purpose, and the rules the tool never breaks

A project keeps its own copy of every skill it uses.
The tool lets a person take that copy from any Git repository, see all of it before anything is written, keep the untouched original beside it, update it later with the same review, and record each step where co-authors and the trace record can see it.

These rules hold for every command.

1. **A package is read as data.** The tool never executes, imports, sources or installs a file from a package, and never runs a package's tests. It never checks a package out through Git, so no smudge filter, hook, LFS process or attribute of the source applies. It fetches into a bare repository in a temporary folder and reads each blob with `git cat-file`. Package text printed to a terminal has its control characters escaped (6.2).
2. **Symlinks, submodules and escaping paths are refused**, with the reason. So are non-portable names and Git control files (2.2), and a package path argument that leaves the repository (S8).
3. **Digests are re-checked just before writing.** The staged bytes are hashed again and compared with the manifest the person was shown. Every destination file is compared with the hash read when the plan was made, and the transaction compares it once more as it writes.
4. **No credential is written** to the lock, a receipt, a candidate or the operation journal. An address carrying a user name and password, a token, a query or a fragment is refused (S1). No environment value is copied into a file, and no Git or `gh` output is copied either, except commit ids and the GitHub visibility word.
5. **An adapted copy is never merged by the tool.** `update` on an adapted copy prints the three versions and writes nothing. `update --accept-merged` records the person's merge as it stands and never changes the copy.
6. **Every update is reviewed like a first adoption.** The whole new package is printed, validated and confirmed.
7. **Nothing is applied without the person.** A writing command applies after a "y" at a terminal, or with `--yes`, which an agent passes only once the person has agreed. `--yes` applies exactly the bytes that were shown: a candidate file, or a full commit id (C3).
8. **One operation is one transaction, and the receipt is written last.** A receipt exists only for an operation that completed (section 12).
9. **The tool never commits, pushes or changes Git configuration.** It leaves its writes for the person to review and commit.
10. **Reports never block.** `status`, `list` and `show` exit 0 whatever states they report. A refusal stops only the tool's own write, and always says what to do next.

Out of scope for version 1: a registry service, dependency resolution, version ranges, signatures, automatic updates, running package code, writes across several projects, and sharing an adaptation back to its source.

## 2. The package

### 2.1 Where a package is read

A package is the folder `<path>` of a Git repository `<repository>` at one commit.
In the temporary bare repository (3.3), the tool lists it with `git ls-tree -r -z --full-tree <commit> -- <path>` and reads every member's blob in one `git cat-file --batch` process, which applies no filter or attribute.
The members are the listed entries under `<path>/`, with that prefix removed.
When `<path>` names a blob rather than a folder, or nothing, the result is S7.
`<path>` must pass `bootstrap.relative` (no backslash, no colon, not absolute, no empty, `.` or `..` part), or the result is S8. `.` alone is refused with S8, because a package's folder name is its skill name and the root has none.

The same rules (2.2 to 2.4) apply when the tool reads a folder on disk: the project's copy (`--keep-existing`, `--accept-merged`, `status`) and a declared skill (`declare --path`).
On disk, a member's mode is `100755` when its owner-execute bit is set, otherwise `100644`. On Windows, which keeps no such bit, the mode is the one the latest receipt records for that path, or `100644` for a path it does not record.

### 2.2 Members: refused, left out, allowed

The tool sorts the entries by path (ASCII order) and applies the steps below one at a time, each over every entry in that order, before moving to the next step. The first refusal found is reported as `the package at <path> is refused: <reason>` (exit 1), so two builders report the same one.

1. **Litter is left out.** An entry whose final component is `.DS_Store`, `Thumbs.db` or `desktop.ini`, or begins with `._` (`bootstrap.OS_FILES` and the `._` rule of `bootstrap.skill_litter`), is not copied and not digested. The candidate and the receipt list it under `litter`.
2. **Mode.** A symbolic link (Git mode `120000`) is refused with P1 and a submodule (`160000`) with P2. On disk, a symlink anywhere under the folder, a file or a directory, is P1, and any other non-regular entry is P1b. `100644` and `100755` are allowed.
3. **Characters.** Each component must match `[A-Za-z0-9._-]+` and must not be `.` or `..`; otherwise P3.
4. **Case.** Two members whose paths are equal after `str.casefold()` are refused with P4.
5. **Git control files.** A final component `.gitignore`, `.gitattributes` or `.gitmodules`, or any component that casefolds to `.git`, is refused with P5. Such a file would change how the project's own Git treats the copy.
6. **Windows names.** A component whose part before its first `.` casefolds to `con`, `prn`, `aux`, `nul`, `com1` to `com9` or `lpt1` to `lpt9`, or a component ending in `.`, is refused with P6.
7. **SKILL.md.** The package root must hold a member `SKILL.md`; otherwise P7.

Every other entry is a member. Version 1 sets no size limit; `show` prints every size.

### 2.3 The SKILL.md front matter reader

The reader takes the text of SKILL.md and returns a dict of strings, plus one dict for a mapping value. It uses no YAML library. It reads the subset below, which every skill in the kit and in the setup skill's templates uses. Plain values may contain `: `, as the kit's descriptions do, which a strict YAML parser would reject.

1. The bytes must decode as strict UTF-8; otherwise P8. One leading U+FEFF is dropped before parsing.
2. The text is split on `\n`, and one trailing `\r` is removed from each line.
3. Line 1, with trailing spaces removed, must be `---`. The front matter ends at the next line that is `---` after the same removal. With no such pair, the result is P9.
4. Front matter lines are read in order:
   - A blank line, or a line whose first non-space character is `#`, is skipped.
   - A tab anywhere in the leading whitespace makes the line unreadable.
   - A **top-level line** (no leading space) must match `^([A-Za-z0-9_-]+):(.*)$`. Group 1 is the key, and group 2 with surrounding spaces removed is `rest`. A key seen twice is P11.
   - When `rest` is empty, the value comes from the indented lines that follow. If the first indented line matches `^ +([A-Za-z0-9_.-]+):(.*)$`, the value is a mapping: every following indented line must match that pattern at the same indentation, and each value is read as a single-line scalar. Otherwise the indented lines, stripped, are joined with one space into a string. With no indented line, the value is the empty string.
   - When `rest` is `|`, `|-`, `|+`, `>`, `>-` or `>+`, the value is a block scalar: the following indented lines, with the first such line's indentation removed, joined with `\n` (for `|`) or with one space between non-blank lines (for `>`), then stripped.
   - When `rest` begins with `"`, it must end with `"`; the inside is the value, with `\"` read as `"` and `\\` as `\`. When it begins with `'`, it must end with `'`; the inside is the value, with `''` read as `'`. A quoted value ends on its own line.
   - Otherwise `rest` is a plain value, taken as written (no comment is stripped), and any indented lines that follow are joined to it with one space.
   - Any other line, a top-level list item (`- x`) for example, is unreadable.
5. The reader raises `FrontMatterError(line, reason)`, where `reason` is `no front matter` (with `line` None; refused with P9), `not readable` (P10, where `line` counts from line 1 of the file) or `duplicate key <k>` (P11).

### 2.4 Adoptable, and valid in format

A package is **adoptable** when it passes 2.2, its SKILL.md is UTF-8, its front matter is readable, and `name` is present and not empty (P12), matches `^[a-z0-9]+(-[a-z0-9]+)*$` and is 1 to 64 characters long (P13), and equals the package's folder name (P14).
These are the only refusals that come from the package's text, because the name is the skill's identity in the project (`.skills/<name>/`).
Everything else in definition 1 is reported and never refused.

### 2.5 Definition 1: the three reported states

The plan calls a package TRACE-compatible when it meets a definition the tool reports. In version 1 the definition has three checks, each reported on its own line part, with this exact wording:

| check | states |
| --- | --- |
| format | `format valid`, or `format invalid: <reasons, in the order below, joined by "; ">` |
| licence | `licence stated`, or `licence not stated` |
| handoff | `handoff stated`, or `handoff not stated: <absent signals joined by ", ">`, or `handoff not checked: scripts/check_skill_handoff.py is not here` |

Format reasons, in checking order: `no SKILL.md`; `SKILL.md is not UTF-8 text`; `no front matter between --- lines`; `front matter line <n> is not readable`; `front matter key <k> appears twice`; `no name`; `name <x> is not 1 to 64 lowercase letters, digits and single hyphens`; `name <x> differs from the folder <y>`; `no description`; `description is longer than 1024 characters`; `compatibility is not 1 to 500 characters` (only when the key is present); `metadata is not a mapping of strings` (only when present); `license is empty` (only when present).
Lengths count characters (Python `len`).
Front matter keys other than `name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools` are listed as a note (`front matter keys not in the specification: <k>, <k>`) and do not make the format invalid.

Licence is stated when the package root holds a member `LICENSE.txt` or `LICENSE`, or the front matter's `license` is non-empty.

Handoff is read by importing `check_skill_handoff` from the tool's own folder and calling `absent_signals(text, name)` on SKILL.md's text, with the folder name as the id. Its constants are reused: `STATED` is `handoff stated`, and the absent signals are `contract named`, `record mechanism named` and `procedure set to its own id`. When the module cannot be imported, the state is `handoff not checked: ...`. The tool keeps no second copy of the signals.

The printed line is `definition 1: <format>; <licence>; <handoff>`.
Every output that prints it also prints this limit line, word for word:

`Limit: definition 1 reads the skill's text; it does not establish that a method is valid, that its scripts are safe, or that any agent discovers the skill.`

In JSON the states are `{"version": 1, "format": "...", "licence": "...", "handoff": "..."}`, under the key `definition`.
Never call this definition "compatibility": the Agent Skills specification uses that word for its own front matter field, which states environment requirements.

## 3. Files in a project

### 3.1 What the tool reads and writes

| path | in Git | written by | holds |
| --- | --- | --- | --- |
| `.skills/<name>/...` | yes; a member the project's Git ignores is listed in the receipt under `not_tracked` | adopt, update, retire | the project's copy, the one agents use |
| `<agent root>/<name>/...` (`.claude/skills`, `.agents/skills` or `.cline/skills`, from `bootstrap.AGENTS`) | as bootstrap leaves them | adopt, update, accept-merged, retire (section 9) | generated agent copies |
| `.trace-os/install.json` | yes | the same commands, only when its bytes change | the kit's installation state (section 9) |
| `.trace-os/skills.lock.json` | yes | every writing command except link and recover | the lock (section 4) |
| `.trace-os/skill-bases/<tree_sha256>/...` | yes, except members the project ignores | adopt, update, accept-merged; removed by update, accept-merged and retire --drop-base | the untouched upstream package |
| `.trace-os/skill-bases/README.md` | yes | adopt, update and accept-merged, in the transaction of an operation that writes an original, when the file is not there; never removed | a note that the folders beside it are originals kept for comparison, not skills to follow (AH-242) |
| `.trace-os/skill-adoptions/<operation id>.json` | yes | the last step of every writing command except link, or recover when it completes one; never rewritten | one receipt per operation (section 5) |
| `ai/SKILLS-IN-USE.md`, between the inventory markers | yes | every writing command except link and recover | the inventory (3.2) |
| `sources/intake.jsonl` | yes | `trace_record.py intake`, run by adopt, update and accept-merged | intake rows (section 10) |
| `sources/transitions.jsonl` | yes | `trace_record.py transition`, run by link | default-adoption rows |
| `.trace-os/receipts/skill-operation.json` | ignored: every install's `.gitignore` ignores `.trace-os/receipts/`, where bootstrap writes its own run receipts (14, A2) | every writing command, during the operation | the operation journal (12.2) |
| `.trace-os/setup.lock`, `.trace-os/transaction.json`, `.trace-os/backups/` | ignored by the starter's `.gitignore` | `bootstrap.locked` and `bootstrap.transaction` | the shared lock and the undo journal |

The tool refuses to write when Git ignores `.skills/<name>/SKILL.md`, `.trace-os/skills.lock.json`, the receipt path, the snapshot's `SKILL.md` or `ai/SKILLS-IN-USE.md` (E18): the record would not reach the project's history.
It checks with `bootstrap.git_ignored(project, names)` over every path it is about to write, and lists the other ignored paths in the receipt under `not_tracked`.

It also refuses when Git would change a file's bytes on commit (E19).
Git's clean step changes a file's bytes only through a `filter`, `ident` or `working-tree-encoding` attribute, or by turning CRLF into LF. So the tool reads those three attributes for every file it is about to write with one `git check-attr -z --stdin filter ident working-tree-encoding`, and a file whose bytes hold no CRLF, at a path where none of the three is set, is stored as it is. For every other file it runs `git hash-object --path=<destination> --stdin` and `git hash-object --no-filters --stdin` on the same bytes; when the two blob ids differ, a line-ending rule or a clean filter would rewrite the file, and the digest recorded for it would not survive a clone. When `git check-attr` fails, every file is hashed both ways. The starter's `.gitattributes` (`* text=auto eol=lf`) rewrites any text file holding CRLF.

### 3.2 The inventory section of `ai/SKILLS-IN-USE.md`

The section lies between `<!-- trace-skills:begin inventory -->` and `<!-- trace-skills:end inventory -->`, outside the updater's managed block (`<!-- trace-os:begin skill-routing -->` to `<!-- trace-os:end skill-routing -->`, an `instruction_sections` entry in `starter/export-manifest.json`; in a project no update to 0.1.0-rc19 has reached yet, the same block under `trace-kit:` markers).

Placement:
- Both inventory markers absent: the section is appended. A file that does not end with `\n` gets one, then a blank line, then the section. A missing file is created holding the section alone.
- Each marker present exactly once, begin before end: the bytes from the begin marker through the end marker, and one following `\n` if present, are replaced (the rule of `bootstrap.marked_range`).
- Any other count or order, or an inventory range that overlaps the routing block: refused with I1.

The section is generated from the lock as it will stand after the operation, plus each external entry's latest receipt for the `--share-with-project` note (the lock holds no such field), in the same transaction as the lock, and its bytes are exactly:

```text
<!-- trace-skills:begin inventory -->
## Skills from outside this project

Written by the kit's skill tool (scripts/trace_skills.py in an installed project) from .trace-os/skills.lock.json; change it with that tool, not by hand.

### Adopted from a source

| Skill id | Source | Commit | Audience | Detached | Receipt |
| --- | --- | --- | --- | --- | --- |
| <name> | <repository> `<path>` | <c12> | <audience cell> | <yes or no> | `<receipt>` |

### Relied on, not in the project

| Skill id | Held by | Digest | Limitation |
| --- | --- | --- | --- |
| <name>, or <name> (relied on by the project's own .skills/<name>) for a `beside_project` entry | <held_by> | <d12> (attested), or not stated | <limitation> |

### Retired

| Skill id | Was | Source or holder | Original kept | Receipt |
| --- | --- | --- | --- | --- |
| <name> | adopted, or declared | <repository> `<path>` at <c12>, or held by <held_by> | `.trace-os/skill-bases/<d12>...`, or no (dropped; Git history keeps it), or not applicable | `<receipt>` |
<!-- trace-skills:end inventory -->
```

The header line names the tool as an installed project holds it, because in the kit's own repository the script is `starter/files/scripts/trace_skills.py` (AH-245).
Rows are sorted by skill id. A subsection with no row holds the single line `None.` in place of its table.
In a cell, `|` is written `\|` and a newline becomes a space.
The `<d12>...` cell is the snapshot folder path cut after the first twelve hex characters of its digest, followed by `...`.
The audience cell is one of: `source public`; `source shared with named collaborators`; `source private to <declared_by>; updates come through them`; `source public (observed)`; `source not public (observed; who can read it is not known)`; `source audience not known`. When `--share-with-project` was given, `; copied here with --share-with-project by <by>` follows.

### 3.3 Temporary folders

Each fetch creates `tempfile.mkdtemp(prefix='trace-skills-')` under `tempfile.gettempdir()`. When that folder resolves to a path inside the project, the command is refused (E10).
Its layout is `<tmp>/source.git` (the bare repository), `<tmp>/package/<member>` (the staged bytes, with the executable bit set for mode `100755`) and `<tmp>/candidate.json`.
`list` removes its folder on exit.
`show` keeps it, so `adopt --candidate` can apply exactly those bytes.
`adopt`, `update` and `update --accept-merged` remove the folder they used once the operation has completed.
An `update` that finds an adapted copy keeps its folder, because the person merges from it.
The operating system's cleaning of temporary files may remove a kept folder; a candidate whose folder is gone is fetched again (6.3).

## 4. The lock

### 4.1 Top level

`.trace-os/skills.lock.json`:

```json
{
  "schema_version": 1,
  "project_audience": {"declared": "collaborators", "declared_by": "Full Name", "declared_at": "2026-10-01T12:00:00Z"},
  "skills": {"<name>": {"manager": "external"}}
}
```

`project_audience` appears once the project's audience has been declared (section 8); it is the standing declaration. `skills` maps each name to one entry.
Kit skills and the project's own skills are not in the lock. They are read from the install state and the tree (section 7).
The lock is written whole, through the transaction, every time.

### 4.2 An `external` entry (adopted)

```json
{
  "manager": "external",
  "source": {"repository": "https://github.com/<owner>/trace-skill-library.git",
             "path": "skills/literature-processing",
             "requested_ref": "main",
             "commit": "<40 hex>",
             "audience": {"declared": "public", "declared_by": "Full Name",
                          "observed": "public (gh, 2026-10-01)"}},
  "project_audience": {"declared": "collaborators", "declared_by": "Full Name", "observed": "not checked: the project has no origin remote"},
  "upstream": {"tree_sha256": "<64 hex>", "tracked_sha256": "<64 hex>",
               "snapshot": ".trace-os/skill-bases/<tree_sha256>"},
  "adopted": {"path": ".skills/literature-processing", "tree_sha256": "<64 hex>",
              "receipt": ".trace-os/skill-adoptions/<operation id>.json", "kept_existing": false},
  "detached": false,
  "replaced_kit_copy": false,
  "receipts": [".trace-os/skill-adoptions/<operation id>.json"]
}
```

| field | type | meaning |
| --- | --- | --- |
| `manager` | `"external"` | adopted from a source; updated only by this tool |
| `source.repository` | string | the address as given, after S1 and S2; a local path is stored absolute |
| `source.path` | string | the package folder inside the repository |
| `source.requested_ref` | string | what was asked for: `HEAD` when `--ref` was not given, a branch, a tag, a full ref or a commit id |
| `source.commit` | 40 hex | the commit it resolved to; with the package digest, this is the version |
| `source.audience.declared` | `private`, `collaborators`, `public` or `not declared` | from `--audience source=...` |
| `source.audience.declared_by` | string or null | who declared it (`--by`) |
| `source.audience.observed` | string | section 8's observed form |
| `project_audience` | object | the project's audience as compared at this operation: `declared`, `declared_by`, `observed` |
| `upstream.tree_sha256` | 64 hex | the package digest over every member (4.6); names the snapshot folder |
| `upstream.tracked_sha256` | 64 hex | the same digest over the members the project's Git does not ignore at `.skills/<name>/`; equal to `tree_sha256` when none is ignored |
| `upstream.snapshot` | string | `.trace-os/skill-bases/<tree_sha256>` |
| `adopted.path` | string | `.skills/<name>` |
| `adopted.tree_sha256` | 64 hex | the digest of the copy as the last operation left it, over the members Git does not ignore |
| `adopted.receipt` | string | the latest adopt, update or accept-merged receipt |
| `adopted.kept_existing` | boolean | the copy was already here and was kept (`--keep-existing`) |
| `detached` | boolean | set by `update --detach`; updates are no longer offered |
| `detached_at`, `detached_by` | string, present only when detached | when and by whom |
| `replaced_kit_copy` | boolean | the adoption removed the kit's copy (`--replace-kit-copy`) |
| `receipts` | list of strings | every receipt for this name, oldest first, including those of a retired entry this one replaced |

**Unchanged or adapted** is computed, never stored. The copy is unchanged when its current digest (4.6, over its members on disk without litter and without files Git ignores, the rule of `bootstrap.skill_files`) equals `upstream.tracked_sha256`. Otherwise it is adapted.
A `--keep-existing` copy that differs from the package is therefore adapted from the start, and an `--accept-merged` copy is adapted unless the person took the upstream unchanged.

### 4.3 An `outside` entry (declared)

```json
{
  "manager": "outside",
  "held_by": "Full Name",
  "path": "~/.claude/skills/<name>",
  "digest": {"tree_sha256": "<64 hex>", "basis": "attested"},
  "limitation": "Not in the project: co-authors cannot read or check this method; a method run records the digest it read.",
  "declared_by": "Full Name",
  "receipt": ".trace-os/skill-adoptions/<operation id>.json",
  "receipts": [".trace-os/skill-adoptions/<operation id>.json"]
}
```

`beside_project: true` is present only for a declaration made with `--beside-project-skill` (6.9, AH-260): the project's own skill of the same name stays and relies on this one too.
`path` is the `--path` given, with the home folder written `~`, or null. `digest.tree_sha256` is computed over that folder (4.6) when `--path` is readable, otherwise null with `basis` `not stated`. When computed, `basis` is always `attested`: the digest is the holder's report, made on their machine, and nobody in the project observed it. `limitation` is `--limitation` or the default shown.

### 4.4 A `retired` entry

```json
{
  "manager": "retired",
  "was": "external",
  "source": {"repository": "...", "path": "...", "requested_ref": "...", "commit": "..."},
  "held_by": null,
  "upstream": {"tree_sha256": "<64 hex>", "snapshot": ".trace-os/skill-bases/<tree_sha256>", "dropped": false},
  "retired_at": "2026-10-01T12:00:00Z",
  "receipt": ".trace-os/skill-adoptions/<operation id>.json",
  "receipts": ["..."]
}
```

`was` is `external` or `outside`. For a retired `outside` entry, `source` and `upstream` are null and `held_by` is kept.
`upstream.dropped` is true after `retire --drop-base`, and `snapshot` then names a folder whose committed files are gone; the folder itself may remain when it held a file Git ignores, which the retirement leaves in place (AH-235).
A later adoption of the same name replaces the retired entry, and carries its `receipts` forward.

### 4.5 Operation ids

An operation id is `<stamp>-<name>-<operation>`, where `<stamp>` is the plan's time as `YYYYMMDDTHHMMSSZ` and `<operation>` is one of `adopt`, `update`, `accept-merged`, `detach`, `attach`, `retire`, `declare`.
An id is never reused while anything names it: when `.trace-os/skill-adoptions/<id>.json` exists, or any lock entry's `receipts` or the operation journal names it, `-2`, `-3` and so on is appended until it is free. An undone operation leaves nothing that names its id, so a re-run plans again with its own stamp and may, within the same second, take the same id. The check runs at the plan and again under the lock.
Because names and operations both contain hyphens, a reader takes the name and the operation from the receipt's fields, never by splitting the id.

### 4.6 The package digest

`package_digest(files)` takes a list of members, each with `path`, `mode`, `size` and `sha256`, and returns 64 lowercase hex characters:

1. Litter is already out (2.2, step 1).
2. Sort the members by `path`, compared as ASCII strings (every path is ASCII after P3).
3. For each member, form the line `<mode> <size> <sha256> <path>\n`. `mode` is `100644` or `100755`. `size` is the byte count in decimal without leading zeros. `sha256` is the lowercase hex SHA-256 of the member's bytes exactly as they are, with no line-ending conversion. `path` is relative to the package folder, with `/` separators.
4. Concatenate the lines, encode them as ASCII, and take the SHA-256.

`tree_sha256` uses every member. `tracked_sha256`, and the digest of a copy on disk, use the members Git does not ignore at their `.skills/<name>/` path.

Test vector. A package with three members:

```text
100644 19 4037bd3dec2b7a4ad2e609290f0be9ee3d64738190db987950c73ee38aa6b5d3 LICENSE.txt
100644 75 cbc7dd7439c1652df4c5607a8061bd264347d12c393c4898cc4251c2b8e4111b SKILL.md
100755 12 ad64355106bb158b020ecf9702be48f7730fc091dd4bb6a2f092b40393495b3d scripts/run.py
```

The members' bytes are `Apache License 2.0\n`; `---\nname: example-skill\ndescription: An example skill for tests.\n---\nBody.\n`; and `print('ok')\n` (executable).
The package digest is `00c6ea65a3bcf3089fb6c48e684604dc26207fac72c61276d41c0d291e5396d3`.
With `scripts/run.py` ignored by the project, the tracked digest is `c28c830a561645ef6da9382caefb0072a7ebf60f20fff0a9efa5c4dceec31324`.

### 4.7 Reading the lock

A missing lock is an empty lock. A lock that is not valid JSON, not an object, or holds an entry of an unknown `manager` or with a field of the wrong type is E3. A `schema_version` other than 1 is E4.
A lock entry whose receipt file is missing is reported by `status` (`receipt: <path> (missing)`). A writing command that needs that receipt falls back to the snapshot.

## 5. The receipt and the candidate file

### 5.1 The receipt

`.trace-os/skill-adoptions/<operation id>.json`, written once by `bootstrap.write_atomic` as the operation's last write and never rewritten:

```json
{
  "schema_version": 1,
  "kind": "trace-skills-receipt",
  "contract": 1,
  "operation": "adopt",
  "operation_id": "20261001T120000Z-literature-processing-adopt",
  "name": "literature-processing",
  "started_at": "2026-10-01T12:00:00Z",
  "completed_at": "2026-10-01T12:00:04Z",
  "recovered": false,
  "tool": {"path": "scripts/trace_skills.py", "sha256": "<64 hex of the running script>"},
  "project": {"installed": true, "kit_version": "0.1.0-rc19", "agent": "claude", "head": "<40 hex or null>"},
  "source": {"repository": "...", "path": "...", "requested_ref": "...", "commit": "..."},
  "upstream": {"tree_sha256": "...", "tracked_sha256": "...", "snapshot": "...",
               "files": [{"path": "SKILL.md", "mode": "100644", "size": 75, "sha256": "..."}],
               "litter": []},
  "previous": null,
  "copy": {"path": ".skills/literature-processing", "tree_sha256": "...", "kept_existing": false,
           "files": [{"path": "SKILL.md", "mode": "100644", "size": 75, "sha256": "..."}]},
  "definition": {"version": 1, "format": "format valid", "licence": "licence stated", "handoff": "handoff stated"},
  "audience": {"source": {"declared": "public", "declared_by": "Full Name", "observed": "public (gh, 2026-10-01)"},
               "project": {"declared": "collaborators", "declared_by": "Full Name", "observed": "not checked: the project has no origin remote"},
               "comparison": "source at least as visible as the project",
               "share_with_project": {"required": false, "given": false}},
  "ownership": {"before": "free", "replaced_kit_copy": false, "kit_files_removed": [], "retired_skills_added": []},
  "agent_copies": {"agent": "claude", "written": [".claude/skills/literature-processing/SKILL.md"], "kept": [],
                   "removed": [],
                   "note": "copies of the members Git does not ignore, as scripts/bootstrap.py --sync-skills makes them"},
  "writes": {".skills/literature-processing/SKILL.md": {"before": null, "after": "<64 hex>"}},
  "modes": {".skills/literature-processing/scripts/run.py": "100755"},
  "not_tracked": [],
  "decided": {"by": "Full Name", "words": "yes, adopt it", "authority": "T3 of ai/records/<id>",
              "execution_agent": "claude-opus-5-5", "confirmed": "--yes"},
  "records": {"intake": {"id": "IN-012", "registry": "sources/intake.jsonl", "row_sha256": "<64 hex>"},
              "transition": "not yet: link writes it after the commit"},
  "limits": ["..."]
}
```

| field | meaning |
| --- | --- |
| `operation` | `adopt`, `update`, `accept-merged`, `detach`, `attach`, `retire` or `declare` |
| `started_at`, `completed_at` | when the plan was made, and when the receipt was written |
| `recovered` | true when `recover` finished the operation (12.4) |
| `tool` | the running script's path and SHA-256 |
| `project` | `installed`: whether `.trace-os/install.json` exists; `kit_version` and `agent` from it, or null; `head`: `git rev-parse --verify HEAD` at the plan, or null |
| `source`, `upstream` | as in the lock. `upstream.files` is the full manifest (every member with path, mode, size and sha256) and `litter` the entries left out. Null for detach, retire of an outside entry, and declare |
| `previous` | for update and accept-merged: `{"commit", "tree_sha256", "adopted_tree_sha256"}` before the operation; otherwise null |
| `copy` | the copy as the operation left it: its digest, its manifest (members Git does not ignore), and `kept_existing`. Null for detach, retire and declare |
| `definition` | the three states of the package (adopt, update) or of the merged copy (accept-merged); null otherwise |
| `audience` | section 8; null for detach, retire and declare |
| `ownership.before` | what the name was before: `free`, `retired`, `kit`, `project`, `external` or `outside` |
| `ownership.kit_files_removed`, `retired_skills_added` | for `--replace-kit-copy` |
| `kept_outside` | adopt only: with `--keep-existing`, `scripts/<name>` when the project holds that folder (6.3, AH-251); otherwise `[]` |
| `agent_copies` | `agent` is the install state's agent, or null when there is none; `written` lists the copies this operation wrote, `kept` the copies it leaves that already held their bytes (a mode the forward step sets does not make a copy written), and `removed` the copies it removed, all as paths (AH-241); `note` is one of the three notes in section 9 |
| `writes` | every path in the transaction with its hash before and after (null for absent), sorted by path |
| `modes` | every path the operation left with mode `100755` |
| `not_tracked` | every path it wrote that the project's Git ignores |
| `decided` | `by` from `--by` (default `git config user.name`, else `not stated`); `words` from `--words` verbatim, else `not stated`; `authority` from `--authority`, else `nothing stated`; `execution_agent` from `--execution-agent`, else null; `confirmed` is `--yes` or `at the terminal`; for accept-merged only, `merged_by` from `--merged-by`, else null |
| `records.intake` | for adopt, update and accept-merged: the intake row's id, its registry, and the SHA-256 of that row's line as written (UTF-8, without its newline). Null for the other operations |
| `records.transition` | for adopt, update and accept-merged, the fixed text shown, and `link` writes the row and never edits the receipt; for detach, attach, retire and declare, which adopt no instruction and whose receipts `link` refuses (L2), `not applicable: no instruction was adopted by this operation` (AH-240) |
| `history` | retire only: `Git history keeps every file this operation removed, once the removal is committed.` |
| `left_in_place` | retire only: every file Git ignores under the copy and, with `--drop-base`, under the snapshot, which the operation left where it was (AH-235); `[]` when nothing was left or a declaration was retired |
| `limits` | fixed lines, below |

`limits` always holds these two lines:
`This receipt records what the tool wrote and what it was told; it is not acceptance, and it does not establish that the method is valid or that its scripts are safe.`
and
`The intake row names this receipt by its path; this receipt names the row by its id and digest.`
A retire receipt adds: `A file that was never committed is not kept anywhere.`

### 5.2 The candidate file

`show` writes `<tmp>/candidate.json`. So does `adopt` or `update` when it ends without applying (C1); an `update --accept-merged` that ends without applying writes its candidate with `for.operation` `update`, so that a re-run never claims that the three versions were printed when that run did not print them (version 1 cannot otherwise tell the two candidates apart).

```json
{
  "schema_version": 1,
  "kind": "trace-skills-candidate",
  "contract": 1,
  "created_at": "2026-10-01T12:00:00Z",
  "created_by": "show",
  "for": {"operation": "adopt", "name": "literature-processing", "from_commit": null},
  "source": {"repository": "...", "path": "...", "requested_ref": "main", "commit": "<40 hex>"},
  "package": {"name": "literature-processing", "tree_sha256": "<64 hex>",
              "files": [{"path": "SKILL.md", "mode": "100644", "size": 75, "sha256": "..."}],
              "litter": [], "definition": {"version": 1, "format": "...", "licence": "...", "handoff": "..."},
              "front_matter_notes": []},
  "staged": "/absolute/tmp/trace-skills-abc123/package"
}
```

`created_by` is `show`, `adopt` or `update`. `for.operation` is `adopt`, `update` or `accept-merged`; `for.from_commit` is the lock's commit for an update, otherwise null. `staged` is the absolute path of the staged bytes.
A candidate is valid when `schema_version` is 1 and `kind` matches (otherwise C6), and its source repository and path equal the command's (otherwise C4).
It is used by re-hashing every file under `staged`. The set of paths, their modes and their SHA-256 must equal `package.files` exactly, and `package_digest` must equal `tree_sha256`; otherwise C5.
When the `staged` folder no longer exists, the tool fetches `source.commit` again into a new temporary folder and applies the same comparison. A source that no longer serves the commit is S6.
`staged` must resolve outside the project; otherwise E10.

## 6. Commands

### 6.0 Options and messages shared by commands

Every command accepts `--project DIR`, before or after the command name. The default is the folder above the script's `scripts/` folder. The project must be the top of a Git working tree (`git rev-parse --show-toplevel` equals it, resolved) and must not be a symlink; otherwise E1. In the kit's own repository the script lives at `starter/files/scripts/`, so there it runs with `--project .`.

Writing commands (adopt, update, retire, declare) also accept:
- `--yes`: the person has seen the plan and agreed; apply without asking (rule 7);
- `--by NAME`: who directed the operation (default `git config user.name` in the project, else `not stated`);
- `--words TEXT`: the person's words directing or agreeing to it, verbatim;
- `--authority TEXT`: what authorized it, a turn id in a trace record or a standing instruction;
- `--execution-agent MODEL`: the model id of an agent running the tool, checked with `bootstrap.check_executor` (E12).

Commands that fetch (list, show, adopt, update) accept `--ref REF`. adopt and update accept `--candidate FILE`, `--audience source=VALUE` and `--audience project=VALUE` (repeatable; VALUE is `private`, `collaborators` or `public`; otherwise A2), `--share-with-project`, and `--used-for TEXT`.

**Confirmation.** A writing command prints its plan, then:
- with `--yes`: applies;
- otherwise, when stdin is a terminal: prints `Apply? Nothing is committed or pushed. [y/N] ` and applies only on `y` or `yes`, compared after `casefold()`; anything else gives C2;
- otherwise: writes a candidate where the command fetched, prints C1 and exits 1.
`--yes` without `--candidate` is accepted only when `--ref` is a full 40-hex commit id, or the command fetches nothing (retire, detach, declare); otherwise C3.
The plan is made without the setup lock. The lock is taken after the confirmation, and every precondition is read again under it before the first write.

**Message catalogue.** Every refusal and error has an id, a message and an exit code. Builders print the message exactly, with placeholders filled. Messages on stderr carry the `trace-skills: ` prefix; C1 and C2 go to stdout without it.

| id | message | exit |
| --- | --- | --- |
| E1 | `<project> is not the top of a Git working tree; run this from the project's root, or pass --project` | 2 |
| E2 | `Git is not installed or not on PATH` | 2 |
| E3 | `cannot read .trace-os/skills.lock.json (<reason>); restore it from Git: git checkout HEAD -- .trace-os/skills.lock.json` | 2 |
| E4 | `.trace-os/skills.lock.json has schema_version <v>; this tool reads version 1` | 2 |
| E5 | `cannot read .trace-os/install.json (<reason>)` | 2 |
| E6 | `another setup process owns this target; wait for it to finish, then run this again` | 2 |
| E7 | `an earlier operation (<operation id>) did not finish; run <tool> recover first` | 1 |
| E8 | `an unfinished setup transaction is pending (.trace-os/transaction.json); run <tool> recover first` | 1 |
| E9 | `a kit update did not finish; run python3 scripts/bootstrap.py --recover first` | 1 |
| E10 | `the temporary folder <dir> is inside the project; set TMPDIR to a folder outside it` | 2 |
| E11 | `scripts/trace_record.py is not in this project, so the <intake or transition> row cannot be recorded; nothing was written` | 2 |
| E12 | `execution agent must be one identifier` | 2 |
| E13 | `writing failed (<error>); everything this operation wrote was undone` | 2 |
| E14 | `<path> changed after the plan was shown; nothing was written. Run the command again for a new plan` | 1 |
| E15 | `<path> changed while this operation was writing; everything else it wrote was undone, and <path> keeps the later edit` | 1 |
| E16 | `interrupted; run <tool> recover to finish or undo <operation id>` | 128+n |
| E16n | `interrupted; nothing was written` (before the operation journal exists) | 128+n |
| E17 | `the files were written, but <step> failed (<error>); run <tool> recover to finish <operation id>` | 2 |
| E18 | `Git ignores <path> in this project, so it would not reach the project's history; change that ignore rule first` | 1 |
| E19 | `Git would rewrite <path> when it is committed (a line-ending rule or a filter in .gitattributes, or core.autocrlf), so its recorded digest would not survive a clone; exclude <folder> from conversion in .gitattributes, for example "<folder>/** -text", then run this again` | 1 |
| E20 | `symlink is not supported: <path>` | 2 |
| E21 | `--merged-by applies to update --accept-merged only` | 2 |
| I1 | `the inventory markers in ai/SKILLS-IN-USE.md are not one begin and one end, in that order, outside the kit's skill-routing block; fix them by hand, then run this again` | 1 |
| S1 | `the repository address carries a user name and password, a token, a query or a fragment; give it without them and let Git's credential helper supply access` | 2 |
| S2 | `a repository address cannot begin with "-"` | 2 |
| S3 | `the source has no branch or tag named <ref>` | 2 |
| S4 | `<ref> names both a branch and a tag in the source; pass refs/heads/<ref> or refs/tags/<ref>` | 2 |
| S5 | `could not fetch <repository>: <first line of Git's error>` | 2 |
| S6 | `the source does not serve commit <commit>` | 2 |
| S7 | `there is no package folder <path> at commit <c12>` | 2 |
| S8 | `the package path must be a relative path inside the repository, with no empty, "." or ".." part: <path>` | 2 |
| P | `the package at <path> is refused: <reason>`, with the reasons P1 to P14 below | 1 |
| N2 | `<name> is adopted from this source at <c12>; use update <name> to move it` | 1 |
| N3 | `<name> is adopted from another source (<repository> <path>); retire it first: <tool> retire <name>` | 1 |
| N4 | `<name> is declared as held outside the project by <held_by>; retire the declaration first: <tool> retire <name>` | 1 |
| N5 | `<name> is a skill the kit ships; adopting it would leave two copies under one name. Pass --replace-kit-copy to remove the kit's copy and keep the kit's updater from installing it again, or choose another package` | 1 |
| N6 | `the kit's copy of <name> was edited here (<path>); --replace-kit-copy removes only an unedited kit copy. Move the edit to a file of your own, then run this again` | 1 |
| N7 | `<name> already exists in this project (.skills/<name>/), written here or of unknown origin; pass --keep-existing to keep it and record this package as its upstream original, or move it first` | 1 |
| N8 | `a single-file skill .skills/<name>.md has this name; turn it into .skills/<name>/SKILL.md or remove it first` | 1 |
| N9 | `--replace-kit-copy: <name> is not a skill the kit ships here; nothing to replace` | 1 |
| N10 | `--keep-existing: there is no .skills/<name>/ here to keep` | 1 |
| N11 | `--replace-kit-copy and --keep-existing exclude each other` | 2 |
| N12 | `no skill named <name> is adopted here` | 1 |
| N13 | `<name> is a skill the kit ships; the kit's updater updates it (SETUP-AGENT.md, Update)` | 1 |
| N13r | `<name> is a skill the kit ships; retire it as SETUP-AGENT.md describes: delete .skills/<name>/ and run python3 scripts/bootstrap.py --sync-skills` | 1 |
| N14 | `<name> is the project's own skill; this tool updates and retires only skills it adopted or declared` | 1 |
| N15 | `<name> is declared, not in the project; there is nothing to update` | 1 |
| N16 | `<name> is retired; adopt it again to bring it back` | 1 |
| N17 | `<name> is detached: updates are not offered; status reports whether its source moved` | 1 |
| N18 | `the package at <path> is now named <x>; a renamed package is another skill: retire <name>, then adopt <x>` | 1 |
| N19 | `<name> is not a valid skill name (1 to 64 lowercase letters, digits and single hyphens)` | 2 |
| N20 | `the skill at <path> is named <x>, not <name>` | 1 |
| N21 | `--path <path> is not a folder holding a SKILL.md` | 2 |
| N22 | `retiring <name> would delete changes Git has not recorded (<paths>); commit them first, so that history keeps them` | 1 |
| N23 | `a generated copy of <name> was edited separately (<path>); merge or remove it, then run this again` | 1 |
| N24 | `the upstream original under <snapshot> was edited (<path>); it is the record of what was adopted. Restore it with git checkout HEAD -- <path>, then run this again` | 1 |
| N25 | `<name> is pinned to commit <c12>; name the ref to update to with --ref` | 2 |
| N26 | `the copy .skills/<name>/ is not here; retire <name>, or adopt it again` | 1 |
| N27 | `<name> is a <manager> entry; <operation> applies to adopted skills only` | 1 |
| N28 | `<name> is already a skill of this project (<owner>); declare records only a skill that is not in the project`; for the owner `project` it ends `; if that skill only says to use a personal copy, remove it first and declare the personal skill; if it holds rules of its own and relies on a personal copy too, declare the personal skill with --beside-project-skill, which keeps the project's skill as it is` (AH-252, AH-260) | 1 |
| N29 | `--beside-project-skill needs a skill of the project named <name> (.skills/<name>/ or .skills/<name>.md); there is none, so declare it without the flag` (AH-260) | 2 |
| C1 | `Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, run: <the same command> --candidate <file> --yes` | 1 |
| C1n | `Nothing was applied: no terminal to confirm on. Show the plan above to the person; when they agree, run: <the same command> --yes` (detach, attach, retire and declare, which fetch nothing and write no candidate) | 1 |
| C2 | `Nothing was applied: not confirmed.` | 1 |
| C3 | `--yes applies what the person saw: pass the --candidate file that show, adopt or update wrote, or --ref with a full commit id` | 1 |
| C4 | `the candidate is for <repository> <path> at <c12>, which does not match this command (<repository> <path>, ref <ref>)` | 1 |
| C5 | `the package's bytes changed since they were shown (<member>); run show again and review it` | 1 |
| C6 | `<file> is not a trace-skills candidate file` | 2 |
| A1 | `the source (<source audience>) is not known to be public, and the project is <project audience>: copying it here lets everyone who can read the project read it. If the person wants that, pass --share-with-project, with their words in --words` | 1 |
| A2 | `--audience takes source=<value> or project=<value>, where the value is private, collaborators or public` | 2 |
| L1 | `<receipt> is not a receipt this tool wrote` | 2 |
| L2 | `<receipt> records a <operation>, which adopts no instruction; link records adoptions, updates and accepted merges` | 1 |
| L3 | `not a commit in this repository: <commit>` | 2 |
| L4 | `commit <c12> does not contain <receipt> as it is here; name the commit that recorded this operation` | 1 |
| L5 | `commit <c12> holds no .skills/<name>/SKILL.md` | 1 |
| L6 | `<receipt> is already linked to <commits> by <TR id>; a transition row is not amended` | 1 |
| R1 | `the pending setup transaction is not this tool's (it names <path>); run python3 scripts/bootstrap.py --recover` | 1 |
| R2 | `recovery would overwrite a later edit: <path>` | 1 |
| R3 | `the operation journal .trace-os/receipts/skill-operation.json cannot be read (<reason>); inspect it and .trace-os/backups/ before removing it` | 2 |

Package reasons, printed after `is refused: `:
P1 `<member> is a symbolic link`; P1b `<member> is not a regular file`; P2 `<member> is a submodule`; P3 `<member> uses characters outside A-Z, a-z, 0-9, ".", "_", "-" and "/"`; P4 `<member> and <other> differ only in letter case`; P5 `<member> is a Git control file, which would change how this project's Git treats files`; P6 `<member> uses a name Windows reserves`; P7 `it has no SKILL.md`; P8 `SKILL.md is not UTF-8 text`; P9 `SKILL.md has no front matter between --- lines`; P10 `SKILL.md front matter line <n> is not readable`; P11 `SKILL.md front matter key <key> appears twice`; P12 `SKILL.md names no skill`; P13 `its name <x> is not 1 to 64 lowercase letters, digits and single hyphens`; P14 `its name <x> differs from its folder <y>`.

A `bootstrap.SetupError` the tool does not translate is printed as `trace-skills: <its message>` and exits 2. A `PermissionError` while writing is E13, followed by bootstrap's own advice: `keep the permission boundary. Use the normal permission process or the explicit-instruction --agent none route.`

**Fetching** (list, show, adopt, update, status):
1. The address is checked first. It must not begin with `-` (S2); on the command line such an address reaches the check only after `--`, since argparse reads it as an option otherwise. An `http`, `https` or `ssh` URL must have no password and no query or fragment, and an `http` or `https` URL no user name either (S1); `git@host:path` and `ssh://git@host/path` are accepted. A local path is made absolute.
2. Every Git command on the source runs with `GIT_TERMINAL_PROMPT=0` and `-c protocol.ext.allow=never`, and with `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` when neither `GIT_SSH_COMMAND` nor `GIT_SSH` is set. Each has a timeout of 300 seconds, whose expiry is S5. The person's own credential helpers are used.
3. Resolution. With no `--ref`, the requested ref is `HEAD`, resolved by `git ls-remote --symref <repository> HEAD`. A 40-hex `--ref` is the commit. A `--ref` beginning `refs/` must appear exactly in `git ls-remote <repository>`. Any other ref is looked up as `refs/heads/<ref>` and `refs/tags/<ref>`, preferring the peeled `^{}` line for a tag: none is S3, both is S4. An abbreviated commit id is not supported and falls through to S3.
4. `git init --bare -q <tmp>/source.git`, then `git -C <tmp>/source.git fetch -q --no-tags --depth=1 <repository> <commit>`. If that fails, a full fetch of `+refs/heads/*:refs/remotes/source/heads/*` and `+refs/tags/*:refs/remotes/source/tags/*` follows. Then `git cat-file -e <commit>^{commit}` must succeed, or S6. A failure of ls-remote or of both fetches is S5.

### 6.1 `list <repository> [--ref REF]`

Reads: the source at the resolved commit. Writes: nothing in any project; its temporary folder is removed on exit.
A package is every folder at that commit holding a `SKILL.md` blob directly, at any depth except the root.
Prints:

```text
Source: <repository> at <c12> (requested <ref>)
Packages: <n>
  <path>: <name or "(no name)">
    <description, cut at 200 characters with "..." when longer>
    definition 1: <format>; <licence>; <handoff>
  <path>: refused: <reason>
Nothing was copied into any project.
Limit: definition 1 reads ...
```

With no package, the second line is `Packages: 0 (no folder holding a SKILL.md at this commit)`.
Exit 0, or 2 on S1 to S6.

### 6.2 `show <repository> <path> [--ref REF]`

Reads: the package. Writes: the staging folder and `<tmp>/candidate.json` (5.2, `created_by` `show`, `for.operation` `adopt`). Nothing is written in a project.
Prints:

```text
Source: <repository> at <commit> (requested <ref>)
Package: <path> (<name>), <n> files, <total bytes> bytes
Package digest: <tree_sha256>
definition 1: <format>; <licence>; <handoff>
Front matter keys not in the specification: <keys>          (only when there are any)
Left out as operating-system files: <paths>                (only when there are any)

--- <member> (<mode>, <size> bytes, sha256 <64 hex>)
<content>
--- end of <member>
(one such block per member, in path order)

Candidate: <tmp>/candidate.json
To adopt exactly these bytes: <tool> adopt <repository> <path> --candidate <tmp>/candidate.json
The staged copy at <tmp>/package is removed when adopt applies it.
Limit: definition 1 reads ...
```

Content is printed when it decodes as strict UTF-8 and contains no NUL. Otherwise the block holds `(not UTF-8 text: <size> bytes; not printed)`.
Printed text is escaped. Every C0 control character except `\t` and `\n` is shown as `\xNN`, and so are DEL and C1 controls (U+0080 to U+009F). The bidirectional controls U+202A to U+202E and U+2066 to U+2069 are shown as `\uNNNN`. A `\r` is shown as `\r`. So a terminal never interprets package bytes, and a reader sees reordered text for what it is.
Exit 0; 1 on a refusal (no candidate is written); 2 on S1 to S8.

### 6.3 `adopt <repository> <path> [--ref REF] [--candidate FILE] [--audience ...] [--share-with-project] [--replace-kit-copy] [--keep-existing] [--used-for TEXT] [--yes] [--by] [--words] [--authority] [--execution-agent]`

Reads: the package (fetched, or from the candidate), the lock, the install state when present, the tree under `.skills/`, the agent copies, `ai/SKILLS-IN-USE.md`, Git's ignore and attribute rules, and `git config user.name`.

Preconditions, checked in this order at the plan and again under the lock:
1. E1, E2; N11 when both exclusive flags are given; A2.
2. No operation journal (E7), no `.trace-os/transaction.json` (E8), no `.trace-os/pending-update.json` (E9).
3. The lock and the install state are readable (E3, E4, E5).
4. The package: fetched or from the candidate (C4, C5, C6), adoptable (P), with `--ref` and the candidate's ref agreeing when both are given (C4).
5. The name (section 7): N2, N3, N4, N5, N6, N7, N8, N9, N10. The same source, path and commit as the active entry is not a refusal: it prints `Already adopted: <name> from <repository> <path> at <c12>; nothing to do.` and exits 0.
6. The audience (section 8): A1 when `--share-with-project` is required and not given.
7. `scripts/trace_record.py` exists (E11).
8. Ignore and conversion checks over every path to be written (E18, E19). Agent copies (section 9): N23. The inventory markers: I1.
9. Under the lock only: every destination's current hash equals the hash read at the plan (E14), and the staged bytes still match the manifest (C5).

Shown before writing:
- without `--candidate`, the whole package exactly as `show` prints it, from `Source:` to the last member block;
- with `--candidate`, the `Source:` to `Left out` header lines and a manifest of one line per member, `<mode> <size> <sha256> <path>` (show printed the contents);
- then the plan:

```text
Plan: adopt <name> into <project>
  from: <repository> <path> at <commit> (requested <ref>)
  package digest: <tree_sha256>
  definition 1: <format>; <licence>; <handoff>
  name: <name> is free | the kit's copy is replaced (--replace-kit-copy) | the existing copy is kept and connected (--keep-existing) | a retired entry is replaced
  difference from the package to the kept copy (status will report the copy adapted):      (--keep-existing only)
    added <path> | removed <path> | changed <path> | mode <path>
  the project holds scripts/<name>/, where the template route put this skill's scripts; the package keeps its scripts under .skills/<name>/scripts/, and the two are not compared. To move them into the package's folder, change every command in .skills/<name>/ that names scripts/<name>/ to the new path in the same step and run one of them before connecting; or keep them where they are and know that status compares only .skills/<name>/      (--keep-existing only, when that folder is there)
<the unified diff of each changed member>                         (--keep-existing only)
  the kit's files that share this skill's name stay where the kit ships them: <paths>      (--replace-kit-copy only, when there are any)
  audience: source <source form>; project <project form>; <comparison>
  Shared with the project: by <by>; words: <words>              (only with --share-with-project; without it a source not known to be public is A1)
  writes, in this order:
    <n> file(s) under .skills/<name>/                     or: .skills/<name>/ is kept as it is
    .trace-os/skill-bases/README.md (says the originals there are not skills to follow)      (only when it is not there; 3.1)
    <n> file(s) under .trace-os/skill-bases/<tree_sha256>/
    <n> agent copies under <agent root>/<name>/           or: no agent copies (agent none) | no agent copies (no adapter state here)
    <n> kit file(s) removed: <paths>                       (--replace-kit-copy only)
    .trace-os/install.json                                (only when it changes)
    .trace-os/skills.lock.json
    ai/SKILLS-IN-USE.md (the inventory section)
  then: an intake row in sources/intake.jsonl, and last the receipt .trace-os/skill-adoptions/<operation id>.json
  not tracked by Git here: <paths> | none
```

With `--keep-existing`, the difference lines compare the package's members that Git does not ignore at `.skills/<name>/` with the kept copy, in the format of 6.5's difference list, one line per member that differs, in path order; with none, the plan prints the single line `  difference from the package to the kept copy: none (status will report the copy unchanged)` in their place (AH-239). Either way the person learns, before agreeing, whether status will call the copy adapted.
After the difference lines comes a unified diff from `package/<path>` to `copy/<path>` for every changed member that is UTF-8 text in both, in 6.5's form (three lines of context, escaped as in 6.2), so the person can tell whose change each one is (AH-256).
When the project holds a folder `scripts/<name>/`, where the setup skill's template route copied a skill's scripts, the plan names it after the difference lines and the receipt lists it under `kept_outside`; the tool never compares that folder with the package (AH-251).
The agent copies line counts every copy the skill has once the operation is done, those it writes and those that already hold their bytes; the result line below counts only those written (AH-241).

Writes, in this order: the transaction of 12.3, then the forward steps of 12.4.
Prints after applying:

```text
Adopted <name> from <repository> <path> at <c12>.
  receipt: .trace-os/skill-adoptions/<operation id>.json
  intake: <IN id> in sources/intake.jsonl
  agent copies: <n written> written for <agent> | none (agent none) | none (no adapter state here)
Next: review and commit these files, then record the adoption against that commit:
  <tool> link .trace-os/skill-adoptions/<operation id>.json --commit <sha>
Nothing was committed or pushed.
```

Exit 0 when applied or already adopted. Other exits follow the catalogue.

### 6.4 `status [--offline]`

Section 11. Reads: the install state, the lock, the receipts, the tree under `.skills/`, the snapshots, the operation journal, the folders under the home directory named in 11.3, and each external entry's source unless `--offline`. Takes no lock and writes nothing, not even `.trace-os/`. Exit 0 whatever it reports; 2 only when the lock or the install state cannot be read (E3, E4, E5) or the project is not a working tree (E1).

### 6.5 `update <name> [--ref REF] [--candidate FILE] [--audience ...] [--share-with-project] [--used-for TEXT] [--yes] [--by] [--words] [--authority] [--execution-agent]`

Reads: as adopt, plus the entry's snapshot and latest receipt.
Preconditions, in order: as adopt steps 1 to 3; then the name must be an `external` entry (N12, N13, N14, N15 for a plain update of a declared entry, N16 for a retired entry under every form, N27 for `--accept-merged`, `--detach` or `--attach` on a declared entry), not detached (N17), with its copy present (N26). When `--ref` is absent the lock's `requested_ref` is used, and a commit id there is N25.
The source is fetched (or the candidate used, for `update`), and the package must be adoptable and still carry the name (N18).
When the resolved commit equals `source.commit`, the command prints `Current: <name> is at <c12>, the commit <ref> resolves to; nothing to do.` and exits 0.
When the commit differs but `tree_sha256` is unchanged, the update records only the new commit. It is applied like any update, with no copy or snapshot file in its transaction, and so no README.md of 3.1 either.

**An unchanged copy** (current digest equals `upstream.tracked_sha256`). Shown: the whole new package, as `show` prints it; then the difference from the current upstream, one line per member, `added <path>`, `removed <path>`, `changed <path>` or `mode <path>` (path order); then the plan, as adopt's with `Plan: update <name> from <c12> to <c12>` and these writes in order:
- the copy's changed and added members, and the removal of members the new package lacks;
- `.trace-os/skill-bases/README.md` when it is not there (3.1), then the new snapshot;
- agent copies;
- removal of the old snapshot's files, when no other lock entry names it;
- `install.json` when it changes;
- the lock;
- the inventory.
A file Git ignores that the package never held is left in place. A changed old-snapshot file is N24.
Prints after applying: as adopt, with `Updated <name> from <c12> to <c12>.`

**An adapted copy.** Nothing is written in the project. Shown, in order:
- `<name> is adapted here; the tool does not merge. The three versions:`;
- `  original (what was adopted): <snapshot path, or "the receipt's manifest; the snapshot is not here">`;
- `  this project's copy: .skills/<name>/`;
- `  new upstream: <tmp>/package (<repository> <path> at <c12>)`;
- the difference lists from original to copy and from original to new upstream (the format above), with a unified diff (`difflib.unified_diff`, three lines of context, escaped as in 6.2) for every changed member that is UTF-8 text in both;
- `Nothing was written. Merge by hand into .skills/<name>/, then record the merge: <tool> update <name> --accept-merged --candidate <tmp>/candidate.json`.
The candidate (`for.operation` `accept-merged`, `for.from_commit` the lock's commit) and the staging folder are kept. Exit 1.

### 6.6 `update <name> --accept-merged [--candidate FILE | --ref COMMIT] [--merged-by NAME] [--audience ...] [--share-with-project] [--used-for TEXT] [--yes] [--by] [--words] [--authority] [--execution-agent]`

For after a person has merged the new upstream into the copy by hand. It records the merge and never changes the copy.
`--merged-by NAME` names whoever made the merge, a person's name or a model id, and is recorded as `decided.merged_by` (null without the flag); `--execution-agent` names only the agent running the tool and is never taken to be the merger, and any other form of `update` given `--merged-by` is E21.
Preconditions: as update, except that the copy may be adapted. A `--candidate` (C3 applies to `--yes`) or a 40-hex `--ref` names the new upstream. The copy on disk must pass 2.2 and 2.4 read as a folder (P), with its front matter name still `<name>` (N18).
Shown: the new upstream's `Source:` header and manifest, the copy's digest, the difference from the new upstream to the copy (lists and diffs as in 6.5), the copy's `definition 1` line, and the plan, `Plan: record the merged copy of <name> against <c12>`, whose line after `merged copy:` is `merged by: <merged-by>` or `merged by: not stated`. The plan's writes, in order: `.trace-os/skill-bases/README.md` when it is not there (3.1), then the new snapshot; agent copies made from the copy as it stands; removal of the old snapshot (when no other entry names it); `install.json` when it changes; the lock (new `source.commit`, `upstream`, and `adopted.tree_sha256` equal to the copy's digest now); the inventory.
Then come the intake row and the receipt (`operation` `accept-merged`, `copy` the merged copy, `previous` the earlier commit and digests).
Prints `Recorded the merged copy of <name> against <c12>.`, followed by the lines adopt prints after its first.

### 6.7 `update <name> --detach [--yes] [--by] [--words] [--authority] [--execution-agent]`

Preconditions: steps 1 to 3 of adopt; an `external` entry (N12 to N16, N27). An entry already detached prints `Already detached: <name>; nothing to do.` and exits 0.
Shown: `Plan: detach <name>: later updates are not offered; status keeps reporting whether its source moved.` and the writes: the lock (`detached`, `detached_at`, `detached_by`) and the inventory.
Writes: the transaction, then the receipt (`operation` `detach`, `records.intake` null). No intake row is written, because nothing entered the project. Detached and adapted are independent states, and both are shown by status.
Prints `Detached <name>. Nothing was committed or pushed.`

**`update <name> --attach [--yes] [--by] [--words] [--authority] [--execution-agent]`** undoes a detach. Preconditions: as `--detach`; an entry not detached prints `Already attached: <name>; nothing to do.` and exits 0. Shown: `Plan: attach <name>: updates are offered again from <repository> <path>.` and the writes: the lock (`detached` false, `detached_at` and `detached_by` removed) and the inventory. Writes: the transaction, then the receipt (`operation` `attach`, `records.intake` null). Prints `Attached <name>. Nothing was committed or pushed.` The copy is not touched, so an adapted copy stays adapted and its next `update` prints the three versions as 6.5 says.

### 6.8 `retire <name> [--drop-base] [--yes] [--by] [--words] [--authority] [--execution-agent]`

Preconditions: steps 1 to 3 of adopt. The name is an `external` or `outside` entry (N12; N13r for a kit skill; N14 for the project's own). An entry already retired prints `Already retired: <name>; nothing to do.` and exits 0.
For an external entry, `git status --porcelain=v1 -z --untracked-files=all -- .skills/<name> <snapshot>` must print nothing; otherwise N22 names the paths. Agent copies are checked for separate edits (N23).
Shown: `Plan: retire <name>` and the writes:
- the removal of every copy file (the latest receipt's `copy.files`, plus any other file under `.skills/<name>/` Git does not ignore);
- the removal of its agent copies and their entries in `install.json`;
- with `--drop-base`, the removal of the snapshot's files that Git does not ignore (an ignored, uncommitted file is left in place, because Git history could not restore it; AH-235);
- the lock entry, which becomes `retired`;
- the inventory.
A further line reads `Git history keeps every removed file once the removal is committed; files Git ignores under .skills/<name>/ are left in place: <paths or none>`, and with `--drop-base` the same line names both folders: `files Git ignores under .skills/<name>/ and <snapshot>/ are left in place: <paths or none>`.
Writes: the transaction, then empty-folder pruning (12.4), then the receipt (`operation` `retire`, with its `history` line). No intake row and no transition row are written (14, Q10).
Prints `Retired <name>.`, then `Original kept at <snapshot>.` or `Original dropped; Git history keeps it.`, then `Nothing was committed or pushed.`

### 6.9 `declare <name> --held-by NAME [--path PATH] [--limitation TEXT] [--beside-project-skill] [--yes] [--by] [--words] [--authority] [--execution-agent]`

Records a skill a person runs from their own setup on this project. Nothing is copied.
Preconditions: steps 1 to 3 of adopt; `<name>` valid (N19). The name must be free (section 7): an active external entry, a kit skill, a project folder or a single file is N28 with its owner, and another holder's declaration is N4.
With `--beside-project-skill`, a project folder or single file of that name is not N28: the declaration is recorded beside it, for a project skill that holds rules of its own and relies on a personal skill of the same name (AH-260). The flag without such a project skill is N29. The project's skill is not read or changed, the entry carries `beside_project: true`, the receipt's `ownership.before` is `project`, and the plan adds, after the limitation, `  beside: the project's own <.skills/<name>/ or .skills/<name>.md> stays as it is and relies on this personal skill; nothing of it is read or changed`.
An identical existing declaration prints `Already declared: <name>; nothing to do.` and exits 0.
With `--path`, the folder must hold a `SKILL.md` (N21). It is read under 2.2 and 2.4 (P), its front matter name must be `<name>` (N20), and its digest is computed as `attested`.
Shown: `Plan: declare <name> as held outside the project by <held_by>`, the path as it will be recorded, the digest or `digest not stated`, the limitation, and the writes: the lock and the inventory.
Writes: the transaction, then the receipt (`operation` `declare`). No intake row (nothing entered), no agent copies.
Prints `Declared <name>, held by <held_by>. Nothing was committed or pushed.`

### 6.10 `link <receipt> --commit SHA`

Records the default-adoption transition for one receipt, after the person has committed the operation.
Reads: the receipt, the commit's tree, and `sources/transitions.jsonl`. Takes the setup lock and writes only through `trace_record.py transition`. It asks for no confirmation, because it records a commit that already exists.
Preconditions, in order:
1. E1, E2, E7, E11.
2. `<receipt>` is a file under `.trace-os/skill-adoptions/` with `kind` `trace-skills-receipt` and `schema_version` 1 (L1), and its operation is adopt, update or accept-merged (L2).
3. `git rev-parse --verify <SHA>^{commit}` resolves to a full commit id (L3).
4. `git show <commit>:<receipt>` equals the receipt's bytes (L4).
5. `git show <commit>:.skills/<name>/SKILL.md` exists (L5); its SHA-256 is the digest recorded.
6. `sources/transitions.jsonl` holds no row with `kind` `default-adoption` and `checks` equal to the receipt path. When such a row covers this commit, the command prints `Already linked: <TR id> (sources/transitions.jsonl); nothing written.` and exits 0. When it covers other commits, the result is L6.
Runs the command of 10.2 (a non-zero exit is E13, since nothing was written), then prints `Linked <receipt> to <c12>: <TR id> in sources/transitions.jsonl.` When the committed SKILL.md's digest differs from the receipt's `copy.files` entry for `SKILL.md`, it adds `The committed .skills/<name>/SKILL.md differs from the one this receipt wrote; the row records the committed file.`
For a receipt whose `copy.kept_existing` is true, it adds, before that line, `The receipt kept the project's existing .skills/<name>/; the row says so.`, or, when the commit did not change the SKILL.md (10.2), `The receipt kept the project's existing .skills/<name>/; the row says so, and that <c12> did not change its SKILL.md.` (AH-261).

### 6.11 `recover`

Section 12.5. Takes the setup lock; asks for no confirmation. Prints one of:
- `Nothing to recover.`
- `Undone: <operation id>; the project is as it was before. Run the command again to <operation>.`
- `Cleared: nothing of <operation id> had been written.`
- `Completed: <operation id>; receipt <path>.`
Exit 0 for these; 1 for R1 and R2; 2 for R3 and for E13 or E17 met while completing.

## 7. Ownership and names

### 7.1 Owners

| owner | how it is read |
| --- | --- |
| `kit` | the install state lists a file `.skills/<name>/...` with `policy` `managed` and no `canonical` key. Only a release writes such an entry (`bootstrap.dropped_files`). A generated agent copy carries `canonical` whatever its source, so `canonical` never decides ownership |
| `external` | the lock's `external` entry |
| `outside` | the lock's `outside` entry |
| `project` | a file under `.skills/<name>/` that is not litter and not ignored by Git (the rule of `bootstrap.skill_files`), or a single file `.skills/<name>.md`, with none of the above |
| `retired` | the lock's `retired` entry; it does not hold the name |

Two claims on one name (for example a kit entry and an external entry, or `.skills/<name>.md` beside an adopted `.skills/<name>/`) are reported by status as `owner: conflict (<what claims it>)`, and every writing command on that name is refused with the matching N message.
The one pair that is not a conflict is an `outside` entry with `beside_project: true` and the project's own folder or single file of that name (6.9, AH-260): status reports the project's skill and the personal skill it relies on.

### 7.2 Without an install state

A project with no `.trace-os/install.json`, such as the kit's development repository itself, has no kit skills. Every skill there is `project` unless the lock says otherwise, and `--replace-kit-copy` is always N9.
No agent copy is written, and the receipt's `agent_copies.note` says `no adapter state here`. In the development repository, `.claude/skills` is a link to `.skills`, which serves Claude Code. That says nothing about Codex or Cline discovery, which the pilot rehearses.

### 7.3 `retired_skills`

`retired_skills` in the install state only stops the kit's updater from installing a shipped name again (`bootstrap.retired_skills`). It says nothing about who owns a later copy under that name.
A name in `retired_skills` with no files on disk is free for `adopt`. The plan then adds the line `the kit's updater keeps <name> retired; this adoption does not change that`.
The tool adds to `retired_skills` only through `--replace-kit-copy`, and never removes a name from it.

### 7.4 One active name, and its two exceptions

Every name has at most one active owner. `adopt` refuses a name that is taken (N2 to N8), with two explicit exceptions.

**`--replace-kit-copy`** transfers a kit-owned name to the adopted package, in one transaction:
- every kit file of the name (install-state entries `.skills/<name>/...` with `policy` `managed` and no `canonical`) must be unedited, its current hash equal to the recorded `sha256`, or N6;
- those files and their install-state entries are removed;
- their generated agent copies and entries are removed, with N23 for an edited copy;
- the name is added to `retired_skills` (sorted, unique);
- the package's copy and its agent copies are written (a path both remove and write is written once, with the new bytes);
- the entry records `replaced_kit_copy: true`, and the receipt lists `kit_files_removed` and `retired_skills_added`.
A later kit update then leaves the name alone, because `bootstrap.plan` drops retired names from what it installs, and the external skill's agent copies stay recorded (`obsolete_preserved`).
The kit's other files for the skill stay where the kit ships them, and the plan names them by a name match only: every install-state entry outside `.skills/` with `policy` `managed`, no `canonical`, and a path containing `<name>` (or `meeting_quotes`, for the kit's `meeting-and-idea-intake`), so it can miss a file or name an unrelated one (AH-255).

**`--keep-existing`** connects a project-owned folder to the package without touching it. The existing `.skills/<name>/` is read under 2.2 and 2.4 (P; its front matter name must be `<name>`, N20) and kept byte for byte. The snapshot, the lock entry (`adopted.kept_existing: true`, `adopted.tree_sha256` the existing copy's digest), the agent copies (made from the existing copy) and the records are written as for any adoption. This is how the development repository's own adapted `literature-processing` and `claim-evidence-audit` are connected to their library packages.

### 7.5 Single-file skills

An older project may keep a skill as `.skills/<name>.md`. Such a file holds the name `<name>` (N8 for adopt; `owner: project (single file .skills/<name>.md)` in status). The tool never writes, moves or removes one, and `--keep-existing` does not apply to it.

## 8. Audiences

**Values.** A declared audience is `private` (only its holder), `collaborators` (named people) or `public`. It comes from `--audience source=VALUE` for the source and `--audience project=VALUE` for the project, and `declared_by` is `--by`.
The project's declaration is kept at the top of the lock (`project_audience`) and used by later operations until a new one is given. A source declaration lives in its entry. When nothing is declared, `declared` is `not declared`.

**Observed values** come from `observe_audience(address)`, which uses `gh` when the address is on GitHub:
- recognized forms: `https://github.com/<owner>/<repo>[.git][/]`, `git@github.com:<owner>/<repo>[.git]` and `ssh://git@github.com/<owner>/<repo>[.git]`, where owner and repo match `[A-Za-z0-9_.-]+`;
- the call is `gh repo view <owner>/<repo> --json visibility --jq .visibility`, with a 30-second timeout and no other output kept;
- `PUBLIC` gives `public (gh, <date>)`; `PRIVATE` or `INTERNAL` gives `restricted (gh, <date>)`, where `<date>` is `YYYY-MM-DD`;
- otherwise one of: `not checked: a local repository`, `not checked: not a GitHub repository`, `not checked: gh is not installed`, `not checked: gh could not see the repository`.
The project's observed audience reads the address `git remote get-url origin` returns, or is `not checked: the project has no origin remote`.

**Comparison.** Each side becomes a set of possible ranks (`private` 0, `collaborators` 1, `public` 2). A declared value gives its own rank and takes precedence over an observed one. Observed `public` gives {2}, observed `restricted` gives {0, 1}, and anything else gives {0, 1, 2}. With S for the source and P for the project:

| condition | comparison, as printed and recorded | `--share-with-project` |
| --- | --- | --- |
| S is exactly {2} (the source is known to be public) | `source public; the copy widens nothing` | not required |
| max(S) < min(P) | `source narrower than the project` | required (A1) |
| otherwise | `source not known to be public: copying it here lets everyone who can read the project read it` | required (A1) |

So the flag is required whenever the source is not known to be public, whether the narrowing is known or only possible: that is the case Miros described (a private skill a shared project relies on), and his decision was that it is allowed and highlighted. A public source (declared or observed) needs no flag.
When the flag is given, the plan prints `Shared with the project: by <by>; words: <words, or "not stated">` (6.3), and the inventory's audience cell carries the `copied here with --share-with-project` suffix (3.2); without the flag, a source not known to be public is refused with A1 before the plan is applied.
The person decides. An agent passes `--share-with-project` only when the person has said so, and their words go in `--words`.
The receipt records both audiences, the comparison, `share_with_project` (`required` and `given`), and who decided with their words (`decided.by`, `decided.words`). The inventory's audience cell says it to co-authors (3.2).
`update` and `accept-merged` observe and compare again, under the same rule.

## 9. The agent copies

**Why the tool does not call `sync_skills`.** `bootstrap.sync_skills` takes the setup lock itself. A second `locked()` from the same process conflicts with the first and raises `another setup process owns this target`. It also runs its own transaction, refuses while a transaction or an update is pending, raises under `--agent none` when nothing is missing, and syncs every skill in the project, not just the one being operated on.
So the tool never invokes `sync_skills`. Inside its own transaction it applies the same per-file rules to the operated skill only. Section 14, Q1 proposes moving those rules into one shared function.

**The rules**, for an installed project whose `agent` is in `bootstrap.AGENTS`:
- Every canonical file `P` the operation leaves under `.skills/<name>/`, without litter and without files Git ignores, has the copy `A = AGENTS[agent] + '/' + P[len('.skills/'):]`. `A` is written with `P`'s bytes when its current hash differs, and `files[A]` in the install state becomes `{"sha256": <digest of P's bytes>, "policy": "managed", "canonical": P}`. The receipt lists `A` under `agent_copies.written` when the transaction writes it, and under `agent_copies.kept` when it already held those bytes, even if the forward step then changes its mode (AH-241).
- Before that, `A`'s current hash must be absent, the recorded `files[A].sha256`, or the new digest; otherwise N23. This is `sync_skills`'s `generated skill was edited separately` rule.
- A canonical file the operation removes (update, retire, `--replace-kit-copy`) has its copy removed, and its entry dropped, when the copy is absent or still holds the recorded hash; otherwise N23.
- The install state is written with `bootstrap.encoded`, inside the transaction, and only when its bytes change.
- The agent copy of a `100755` member is made executable too.
- The copies are written after the copy and the snapshot, and before the install state, the lock and the inventory (12.3).
- A symlinked agent root is refused by `bootstrap.confined` (E20).

**Under `--agent none`**, no copy is written and the install state changes only for `--replace-kit-copy`. The note reads `agent none: no copies; agents read .skills/ directly`.
**Without an install state**, nothing is written, and the note reads `no adapter state here`.
**With an adapter**, the note reads `copies of the members Git does not ignore, as scripts/bootstrap.py --sync-skills makes them`.

**What the parity check sees afterwards.** For a project that was in parity before the operation:
- `check_skills.check(project)` reports no problem;
- `bootstrap.sync_skills(project)` run straight after is a no-op (`synced` 0, `removed` and `retired` empty);
- after a later kit update, the adopted skill's copy entries survive, since the release does not ship them, and the update's postflight check passes.
The scenario tests assert these as the equivalence criterion. Under `--agent none`, only `check_skills` is asserted, because `sync_skills` raises there by design.

## 10. Records

### 10.1 The intake row (adopt, update, accept-merged)

After the transaction and before the receipt, `run_trace_record(project, args)` runs `[sys.executable, 'scripts/trace_record.py', 'intake', ...]` with `cwd` the project root, the caller's environment unchanged, and output captured. The tool prints none of that output.
The arguments, in this order:

```text
--what      "skill package <name> (<repository> <path>) at commit <commit>, package digest <tree_sha256>"
--origin    "<repository> <path> at commit <commit>, requested as <requested_ref>"
--by        "<decided.by>"
--checked   "<checked>"
--used-for  "<--used-for, or: a standing instruction for the agents in this project, at .skills/<name>/>"
--path      ".trace-os/skill-adoptions/<operation id>.json"
--authority "<decided.authority>"
```

`<checked>` for adopt is:
`the whole package (<n> files, <bytes> bytes) was printed before writing and the person confirmed it (<--yes or at the terminal>); every file's SHA-256 and the package digest were re-checked just before writing; definition 1: <format>; <licence>; <handoff>; audience: <comparison>`.
With `--keep-existing`, it opens with `the project's existing copy was kept and connected to the package (differences from the package: <the difference lines of 6.3 joined by ", ", or none>); ` before that text (AH-254).
For update, the opening clause is `the whole new package (<n> files, <bytes> bytes) and its difference from <old c12> were printed before writing and the person confirmed it (...)`, and the rest is as for adopt.
For accept-merged the opening clause says what this run and its predecessor did, never more (AH-231): with a `--candidate` whose `created_by` is `update` and whose `for.operation` is `accept-merged`, it is `the three versions were printed by update on <candidate created_at>`; with `--ref COMMIT`, or a candidate written by `show`, it is `no update run preceded this; the new upstream's manifest and its difference from the merged copy were printed by this command`. The rest is:
`; the merged copy, merged by <decided.merged_by> under <decided.by>'s direction, is recorded as it stands with digest <adopted d12>; the upstream original's files were re-checked against commit <c12>; definition 1 of the merged copy: <format>; <licence>; <handoff>`.
Without `--merged-by`, the clause from `the merged copy` to the digest reads `the merged copy is recorded as it stands with digest <adopted d12> (who merged it was not stated)`, because the merger is named only by `--merged-by` and never by `--by` or `--execution-agent`.
With `--candidate`, "printed before writing" reads "printed by <the candidate's created_by> before writing" (show, or an earlier adopt or update that ended without applying); for an update whose candidate came from show or adopt, which print no difference, the clause reads "was printed by <created_by>, and its difference from <old c12> by update, before writing".

The receipt does not exist yet when the row is written, so `trace_record.py` records the path as given, with no digest. The receipt then names the row by its id and the SHA-256 of its line.
Idempotence: before running the command, the tool looks in `sources/intake.jsonl` for a row whose `object` equals the receipt path. When one is there (an operation completed by `recover`), it takes that row's id and does not run the command again.
A non-zero exit, or no such row afterwards, is E17 with step `the intake row`.

### 10.2 The transition row (link)

`run_trace_record(project, args)` runs `[sys.executable, 'scripts/trace_record.py', 'transition', ...]` with these arguments:

```text
--kind        default-adoption
--object      .skills/<name>/SKILL.md
--digest      <SHA-256 of .skills/<name>/SKILL.md in the named commit>
--checks      .trace-os/skill-adoptions/<operation id>.json
--by          "<receipt decided.by>"
--authority   "<receipt decided.authority>"
--decision    adopt
--destination ".skills/<name>/ (a standing instruction for the agents in this project)"
--commits     <full commit id>
```

When the receipt's `copy.kept_existing` is true, the project's own copy was kept and the package did not replace it, so the destination reads `.skills/<name>/ (the project's existing copy, kept and connected to the package, which did not replace it)`, and, when the named commit's SKILL.md has the same bytes as its first parent's, it ends `; this commit did not change its SKILL.md, so no instruction changed)` instead (AH-261).

No `--later-run` is passed: the later run does not exist yet, and is observed later with `trace_record.py later-run`.
The object and digest are the SKILL.md file, because later-run discovery (`later_run_candidates` in `trace_record.py`) matches a row's object path and `object_sha256` against the `.skills/**/*.md` digests that `extract` records in each later record's evidence. A receipt's own digest would match nothing.
The TR id is read back from `sources/transitions.jsonl`, as the row whose `checks` equals the receipt path.
`link` is idempotent per receipt (6.10, step 6).
A method run's check row still records the skill's version as `procedure_version` (`sha256:` and the SKILL.md digest), as `ai/SKILL-RUN-CONTRACT.md` says. The tool changes nothing there.

## 11. Status

### 11.1 Vocabulary

| line | values |
| --- | --- |
| `owner:` | `kit`; `project (.skills/<name>/)`; `project (single file .skills/<name>.md)`; `external, from <repository> <path> at <c12> (requested <ref>)`; `outside, held by <held_by>; digest <d12> (attested)` or `outside, held by <held_by>; digest not stated`; `conflict (<what claims it>)`; `pending (operation <operation id> did not finish; run <tool> recover)` |
| `copy:` | `unchanged`; `adapted (<n> file(s) differ from the upstream original: <up to five paths, then "and <k> more">)`; `missing (.skills/<name>/SKILL.md is not here)`. Without the receipt's manifest, the list reads `(the files cannot be listed: the receipt is missing)` |
| `detached:` | `no`; `yes` |
| `upstream:` | `current`; `current (the source moved to <c12>; the package is unchanged)`; `behind <c12>`; `unknown (<reason>)` |
| `change notice:` | `present`; `absent`; `not needed (copy unchanged)` |
| `definition 1:` | the three states (2.5), or `not applicable (a single-file skill, not a package)` |
| `shadowed:` | `no`; `by a personal skill at ~/.claude/skills/<name>; Claude Code 2.1.280 was observed to load the personal copy instead of the project's (the skills pilot, 2026-10-01); other clients and later versions were not rehearsed`; `by a personal skill at ~/.codex/skills/<name>; Codex 0.154.0 was observed to list personal skills only (the skills pilot, 2026-10-01); other clients and later versions were not rehearsed`; `by a personal skill at ~/<root>/<name>; no client was rehearsed for this folder` for `.agents/skills` and `.cline/skills` |
| `relies on:` | `a personal skill of the same name, held by <held_by>; digest <d12> (attested)` or `...; digest not stated` (AH-260) |
| `receipt:` | `<path>`; `<path> (missing)` |

`upstream` compares `source.commit` with what `requested_ref` resolves to now. When the commits differ, the package digest at the new commit decides between `behind` and `current (the source moved ...)`.
The unknown reasons are: `not fetched (--offline)`; `fetch failed: <first line of Git's error>` (which is all the tool can say about being offline, revoked access or a deleted repository); `the source has no branch or tag named <ref>`; `the package folder <path> is gone at <c12>`; `pinned to a commit; update --ref <ref> compares with a branch or tag`.
A detached entry gets the same `upstream` line, and no update is offered.

A change notice is present when the copy's SKILL.md front matter has a `metadata` mapping with a non-empty `changes` value (14, Q7). The tool never writes one.

### 11.2 Output format

```text
Skills in <project folder name> (trace_skills.py status, contract 1)
Install state: <version>, agent <agent>
Lock: .trace-os/skills.lock.json, <n> entries
Pending: operation <operation id> did not finish; run <tool> recover        (only when the journal exists)

<name>
  owner: ...
  <the lines for this owner, in the order of the table above>

Retired:
  <name>: was adopted from <repository> <path> at <c12>; original <kept at <snapshot> | dropped>; receipt <path>
  <name>: was declared as held by <held_by>; receipt <path>
Limit: definition 1 reads the skill's text; it does not establish that a method is valid, that its scripts are safe, or that any agent discovers the skill.
Limit: file parity between .skills/ and the agent copies is checked by scripts/check_skills.py, not here.
```

With no install state, line 2 reads `Install state: none here (every skill is the project's own unless the lock says otherwise; no agent copies are managed)`. With no lock, line 3 reads `Lock: none`. With no retired entry, the retired block is the single line `Retired: none`.
Skill blocks are sorted by name, separated by one blank line, and each line under a name is indented two spaces.
The lines per owner:
- kit: `owner`, `definition 1`, `shadowed`;
- project: `owner`, `definition 1`, `shadowed`;
- external: `owner`, `copy`, `detached`, `upstream`, `change notice`, `definition 1`, `shadowed`, `receipt`;
- outside: `owner`, then `limitation: <limitation>`, `receipt`;
- project with a `beside_project` declaration: the project lines, then `relies on`, `limitation: <limitation>`, `receipt`;
- conflict: `owner` only;
- pending: `owner` only.
`definition 1` for kit, project and external skills is read from the copy on disk.
While the operation journal exists and names its operation id and a valid skill name, that name is reported as `owner: pending (...)`, whatever its folder or its lock entry holds, and it is left out of the retired block: the operation may have written part of the folder, so nothing of it is read (AH-247). The name is listed even when nothing of it is on disk yet. A journal that cannot be read gives only the `Pending:` line, naming the journal's path.

### 11.3 Shadowing

For each active name and each root in `bootstrap.AGENTS` (`.claude/skills`, `.agents/skills`, `.cline/skills`) plus `.codex/skills` (where the Codex CLI keeps personal skills on the machine this was written on), when `Path.home() / root / name` exists, one `shadowed:` line names that folder, in this order, so a name held in several personal folders gets several lines (AH-257). This is the check `bootstrap.apply` already makes for its receipt's `global_skill_names_present`. The contents are not read.
Each line states what the skills pilot observed on 2026-10-01 for the folder it names (Part 3 of `sources/coding/skills-library-build-20261001/pilot-trace-kit.md` in the kit's repository; AH-237, AH-257): with a personal and a project copy of one name, Claude Code 2.1.280's Skill tool loaded the personal copy under `~/.claude/skills`, and Codex 0.154.0's own skill listing held only the personal skills under `~/.codex/skills`, none of a project installed with the Claude adapter. No client was rehearsed for `~/.agents/skills` or `~/.cline/skills`, other clients (Cline among them) and later versions were not rehearsed, and the lines say so. The same personal folder may be a wrapper that sends the agent back to the project's copy, as `~/.codex/skills/literature-processing` does in `trace-kit`; the tool cannot tell, because it does not read the contents.
Tests set `HOME` to a temporary folder.

## 12. Transactions and recovery

### 12.1 What is reused from bootstrap.py

- `bootstrap.locked(project)` is the one setup lock (`.trace-os/setup.lock`), held by every writing command, `link` and `recover`, from the re-check of preconditions to the end of the forward steps. `status`, `list` and `show` take no lock. A busy lock is E6.
- `bootstrap.transaction(project, updates, expected)` applies the file writes: `updates` maps each path to bytes, or to None for a removal, in the order of 12.3; `expected` maps each path to its plan-time hash, or None for absent. It backs up every existing file under `.trace-os/backups/<run id>/`, writes the undo journal `.trace-os/transaction.json` (`{"schema_version": 1, "entries": {path: {"before", "after", "backup"}}}`), then writes each path atomically. Before each write it checks that the file still holds its `before` hash. On an `Exception` it restores everything from the backups (`bootstrap.recover`) and raises, and on success it removes the journal.
- `bootstrap.recover(project)` undoes an interrupted transaction from that journal. It refuses when a file holds neither its `before` nor its `after` hash (`recovery would overwrite a later edit`).
- `bootstrap.write_atomic` writes the receipt and the operation journal.
- `bootstrap.interruptible()` is open for the whole apply, so SIGTERM and SIGHUP raise `KeyboardInterrupt` as Ctrl-C does.

What `expected` protects: an edit made between the plan the person saw and the write. Under the lock, the tool compares every path's current hash with the plan's first and stops with E14, before the journal exists. The transaction compares again just before each write and undoes everything on a mismatch (E15). A person's edit is therefore never overwritten and left only in a backup.
A path whose current bytes already equal its new bytes is left out of `updates`, so every journal entry changes something.

### 12.2 The operation journal

`bootstrap.transaction`'s journal records file bytes only. The tool needs more to finish an operation: the modes, the intake row and the receipt. It keeps them in `.trace-os/receipts/skill-operation.json`, written with `write_atomic` before the transaction starts (a folder every install already ignores, so no shipped ignore rule changes for it):

```json
{
  "schema_version": 1,
  "kind": "trace-skills-operation",
  "operation_id": "20261001T120000Z-literature-processing-adopt",
  "operation": "adopt",
  "name": "literature-processing",
  "state": "applying",
  "started_at": "2026-10-01T12:00:00Z",
  "argv": ["adopt", "..."],
  "writes": {"<path>": {"before": "<64 hex or null>", "after": "<64 hex or null>"}},
  "modes_after": {"<path>": "100755", "<other path>": "100644"},
  "modes_before": {"<path>": "100644", "<other path>": "100755"},
  "prune": ["<removed path>"],
  "intake": {"args": ["--what", "...", "--path", ".trace-os/skill-adoptions/<id>.json"]},
  "receipt_path": ".trace-os/skill-adoptions/<operation id>.json",
  "receipt": {"...": "the receipt of 5.1 without completed_at and records.intake"},
  "staging": "/absolute/tmp/trace-skills-abc123"
}
```

`state` is `applying` until the transaction has completed, then `applied`. `modes_after` lists every path whose mode after the operation differs from its mode now, with the target mode, in both directions: a member that becomes `100755`, and one that becomes `100644` while its bytes stay the same (such a path is not in the transaction at all, because its bytes do not change, so the mode is the only thing to apply; AH-230). The agent copies of those members are listed the same way. `modes_before` lists the current mode of every path in `modes_after` and of every path the transaction replaces or removes that is `100755` now, so an undo can restore them, since `bootstrap.recover` writes restored files with the default mode. `intake` is null for detach, retire and declare. The journal is removed as the operation's very last step.

### 12.3 The order of an operation

1. Plan without the lock: fetch or read the candidate, validate, compute every write and its plan-time hash, show, confirm (6.0).
2. Take the lock. Check E7, E8 and E9, then every precondition again, then E14 over `expected`, then C5 over the staged bytes. Pick the final operation id (4.5).
3. Write the operation journal, with `state` `applying`.
4. `bootstrap.transaction`, with `updates` in this order:
   1. the copy: `.skills/<name>/` writes and removals, sorted by path;
   2. the original: `.trace-os/skill-bases/README.md` when the operation writes it (3.1), then the new snapshot's files under `.trace-os/skill-bases/<tree_sha256>/`, sorted by path;
   3. agent copies: writes and removals, sorted by path;
   4. kit files removed by `--replace-kit-copy`, and old-snapshot files removed by update, accept-merged or `retire --drop-base`, sorted by path;
   5. `.trace-os/install.json`;
   6. `.trace-os/skills.lock.json`;
   7. `ai/SKILLS-IN-USE.md`.
5. Rewrite the operation journal with `state` `applied`.
6. The forward steps (12.4).

The plan's list put the intake row before the agent copies. Here the intake row comes after the whole transaction, because `trace_record.py` appends to its registry outside any journal. It is the one step that cannot be undone, so it runs only once every file is in place.

### 12.4 The forward steps

Each step is idempotent, and `recover` runs the same steps.
1. Modes: for every path in `modes_after` that exists, set the owner-execute bit (`chmod 0o755`) when the target is `100755` and clear it (`chmod 0o644`) when the target is `100644`, so a mode-only upstream change reaches the copy and its agent copies (AH-230). Skipped on Windows, where the receipt's `modes` still records them.
2. Pruning: empty folders left by removals are removed with `bootstrap.prune_empty_skill_dirs` for `.skills/` and the agent roots, and, for the snapshots, upward from each removed path to `.trace-os/skill-bases/` (that folder itself is kept).
3. The intake row (10.1), when `intake` is not null.
4. The receipt: `completed_at`, `recovered` and `records.intake` are filled in and the receipt is written with `write_atomic`. When the receipt file already exists and carries this operation id, it is kept as it is.
5. The staging folder is removed when the operation used one and does not keep it (3.3).
6. The operation journal is removed.
A failure in steps 1 to 5 leaves the journal in place, `applied`, and gives E17.

### 12.5 Interruption and `recover`

**During a run.** An `Exception` inside the transaction is undone by the transaction itself. The tool then restores `modes_before`, removes its operation journal, and reports by the error: a `SetupError` beginning `file changed since the setup plan: ` is E14 (nothing had been written), one beginning `file changed while applying setup: ` is E15, and any other is E13. `bootstrap.recover` refuses to restore a file that holds neither its before nor its after hash, which is exactly the edited file, and then restores nothing; so the tool undoes the rest itself from the transaction's backups and leaves the edited file as the person left it, which is what E15 says. A failure of that undo is reported as R2 or E17, never as E13, which would claim an undo that did not happen. An interruption (Ctrl-C, SIGTERM or SIGHUP) is not undone on the spot: the tool prints E16 and exits 128 plus the signal number, and the journals stay for `recover`. A process killed outright leaves the same state. The tool never runs a second write sequence while a signal is arriving.

**Every writing command** refuses while either journal exists (E7, E8), and while a kit update is pending (E9).
`bootstrap.py --apply`, `--update`, `--rollback` and `--recover` undo a pending `transaction.json` themselves. The tool's recovery accepts that outcome (case 3b below).

**`recover`**, under the lock:
1. A pending kit update (`.trace-os/pending-update.json`) gives E9.
2. No operation journal: a `transaction.json` present gives R1 (it is not the tool's); otherwise `Nothing to recover.`. An unreadable journal is R3.
3. Journal `applying`:
   1. `transaction.json` present. When every path in its `entries` is a key of the journal's `writes`, it is the tool's: `bootstrap.recover` undoes it, every path in `modes_before` that exists gets that mode back, the staging folder and the journal are removed, and the result is `Undone`. Otherwise R1.
   2. No `transaction.json`: compare every path in `writes` now. All at `before`: the journal is removed, and the result is `Cleared`. All at `after`: the transaction completed before its state was recorded, so go to step 4. Anything else: R2, naming the first such path in path order.
4. Journal `applied`: run the forward steps (12.4) with `recovered: true`. The result is `Completed`.

So an operation interrupted before or during its file writes is undone, and one interrupted after them is completed. Either way the next run leaves no half-state.
A re-run is idempotent. After an undo, the same command plans again with its own stamp (4.5). After completion, the same adopt, update, detach, retire, declare or link reports `Already ...` or `Current ...` and exits 0.
A receipt exists only for an operation whose files and intake row are all in place, because it is written after both.

### 12.6 Known interaction with the kit's rollback

`bootstrap.py --rollback` refuses when a file the update wrote changed afterwards. An adoption made after a kit update changes `.trace-os/install.json` whenever it writes agent copies, so that update's rollback then refuses with `rollback would overwrite a later edit: .trace-os/install.json`.
An adoption made before the update is part of the state the update backed up, and rolls back cleanly. The scenario "kit update over adopted skills, then its rollback" tests that order (14, Q4).

## 13. Test fixtures

Everything runs on temporary folders, with stdlib `unittest`, no network and no credential. Tests call `trace_skills.main(argv)` with `--project` and capture stdout and stderr as `test_kit_projects.py`'s `run_main` does.

**Environment.** A `mock.patch.dict(os.environ, ...)` holding:
- the `ENVIRONMENT` of `test_kit_projects.py` (author and committer names, and `commit.gpgsign=false` through `GIT_CONFIG_COUNT`);
- `HOME` set to a temporary folder, for the shadowing check and so that no personal Git configuration applies;
- `GIT_CONFIG_NOSYSTEM=1`;
- `GIT_ALLOW_PROTOCOL=file`, so an accidental network address fails instead of fetching;
- `GIT_TERMINAL_PROMPT=0`.
`trace_skills.observe_audience` is patched to return `not checked: test`, except in the audience tests, which return the observed strings of section 8. Credential tests plant a fake token (`GH_TOKEN=ghp_FAKE0000...`) and a URL with userinfo, then assert that no lock, receipt, candidate or operation journal contains the token, the `@` userinfo, or the value of `HOME` (a temporary path such as `TMPDIR` appears in a staged path by design, and `--by` may equal a Git author name, so the assertion is over planted secrets, not over every environment value).

**A source.** `make_source(root, files, branch='main')`:
- `git init -q --bare -b main src.git`, so `HEAD` names the branch the fixture pushes. With `git init --bare` and no `-b`, `ls-remote --symref HEAD` prints nothing;
- `git init -q -b main work`; write the members; `git -C work add -A`; commit; `git -C work push -q <src.git> main`;
- return the bare repository's absolute path and the commit id.
Special entries are made without the file system, so they work on every platform: `git -C work hash-object -w --stdin` for the blob, then `git -C work update-index --add --cacheinfo <mode>,<oid>,<path>`, with mode `120000` for a symlink, `160000` with any 40-hex id for a submodule, and `100755` for an executable script. A member named `a\b` is made the same way.
A tag is `git -C work tag v1` pushed with `--tags`. A second commit on `main` gives an update; a commit that changes only `scripts/run.py` gives the script-only update.
The package of the 4.6 test vector should be one fixture, so the digest is asserted against a fixed value.

**An installed project.** The release pattern of `test_kit_projects.py`'s `Kit` (or `test_bootstrap.py`'s `release()`): a release folder whose manifest lists a contents dict, then `bs.apply(release, project, 'academic', agent)`, `git add -A`, and a commit. The contents include:
- `.gitignore`: the `IGNORED` lines of `test_kit_projects.py` (they ignore `.trace-os/receipts/`, where the operation journal lives);
- `.skills/example/SKILL.md` as a kit-shipped skill, `managed`, with front matter `name: example` and a description;
- `ai/SKILLS-IN-USE.md` as a `seed`, holding a `<!-- trace-os:begin skill-routing -->` to `<!-- trace-os:end skill-routing -->` block, so placement outside it is tested.
After applying, the fixture copies `trace_record.py`, `trace_story.py`, `input_provenance.py`, `trace_defaults.py`, `record_privacy.py` (which `trace_story.add_commands` imports whenever `trace_record.py` starts) and `check_skill_handoff.py` into the project's `scripts/`, and `trace_skills.py` with `bootstrap.py` and `check_skills.py`. In an exported project these are found in `tests/../scripts/`; in the kit's source tree, in `starter/files/scripts/` and in the repository's `scripts/`. A helper `kit_script(name)` tries both.
It then sets `git config user.name` and `user.email` in the project.
Make one fixture for each of the agents `claude` and `none`. A `.gitattributes` holding `* text=auto eol=lf` is added only in the line-ending test (E19), with a CRLF member in the source.

**An uninstalled project** (like the kit's development repository):
- `git init -q -b main`, a user name and email, `README.md`;
- `.skills/own/SKILL.md` (the project's own), `.skills/legacy.md` (a single-file skill) and an adapted `.skills/<library name>/` for `--keep-existing`;
- the same `scripts/` modules, and a `.gitignore` with the `.trace-os/` lines;
- one commit.
On POSIX only, a variant adds `.claude/skills` as a symlink to `../.skills`, mirroring the development repository; the tool writes no agent copy there (7.2).

**Interruptions.** Patch `trace_skills.bootstrap.write_atomic` with a wrapper that raises `KeyboardInterrupt` when its path is, in turn: the first path under `.skills/<name>/` (before the copy); the first under `.trace-os/skill-bases/` (after the copy); `.trace-os/skills.lock.json` (before the lock); and the receipt (before the receipt). Patch `trace_skills.run_trace_record` to raise, or to return 1, for an intake that fails.
Then call `main(['recover', ...])` and assert, for each boundary: `Undone` with every file back at its plan-time bytes and modes, or `Completed` with every file at its new bytes, one intake row and one receipt; no journal left; and `check_skills.check` reporting no problem.
A second `recover` prints `Nothing to recover.`

**Assertions common to the scenarios.**
- File bytes and modes.
- The lock and receipt fields against sections 4 and 5.
- Rows in `sources/intake.jsonl` and `sources/transitions.jsonl`, checked with `trace_record.validate_intake` and `validate_transitions`.
- `later_run_candidates` finding a record whose fixture `evidence.json` lists `.skills/<name>/SKILL.md` with the linked digest.
- The equivalence criterion of section 9.
- Exit codes and the catalogue's messages, matched exactly.
- That the package's files are never executed: the fixture package holds a script that would write a marker file if run, and the test asserts the marker never appears.

## 14. Open questions, and the coordinator's answers

Each question is followed by its answer, both as written on 2026-10-01, when the kit was called "TRACE kit" (`trace-kit`) and its state folder `.trace-kit/`; read them as TRACE-OS, `.trace-os/` and the development repository from 0.1.0-rc20 (TRACEware and `.traceware/` in 0.1.0-rc19). The answers were given on 2026-10-01 within the authority of Miros's "agreed with all" on `ai/skills-library-plan.md`; the record `ai/records/20261001-skills-library-build` says who gave them and what was revised on the way.

- A1. No change to `bootstrap.py` in rc18: the tool carries the per-file rule for one skill (section 9), and the scenario tests hold it to the equivalence criterion (`sync_skills` a no-op afterwards, `check_skills` clean). Extracting one shared function is noted for a later release.
- A2. The journal lives at `.trace-kit/receipts/skill-operation.json`, under a folder every install's `.gitignore` already ignores and where bootstrap writes only its own named receipts, so no shipped ignore rule changes for it. The reason: `.gitignore` is a managed file most installed projects have edited, and a release that changed it would stop their update at a conflict. `kit_projects.py` and `bootstrap.py --plan` do not report a pending skills operation in rc18; `SETUP-AGENT.md` states it as a limit.
- A3. The shipped `.gitattributes` is unchanged, for the same reason (it is managed and edited in projects). E19 is the gate: a package Git would rewrite is refused before anything is written, so the originals under `.trace-kit/skill-bases/` are only ever bytes Git leaves alone.
- A4. Accepted and documented in `SETUP-AGENT.md`: an adoption made after a kit update makes that update's rollback refuse; roll back first, adopt after.
- A5. Documented as a known conflict; the tool offers no step for it in rc18.
- A6. Changed (section 8): `--share-with-project` is required whenever the source is not known to be public. Miros's decision was "allowed, and highlighted", and a flag that records the person's words is how the kit records a decision, as `--yes` does; nothing is blocked, a decision is asked for.
- A7. `metadata.changes` confirmed.
- A8. `LICENSE` counts as well as `LICENSE.txt` (2.5).
- A9. Added: `update <name> --attach` (6.7), operation `attach`.
- A10. No new transition kind in rc18; `retire` writes no transition row, and the receipt and the inventory record it.
- A11. Accepted as a stated limit in `SETUP-AGENT.md`: later-run discovery sees the SKILL.md digest only.
- A12. Confirmed: the receipt is written last, the intake row names it by path, the receipt names the row by id and digest.
- A13. `.codex/skills` added to the shadow roots (11.3); the wording stays provisional until the pilot. The pilot has since settled it (11.3, AH-237).
- A14. No root copy; in `trace-kit` the tool runs as `python3 starter/files/scripts/trace_skills.py --project .`.
- A15. Notes only; definition 1 does not follow a stricter validator.
- A16. Accepted; the library's README says member names are ASCII.
- A17. Confirmed.
- A18. Confirmed: the tree holds the current original; Git history keeps the rest.
- A19. No move and no copy: the tests add the repository's `scripts/` to the path in the source tree, and an exported project has both in `scripts/`.

1. **Agent copies and `bootstrap.py`.** `sync_skills` cannot run inside the tool's transaction (section 9), so the contract has the tool repeat its per-file rules for one skill. That is a second implementation of one rule. Proposal: extract the rule into `bootstrap.py` as one function returning `(updates, expected, state)` for a given set of canonical files, used by `sync_skills` and the tool alike. That touches a file outside B's package. Who makes the change, and when?
2. **The operation journal and ignore rules.** `.trace-kit/skill-operation.json` must be ignored. The starter's `.gitignore` is a managed file, so E's package should add the line for rc18. `trace-kit`'s own `.gitignore` has no `.trace-kit/` lines at all, so the pilot there would leave `setup.lock`, `transaction.json`, `backups/` and the journal untracked and visible. Add them before Stage 4? Should `kit_projects.py` (`UNFINISHED`) and `bootstrap.py --plan` also report a pending skills operation?
3. **Line endings.** The starter's `.gitattributes` (`* text=auto eol=lf`) normalizes CRLF on commit, so the contract refuses a package holding a CRLF text file (E19), because its digest would not survive a clone. Should the starter ship `.trace-kit/skill-bases/** -text` (frozen originals) and perhaps `.skills/** -text`, as this repository's rule for checksum-frozen files asks?
4. **Rollback.** An adoption made after a kit update makes that update's rollback refuse (12.6). Is that acceptable, documented in `SETUP-AGENT.md`, or should `bootstrap.py`'s rollback carry agent-copy entries it did not write?
5. **A release that starts shipping an adopted name.** The updater then reports a conflict on `.skills/<name>/...`, and the only resolution today is adding the name to `retired_skills` by hand. Should the tool offer that step?
6. **Unknown audiences.** `--share-with-project` is required only when the narrowing is known. A GitHub-private source going into a GitHub-private project, the case Miros described, is `not compared` and gets only the Caution line unless both sides are declared. Is that the intended balance with "nothing forced"?
7. **The change notice.** The contract reads `metadata.changes` in the copy's front matter. Confirm the key or name another. Apache-2.0 section 4(b) asks for a notice in every modified file, which the tool does not check beyond SKILL.md.
8. **The licence check.** The plan names `LICENSE.txt` or `license:`; the architecture proposal used `LICENSE`. Should `LICENSE` also count in definition 1?
9. **Undoing a detach.** No command re-attaches a detached copy. Add `update <name> --attach`, or is retire and adopt enough?
10. **Retirement as a transition.** `TRANSITION_KINDS` has no kind for removing a standing instruction, so `retire` writes no transition row and `link` refuses its receipt (L2). Is a new kind, or a `default-adoption` row with another decision, wanted?
11. **Later-run discovery sees SKILL.md only.** A script-only update keeps the SKILL.md digest, so records made under the old and the new scripts look the same to `later_run_candidates`. Accept this as a stated limit, or extend `extract`'s instruction capture to package digests?
12. **The direction of binding.** The receipt is written last, so the intake row names it by path without a digest, and the receipt names the row by id and digest. Confirm, or write the receipt before the intake row and give up "receipt last".
13. **Personal skill locations.** The shadow check looks under `~/.claude/skills`, `~/.agents/skills` and `~/.cline/skills`. Codex's personal skills may live elsewhere (for example `~/.codex/skills`). The pilot should settle the roots along with the wording.
14. **Where the tool runs in `trace-kit`.** Its default project is the folder above its `scripts/`, so in `trace-kit` it must run as `python3 starter/files/scripts/trace_skills.py --project .`. Is a root `scripts/` copy wanted?
15. **Unknown front matter keys** are reported as notes and do not make the format invalid. The `skills-ref` validator may be stricter. Should definition 1 follow it?
16. **Portable names.** Member paths are limited to `A-Z a-z 0-9 . _ - /` (P3), which refuses non-ASCII file names. Acceptable for the library?
17. **Flags beyond the plan's table.** `--by`, `--words`, `--authority`, `--used-for`, `--execution-agent`, `--limitation`, `--offline` and `--project` are added, because the records need them (sections 5, 8 and 10). Confirm.
18. **Old originals.** `update` and `accept-merged` remove the previous snapshot, leaving it to Git history; `retire` keeps it unless `--drop-base`. Keep every past original instead?
19. **`check_skill_handoff` in the source tree.** The tool imports it from its own folder, but in the kit's source tree it lives in the repository's `scripts/`, not in `starter/files/scripts/`. The tests add that folder to `sys.path`. Should the module move, or ship a copy beside the tool?
