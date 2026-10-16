# Set up this project

Each candidate is published as a pre-release tagged `0.1.0-rcN` at <https://github.com/amrhs-zohrevand/trace-os/releases>, with two assets: the archive `trace-os-0.1.0-rcN.zip` and its digest sidecar `trace-os-0.1.0-rcN.zip.sha256`.
That repository's history begins at 0.1.0-rc24; the candidates before it, 0.1.0-rc1 to rc23, are withdrawn and not on its releases page.
TRACE-OS was called TRACEware in 0.1.0-rc19 and the TRACE kit (`trace-kit`) up to 0.1.0-rc18; an archive from then keeps the name it was published under, and "From a release before 0.1.0-rc20" under Update says what changes for a project installed then.
TRACE-OS is distributed under a perpetual, non-exclusive licence, as its paper is on arXiv: a researcher may install and use it in their own projects and share it with the people they work with, and making it public, a public project that holds it included, needs the authors' written permission. The full text is `LICENSE` at the archive's root, which setup does not copy into a project, because a project's own licence is its owner's choice.
A pre-release is not a public release; RELEASE-NOTICE.md states what this candidate was and was not tested on.
Read that notice and AGENTS.md.
Use a trusted copy of the selected release. The manifest detects changed files; it is not a signature proving who supplied the archive.
An adjacent checksum downloaded with an archive does not independently establish publisher identity.

## Requirements

Use Python 3.11 or later and Git on macOS, Linux or Windows. On Windows, `python` below may be replaced with `py -3.11` when the Python launcher selects the required version. On macOS, use `python3` when no `python` command is installed. WSL is optional rather than required.
The checks use the Python standard library. LaTeX and latexmk are optional until document builds. Paper builds use pdflatex; the presentation skeleton uses xelatex with fonts supplied by TeX.
Configure your own Git user.name and user.email before initializing a new project. Setup does not invent an author, install software, create service accounts, configure remotes, or push.

## Initialize

For a new project, run these from the unpacked starter. Replace the target directory and choose claude, codex, cline, or none. The default profile is academic; --profile core omits document files in a new target.
An adapter installs checked copies of the skills where that agent looks for them; none installs no copies, and the agent reads the canonical skills in .skills/ itself (see Skills and agent discovery).

An AI agent should add --execution-agent followed by its actual model ID, which `python scripts/trace_record.py session` reads off a running Claude Code or Codex session. A manual invocation defaults to the script identifier starter-bootstrap.

```bash
python scripts/bootstrap.py --target ../my-research --agent claude --plan
```

Read the plan before applying it. A conflict leaves existing files unchanged; review the conflicting paths before retrying.

```bash
python scripts/bootstrap.py --target ../my-research --agent claude --apply
```

For an in-place archive or GitHub-template setup, use --target . instead. A core-profile installation must continue to use --profile core on later invocations.
Keep the Git working copy outside actively synchronized cloud folders and bucket mounts.
Bulk inputs stay out of Git, in the project's shared folder: after setup, connect it as Shared folder below says.

Existing projects retain their Git history and index. Setup does not merge competing instruction files or overwrite an existing research registry.
A new directory gets one local baseline commit containing the starter files. Setup then writes this project's epochs into ai/project.json. Review that file and commit it with the setup changes, using the normal attribution rule. Existing staged work must remain separate.
Run the same apply command again to check repeatability; it must preserve your edits to seeded research files and the original epochs, and report an edited managed file as a conflict.

## Commit messages and the range report

There is no commit hook and setup changes no Git configuration. A commit a person types carries no trailer and needs no flag. A commit an agent executes carries `Execution-agent: <model-id>` because the instruction file says so; that instruction, not a hook, is what produces the trailer.
`python scripts/trace_record.py verify-range <range>`, run on your own machine (before a push, for example over `origin/main..HEAD`), reads each commit message in the range. It prints an AI co-authorship claim or a drifted trailer so it is on the record, counts commits with no trailer as ones a person typed, blocks nothing, and fails only on a range it cannot read.
The kit ships no GitHub workflow and runs no continuous integration: the checks run on each researcher's own machine, and whether they pass on a coauthor's machine is established by running `python scripts/check_workflow.py --out workflow-check.json` there.
The kit ships no shell script; every command is `python scripts/<name>.py`, on any platform. The receipt still records `executable`, null when there is nothing to mark.

## Check and report

From the initialized project root, run:

```bash
python scripts/check_workflow.py --out workflow-check.json
python scripts/trace_stats.py --report workflow-check.json --out TRACE-STATS.md
```

The workflow command runs the kit's test modules, the ones .trace-os/install.json lists, names any other test file under tests/ as not run, and compares the installed skill copies with .skills/.
To diagnose setup, run `python -X utf8 -I -S -m unittest discover -s tests -v` directly; that also runs the project's own test files. Render the Markdown even if the workflow check fails.
For the academic profile, run plain latexmk from paper/ and then from presentations/_template/. Inspect the resulting PDFs in each build/ directory.
Then connect the shared folder, as the next section says.
Return the location of .trace-os/receipts/latest.json, the check outcomes, skipped checks, the shared folder's state and remaining actions.
The receipt records setup actions and tool versions, not a claim that tests or agent discovery succeeded.

## Shared folder

Every project has one shared place for large and bulky material such as PDFs, datasets, recordings and exports, kept out of Git.
The person names it once. The agent does everything else, says in plain words what it did, and never asks the person to type a command, edit .gitignore or set a variable.

```bash
python scripts/shared_folder.py status
```

When status reports anything but connected or bucket copy, act on its `next` line.

### The default: a sync app folder

Recommend this route first: OneDrive or Google Drive, which most people already have through their university or a Google account, or Dropbox, SURFdrive or SURF Research Drive (both Nextcloud now).
The sync app on each computer keeps the folder in step; the project only links to it, and nothing is installed or paid for beyond the app.
Ask one question: "Where is the shared folder for this project's large files? It is a folder in OneDrive, Google Drive or another sync app on this computer."
If the person is unsure, run `find`, which lists the sync apps' folders in their documented places (on macOS under ~/Library/CloudStorage, on Windows the OneDrive folders and Google Drive's drive letter), show them what it found, and connect only the folder they confirm.
If the person has none yet, tell them to make one folder for the project in their sync app and share it there with their coauthors; the agent cannot share it, and the kit makes no account. With their agreement, `connect --create` can make the folder inside the sync app's folder, which they then share in the app.
Neither OneDrive nor Google Drive has a desktop app for Linux.
A coauthor on Linux in a team that keeps its files there makes the team's folder a local folder with rclone (`rclone mount`, or `rclone bisync` into a folder), then connects that folder as above.
Do not run `bucket set` for it: that records a bucket for the whole project, and the coauthors whose sync apps hold the same folder could then no longer connect it. This Linux path was not run here.

```bash
python scripts/shared_folder.py find
python scripts/shared_folder.py connect "<the folder the person confirmed>" --plan
python scripts/shared_folder.py connect "<the folder the person confirmed>"
```

Read the plan to the person in plain words before the second command.
connect makes workspace/shared (or shared_root in ai/project.json) a link to that folder, so every skill and script that reads workspace/shared reads the team's folder, and adds the link to .gitignore, because Git's rule for a folder does not match a link.
On Windows without Developer Mode the link is a directory junction, which needs no administrator rights; that path was not run on Windows.
If workspace/shared already holds files, they are in this copy only and nobody else sees them: ask the person before adding --move-existing. A name present in both places stops the move, and nothing moves; a name that appears in the shared folder during the move stops it there, and nothing is ever replaced.
connect refuses a relative path, a folder inside the project, and a project that itself sits inside the shared folder; say what to change rather than working around it.
Each coauthor connects their own copy on their own machine. The link and its path stay out of Git, because each person's path differs.
`disconnect` removes only the link; the shared folder and its files are untouched. When the folder moved or is not synced on this machine yet, status says broken, and `connect --replace` points the link at its new place.

OneDrive and Google Drive keep files online-only until they are opened: listing them downloads nothing, and reading one downloads it.
Before an analysis over many files, ask the person to right-click the project's folder and choose "Always keep on this device" (OneDrive) or "Available offline" (Google Drive); `check` names files still online-only, on Windows and macOS.

### For data too large for a sync app: a bucket

A bucket (Google Cloud Storage, SURF's Object Store, any S3-compatible store) holds objects, not a synced folder: no app keeps a copy on each computer, so the agent copies files between the bucket and workspace/shared with rclone.
Buckets are paid: Google Cloud Storage bills storage per GB per month and downloads per GB, needs a billing account even for its free tier, whose free storage applies only in some US regions; SURF's Object Store is contracted through the institution.
Suggest this route only when the data outgrows the sync app or the team already has a bucket.
It is the whole project's route: `bucket set` is committed in ai/project.json, and from then on every coauthor pulls and pushes instead of connecting a sync app's folder.
The person, or their IT department, creates the bucket and gives coauthors access in the provider's console; the agent cannot.
When rclone is not installed, status and bucket set say how to install it; ask the person before installing anything.
Then help each person run `rclone config` once, naming the remote the same on every machine (for example the project's name): type `google cloud storage` for Google Cloud Storage, `s3` with provider `Ceph` for SURF's Object Store. rclone keeps the credentials in its own configuration, outside the project; never copy them into the project.

```bash
python scripts/shared_folder.py bucket set <remote>:<bucket>/<folder> --note "what it is"
python scripts/shared_folder.py pull --plan
python scripts/shared_folder.py pull
python scripts/shared_folder.py push --plan
python scripts/shared_folder.py push
```

bucket set records the location in ai/project.json, which is committed so coauthors use the same bucket; it holds no credential.
pull and push use rclone copy with --update: nothing is deleted on either side, and a file that is newer at the destination is kept and reported, so ask the person which version is right.
Pull before work that reads the shared files, push after adding any, and read each plan to the person before a large transfer, since downloads from a bucket are billed.

### Where each file goes

| Where | What goes there |
| --- | --- |
| Git | What the project writes and reviews: notes, code, coding records, trace records, the manuscript and slides. |
| The shared folder (`workspace/shared/`) | Large or bulky material the team needs: PDFs, datasets, exports, recordings and transcripts the team may see. |
| The shared folder's `incoming/` | Outside material waiting for a decision: a delivered dataset, a provider's corrected version, a reviewer's files. It is not the project's data until an intake row admits it. |
| Kept local | Anything else that should stay out of Git on this machine: `python scripts/shared_folder.py keep-local "<pattern>"`. When Git already tracks files under the pattern, name them and ask before removing them from the index. |
| Neither | Credentials and raw private transcripts: outside the project, or in the ignored workspace/record-staging/. |

### Outside material waiting for a decision

Material that arrives from outside the project (a commercial dataset, a coauthor's delivery from a provider, a reviewer's files) goes first into `incoming/` inside the shared folder, kept apart from the data the analysis reads; `incoming_root` in ai/project.json names another folder below the project when the team keeps it elsewhere.
Check it there against what the work needs, then record the decision with `trace_record.py intake --path <the file> --decision admit|return|refuse`, naming who decided with `--by` and declaring their kind with `--person`, `--agent` or `--authorized-by`.
Move a file into the data only after an admitting row: the row's digest names it wherever it goes. A returned or refused version stays in `incoming/` or is removed, and is never copied into the data.
status names the folder and counts what is in it; the governing-actions report counts what is held there by decision and names a returned or refused file found in the data or the inventory anyway. Both report; neither moves or refuses anything.

### Keeping everyone's copy in step

Git records what the shared folder should hold, and the sync app or the bucket moves the bytes:

```bash
python scripts/shared_folder.py check       # conflict copies, online-only files, Google Docs shortcuts, temporary files
python scripts/shared_folder.py inventory   # writes sources/shared-inventory.json: each file's path, size and SHA-256
python scripts/shared_folder.py verify      # this machine's files against that inventory
```

After adding or changing shared files, run inventory and commit sources/shared-inventory.json with the work that used them.
After a pull of the Git repository, run verify: it names the recorded files missing here (still syncing, or removed), the ones that differ, and the ones not yet recorded. Tell the person what it found in plain words, and ask before using a file that differs.
check reports conflict copies (Dropbox and Nextcloud "conflicted copy", ownCloud "_conflict-", Google Drive "[Conflict]", and a sibling's name with a computer name or " (1)" added, as OneDrive and Google Drive make them); it deletes nothing, and which version to keep is the person's decision.
Google Docs, Sheets and Slides sit in a Google Drive folder as links, not files; ask the person to export the ones an analysis needs.
Hashes are cached by size, modification time and file identity in an ignored file; an online-only file is compared by size unless --download is given, because reading it downloads it, and inventory keeps the digest already recorded for it while its size is unchanged.
When part of the shared folder cannot be read, inventory leaves the recorded inventory as it was and says what it could not read, and check and verify name it; ask the person about access.

### Snapshots before reported results

The shared folder changes whenever a coauthor's sync app or push does. Before an analysis whose results will be reported, take a verified local snapshot of its inputs and point the analysis at the data folder it prints:

```bash
python scripts/shared_folder.py snapshot "<subfolder of the shared folder>" --dataset <short-name>
```

The snapshot lands in workspace/snapshots/<short-name>/, which Git ignores, and its identifier is the digest of its manifest. Record the input with `trace_record.py intake`, giving the snapshot identifier as `--version` and what was checked before use; the governing-actions report counts a snapshot no admitting intake row names, and names one that only a return or refuse row names as returned or refused yet present.
scripts/shared_snapshot.py is the underlying tool, for a source outside the shared folder.
The kit syncs and shares nothing itself: the sync app or rclone moves the files, and an ignored file is not access-controlled. The synced-folder hint in status is read from folder names and is a hint, not a verdict.

## Skills and agent discovery

Canonical skills live in .skills/. Setup generates copies in .claude/skills, .agents/skills or .cline/skills for the selected agent.
Skills with a `trace-` prefix are entry points over the governing actions; the others are example research methods under descriptive names.
`ai/SKILLS-IN-USE.md` lists them and is the routing table; replacing the examples with your own methods is expected, and its Adding a method routine says how.
CLAUDE.md imports AGENTS.md; the other supported instruction routes use AGENTS.md.
After editing a canonical skill, run `python scripts/bootstrap.py --sync-skills` unless the project uses `--agent none`. A separately edited generated copy is a conflict.
To retire an example, delete its folder under .skills/ and run `--sync-skills`, under `--agent none` too: an unchanged copy for your agent is removed with it, `.trace-os/install.json` records it under `retired_skills`, and later applies and updates do not install it again.
A copy you edited is named and left in place until you merge or remove it.
To undo a retirement, remove its id from `retired_skills` in `.trace-os/install.json` and run `--apply` from a fresh unpacked copy of the installed release, with `--target` naming the project, which installs the skill and its copy; an in-place install cannot be its own source here, because the skill's files are gone.
Operating-system files such as `.DS_Store` and files Git ignores are not treated as skill files.
The install receipt lists matching skill names found in the conventional user-global location, without reading their contents; the update and rollback receipts do not. That is a partial override check, not proof that all client configuration has been discovered.
A Codex CLI 0.144.6 rehearsal (2026-09) with workspace-write permissions blocked creation of .agents/skills; setup rolled back. No later CLI version has been rehearsed for this step. For that mode, choose --agent none and have the runner read canonical skills explicitly, or let the user install the selected adapter through their normal permission process. Do not disable a sandbox to install skills.
Native instruction loading, skill selection, output formatting and tool permissions require a separate recorded rehearsal for the installed CLI or extension version. The generated paths alone do not establish compatibility. A generic headless runner must supply AGENTS.md, the relevant skill and the task inputs explicitly.

## Skills from a source

`scripts/trace_skills.py` brings a skill package from a Git repository into this project, updates it later with the same review, and records each step. Its interface, files and recovery rules are in `scripts/trace-skills-contract.md` (contract version 1).
A package is a folder holding a `SKILL.md` at one commit of a repository. The repository is anything the person's own Git can read: an `https` or `ssh` address, `git@host:path`, or a local path. Git's credential helper supplies access, so an address carrying a user name and password, a token, a query or a fragment is refused.
The kit's authors keep a library of such packages at <https://github.com/amrhs-zohrevand/trace-skill-library>; it is private for now, so only people given access can read it.

The tool keeps five things, all tracked by Git: the project's copy under `.skills/<name>/`, which is the one agents use; the untouched original under `.trace-os/skill-bases/<package digest>/`; the lock, `.trace-os/skills.lock.json`, one entry per adopted, declared or retired skill; one receipt per operation under `.trace-os/skill-adoptions/`, written last, so a receipt exists only for an operation that completed; and the section "Skills from outside this project" in `ai/SKILLS-IN-USE.md`, between its own `trace-skills:begin inventory` and `trace-skills:end inventory` markers.
That section lies outside the kit's skill-routing block, so the kit's updater never meets it as a local edit. The tool rewrites it from the lock whenever it changes the lock; do not edit it by hand.
Because each original is a whole package with its own `SKILL.md`, an agent that searches the project for skills finds them too; `.trace-os/skill-bases/README.md`, which the tool writes beside them, says they are kept for comparison and are not skills to follow.
With an agent adapter the tool also writes the skill's agent copies and their entries in `.trace-os/install.json`, as `--sync-skills` makes them.
Under `--agent none`, or in a project without `.trace-os/install.json`, it writes none.
It never runs, imports or checks out a file from a package: it fetches into a bare repository in a temporary folder outside the project and reads each file as data. It never merges an adapted copy, and it never commits or pushes. Symlinks, submodules, Git control files such as `.gitattributes`, and paths that leave the package are refused with the reason.

Look before adopting. `list` prints the packages a source holds, each with its description and its `definition 1` line; `show` prints every file of one package and writes a candidate file naming exactly the bytes it printed. Neither writes anything in the project.

```bash
python scripts/trace_skills.py list <repository> [--ref <branch, tag or commit>]
python scripts/trace_skills.py show <repository> <path> [--ref <branch, tag or commit>]
```

`definition 1` reports three states, each on its own: the package format (front matter with `name` and `description` as the Agent Skills specification asks, the name equal to the folder), whether a licence is stated, and whether the skill states its handoff to the record, as `scripts/check_skill_handoff.py` reads it.
Every output that prints it also prints its limit: `Limit: definition 1 reads the skill's text; it does not establish that a method is valid, that its scripts are safe, or that any agent discovers the skill.`

```bash
python scripts/trace_skills.py adopt <repository> <path> --candidate <candidate.json> \
  --by "Full Name" --words "the person's words" --authority "T3 of ai/records/<id>"
```

`adopt` shows the package (all of it, or with `--candidate` the manifest of the bytes `show` printed) and a plan naming every file it will write.
After a yes it applies the plan as one transaction: the copy, the original, the agent copies, `.trace-os/install.json` when it changes, the lock and the inventory section.
Then it writes an intake row in `sources/intake.jsonl` naming the receipt, and last the receipt.
At a terminal it asks `Apply? Nothing is committed or pushed. [y/N]`. An agent has no terminal: the command prints the plan and `Nothing was applied: no terminal to confirm on`, writes a candidate file, and names the command to run once the person agrees, ending in `--candidate <file> --yes`. Show the person the plan, and pass `--yes` only once they have agreed, with their words in `--words`, verbatim.
`--yes` applies only what the person saw: a candidate file, or `--ref` with a full commit id. `--ref` otherwise names a branch or a tag; without it the source's `HEAD` is used.
`--by` (by default `git config user.name`), `--words`, `--authority` and `--execution-agent <model id>` go into the receipt and the intake row, and `--used-for` says what the skill will be used for.

Copying a skill into the project lets everyone who can read the project read it.
`--audience source=<value>` and `--audience project=<value>`, each `private`, `collaborators` or `public`, declare who can read the source and the project; the project's declaration stays in the lock for later operations. For a GitHub address the tool also asks `gh`, when it is installed, whether the repository is public.
Whenever the source is not known to be public, declared or seen, `adopt` refuses without `--share-with-project`. The person decides: an agent passes it only when they have said so, with their words in `--words`. The receipt records both audiences, who decided and their words, and the inventory says it to co-authors. `update` and `--accept-merged` compare again under the same rule.

One name has one owner.
A name the kit ships is refused unless `--replace-kit-copy` is given, which removes the kit's unedited copy and its agent copies, adds the name to `retired_skills` in `.trace-os/install.json` so the updater does not install it again, and writes the package in its place, all in one transaction.
`--replace-kit-copy` leaves the kit's own scripts and tests for that skill at the root, for example `scripts/meeting_quotes.py` and `tests/test_meeting_quotes.py` for the kit's meeting example, because the kit ships them, while the package's copies live under `.skills/<name>/scripts/`; the plan names such files by a name match.
A `.skills/<name>/` the project already holds is refused unless `--keep-existing` is given, which keeps that folder byte for byte and records the package as its original; a copy that differs from the package reads as adapted from the start, the plan lists how it differs, with a unified diff of each changed text file, before the person agrees, and the intake row says the existing copy was kept.
When a skill the setup skill's template route installed also has its scripts in `scripts/<name>/`, the plan names that folder and the receipt lists it under `kept_outside`, because `status` compares only `.skills/<name>/`: either move those scripts into the package's folder and, in the same step, change every command in `.skills/<name>/` that names `scripts/<name>/` to the new path, then run one of them before connecting, or keep them there knowing they are not compared.
A single-file skill `.skills/<name>.md` is never touched; turn it into `.skills/<name>/SKILL.md` first.

```bash
python scripts/trace_skills.py status [--offline]
```

`status` says where every skill stands, and only reads. Each skill gets an `owner:` line: `kit`, `project`, `external` with its source and commit, `outside` with the person who holds it, or `conflict` when two claim the name.
An adopted skill adds `copy:` (`unchanged`, or `adapted` with the files that differ from the original), `detached:` (`no` or `yes`), `upstream:`, `change notice:`, `definition 1:`, `shadowed:` and `receipt:`. `Pending:` heads the report when an operation did not finish, and two Limit lines end it: the one for `definition 1`, and one saying that file parity between `.skills/` and the agent copies is checked by `scripts/check_skills.py`, not here.
`upstream:` reads `current`, `behind <commit>`, or `unknown (<reason>)`: not fetched (`--offline`), the fetch failed (which is all the tool can say about a network outage, revoked access or a deleted repository), the ref or the package folder is gone, or the skill is pinned to a commit.
A change notice is a `metadata:` mapping with a `changes:` entry in the copy's `SKILL.md` front matter, saying what this project changed. `status` reports an adapted copy without one as `absent`; the tool never writes one.
`shadowed:` names, one line per folder, each of `~/.claude/skills`, `~/.agents/skills`, `~/.cline/skills` and `~/.codex/skills` that holds a skill of the same name, without reading it, and states what the kit's skills pilot observed on 2026-10-01 for that folder: Claude Code 2.1.280 loaded the personal copy under `~/.claude/skills` instead of the project's, Codex 0.154.0 listed only the personal skills under `~/.codex/skills`, and no client was rehearsed for the other two folders. Other clients and later versions were not rehearsed. A personal folder may be a wrapper that sends the agent back to the project's copy; `status` cannot tell, because it does not read it.
While an operation is unfinished, the skill it was working on reads `owner: pending`, with the command that finishes or undoes it, and nothing else is reported for that name.

```bash
python scripts/trace_skills.py update <name> [--ref <branch, tag or commit>]
python scripts/trace_skills.py update <name> --accept-merged --candidate <candidate.json> [--merged-by "Full Name"]
python scripts/trace_skills.py update <name> --detach
python scripts/trace_skills.py update <name> --attach
```

`update` fetches the ref the lock records, or `--ref`, which a skill adopted at a commit id needs. When the source has not moved it prints `Current: ...; nothing to do.`
Otherwise it prints the whole new package and its difference from the original, as for a first adoption, and applies to a copy nobody adapted after the same yes. The new original replaces the old one, which Git history keeps.
For an adapted copy it leaves every file in the project as it is: it prints the three versions (the original under `.trace-os/skill-bases/`, the project's copy under `.skills/<name>/`, and the new upstream in a temporary folder), the differences from the original to each, and the command that records the merge, then exits 1.
The person merges by hand into `.skills/<name>/`, then runs `python scripts/bootstrap.py --sync-skills` as for any edited skill, unless the project uses `--agent none`. `--accept-merged --candidate <file>` then records the merged copy as it stands against the new commit: it replaces the original with the new upstream, records the copy's digest and writes an intake row and a receipt. It never changes the copy.
`--merged-by "Full Name"` (or a model id) says who made the merge; without it the intake row says who merged was not stated, and `--execution-agent`, which names the agent running the tool, is never taken to be the merger.
`--detach` stops offering updates, while `status` keeps reporting whether the source moved; `--attach` offers them again. Detached and adapted are separate states, and `status` shows both.

```bash
python scripts/trace_skills.py retire <name> [--drop-base]
python scripts/trace_skills.py declare <name> --held-by "Full Name" [--path <folder>] [--limitation "..."] [--beside-project-skill]
```

`retire` removes the copy and its agent copies and marks the lock entry retired. It keeps the original and the receipts; `--drop-base` removes the original too. It refuses while the copy or the original holds changes Git has not recorded, so commit them first; once the removal is committed, Git history keeps every file it removed. When the retired name had replaced one of the kit's examples, the kit's own routing table inside the managed block still lists that example: that table is the kit's text, a list of what the kit ships, and the inventory's Retired row is this project's statement of what it holds.
`declare` records a skill a person runs from their own setup on this project, and copies nothing. The lock and the inventory's "Relied on, not in the project" table get its holder, the limitation, and a digest of `--path` when that folder is readable, recorded as attested because nobody in the project observed it. A declaration certifies nothing about any run; a method run's check row records the digest it actually read. `retire` ends a declaration.
`declare` refuses a name the project already holds; when the project's skill of that name only says to use a personal copy, remove it first and declare the personal skill. When that skill holds rules of its own and relies on a personal copy too, `--beside-project-skill` records the personal skill beside it and leaves the project's skill as it is.
`declare --path` refuses a folder holding a symbolic link anywhere under it, so declare a folder with `node_modules` or the like without `--path`, and its digest reads not stated.
`retire`, `declare`, `--detach` and `--attach` fetch nothing and write no candidate file: without a terminal they print the plan and the same command ending in `--yes`, to run once the person agrees.

`adopt`, `update` and `--accept-merged` write an intake row naming their receipt. `--detach`, `--attach`, `retire` and `declare` write none, because nothing enters the project, and no command writes a transition row for a retirement.
After the person has committed an adoption, an update or an accepted merge, `link` writes the default-adoption transition row for its receipt:

```bash
python scripts/trace_skills.py link .trace-os/skill-adoptions/<operation id>.json --commit <sha>
```

The row waits for the commit for two reasons: a transition row names the commits it covers, and nothing amends a row once written; and later-run discovery (`trace_record.py later-run`) matches the committed `.skills/<name>/SKILL.md` by its digest, which `link` reads from that commit. It writes one row per receipt in `sources/transitions.jsonl`; commit that file afterwards.

A command that finds nothing to do says so, changes no file and exits 0: a line beginning `Already` (adopted, detached, attached, retired, declared or linked), or `Current:` from `update`.
Every writing command takes the setup lock and keeps an operation journal, `.trace-os/receipts/skill-operation.json`, while it writes; the kit's ignore rules already cover `.trace-os/receipts/`, so the journal never reaches the project's history.
An interrupted operation stops the next writing command until `recover` completes it or undoes it; `recover` prints `Nothing to recover.` when there is none.

```bash
python scripts/trace_skills.py recover
```

Exit codes: 0 done, nothing to do, or a report; 1 a person is needed (a refusal, a stale candidate, an adapted copy, an apply nobody confirmed); 2 a usage or environment error; 128 plus the signal number when interrupted.

The tool refuses to write where Git would ignore the record.
It also refuses a package holding a file Git would rewrite on commit, such as a text file with CRLF line endings under the kit's `* text=auto eol=lf`, because that file's recorded digest would not survive a clone. The refusal comes before anything is written, and names the file and the folder Git would convert.

Limits of this release:

- `--rollback` refuses when a file the update wrote changed afterwards, and a skills operation after a kit update changes such files: `.trace-os/install.json` whenever it writes agent copies, and `ai/SKILLS-IN-USE.md` when the update rewrote its routing block, as the updates to 0.1.0-rc19 and 0.1.0-rc20 do in every project, since they convert the block's markers, so any skills operation after such an update makes its rollback refuse.
  Roll back first, then adopt.
- When a later kit release starts shipping a name this project adopted, the update names the adopted copy as a conflict and waits.
  The tool offers no step for this, and the person resolves it; adding the name to `retired_skills` in `.trace-os/install.json` keeps the updater from installing the kit's copy under that name.
- Later-run discovery sees the `SKILL.md` digest only, so after an update that changed only a package's scripts, records made under the old and the new scripts look the same to `trace_record.py later-run`.
- `scripts/kit_projects.py` and `bootstrap.py --plan` do not report an unfinished skills operation. `status` prints it as `Pending:`; run `recover` before a kit update.
- The `shadowed:` lines state what was observed for two clients at two versions, as above; Cline and later versions were not rehearsed.

In the kit's own development tree the script sits in `starter/files/scripts/`, so its default project, the folder above `scripts/`, is not the repository; there it runs as `python3 starter/files/scripts/trace_skills.py --project .` from the repository's root.

## Recovery

Setup saves an interrupted file transaction in .trace-os/transaction.json and its backups under .trace-os/backups/.
Repeating --apply first restores those recorded changes, then replans the installation. A later human edit stops recovery rather than being overwritten. If a skill sync was interrupted, run `python scripts/bootstrap.py --recover`, then retry `--sync-skills`; this recovery action does not require an unchanged release manifest.
The operating system releases the setup lock when the process ends. Do not remove a live process's lock or edit its journal. Backups are local and ignored; they are not an external backup service.
If the initial Git commit fails, inspect Git's error and the staged files before retrying. Setup will not include unrelated staged work in an initial commit.

## Update

To see whether an update exists, compare `version` in `.trace-os/install.json` (`.traceware/install.json` in 0.1.0-rc19, `.trace-kit/install.json` before) with the newest release on https://github.com/amrhs-zohrevand/trace-os/releases, where each release carries its archive and its `.sha256` digest file.
One release ahead is a single update, below; more than one ahead is a chain, `--update-chain`, further down.

An update follows a pair the new release's manifest nominates in `supported_upgrades`: from the installed version to this one, for the installed profile and agent adapter, with the installation-state and project schemas unchanged.
The plan stops with `unsupported release pair` for any other pair.
Trace records written under an earlier release stay readable.
Changing the profile or agent, migrating the installation-state or project schema, skipping a release outside the nominated pairs, release discovery and publisher-key verification require separately specified paths and are not implemented in this candidate.

Unpack the new release separately, take the archive SHA-256 from the `.sha256` sidecar on its release page, and plan from the new release directory:

```bash
python scripts/bootstrap.py --target /path/to/project --profile academic --agent claude --plan
```

The plan identifies managed-file conflicts and the shared `core-workflow` instruction section separately.
An unchanged shared prefix from the previous release is replaced by the new bounded section while project instructions appended after it are preserved.
A file that still begins with the seed an earlier release installed, unedited, counts as that prefix too, because an update never rewrites a seeded file.
Later bounded sections update when they still match the installed release baseline.
A locally edited bounded section requires an explicit compatibility declaration in `ai/project.json`:

```json
{
  "trace_os": {
    "instruction_overrides": {
      "core-workflow": {
        "compatible_with": ["1"],
        "basis": "Why the retained local rules satisfy this release's shared rule contract."
      }
    }
  }
}
```

The key is `trace_os`; a declaration a project wrote under an earlier key still holds while the file has no newer one, `traceware` (0.1.0-rc19) before `trace_kit` (up to 0.1.0-rc18), and no update rewrites `ai/project.json`.
The section version comes from the new release manifest.
A declaration holds until a later release's manifest changes that section's version, even when the section's text changes under the same version.
The basis is a project declaration for review, not proof of compatibility.
Without it, the whole update remains pending and the last successful installation stays active.

Apply the verified update from the unpacked release directory:

```bash
python scripts/bootstrap.py --target /path/to/project --profile academic --agent claude \
  --release-archive /path/to/trace-os-0.1.0-rcN.zip \
  --expected-sha256 <digest-from-the-release-sidecar> --update
```

The bootstrap verifies the archive digest and matches its manifest to the unpacked directory.
It runs the release tests before project writes, applies the candidate through a recoverable transaction, and runs the project's workflow check against it.
A failing check restores the previous files and state together from verified backups, and the update history records the attempt.
So does an update stopped before it finishes (Ctrl-C, a command timeout, SIGTERM), which also stops the check and the processes it started (its process group on macOS and Linux; on Windows its process tree, through `taskkill /T /F`, a path not yet run there) and is recorded as interrupted.
If the process was killed outright, `--plan` reports `pending_recovery` and `pending_update`, and the next `--recover`, `--update`, `--update-chain`, `--apply` or `--rollback` restores the previous installation first and records that; `--sync-skills` asks for `--recover`.
The tracked `.trace-os/update-history.jsonl` records pending, failed, interrupted, completed and rolled-back attempts.
Local rollback bytes stay under the ignored `.trace-os/update-backups/` directory, so rollback is available only while those verified local backups remain.

An installation more than one release behind walks the nominated pairs in order; there is no direct jump. `--update-chain` does the walk in one command:

```bash
python scripts/bootstrap.py --target /path/to/project --profile academic --agent claude \
  --release-archive /path/to/trace-os-0.1.0-rcN.zip \
  --expected-sha256 <digest-from-the-release-sidecar> \
  --release-store /path/to/earlier-release-archives --update-chain
```

The person supplies one digest, the newest archive's, as for a single update.
That archive's manifest pins the digest of every earlier release (`releases`), and the store is any directory of release archives, matched by digest and never by name; after an update that did not finish is restored, every archive the chain needs is resolved before any hop is applied, and a missing one stops the chain before its first hop.
Each hop is then the verified single update: this release's updater over that release's own unpacked archive, with the archive's release preflight, the project's postflight, a transaction and a history event of its own; the chain adds the walk and nothing else.
A hop that stops, on a conflict or a failed check, ends the chain there: the earlier hops stay completed, the installed version says where the project stands, and running the same command again resumes from it once the named path is resolved.
A one-hop chain needs no store, and `--update-chain` at the current release applies nothing and writes only its receipt.
The releases page carries 0.1.0-rc24 and later only, so the earlier archives a chain from 0.1.0-rc22 or before needs come from a folder where they were kept, named with `--release-store`; each is still checked against the digest this release pins.
The receipt lists every hop with its own receipt or its stderr; `.trace-os/update-history.jsonl` carries one event per hop, so a rollback after a chain steps back one hop at a time, as far as the local backups remain.

### From a release before 0.1.0-rc20

In 0.1.0-rc19, as TRACEware, the kit kept a project's installation state in `.traceware/`, named its managed instruction sections with `<!-- traceware:begin <id> -->` and `<!-- traceware:end <id> -->` markers, and read variables named `TRACEWARE_*`.
Up to 0.1.0-rc18 it used `.trace-kit/`, `<!-- trace-kit:begin <id> -->` markers and `TRACE_KIT_*` variables.
Every tool of this release reads `.trace-os/` first, then `.traceware/`, then `.trace-kit/`, so a project not yet updated keeps working and `scripts/kit_projects.py` finds it.
Update it with the commands above, one release at a time or with `--update-chain`; no step is done by hand.

- An update moves the state folder to the one its own release names, and only to a newer name: the hop to 0.1.0-rc19 moves `.trace-kit/` to `.traceware/`, and the hop to this release moves either earlier folder to `.trace-os/`.
  So a chain from 0.1.0-rc18 or earlier moves it twice, and a project on 0.1.0-rc19 once.
  Each such update applies and checks the release as any update does, in the folder the state is in, and once it is final renames that folder whole: `install.json`, `update-history.jsonl` with the update's event, the receipts, the skill tool's lock, receipts and originals, and the local backups; nothing is left under the old name.
- Stage both folders, for example `git add -A .traceware .trace-os`, so Git records the tracked state as renamed; the receipt's remaining steps name the two folders of each move, and say what else moved.
- The sections in `AGENTS.md`, `ai/SKILLS-IN-USE.md` and `WORKFLOW.md` open and close with `<!-- trace-os:begin <id> -->` and `<!-- trace-os:end <id> -->`; the update converts either earlier form of the markers, and a section kept under a compatibility declaration keeps its text and changes only its two marker lines.
- A `.gitignore` as the kit installed it becomes this release's, which names `.trace-os/`.
  One the project edited is the project's own: it keeps every line, and each rule naming an earlier folder gains the same rule for `.trace-os/` on the line after it; where both earlier folders have the rule, it is added once, after the first.
- `ai/project.json` is never rewritten; the overrides key is read as above.
- `TRACE_OS_SHARED_ROOT`, `TRACE_OS_REQUIRE_SHARED` and `TRACE_OS_DEFECT_LEDGER` are read first; while one is unset its `TRACEWARE_` name, then its `TRACE_KIT_` name, still works.
- A path the history, the skill tool's lock or a receipt recorded under an earlier folder is read where the file is now, after one move or two, and `trace_skills.py link` takes a receipt under any of the folder's names.
- `--rollback` of such an update moves the folder back first and then restores the files, so the project holds the old folder, markers and `.gitignore` again, with the update and its rollback recorded in the history as every rollback records them.
- On Windows a folder holding an open file cannot be renamed, so the entries move one by one and the setup lock's file goes once released (a path simulated in tests, not run on Windows).
  A move that cannot be made at all leaves this release installed in the earlier folder, where every tool still reads it; `--recover`, or the next `--apply`, `--update-chain` or `--sync-skills`, makes it.

This archive's files sit under one top folder, `trace-os/`; the 0.1.0-rc19 archive has them under `traceware/`, and an archive up to 0.1.0-rc18 under `trace-kit-starter/`.
In every update path above the new release's own `bootstrap.py` reads the new archive: a single update runs from its unpacked folder, a chain applies each earlier archive with the newest release's updater, and `kit_projects.py` runs the release's own `bootstrap.py`.
That is why this archive uses the new name, and why this release's `bootstrap.py` and `kit_projects.py` read all three folders, so a chain still applies the archives before it.
A `kit_projects.py` from 0.1.0-rc19 or earlier cannot read this archive, so run the one this release ships.

## Updating every TRACE project on this machine

`scripts/kit_projects.py` does for several projects what the Update section does for one; run this release's copy, from the unpacked release or from a project already on it.
It finds every Git working tree with `.trace-os/install.json`, or the same file in `.trace-os/` or `.trace-kit/`, under the folders named with `--root` (three levels down by default; hidden, dependency and cloud-sync folders are not searched) and reads each project's installed release, profile and agent:

```bash
python scripts/kit_projects.py list --root ~/Developer
python scripts/kit_projects.py plan --root ~/Developer \
  --release-archive /path/to/trace-os-0.1.0-rcN.zip --sha256-file /path/to/trace-os-0.1.0-rcN.zip.sha256
python scripts/kit_projects.py update --root ~/Developer --all \
  --release-archive /path/to/trace-os-0.1.0-rcN.zip --sha256-file /path/to/trace-os-0.1.0-rcN.zip.sha256
```

`plan` and `update` verify the archive's digest once, unpack it once and run that release's own `scripts/bootstrap.py` on each project: `--plan`, then `--update`, or `--update-chain` for a project more than one release behind when `--release-store` names a folder of the earlier archives.
`plan` changes nothing. `update` prints the plan and applies it only after the person has seen it: an answer at the terminal, or `--yes`, which an agent passes only once the person has agreed. Without a terminal and without `--yes`, nothing is applied.
`update` acts on the projects named with `--project`, or on every one found with `--all`; it skips a project with uncommitted changes unless `--allow-dirty`, reading each working tree again just before that project's update, and stops at the first project that conflicts or fails unless `--keep-going`. An agent adds `--execution-agent` with its model ID.
Ctrl-C, or SIGTERM or SIGHUP to the script, while a project updates is passed on to that project's `bootstrap.py`, which restores what it applied; the script waits for it (up to two minutes), reports the project as restored or, with the `--recover` command to run, as recovery incomplete, attempts no further project and exits with 128 plus the signal's number (130 for Ctrl-C, 143 for SIGTERM).
It never commits or pushes: each updated project is left for its person to review and commit under that project's own rules.
`--download` fetches the release with the GitHub CLI when `gh` is installed and signed in, and takes the digest from the release's `.sha256` file, which detects a damaged download but not who published it; without `gh`, download both files by hand.
It fetches only what the releases page carries, 0.1.0-rc24 and later: for a project on an earlier release, add `--release-store` with a folder of the earlier archives.
Exit code 0 means every project is current or updated, 1 that something needs a person, and 2 a usage or environment error.

## Conflicts

Edited managed code remains a conflict when the new release changes that file.
An edited managed file the new release ships unchanged, such as the project's own rules below the kit's lines in `.gitignore`, stays as it is and is listed under `kept_project_edits` in the plan, the receipt and the update history; an edited skill is kept so once `--sync-skills` has copied it.
A managed file the new release no longer ships is removed when unchanged and named as a conflict when edited, which moving the edited file to a path of the project's own or deleting it resolves; seeded files and the copies `--sync-skills` made of the project's own skills stay, listed as `obsolete_preserved`.
A deleted instruction file keeps the update pending as `missing-instruction:<path>`; restore it with `git checkout HEAD -- <path>` and update again.
A release may also rename a shipped skill. The update then removes the old canonical file and the old generated copy for the selected agent and installs the new ones, so the installed-skill check passes afterwards. A copy you edited locally keeps the whole update pending and is named in the conflicts, as an edited copy of any other managed file is; the plan lists it under `renamed.retained` with the rename it blocks. Merge or remove that file and update again.

## Rollback

Roll back the latest completed update from the project:

```bash
python scripts/bootstrap.py --target . --rollback
```

Rollback restores only affected kit files and installation state.
It refuses if any affected file changed after the update and leaves research, evidence and unrelated files alone.
Run `--recover` after an interrupted operation; it restores an unfinished candidate before another update can be planned.
If an update was left unfinished, `--rollback` first restores it and reports that separately (`recovered_update` in the receipt, an interrupted event in the history), then rolls back the latest completed update; run `--recover` alone to restore the unfinished update without rolling anything back.
